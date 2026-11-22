"""Read the latest stored PM5 workout record on explicit request."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from bleak.exc import BleakError
from homeassistant.exceptions import HomeAssistantError

from .const import CSAFE_COMMAND_WRAPPER_GETPMCFG, CSAFE_COMMAND_WRAPPER_GETPMDATA
from .pm5_ble import PM5Unavailable

if TYPE_CHECKING:
    from . import PM5Coordinator

_LOG_PARAMS = 0x99
_LOG_MEMORY = 0x6A
_MAX_RECORD_BYTES = 4096
_BLOCK_BYTES = 64


async def read_latest(coordinator: PM5Coordinator) -> dict:
    """Return bytes from a single consistent, monitor-supplied descriptor."""
    session = coordinator.session
    client = session._client

    def require_idle():
        if not session.is_connected or client is None or (
            session._client is not client or not client.is_connected
        ):
            raise HomeAssistantError("The PM5 must stay connected during the probe")
        # A fresh general status packet sets this field on every new connection.
        if coordinator.recorder.recording or session._workout_state_raw not in (
            0, 10, 11, 12, 13
        ):
            raise HomeAssistantError("Wait for idle workout status from the PM5")

    async def request(wrapper: int, command: int, length: int, data: bytes = b""):
        require_idle()
        reply = await session._csafe_request(
            wrapper, command, data, before_write=require_idle, timeout=3.0
        )
        require_idle()
        if len(reply) != length + 2 or reply[:2] != bytes((command, length)):
            raise HomeAssistantError("The PM5 returned an invalid log response")
        return reply[2:]

    try:
        async with asyncio.timeout(30):
            descriptor = await request(CSAFE_COMMAND_WRAPPER_GETPMCFG, _LOG_PARAMS, 6)
            address = int.from_bytes(descriptor[:4], "big")
            length = int.from_bytes(descriptor[4:], "big")
            if length == 0:
                raise HomeAssistantError("The PM5 reports no latest record")
            if length > _MAX_RECORD_BYTES or address + length > 2**32:
                raise HomeAssistantError("Latest PM5 log record exceeds probe limits")

            record = bytearray()
            while len(record) < length:
                size = min(_BLOCK_BYTES, length - len(record))
                args = (address + len(record)).to_bytes(4, "big") + bytes([size])
                block = await request(
                    CSAFE_COMMAND_WRAPPER_GETPMDATA, _LOG_MEMORY, _BLOCK_BYTES + 1, args
                )
                if block[0] != size:
                    raise HomeAssistantError("The PM5 returned an incomplete log block")
                record.extend(block[1 : size + 1])

            if descriptor != await request(CSAFE_COMMAND_WRAPPER_GETPMCFG, _LOG_PARAMS, 6):
                raise HomeAssistantError("The latest PM5 log record changed during the probe")
    except asyncio.CancelledError:
        # Link loss cancels the transport's pending response. Preserve genuine
        # task cancellation on HA shutdown or cancellation of the service call.
        if not asyncio.current_task().cancelling():
            raise HomeAssistantError("The PM5 disconnected during the probe") from None
        raise
    except TimeoutError as err:
        raise HomeAssistantError("Timed out reading the latest PM5 log record") from err
    except (BleakError, PM5Unavailable) as err:
        raise HomeAssistantError(f"Could not read the PM5 log record: {err}") from err

    return {"address": address, "length": length, "record_hex": record.hex()}
