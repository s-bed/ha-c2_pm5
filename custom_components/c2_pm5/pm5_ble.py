from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    establish_connection,
    BleakClientWithServiceCache,
    BleakOutOfConnectionSlotsError,
    BleakNotFoundError,
    BleakConnectionError,
)

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, CALLBACK_TYPE, callback
from homeassistant.util import dt as dt_util

from .const import (
    BASE_UUID_FMT,
    BATTERY_POLL_SECONDS,
    CHAR_0031,
    CHAR_0032,
    CHAR_0033,
    CHAR_0035,
    CHAR_0036,
    CHAR_0037,
    CHAR_0038,
    CHAR_0039,
    CHAR_003A,
    CHAR_0080,
    CHAR_CSAFE_RX,
    CHAR_CSAFE_TX,
    CONF_IDLE_DISCONNECT_MINUTES,
    CSAFE_COMMAND_WRAPPER_NONE,
    CSAFE_COMMAND_WRAPPER_GETPMCFG,
    CSAFE_COMMAND_WRAPPER_GETPMDATA,
    CSAFE_COMMAND_WRAPPER_SETPMCFG,
    CSAFE_COMMAND_WRAPPER_SETPMDATA,
    CSAFE_EXT_START,
    CSAFE_PM_GET_BATTERYLEVELPERCENT,
    CSAFE_PM_GET_EXTENDED_HRBELT_INFO,
    CSAFE_PM_SET_DATETIME,
    CSAFE_PM_SET_EXTENDED_HRBELT_INFO,
    CSAFE_PM_TERMINATE_WORKOUT_CONTENTS,
    CSAFE_REQUEST_TIMEOUT_SECONDS,
    CSAFE_STD_START,
    CSAFE_STOP,
    CSAFE_STUFF,
    DATETIME_SYNC_CONNECT_GRACE_SECONDS,
    DATETIME_SYNC_RATE_LIMIT_SECONDS,
    DATETIME_SYNC_TIMEOUT_SECONDS,
    DEFAULT_IDLE_DISCONNECT_MINUTES,
    DEVICE_STALE_SECONDS,
    ERG_MACHINE_TYPE,
    HRM_DEVICE_TYPE,
    HRM_POLL_SECONDS,
    IDLE_DISCONNECT_COOLDOWN_SECONDS,
    INTERVAL_TYPE,
    ROWING_STATE,
    STROKE_STATE,
    TERMINATE_GRACE_SECONDS,
    TERMINATE_RATE_LIMIT_SECONDS,
    WORKOUT_DURATION_TYPE,
    WORKOUT_STATE,
    WORKOUT_TYPE,
)

_LOGGER = logging.getLogger(__name__)
_CSAFE_MAX_FRAME_BYTES = 120  # Includes flags, checksum and byte stuffing.
_CSAFE_WRAPPERS = {
    CSAFE_COMMAND_WRAPPER_GETPMCFG,
    CSAFE_COMMAND_WRAPPER_GETPMDATA,
    CSAFE_COMMAND_WRAPPER_SETPMCFG,
    CSAFE_COMMAND_WRAPPER_SETPMDATA,
}

# https://www.concept2.com/support/software-development
# https://www.concept2.nl/files/pdf/us/monitors/PM5_BluetoothSmartInterfaceDefinition.pdf
# https://cms.concept2.com/sites/default/files/2024-05/PM5_CSAFECommunicationDefinition_0.pdf
# https://www.concept2.sg/files/pdf/us/monitors/PM5_CSAFECommunicationDefinition.pdf


def c2_uuid(short: int) -> str:
    return BASE_UUID_FMT.format(short=short)


def u16_le(b: bytes, off: int) -> int:
    return b[off] | (b[off + 1] << 8)


def u24_le(b: bytes, off: int) -> int:
    return b[off] | (b[off + 1] << 8) | (b[off + 2] << 16)


def heart_rate_bpm(value: int) -> int | None:
    """Treat zero and the PM5 invalid sentinel as missing heart rate."""
    return None if value in (0, 255) else value


def csafe_xor_checksum(frame_contents: bytes) -> int:
    """CSAFE checksum is byte-by-byte XOR of *frame contents* (not including start/stop flags)."""
    c = 0
    for byte in frame_contents:
        c ^= byte
    return c


def csafe_byte_stuff(payload: bytes) -> bytes:
    """
    Byte-stuff unique flag values within the stream as:
      F0 -> F3 00
      F1 -> F3 01
      F2 -> F3 02
      F3 -> F3 03
    """
    out = bytearray()
    for b in payload:
        if b == CSAFE_EXT_START:
            out += bytes([CSAFE_STUFF, 0x00])
        elif b == CSAFE_STD_START:
            out += bytes([CSAFE_STUFF, 0x01])
        elif b == CSAFE_STOP:
            out += bytes([CSAFE_STUFF, 0x02])
        elif b == CSAFE_STUFF:
            out += bytes([CSAFE_STUFF, 0x03])
        else:
            out.append(b)
    return bytes(out)


def csafe_byte_unstuff(payload: bytes) -> bytes:
    """Reverse of csafe_byte_stuff()."""
    out = bytearray()
    i = 0
    n = len(payload)
    while i < n:
        b = payload[i]
        if b != CSAFE_STUFF:
            out.append(b)
            i += 1
            continue
        # Stuff byte must be followed by selector 00..03
        if i + 1 >= n:
            raise ValueError("CSAFE unstuff: truncated escape sequence")
        sel = payload[i + 1]
        if sel == 0x00:
            out.append(CSAFE_EXT_START)
        elif sel == 0x01:
            out.append(CSAFE_STD_START)
        elif sel == 0x02:
            out.append(CSAFE_STOP)
        elif sel == 0x03:
            out.append(CSAFE_STUFF)
        else:
            raise ValueError(f"CSAFE unstuff: invalid selector 0x{sel:02x}")
        i += 2
    return bytes(out)


def build_csafe_standard_frame(frame_contents: bytes) -> bytes:
    """
    Build a CSAFE *standard* frame:
      F1 + stuff(contents + checksum) + F2
    """
    checksum = csafe_xor_checksum(frame_contents)
    stuffed = csafe_byte_stuff(frame_contents + bytes([checksum]))
    return bytes([CSAFE_STD_START]) + stuffed + bytes([CSAFE_STOP])


def _response_chunks(payload: bytes, *, allow_bare_ack: bool = False):
    """Read complete command responses within one frame or wrapper."""
    index = 0
    while index < len(payload):
        if index + 1 == len(payload) and allow_bare_ack:
            yield payload[index:]
            return
        if index + 1 >= len(payload):
            raise ValueError("Missing CSAFE response length")
        end = index + 2 + payload[index + 1]
        if end > len(payload):
            raise ValueError("Truncated CSAFE response")
        yield payload[index:end]
        index = end


