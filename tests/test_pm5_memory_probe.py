"""Exercise the manual PM5 memory action and the CSAFE transport it shares."""

import asyncio
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from bleak.exc import BleakError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized

from custom_components.c2_pm5 import async_setup, async_setup_entry, memory_probe
from custom_components.c2_pm5.const import (
    CSAFE_COMMAND_WRAPPER_GETPMCFG as GET_CFG,
    CSAFE_COMMAND_WRAPPER_GETPMDATA as GET_DATA,
    CSAFE_COMMAND_WRAPPER_SETPMDATA as SET_DATA,
    DOMAIN,
)
from custom_components.c2_pm5.pm5_ble import PM5BleSession, build_csafe_standard_frame


def frame(wrapper, command, data):
    response = bytes([command, len(data)]) + data
    return build_csafe_standard_frame(bytes([0x81, wrapper, len(response)]) + response)


class TransportTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = PM5BleSession(None, "RowErg", "PM5", Mock(), Mock())
        self.client = self.session._client = SimpleNamespace(write_gatt_char=AsyncMock())

    async def start_request(self, wrapper, command, data=b""):
        task = asyncio.create_task(self.session._csafe_request(wrapper, command, data))
        await asyncio.sleep(0)
        return task

    async def test_wrapped_blocks_fragmentation_and_concatenation(self):
        task = await self.start_request(GET_DATA, 0x6A, b"\x00" * 5)
        # Below is a 65-byte response with delimiters that need byte stuffing.
        body = bytes([64, 0xF0, 0xF1, 0xF2, 0xF3]) + bytes(60)
        encoded = frame(GET_DATA, 0x6A, body)
        split = encoded.index(0xF3) + 1
        self.session._handle_csafe_tx(
            "", b"noise" + frame(GET_CFG, 0x6A, b"") + encoded[:split]
        )
        self.assertFalse(task.done())
        for pos in range(split, len(encoded), 20):
            self.session._handle_csafe_tx("", encoded[pos : pos + 20])
        self.assertEqual(await task, bytes([0x6A, 65]) + body)

    async def test_invalid_frames_cannot_satisfy_request(self):
        task = await self.start_request(GET_DATA, 0x97)
        valid = frame(GET_DATA, 0x97, b"\x42")
        inputs = [
            valid[:-2] + b"\x00\xf2",  # checksum
            build_csafe_standard_frame(bytes.fromhex("817f04970142")),  # wrapper length
            build_csafe_standard_frame(bytes.fromhex("817f03970242")),  # inner length
            build_csafe_standard_frame(bytes.fromhex("917f03970142")),  # rejected
            b"\xf1" + bytes(125) + b"\xf2",  # too long
        ]
        for bad in inputs:
            self.session._handle_csafe_tx("", bad)
        self.assertFalse(self.session._pending_csafe[(GET_DATA, 0x97)].done())
        self.session._handle_csafe_tx("", valid)
        self.assertEqual(await task, b"\x97\x01\x42")

    async def test_preserves_set_ack_and_logged_battery_reply(self):
        task = await self.start_request(SET_DATA, 0x39, b"\x00" * 7)
        self.session._handle_csafe_tx(
            "", build_csafe_standard_frame(bytes.fromhex("01770139"))
        )
        self.assertEqual(await task, b"\x39")
        def reply(*_, **__):
            self.session._handle_csafe_tx("", bytes.fromhex("f1817f0397014229f2"))

        self.client.write_gatt_char.side_effect = reply
        await self.session.poll_battery_once()
        self.assertEqual(self.session.on_data.call_args.args[0].values, {"battery_level_percent": 66})

    async def test_timeout_failed_write_and_cancel_clear_partial_frame(self):
        async def receive_prefix(*args, **kwargs):
            self.session._handle_csafe_tx("", b"\xf1\x81")

        self.client.write_gatt_char.side_effect = receive_prefix
        with self.assertRaises(TimeoutError):
            await self.session._csafe_request(GET_DATA, 0x97, timeout=0.001)
        self.assertFalse(self.session._pending_csafe)
        self.assertFalse(self.session._csafe_rx_buffer)
        task = await self.start_request(GET_DATA, 0x97)
        self.session._cancel_pending_csafe()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.session._csafe_rx_buffer)
        self.client.write_gatt_char.side_effect = OSError("disconnected")
        with self.assertRaises(OSError):
            await self.session._csafe_request(GET_DATA, 0x97)
        self.assertFalse(self.session._pending_csafe)


class MemoryProbeTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = PM5BleSession(None, "RowErg", "PM5", Mock(), Mock())
        self.session._client = SimpleNamespace(is_connected=True)
        self.session._connected = True
        self.session._workout_state_raw = 0
        self.coordinator = SimpleNamespace(
            session=self.session, recorder=SimpleNamespace(recording=False)
        )
        self.address = 0x1244E
        self.record = bytes(range(210))
        self.descriptor = bytes.fromhex("0001244e00d2")
        self.before_reply = lambda: None
        self.session._csafe_request = AsyncMock(side_effect=self.reply)

    async def reply(self, wrapper, cmd, data, *, before_write, timeout):
        before_write()
        if cmd == 0x99:
            self.assertEqual((wrapper, data), (GET_CFG, b""))
            payload = self.descriptor
        else:
            self.assertEqual((wrapper, cmd), (GET_DATA, 0x6A))
            index = int.from_bytes(data[:4], "big") - self.address
            size = data[4]
            self.assertLessEqual(size, 64)
            self.assertLessEqual(index + size, len(self.record))
            payload = bytes([size]) + self.record[index : index + size].ljust(64, b"\xff")
        self.before_reply()
        return bytes([cmd, len(payload)]) + payload

    async def test_latest_only_bounded_blocks_and_descriptor_recheck(self):
        result = await memory_probe.read_latest(self.coordinator)
        self.assertEqual(
            result, {"address": self.address, "length": 210, "record_hex": self.record.hex()}
        )
        reads = self.session._csafe_request.await_args_list
        self.assertEqual([call.args[1] for call in reads], [0x99, *([0x6A] * 4), 0x99])
        self.assertEqual([call.args[2][-1] for call in reads[1:-1]], [64, 64, 64, 18])

    async def test_rejects_empty_oversized_and_overflowing_descriptors(self):
        for address, size in ((0, 0), (10, 4097), (0xFFFFFFFE, 8)):
            self.descriptor = address.to_bytes(4, "big") + size.to_bytes(2, "big")
            with self.subTest(address=address, size=size), self.assertRaises(HomeAssistantError):
                await memory_probe.read_latest(self.coordinator)
            self.assertEqual(self.session._csafe_request.await_count, 1)
            self.session._csafe_request.reset_mock()

    async def test_unknown_or_active_workout_and_link_change(self):
        for state in (None, 1, 3):
            self.session._workout_state_raw = state
            with self.assertRaises(HomeAssistantError):
                await memory_probe.read_latest(self.coordinator)
        self.session._csafe_request.assert_not_awaited()
        self.session._workout_state_raw = 0
        def reconnect():
            self.session._client = SimpleNamespace(is_connected=True)

        self.before_reply = reconnect
        with self.assertRaisesRegex(HomeAssistantError, "stay connected"):
            await memory_probe.read_latest(self.coordinator)
        self.assertEqual(self.session._csafe_request.await_count, 1)

    async def test_rejects_inconsistent_descriptor_or_block(self):
        def descriptor_change():
            if self.session._csafe_request.await_count == 3:
                self.descriptor = bytes(6)

        self.before_reply = descriptor_change
        with self.assertRaisesRegex(HomeAssistantError, "changed"):
            await memory_probe.read_latest(self.coordinator)
        for block in (b"\x6a\x01\x40", b"\x6a\x41" + bytes(65)):
            self.session._csafe_request.side_effect = [
                bytes.fromhex("99060001244e00d2"), block
            ]
            with self.assertRaisesRegex(HomeAssistantError, "invalid|incomplete"):
                await memory_probe.read_latest(self.coordinator)

    async def test_queued_probe_checks_idle_under_transport_lock(self):
        real_session = PM5BleSession(None, "RowErg", "PM5", Mock(), Mock())
        client = real_session._client = SimpleNamespace(is_connected=True, write_gatt_char=AsyncMock())
        real_session._connected = True
        real_session._workout_state_raw = 0
        self.coordinator.session = real_session
        async with real_session._csafe_lock:
            task = asyncio.create_task(memory_probe.read_latest(self.coordinator))
            await asyncio.sleep(0)
            real_session._workout_state_raw = 1
        with self.assertRaisesRegex(HomeAssistantError, "idle"):
            await task
        client.write_gatt_char.assert_not_awaited()