def try_lookup(address: str, name: str, data: dict, key: str):
    try:
        return data[key]
    except KeyError:
        _LOGGER.debug("Attempted to lookup unknown %s (%s): %i", name, address, key)
        return None


@dataclass
class PM5Data:
    values: dict


class PM5Unavailable(Exception):
    """PM5 not found / not advertising."""


class PM5BleSession:
    """Handles BLE scanning, availability, connect/reconnect, notifications, and CSAFE control."""

    def __init__(
        self,
        hass: HomeAssistant,
        device_type: str,
        address: str,
        on_data: Callable[[PM5Data], None],
        on_available: Callable[[bool], None],
        *,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        self.hass = hass
        self.address = address.upper()
        self._config_entry = config_entry
        self.on_data = on_data
        self.on_available = on_available

        self._client: BleakClientWithServiceCache | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

        self._uuid31 = c2_uuid(CHAR_0031)
        self._uuid32 = c2_uuid(CHAR_0032)
        self._uuid33 = c2_uuid(CHAR_0033)
        self._uuid35 = c2_uuid(CHAR_0035)
        self._uuid36 = c2_uuid(CHAR_0036)
        self._uuid37 = c2_uuid(CHAR_0037)
        self._uuid38 = c2_uuid(CHAR_0038)
        self._uuid39 = c2_uuid(CHAR_0039)
        self._uuid3a = c2_uuid(CHAR_003A)
        self._uuid80 = c2_uuid(CHAR_0080)

        # CSAFE control characteristics
        self._uuid_csafe_rx = c2_uuid(CHAR_CSAFE_RX)  # write to PM
        self._uuid_csafe_tx = c2_uuid(CHAR_CSAFE_TX)  # notify from PM (optional)

        # CSAFE send guards
        self._csafe_lock = asyncio.Lock()
        self._pending_csafe: dict[tuple[int, int], asyncio.Future[bytes]] = {}
        self._csafe_rx_buffer = bytearray()
        self._last_terminate_attempt_monotonic: float = 0.0
        self._last_datetime_sync_monotonic: float = 0.0

        self._available: bool = False
        self._avail_task: asyncio.Task | None = None
        self._connected: bool = False
        self.supports_memory_reads = False
        self._reconnect_after: float = 0.0
        self._last_active_monotonic: float | None = None
        self._unsub_adv: CALLBACK_TYPE | None = None
        self._workout_state_raw: int | None = None
        self._last_screen_status: tuple[int, int, int] | None = None

        # Edges wake a retry once. Repeated advertisements do not bypass backoff.
        self._presence_changed = asyncio.Event()
        self._connected_event = asyncio.Event()

    @property
    def is_connected(self) -> bool:
        """Return whether the monitor has an active telemetry connection."""
        return self._connected

    @property
    def is_discoverable(self) -> bool:
        """Return whether the monitor is reachable or was recently seen."""
        return self._available

    async def start(self) -> None:
        self._stop.clear()

        self._unsub_adv = bluetooth.async_register_callback(
            self.hass,
            self._async_discovered_device,
            {"address": self.address, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
        )

        self._avail_task = asyncio.create_task(self._availability_watchdog())
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        self._presence_changed.set()

        if self._unsub_adv:
            self._unsub_adv()
            self._unsub_adv = None

        if self._avail_task:
            self._avail_task.cancel()
            await asyncio.gather(self._avail_task, return_exceptions=True)
            self._avail_task = None

        if self._task:
            await self._task
            self._task = None

        self._set_available(False)
        self._set_connected(False)

    # ---------- Advertisement callbacks / availability ----------
    @callback
    def _async_discovered_device(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        self._refresh_availability()

    def _refresh_availability(self) -> None:
        if self._client is not None:
            return
        # The registration callback can replay old history or skip identical
        # advertisements. HA still updates the timestamp of its connectable path.
        info = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=True
        )
        fresh = (
            info is not None
            and info.time >= self._reconnect_after
            and time.monotonic() - info.time <= DEVICE_STALE_SECONDS
        )
        if fresh and not self._available:
            _LOGGER.debug(
                "Detected PM5 (%s) via %s, RSSI %s dBm",
                self.address,
                info.source,
                info.rssi,
            )
        self._set_available(fresh)

    def _set_available(self, available: bool) -> None:
        if self._available == available:
            return

        self._available = available
        self._presence_changed.set()

    def _set_connected(self, connected: bool) -> None:
        if self._connected == connected:
            return

        self._connected = connected
        if connected:
            self._connected_event.set()
        else:
            self._connected_event.clear()

        self.on_available(connected)

    async def _availability_watchdog(self) -> None:
        try:
            while not self._stop.is_set():
                self._refresh_availability()
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return

    # ---------- Connection loop ----------
    async def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            self._refresh_availability()
            self._presence_changed.clear()
            if not self._available:
                backoff = 1
                await self._presence_changed.wait()
                continue

            started = time.monotonic()
            _LOGGER.debug("Starting PM5 connection attempt (%s)", self.address)
            try:
                await self._connect_and_listen()
                backoff = 1
            except (
                BleakOutOfConnectionSlotsError,
                BleakNotFoundError,
                BleakConnectionError,
                PM5Unavailable,
            ) as err:
                _LOGGER.debug(
                    "PM5 connection attempt failed (%s) after %.1fs: %s: %s",
                    self.address,
                    time.monotonic() - started,
                    type(err).__name__,
                    err,
                )
            except Exception as err:
                _LOGGER.exception("PM5 BLE error (%s): %s", self.address, err)

            if self._stop.is_set():
                break
            self._refresh_availability()
            if not self._available or self._presence_changed.is_set():
                backoff = 1
                continue
            _LOGGER.debug("Retrying PM5 connection (%s) in %ss", self.address, backoff)
            try:
                await asyncio.wait_for(self._presence_changed.wait(), timeout=backoff)
            except TimeoutError:
                backoff = min(backoff * 2, 30)
            else:
                backoff = 1

    def _get_ble_device(self) -> BLEDevice:
        dev = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if dev is not None:
            return dev
        raise PM5Unavailable(f"PM5 not found in scanner cache: {self.address}")

    def _workout_looks_active(self) -> bool:
        """Return whether the PM5 reports work, a pause or an interval rest."""
        return self._workout_state_raw in range(1, 10)

    # ---------- CSAFE ----------

    def _cancel_pending_csafe(self) -> None:
        self._csafe_rx_buffer.clear()
        for fut in self._pending_csafe.values():
            if not fut.done():
                fut.cancel()
        self._pending_csafe.clear()

    def _parse_csafe_standard_frame(self, raw: bytes) -> bytes:
        """
        Parse a CSAFE standard frame from raw TX notify data.
        Returns *frame contents* (no checksum).
        """
        if not raw:
            raise ValueError("Empty CSAFE TX payload")

        if raw[0] != CSAFE_STD_START or raw[-1] != CSAFE_STOP:
            raise ValueError(f"Not a CSAFE standard frame: {raw.hex()}")

        stuffed = raw[1:-1]
        unstuffed = csafe_byte_unstuff(stuffed)
        if len(unstuffed) < 2:
            raise ValueError("CSAFE frame too short after unstuff")

        contents = unstuffed[:-1]
        checksum = unstuffed[-1]
        if csafe_xor_checksum(contents) != checksum:
            raise ValueError("CSAFE checksum mismatch")

        return contents

    def _consume_csafe_responses(self, contents: bytes) -> None:
        """Validate response lengths and match the request's wrapper and command."""
        status = contents[0]
        if status & 0x30:
            _LOGGER.debug("CSAFE TX returned status 0x%02x", status)
            return
        # Validate the entire frame before delivering any part of it.
        responses = []
        for outer in _response_chunks(contents[1:]):
            command = outer[0]
            if command in _CSAFE_WRAPPERS:
                is_set = command in (
                    CSAFE_COMMAND_WRAPPER_SETPMCFG, CSAFE_COMMAND_WRAPPER_SETPMDATA
                )
                for chunk in _response_chunks(outer[2:], allow_bare_ack=is_set):
                    responses.append(((command, chunk[0]), chunk))
            else:
                responses.append(((CSAFE_COMMAND_WRAPPER_NONE, command), outer))
        for key, resp in responses:
            fut = self._pending_csafe.get(key)
            if fut is not None and not fut.done():
                fut.set_result(resp)

    async def _send_csafe_frame(
        self, frame: bytes, *, before_write: Callable[[], None] | None = None
    ) -> None:
        """
        Write CSAFE frame to PM (0x0021).

        With ESPHome BT proxy / some BLE stacks, response=False can fail intermittently.
        Try response=False first for speed, then fallback to response=True.
        """
        client = self._client
        if not client:
            raise PM5Unavailable("No BLE client")

        _LOGGER.debug("CSAFE RX (%s): %s", self.address, frame.hex())

        if before_write is not None:
            before_write()
        try:
            await client.write_gatt_char(
                self._uuid_csafe_rx, frame, response=False
            )
            return
        except Exception as err:
            _LOGGER.debug(
                "CSAFE write (response=False) failed; retrying response=True: %s", err
            )

        if before_write is not None:
            before_write()
        await client.write_gatt_char(self._uuid_csafe_rx, frame, response=True)

    async def _csafe_request(
        self,
        wrapper: int,
        cmd: int,
        data: bytes = b"",
        *,
        before_write: Callable[[], None] | None = None,
        timeout: float = CSAFE_REQUEST_TIMEOUT_SECONDS,
    ) -> bytes:
        """
        Send a CSAFE command and await the corresponding response from CSAFE TX.
        Correlates by wrapper and command byte.

        Returns the response chunk: [CMD, RESPONSE_LEN?, DATA...]
        """
        if not self._client:
            raise PM5Unavailable("No BLE client")

        if cmd >= 0x00 and cmd <= 0x7F and data == b"":
            raise ValueError("No data passed for long command")

        if cmd >= 0x80 and cmd <= 0xFF and data != b"":
            raise ValueError("Data passed for short command")

        async with self._csafe_lock:
            # Build contents (standard CSAFE behavior):
            # - no-arg “short get”: just [CMD]
            # - arg commands: [CMD, CMD_BYTE_COUNT, <DATA...>]
            if data:
                if wrapper != CSAFE_COMMAND_WRAPPER_NONE:
                    length = 2 + len(data)  # cmd + len(data) + data
                    contents = bytes([wrapper, length, cmd, len(data)]) + data
                else:
                    contents = bytes([cmd, len(data)]) + data
            else:
                if wrapper != CSAFE_COMMAND_WRAPPER_NONE:
                    contents = bytes([wrapper, 1, cmd])
                else:
                    contents = bytes([cmd])

            frame = build_csafe_standard_frame(contents)

            key = (wrapper, cmd)
            fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
            self._pending_csafe[key] = fut
            try:
                await self._send_csafe_frame(frame, before_write=before_write)
                return await asyncio.wait_for(fut, timeout=timeout)
            finally:
                self._pending_csafe.pop(key, None)
                fut.cancel()
                self._csafe_rx_buffer.clear()

    # ---------- Heartrate monitor ----------
    async def poll_hrbelt_info_once(self) -> None:
        """
        Poll CSAFE_PM_GET_EXTENDED_HRBELT_INFO (0x57).
        Response bytes:
          Byte0: user number (0-4)
          Byte1: manufacturer id
          Byte2: device type
          Byte3-6: belt id (32-bit)
        """
        try:
            resp = await self._csafe_request(
                CSAFE_COMMAND_WRAPPER_GETPMDATA,
                CSAFE_PM_GET_EXTENDED_HRBELT_INFO,
                data=b"\x00",
            )
        except Exception as err:
            _LOGGER.debug("HRM poll failed (%s): %s", self.address, err)
            return

        if len(resp) < 9:
            _LOGGER.debug("Received unexpected length hrbelt_info response: %s", resp)
            return

        if resp[0] != CSAFE_PM_GET_EXTENDED_HRBELT_INFO:
            return

        data_len = resp[1]
        data = resp[2 : 2 + data_len]

        if data_len < 7:
            _LOGGER.debug(
                "Received unexpected length hrbelt_info data response: %s", resp
            )
            return

        user_id = data[0]

        manufacturer_id = data[1]
        device_type_raw = data[2]
        belt_id = int.from_bytes(data[3:7], byteorder="little", signed=False)

        if belt_id == 0:
            manufacturer_id = None
            device_type_raw = None
            belt_id = None

        self.on_data(
            PM5Data(
                {
                    "user_id": user_id,
                    "hrm_manufacturer_id": manufacturer_id,
                    "hrm_device_type": (
                        None
                        if belt_id is None
                        else HRM_DEVICE_TYPE.get(
                            device_type_raw, f"Unknown ({device_type_raw})"
                        )
                    ),
                    "hrm_belt_id": belt_id,
                }
            )
        )

    async def async_set_hrbelt_info(
        self, manufacturer_id: int, device_type: int | str, belt_id: int
    ) -> None:
        """
        Set CSAFE_PM_SET_EXTENDED_HRBELT_INFO (0x39).
        Payload: [unused, manufacturer_id, device_type, belt_id(4 bytes)]
        """
        user_number = 0
        manufacturer_id = int(manufacturer_id)
        device_type_final = None
        try:
            device_type_final = int(device_type)
        except ValueError:
            pass

        try:
            device_type_map = dict((v.lower(), k) for k, v in HRM_DEVICE_TYPE.items())
            device_type_final = device_type_map.get(device_type.lower())
        except (AttributeError, KeyError):
            pass

        belt_id = int(belt_id)

        if user_number < 0 or user_number > 4:
            raise ValueError("user_number must be 0..4")

        if manufacturer_id < 0 or manufacturer_id > 255:
            raise ValueError("manufacturer_id must be 0..255")

        if device_type_final < 0 or device_type_final > 1:
            raise ValueError("device_type must be 0..1")

        if belt_id < 0 or belt_id > 0xFFFFFFFF:
            raise ValueError("belt_id must fit in 32-bit unsigned")

        data = bytes(
            [user_number, manufacturer_id, device_type_final]
        ) + belt_id.to_bytes(4, byteorder="little", signed=False)

        # For set commands, we still await the “echo” response so HA service can report failure quickly.
        await self._csafe_request(
            CSAFE_COMMAND_WRAPPER_SETPMDATA,
            CSAFE_PM_SET_EXTENDED_HRBELT_INFO,
            data=data,
        )

    # ---------- Battery ----------
    async def poll_battery_once(self) -> None:
        """
        Poll CSAFE_PM_GET_BATTERYLEVELPERCENT (0x97).
        Response: one byte percent.
        """
        try:
            resp = await self._csafe_request(
                CSAFE_COMMAND_WRAPPER_GETPMDATA, CSAFE_PM_GET_BATTERYLEVELPERCENT
            )
        except Exception as err:
            _LOGGER.debug("Battery poll failed (%s): %s", self.address, err)
            return

        if len(resp) < 3:
            _LOGGER.debug("Received unexpected length battery response: %s", resp)
            return

        if resp[0] != CSAFE_PM_GET_BATTERYLEVELPERCENT:
            _LOGGER.debug("Received invalid battery response: %s", resp)
            return

        pct = resp[2]
        if pct < 0 or pct > 100:
            return

        self.on_data(PM5Data({"battery_level_percent": pct}))

    # ---------- Workout actions ----------
    async def _terminate_workout(self) -> bool:
        """Attempt to end the current workout via CSAFE terminate screenstate."""
        if not self._client:
            return False

        now = time.monotonic()
        if (
            now - self._last_terminate_attempt_monotonic
        ) < TERMINATE_RATE_LIMIT_SECONDS:
            return False

        if not self._workout_looks_active():
            return False

        frame = build_csafe_standard_frame(CSAFE_PM_TERMINATE_WORKOUT_CONTENTS)

        async with self._csafe_lock:
            now = time.monotonic()
            if (
                now - self._last_terminate_attempt_monotonic
            ) < TERMINATE_RATE_LIMIT_SECONDS:
                return False
            if not self._workout_looks_active():
                return False

            self._last_terminate_attempt_monotonic = now
            try:
                _LOGGER.info(
                    "Attempting to terminate PM5 workout via CSAFE (%s)", self.address
                )
                await self._send_csafe_frame(frame)
                return True
            except Exception as err:
                _LOGGER.warning(
                    "Failed to send CSAFE terminate workout (%s): %s", self.address, err
                )
                return False

    # ---------- Date/time actions ----------
    def _build_set_datetime_contents(self) -> bytes:
        # Use HA's configured timezone (dt_util.now() is timezone-aware and respects HA config)
        now = dt_util.now()

        if (now.second + now.microsecond / 1e6) >= 30:
            # Round up so we're accurate witin 30 seconds
            now += timedelta(minutes=1)

        y1 = now.year >> 8
        y2 = now.year & 0xFF
        mo = now.month
        dd = now.day
        hh = int(now.strftime("%I"))
        mi = now.minute
        xm = 0 if now.strftime("%p").lower() == "am" else 1

        cmd = CSAFE_PM_SET_DATETIME
        data = bytes([hh, mi, xm, mo, dd, y1, y2])
        length = 2 + len(data)  # cmd + len(data) + data
        return bytes([CSAFE_COMMAND_WRAPPER_SETPMCFG, length, cmd, len(data)]) + data

    async def _sync_datetime(self) -> bool:
        """Sync PM5 date/time using CSAFE_PM_SET_DATETIME (0x22)."""
        if not self._client:
            return False

        now_m = time.monotonic()
        if (
            now_m - self._last_datetime_sync_monotonic
        ) < DATETIME_SYNC_RATE_LIMIT_SECONDS:
            return False

        # Proxy stacks can be touchy immediately after connect; let notifies settle.
        if DATETIME_SYNC_CONNECT_GRACE_SECONDS > 0:
            await asyncio.sleep(DATETIME_SYNC_CONNECT_GRACE_SECONDS)

        contents = self._build_set_datetime_contents()
        frame = build_csafe_standard_frame(contents)

        async with self._csafe_lock:
            # Re-check inside lock to avoid duplicate syncs on racing tasks
            now_m = time.monotonic()
            if (
                now_m - self._last_datetime_sync_monotonic
            ) < DATETIME_SYNC_RATE_LIMIT_SECONDS:
                return False

            try:
                _LOGGER.info("Syncing PM5 date/time via CSAFE (%s)", self.address)
                await asyncio.wait_for(
                    self._send_csafe_frame(frame), timeout=DATETIME_SYNC_TIMEOUT_SECONDS
                )
                self._last_datetime_sync_monotonic = now_m
                return True
            except asyncio.TimeoutError:
                _LOGGER.warning("CSAFE date/time sync timed out (%s)", self.address)
                return False
            except Exception as err:
                _LOGGER.warning(
                    "CSAFE date/time sync failed (%s): %s", self.address, err
                )
                return False

    # ---------- Pollers ----------
    async def _poll_loop(self, *, initial_delay: float, interval: float, fn) -> None:
        try:
            await asyncio.sleep(initial_delay)
            while (
                not self._stop.is_set() and self._connected and self._client is not None
            ):
                await fn()
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
        except Exception as err:
            _LOGGER.debug("Poll loop crashed (%s): %s", self.address, err)

    # ---------- Idle watchdog ----------
    @property
    def idle_disconnect_minutes(self) -> int:
        if self._config_entry is None:
            return DEFAULT_IDLE_DISCONNECT_MINUTES
        return self._config_entry.options.get(
            CONF_IDLE_DISCONNECT_MINUTES, DEFAULT_IDLE_DISCONNECT_MINUTES
        )

    async def _idle_watchdog(self, idle_event: asyncio.Event) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(5)

                last_active = self._last_active_monotonic
                if last_active is None:
                    continue

                idle_minutes = self.idle_disconnect_minutes
                idle_seconds = time.monotonic() - last_active
                if idle_seconds < idle_minutes * 60:
                    continue

                _LOGGER.warning(
                    "PM5 idle for %.0f seconds (timeout %d minutes, workout state %s); "
                    "disconnecting to allow sleep (%s)",
                    idle_seconds,
                    idle_minutes,
                    self._workout_state_raw,
                    self.address,
                )
                idle_event.set()
                return
        except asyncio.CancelledError:
            return

    async def _start_notify(
        self,
        client: BleakClientWithServiceCache,
        subscriptions: set[str],
        uuid: str,
        handler: Callable[[str, bytearray], None],
    ) -> bool:
        def forward(_sender: BleakGATTCharacteristic, data: bytearray) -> None:
            if self._client is client:
                handler(uuid, data)

        try:
            await client.start_notify(uuid, forward)
        except BleakError as err:
            _LOGGER.debug("Could not subscribe to %s (%s): %s", uuid, self.address, err)
            return False
        subscriptions.add(uuid)
        return True

    async def _connect_and_listen(self) -> None:
        ble_device = self._get_ble_device()
        started = time.monotonic()

        disconnected = asyncio.Event()
        idle_disconnect = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _disconnected_callback(_client):
            loop.call_soon_threadsafe(disconnected.set)

        client = self._client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            self.address,
            disconnected_callback=_disconnected_callback,
            max_attempts=2,
        )
        subscriptions: set[str] = set()

        try:
            # Reset idle tracking on connect
            self._last_active_monotonic = time.monotonic()
            self._workout_state_raw = None
            self._last_screen_status = None
            self._set_available(True)
            _LOGGER.debug(
                "PM5 BLE link established (%s) in %.1fs",
                self.address,
                time.monotonic() - started,
            )

            multiplexed = await self._start_notify(
                client, subscriptions, self._uuid80, self._handle_0080
            )
            # The PM5 keeps a metric on the multiplexed stream until its direct
            # notification is enabled. Prefer direct packets where they carry more data.
            if not multiplexed:
                for uuid, handler in (
                    (self._uuid31, self._handle_0031),
                    (self._uuid32, self._handle_0032),
                ):
                    if not await self._start_notify(client, subscriptions, uuid, handler):
                        raise BleakConnectionError(
                            f"PM5 live workout stream unavailable: {uuid} ({self.address})"
                        )
                await self._start_notify(
                    client, subscriptions, self._uuid36, self._handle_0036
                )
            for uuid, handler in (
                (self._uuid39, self._handle_0039),
                (self._uuid3a, self._handle_003A),
                (self._uuid33, self._handle_0033),
                (self._uuid35, self._handle_0035),
            ):
                await self._start_notify(client, subscriptions, uuid, handler)

            has_csafe_tx = await self._start_notify(
                client, subscriptions, self._uuid_csafe_tx, self._handle_csafe_tx
            )
            has_csafe_rx = (
                client.services.get_characteristic(self._uuid_csafe_rx) is not None
            )
            if not multiplexed:
                for uuid, handler in (
                    (self._uuid37, self._handle_0037),
                    (self._uuid38, self._handle_0038),
                ):
                    await self._start_notify(client, subscriptions, uuid, handler)

            _LOGGER.info("Connected to PM5 (%s)", self.address)
            _LOGGER.debug(
                "PM5 notifications ready (%s) after %.1fs (%s live stream)",
                self.address,
                time.monotonic() - started,
                "multiplexed" if multiplexed else "direct",
            )
            self.supports_memory_reads = has_csafe_tx and has_csafe_rx
            self._set_connected(True)

            if has_csafe_rx:
                try:
                    await self._sync_datetime()
                except Exception:
                    _LOGGER.exception(
                        "Unexpected error during PM5 datetime sync (%s)", self.address
                    )

            tasks = {
                asyncio.create_task(disconnected.wait()),
                asyncio.create_task(idle_disconnect.wait()),
                asyncio.create_task(self._idle_watchdog(idle_disconnect)),
                asyncio.create_task(self._stop.wait()),
            }
            if has_csafe_rx and has_csafe_tx:
                tasks.update(
                    (
                        asyncio.create_task(
                            self._poll_loop(
                                initial_delay=0.0,
                                interval=HRM_POLL_SECONDS,
                                fn=self.poll_hrbelt_info_once,
                            )
                        ),
                        asyncio.create_task(
                            self._poll_loop(
                                initial_delay=2.0,
                                interval=BATTERY_POLL_SECONDS,
                                fn=self.poll_battery_once,
                            )
                        ),
                    )
                )

            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            if idle_disconnect.is_set() and not self._stop.is_set():
                try:
                    terminated = has_csafe_rx and await self._terminate_workout()
                except Exception:
                    terminated = False
                    _LOGGER.exception(
                        "Unexpected error during PM5 workout termination (%s)",
                        self.address,
                    )

                # Give PM a moment to emit end-of-workout summaries (0039/003A)
                if terminated and not self._stop.is_set():
                    await asyncio.sleep(TERMINATE_GRACE_SECONDS)

                self._reconnect_after = (
                    time.monotonic() + IDLE_DISCONNECT_COOLDOWN_SECONDS
                )
                _LOGGER.debug(
                    "Waiting for a new PM5 advertisement after the %ss sleep cooldown (%s)",
                    IDLE_DISCONNECT_COOLDOWN_SECONDS,
                    self.address,
                )

        finally:
            if self._stop.is_set():
                reason = "integration stop"
            elif idle_disconnect.is_set():
                reason = "idle timeout"
            elif disconnected.is_set():
                reason = "remote disconnect"
            else:
                reason = "connection cleanup"
            teardown_started = time.monotonic()
            _LOGGER.debug("Starting PM5 BLE teardown (%s): %s", self.address, reason)
            self.supports_memory_reads = False
            self._set_connected(False)

            self._cancel_pending_csafe()

            self._client = None  # break reference cycles ASAP

            for uuid in subscriptions:
                try:
                    await client.stop_notify(uuid)
                except Exception as err:
                    _LOGGER.debug(
                        "Could not stop PM5 notifications for %s (%s): %s",
                        uuid,
                        self.address,
                        err,
                    )
            try:
                await client.disconnect()
            except Exception as err:
                _LOGGER.warning(
                    "PM5 BLE disconnect failed (%s) after %.1fs (%s): %s",
                    self.address, time.monotonic() - teardown_started, reason, err,
                )
            else:
                elapsed = time.monotonic() - teardown_started
                if client.is_connected:
                    _LOGGER.warning(
                        "PM5 still reports connected after BLE teardown (%s) in %.1fs (%s)",
                        self.address, elapsed, reason,
                    )
                else:
                    _LOGGER.info(
                        "PM5 BLE teardown complete (%s) in %.1fs (%s)",
                        self.address, elapsed, reason,
                    )

    # ---------- Notification Parsers ----------
    def _handle_csafe_tx(self, _uuid: str, data: bytearray) -> None:
        """Collect complete CSAFE frames across GATT notifications."""
        buffer = self._csafe_rx_buffer
        for byte in data:
            if byte == CSAFE_STD_START:
                buffer.clear()
                buffer.append(byte)
            elif byte == CSAFE_EXT_START or len(buffer) >= _CSAFE_MAX_FRAME_BYTES:
                buffer.clear()
            elif buffer:
                buffer.append(byte)
                if byte == CSAFE_STOP:
                    try:
                        contents = self._parse_csafe_standard_frame(bytes(buffer))
                        self._consume_csafe_responses(contents)
                    except ValueError as err:
                        _LOGGER.debug("CSAFE TX parse error (%s): %s", self.address, err)
                    buffer.clear()

    # 0x0080 (variable) Multiplexed information
    def _handle_0080(self, uuid: str, data: bytearray) -> None:
        if not data:
            return

        cmd = data[0]
        payload = data[1:]
        match cmd:
            case 0x31:
                self._handle_0031(uuid, payload)
            case 0x32:
                self._handle_0032(uuid, payload)
            case 0x33:
                self._handle_0033(uuid, payload)
            case 0x35:
                self._handle_0035(uuid, payload)
            case 0x36:
                self._handle_0036(uuid, payload)
            case 0x37:
                self._handle_0037(uuid, payload)
            case 0x38:
                self._handle_0038(uuid, payload)
            case 0x39:
                self._handle_0039(uuid, payload)
            case 0x3A:
                self._handle_003A(uuid, payload)
            case 0x3E:
                # Additional status 3 repeats at the telemetry cadence, including
                # on menu screens. Log transitions once to show what the PM saw.
                if len(payload) >= 6:
                    status = (payload[0], u16_le(payload, 2), u16_le(payload, 4))
                    if status != self._last_screen_status:
                        self._last_screen_status = status
                        _LOGGER.debug(
                            "PM5 operational state %d, screen %d, error %d (%s)",
                            *status,
                            self.address,
                        )
            case _:
                _LOGGER.debug(
                    "Received unhandled multiplex command (%s): %s -> %s",
                    self.address,
                    hex(cmd),
                    payload.hex(),
                )

    # 0x0031 (19 bytes) Rowing general status
    def _handle_0031(self, _uuid: str, data: bytearray) -> None:
        b = bytes(data)
        if len(b) != 19:
            _LOGGER.debug("Received unexpected length 0x0031 notify: %s", b.hex())
            return

        elapsed_time_s = u24_le(b, 0) / 100.0
        distance_m = u24_le(b, 3) / 10.0
        workout_type_raw = b[6]
        workout_type = try_lookup(
            self.address, "workout type", WORKOUT_TYPE, workout_type_raw
        )
        interval_type_raw = b[7]
        interval_type = try_lookup(
            self.address, "interval type", INTERVAL_TYPE, interval_type_raw
        )
        workout_state_raw = b[8]
        workout_state = try_lookup(
            self.address, "workout state", WORKOUT_STATE, workout_state_raw
        )
        rowing_state_raw = b[9]
        rowing_state = try_lookup(
            self.address, "rowing state", ROWING_STATE, rowing_state_raw
        )
        stroke_state_raw = b[10]
        stroke_state = try_lookup(
            self.address, "stroke state", STROKE_STATE, stroke_state_raw
        )
        total_work_distance_m = u24_le(b, 11)
        workout_duration = u24_le(b, 14)
        workout_duration_type_raw = b[17]
        workout_duration_type = try_lookup(
            self.address,
            "workout duration type",
            WORKOUT_DURATION_TYPE,
            workout_duration_type_raw,
        )
        drag_factor = b[18]

        if self._workout_state_raw is None:
            _LOGGER.debug(
                "First PM5 general status (%s): %s, elapsed %.2fs, distance %.1fm",
                self.address,
                workout_state,
                elapsed_time_s,
                distance_m,
            )
        elif workout_state_raw != self._workout_state_raw:
            _LOGGER.debug(
                "PM5 workout state changed (%s): %s, elapsed %.2fs, distance %.1fm",
                self.address,
                workout_state,
                elapsed_time_s,
                distance_m,
            )
        # Track workout state for terminate gating
        self._workout_state_raw = workout_state_raw

        # Treat rowing "Active" as activity
        if rowing_state_raw == 1:
            self._last_active_monotonic = time.monotonic()

        self.on_data(
            PM5Data(
                {
                    "0031_elapsed_time_s": elapsed_time_s,
                    "0031_distance_m": distance_m,
                    "0031_workout_type": workout_type,
                    "0031_interval_type": interval_type,
                    "0031_workout_state": workout_state,
                    "0031_workout_state_raw": workout_state_raw,
                    "0031_rowing_state": rowing_state,
                    "0031_rowing_state_raw": rowing_state_raw,
                    "0031_stroke_state": stroke_state,
                    "0031_total_work_distance_m": total_work_distance_m,
                    "0031_workout_duration": (
                        workout_duration
                        if workout_duration_type_raw != 0
                        else (workout_duration / 100)
                    ),
                    "0031_workout_duration_type": workout_duration_type,
                    "0031_drag_factor": drag_factor,
                }
            )
        )

    # 0x0032 Rowing additional status 1
    def _handle_0032(self, uuid: str, data: bytearray) -> None:
        b = bytes(data)
        multiplexed = uuid == self._uuid80
        base_size = 18 if multiplexed else 16
        if len(b) not in (base_size, base_size + 1):
            _LOGGER.debug("Received unexpected length 0x0032 notify: %s", b.hex())
            return

        elapsed_time_s = u24_le(b, 0) / 100.0
        speed_m_s = u16_le(b, 3) / 1000.0
        stroke_rate_spm = b[5]
        current_pace_s_per_500m = u16_le(b, 7) / 100.0
        average_pace_s_per_500m = u16_le(b, 9) / 100.0
        rest_distance_m = u16_le(b, 11)
        rest_time_s = u24_le(b, 13) / 100.0
        avg_power_w = u16_le(b, 16) if multiplexed else None
        erg_type = (
            try_lookup(self.address, "erg type", ERG_MACHINE_TYPE, b[base_size])
            if len(b) > base_size else None
        )

        self.on_data(
            PM5Data(
                {
                    "0032_elapsed_time_s": elapsed_time_s,
                    "0032_speed_m_s": speed_m_s,
                    "0032_stroke_rate_spm": stroke_rate_spm,
                    "0032_heart_rate_bpm": heart_rate_bpm(b[6]),
                    "0032_current_pace_s_per_500m": current_pace_s_per_500m,
                    "0032_average_pace_s_per_500m": average_pace_s_per_500m,
                    "0032_rest_distance_m": rest_distance_m,
                    "0032_rest_time_s": rest_time_s,
                    "0032_avg_power_w": avg_power_w,
                    "0032_erg_machine_type": erg_type,
                }
            )
        )

    # 0x0033 (18-20 bytes) Rowing additional status 2
    def _handle_0033(self, uuid: str, data: bytearray) -> None:
        b = bytes(data)
        if len(b) != (18 if uuid == self._uuid80 else 20):
            _LOGGER.debug("Received unexpected length 0x0033 notify: %s", b.hex())
            return

        elapsed_time_s = u24_le(b, 0) / 100.0
        interval_count = b[3]
        if len(b) < 20:
            average_power_w = None
            total_calories = u16_le(b, 4)
            split_interval_avg_pace_s_per_500m = u16_le(b, 6) / 100.0
            split_interval_avg_power_w = u16_le(b, 8)
            split_interval_avg_calories = u16_le(b, 10)
            last_split_time_s = u24_le(b, 12) / 10.0
            last_split_distance_m = u24_le(b, 15)
        else:
            average_power_w = u16_le(b, 4)
            total_calories = u16_le(b, 6)
            split_interval_avg_pace_s_per_500m = u16_le(b, 8) / 100.0
            split_interval_avg_power_w = u16_le(b, 10)
            split_interval_avg_calories = u16_le(b, 12)
            last_split_time_s = u24_le(b, 14) / 10.0
            last_split_distance_m = u24_le(b, 17)

        self.on_data(
            PM5Data(
                {
                    "0033_elapsed_time_s": elapsed_time_s,
                    "0033_interval_count": interval_count,
                    "0033_average_power_w": average_power_w,
                    "0033_total_calories": total_calories,
                    "0033_split_interval_avg_pace_s_per_500m": split_interval_avg_pace_s_per_500m,
                    "0033_split_interval_avg_power_w": split_interval_avg_power_w,
                    "0033_split_interval_avg_calories": split_interval_avg_calories,
                    "0033_last_split_time_s": last_split_time_s,
                    "0033_last_split_distance_m": last_split_distance_m,
                }
            )
        )

    # 0x0035 (18-20 bytes) Stroke data
    def _handle_0035(self, uuid: str, data: bytearray) -> None:
        b = bytes(data)
        if len(b) != (18 if uuid == self._uuid80 else 20):
            _LOGGER.debug("Received unexpected length 0x0035 notify: %s", b.hex())
            return

        elapsed_time_s = u24_le(b, 0) / 100.0
        distance_m = u24_le(b, 3) / 10.0
        drive_length_m = b[6] / 100.0
        drive_time_s = b[7] / 100.0
        stroke_recovery_time_s = u16_le(b, 8) / 100.0
        stroke_distance_m = u16_le(b, 10) / 100.0
        peak_drive_force = u16_le(b, 12) / 10.0
        average_drive_force = u16_le(b, 14) / 10.0
        if len(b) < 20:
            work_per_stroke_j = None
            stroke_count = u16_le(b, 16)
        else:
            work_per_stroke_j = u16_le(b, 16) / 10.0
            stroke_count = u16_le(b, 18)

        self.on_data(
            PM5Data(
                {
                    "0035_elapsed_time_s": elapsed_time_s,
                    "0035_distance_m": distance_m,
                    "0035_drive_length_m": drive_length_m,
                    "0035_drive_time_s": drive_time_s,
                    "0035_stroke_recovery_time_s": stroke_recovery_time_s,
                    "0035_stroke_distance_m": stroke_distance_m,
                    "0035_peak_drive_force": peak_drive_force,
                    "0035_average_drive_force": average_drive_force,
                    "0035_work_per_stroke_j": work_per_stroke_j,
                    "0035_stroke_count": stroke_count,
                }
            )
        )

    # 0x0036 (15-17 bytes) Stroke data
    def _handle_0036(self, uuid: str, data: bytearray) -> None:
        b = bytes(data)
        if len(b) != (17 if uuid == self._uuid80 else 15):
            _LOGGER.debug("Received unexpected length 0x0036 notify: %s", b.hex())
            return

        elapsed_time_s = u24_le(b, 0) / 100.0
        stroke_power_w = u16_le(b, 3)
        stroke_calories_hr = u16_le(b, 5)
        stroke_count = u16_le(b, 7)
        projected_work_time_s = u24_le(b, 9)
        projected_work_distance_m = u24_le(b, 12)

        if len(b) < 17:
            work_per_stroke_j = None
        else:
            work_per_stroke_j = u16_le(b, 15) / 10.0

        self.on_data(
            PM5Data(
                {
                    "0036_elapsed_time_s": elapsed_time_s,
                    "0036_stroke_power_w": stroke_power_w,
                    "0036_stroke_calories_hr": stroke_calories_hr,
                    "0036_stroke_count": stroke_count,
                    "0036_projected_work_time_s": (
                        None if projected_work_time_s == 0 else projected_work_time_s
                    ),
                    "0036_projected_work_distance_m": (
                        None
                        if projected_work_distance_m == 0
                        else projected_work_distance_m
                    ),
                    "0036_work_per_stroke_j": work_per_stroke_j,
                }
            )
        )

    # 0x0037 (18 bytes) Split/interval data
    def _handle_0037(self, _uuid: str, data: bytearray) -> None:
        b = bytes(data)
        if len(b) != 18:
            _LOGGER.debug("Received unexpected length 0x0037 notify: %s", b.hex())
            return

        elapsed_time_s = u24_le(b, 0) / 100.0
        distance_m = u24_le(b, 3) / 10.0
        split_interval_time_s = u24_le(b, 6) / 10.0
        split_interval_distance_m = u24_le(b, 9)
        interval_rest_time_s = u16_le(b, 12)
        interval_rest_distance_m = u16_le(b, 14)
        split_interval_type_raw = b[16]
        split_interval_type = try_lookup(
            self.address, "interval type", INTERVAL_TYPE, split_interval_type_raw
        )
        split_interval_number = b[17]

        self.on_data(
            PM5Data(
                {
                    "0037_elapsed_time_s": elapsed_time_s,
                    "0037_distance_m": distance_m,
                    "0037_split_interval_time_s": split_interval_time_s,
                    "0037_split_interval_distance_m": split_interval_distance_m,
                    "0037_interval_rest_time_s": interval_rest_time_s,
                    "0037_interval_rest_distance_m": interval_rest_distance_m,
                    "0037_split_interval_type": split_interval_type,
                    "0037_split_interval_number": split_interval_number,
                }
            )
        )

    # 0x0038 (18-19 bytes) Split/interval data
    def _handle_0038(self, _uuid: str, data: bytearray) -> None:
        b = bytes(data)
        if len(b) not in (18, 19):
            _LOGGER.debug("Received unexpected length 0x0038 notify: %s", b.hex())
            return

        elapsed_time_s = u24_le(b, 0) / 100.0
        split_interval_avg_stroke_rate_spm = b[3]
        split_interval_avg_pace_s_per_500m = u16_le(b, 6) / 10.0
        split_interval_total_calories = u16_le(b, 8)
        split_interval_avg_calories_hr = u16_le(b, 10)
        split_interval_speed_m_s = u16_le(b, 12) / 1000.0
        split_interval_power_w = u16_le(b, 14)
        split_avg_drag_factor = b[16]
        split_interval_number = b[17]
        erg_type = (
            try_lookup(self.address, "erg type", ERG_MACHINE_TYPE, b[18])
            if len(b) == 19 else None
        )

        self.on_data(
            PM5Data(
                {
                    "0038_elapsed_time_s": elapsed_time_s,
                    "0038_split_interval_avg_stroke_rate_spm": split_interval_avg_stroke_rate_spm,
                    "0038_split_interval_work_heart_rate_bpm": heart_rate_bpm(b[4]),
                    "0038_split_interval_rest_heart_rate_bpm": heart_rate_bpm(b[5]),
                    "0038_split_interval_avg_pace_s_per_500m": split_interval_avg_pace_s_per_500m,
                    "0038_split_interval_total_calories": split_interval_total_calories,
                    "0038_split_interval_avg_calories_hr": split_interval_avg_calories_hr,
                    "0038_split_interval_speed_m_s": split_interval_speed_m_s,
                    "0038_split_interval_power_w": split_interval_power_w,
                    "0038_split_avg_drag_factor": split_avg_drag_factor,
                    "0038_split_interval_number": split_interval_number,
                    "0038_erg_type": erg_type,
                }
            )
        )

    # 0x0039 (18-20 bytes) End-of-workout summary
    def _handle_0039(self, uuid: str, data: bytearray) -> None:
        b = bytes(data)
        if len(b) != (18 if uuid == self._uuid80 else 20):
            _LOGGER.debug("Received unexpected length 0x0039 notify: %s", b.hex())
            return

        log_date = u16_le(b, 0)
        log_time = u16_le(b, 2)
        elapsed_s = u24_le(b, 4) / 100.0
        distance_m = u24_le(b, 7) / 10.0
        avg_stroke_rate = b[10]
        drag_avg = b[15]
        workout_type_raw = b[17]
        workout_type = try_lookup(
            self.address, "workout type", WORKOUT_TYPE, workout_type_raw
        )
        if len(b) < 20:
            avg_pace_s_per_500m = None
        else:
            avg_pace_s_per_500m = u16_le(b, 18) / 10.0

        self.on_data(
            PM5Data(
                {
                    "0039_log_entry_date": log_date,
                    "0039_log_entry_time": log_time,
                    "0039_elapsed_time_s": elapsed_s,
                    "0039_distance_m": distance_m,
                    "0039_avg_stroke_rate_spm": avg_stroke_rate,
                    "0039_ending_heart_rate_bpm": heart_rate_bpm(b[11]),
                    "0039_avg_heart_rate_bpm": heart_rate_bpm(b[12]),
                    "0039_min_heart_rate_bpm": heart_rate_bpm(b[13]),
                    "0039_max_heart_rate_bpm": heart_rate_bpm(b[14]),
                    "0039_drag_factor_avg": drag_avg,
                    "0039_recovery_heart_rate_bpm": heart_rate_bpm(b[16]),
                    "0039_workout_type": workout_type,
                    "0039_avg_pace_s_per_500m": avg_pace_s_per_500m,
                }
            )
        )

    # 0x003A (18-19 bytes) End-of-workout additional summary
    def _handle_003A(self, uuid: str, data: bytearray) -> None:
        b = bytes(data)
        if len(b) != (18 if uuid == self._uuid80 else 19):
            _LOGGER.debug("Received unexpected length 0x003A notify: %s", b.hex())
            return

        log_date = u16_le(b, 0)
        log_time = u16_le(b, 2)
        if len(b) < 19:
            split_interval_type_raw = None
            split_interval_type = None
            split_interval_size = u16_le(b, 4)
            split_interval_count = b[6]
            total_calories = u16_le(b, 7)
            avg_power_w = u16_le(b, 9)
            total_rest_distance_m = u24_le(b, 11)
            interval_rest_time_s = u16_le(b, 14)
            avg_calories_per_hr = u16_le(b, 16)
        else:
            split_interval_type_raw = b[4]
            split_interval_type = try_lookup(
                self.address, "interval type", INTERVAL_TYPE, split_interval_type_raw
            )
            split_interval_size = u16_le(b, 5)
            split_interval_count = b[7]
            total_calories = u16_le(b, 8)
            avg_power_w = u16_le(b, 10)
            total_rest_distance_m = u24_le(b, 12)
            interval_rest_time_s = u16_le(b, 15)
            avg_calories_per_hr = u16_le(b, 17)

        self.on_data(
            PM5Data(
                {
                    "003a_log_entry_date": log_date,
                    "003a_log_entry_time": log_time,
                    "003a_split_interval_type": split_interval_type,
                    "003a_split_interval_size": split_interval_size,
                    "003a_split_interval_count": split_interval_count,
                    "003a_total_calories": total_calories,
                    "003a_avg_power_w": avg_power_w,
                    "003a_total_rest_distance_m": total_rest_distance_m,
                    "003a_interval_rest_time_s": interval_rest_time_s,
                    "003a_avg_calories_per_hr": avg_calories_per_hr,
                }
            )
        )