class MemoryActionTests(IsolatedAsyncioTestCase):
    action = "read_latest_stored_workout"

    async def asyncSetUp(self):
        self.hass = HomeAssistant(self.enterContext(TemporaryDirectory()))
        self.hass.http = SimpleNamespace(async_register_static_paths=AsyncMock())
        self.hass.config_entries = SimpleNamespace(
            async_forward_entry_setups=AsyncMock(), async_entries=lambda domain: []
        )
        self.entry = Mock(
            spec=ConfigEntry, entry_id="monitor", title="RowErg",
            data={"address": "00:11:22:33:44:55"}, options={},
        )
        # Setup the shipped integration and config entry using their regular
        # entry points. The scanner and radio are the only unavailable hardware.
        with (
            patch.object(PM5BleSession, "start", new_callable=AsyncMock),
            patch.object(memory_probe, "read_latest", new_callable=AsyncMock) as read,
        ):
            self.assertTrue(await async_setup(self.hass, {}))
            self.assertTrue(await async_setup_entry(self.hass, self.entry))
            await self.hass.async_block_till_done()
            read.assert_not_awaited()
        self.coordinator = self.hass.data[DOMAIN][self.entry.entry_id]
        self.session = self.coordinator.session
        self.session._client = SimpleNamespace(is_connected=True)

    async def asyncTearDown(self):
        self.coordinator.recorder.close()
        await self.hass.async_block_till_done()

    async def call(self, entry_id="monitor", *, response=True, context=None):
        return await self.hass.services.async_call(
            DOMAIN, self.action, {"config_entry_id": entry_id},
            blocking=True, return_response=response, context=context,
        )

    async def test_idle_monitor_reads_only_when_action_is_explicitly_called(self):
        self.session._csafe_request = AsyncMock(side_effect=[
            bytes.fromhex("99060001244e0001"),
            bytes.fromhex("6a4101ab") + bytes(63),
            bytes.fromhex("99060001244e0001"),
        ])
        self.session._set_connected(True)
        self.session._handle_0031(self.session._uuid31, bytearray(19))
        self.session._handle_0080(self.session._uuid80, bytearray([0x3F]) + bytearray(15))
        await self.hass.async_block_till_done()
        self.session._csafe_request.assert_not_awaited()
        response = await self.call()
        self.assertEqual(response, {"address": 0x1244E, "length": 1, "record_hex": "ab"})
        self.assertEqual(
            [call.args[1] for call in self.session._csafe_request.await_args_list],
            [0x99, 0x6A, 0x99],
        )
        self.assertEqual(self.hass.data[DOMAIN]["workout_manager"].list_workouts(), [])

    async def test_response_admin_and_loaded_monitor_are_required(self):
        self.assertIs(
            self.hass.services.supports_response(DOMAIN, self.action),
            SupportsResponse.ONLY,
        )
        self.hass.auth = SimpleNamespace(
            async_get_user=AsyncMock(return_value=SimpleNamespace(is_admin=False))
        )
        with patch.object(memory_probe, "read_latest", new_callable=AsyncMock) as read:
            with self.assertRaises(ServiceValidationError):
                await self.call(response=False)
            with self.assertRaises(Unauthorized):
                await self.call(context=Context(user_id="non-admin"))
            with self.assertRaisesRegex(HomeAssistantError, "loaded"):
                await self.call(entry_id="unloaded")
            read.assert_not_awaited()

    async def test_busy_monitor_rejects_second_read_and_can_retry_after_failure(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def fail_after_read_starts(coordinator):
            self.assertIs(coordinator, self.coordinator)
            started.set()
            await release.wait()
            raise HomeAssistantError("The PM5 disconnected during the probe")

        with patch.object(memory_probe, "read_latest", side_effect=fail_after_read_starts) as read:
            first = asyncio.create_task(self.call())
            try:
                async with asyncio.timeout(2):
                    await started.wait()
                    with self.assertRaisesRegex(HomeAssistantError, "already running"):
                        await self.call()
            finally:
                release.set()
                with self.assertRaisesRegex(HomeAssistantError, "disconnected"):
                    await first
            read.assert_awaited_once()
            read.side_effect = None
            read.return_value = {"address": 42, "length": 1, "record_hex": "01"}
            self.assertEqual(await self.call(), read.return_value)
            self.assertEqual(read.await_count, 2)

    async def test_transport_errors_are_reported_to_the_action_caller(self):
        self.session._set_connected(True)
        self.session._workout_state_raw = 0
        for failure, message in (
            (TimeoutError(), "Timed out"),
            (BleakError("unsupported command"), "unsupported command"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.session._csafe_request = AsyncMock(side_effect=failure)
                with self.assertRaisesRegex(HomeAssistantError, message):
                    await self.call()
                self.session._csafe_request.assert_awaited_once()

    async def test_offline_recording_and_unknown_state_do_not_read_memory(self):
        self.session._csafe_request = AsyncMock()
        with self.assertRaisesRegex(HomeAssistantError, "connected"):
            await self.call()
        self.session._set_connected(True)
        with self.assertRaisesRegex(HomeAssistantError, "idle"):
            await self.call()
        self.session._workout_state_raw = 1
        with self.assertRaisesRegex(HomeAssistantError, "idle"):
            await self.call()
        self.session._workout_state_raw = 0
        self.coordinator.recorder.receive(
            {"0031_workout_state_raw": 1, "0031_elapsed_time_s": 3, "0031_distance_m": 10},
            now=1000,
        )
        with self.assertRaisesRegex(HomeAssistantError, "idle"):
            await self.call()
        self.session._csafe_request.assert_not_awaited()
