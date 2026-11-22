"""Exercise automatic PM5 memory recovery through normal connection and status events."""

import asyncio
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.c2_pm5 import PM5Coordinator, memory_probe
from custom_components.c2_pm5.workout_manager import WorkoutManager


def stored_row():
    # A missed row from yesterday cannot be recovered by the live finish window.
    end = datetime.now(timezone.utc) - timedelta(days=1)
    record = bytearray(82)
    record[:2] = bytes.fromhex("9501")
    date = ((end.year - 2000) << 9) | (end.day << 4) | end.month
    record[8:12] = date.to_bytes(2, "big") + bytes([end.hour, end.minute])
    record[18:20] = bytes.fromhex("9600")
    record[20:24] = (1224).to_bytes(4, "big")
    record[24:28] = (159).to_bytes(4, "big")
    record[29] = 1
    return {"address": 1024, "length": len(record), "record_hex": record.hex()}


class MemoryRecoveryTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant(self.enterContext(TemporaryDirectory()))
        self.manager = WorkoutManager(self.hass)
        self.coordinator = PM5Coordinator(
            self.hass, Mock(spec=ConfigEntry, title="PM5"),
            "AA:BB:CC:DD:EE:FF", "RowErg", self.manager,
        )
        self.session = self.coordinator.session
        self.read = self.enterContext(patch.object(memory_probe, "read_latest"))
        self.read.return_value = stored_row()

    async def asyncTearDown(self):
        self.session._set_connected(False)
        self.coordinator.recorder.close()
        await self.settle()

    async def settle(self):
        await self.hass.async_block_till_done(wait_background_tasks=True)

    def connect(self, *, early_idle=False, supports_reads=True):
        self.session._client = SimpleNamespace(is_connected=True)
        self.session._workout_state_raw = None
        self.session.supports_memory_reads = supports_reads
        if early_idle:
            # Some PMs send telemetry while the remaining notifies are starting.
            self.status(0)
        self.session._set_connected(True)

    def status(self, state, *, elapsed_cs=0, distance_dm=0):
        packet = bytearray(19)
        packet[:3] = elapsed_cs.to_bytes(3, "little")
        packet[3:6] = distance_dm.to_bytes(3, "little")
        packet[8] = state
        self.session._handle_0031(self.session._uuid31, packet)

    async def test_latest_row_saved_once_across_repeated_packets_and_connections(self):
        self.connect()
        await self.settle()
        self.read.assert_not_awaited()
        self.status(0)
        self.status(0)
        await self.settle()
        self.status(0)
        await self.settle()
        self.read.assert_awaited_once()
        [row] = self.manager.list_workouts()
        self.assertEqual((row["elapsed_seconds"], row["distance_m"]), (122.4, 159))
        self.assertTrue(row["summary_only"])
        self.assertTrue(row["partial"])
        self.assertEqual(row["sample_count"], 0)

        self.session._set_connected(False)
        self.connect(early_idle=True)
        await self.settle()
        [same_row] = self.manager.list_workouts()
        self.assertEqual(same_row["workout_id"], row["workout_id"])
        self.assertEqual(self.read.await_count, 2)

    async def test_without_csafe_or_during_row_no_probe_starts(self):
        self.connect(early_idle=True, supports_reads=False)
        self.status(0)
        await self.settle()
        self.session._set_connected(False)
        self.connect()
        self.status(1)
        await self.settle()
        self.read.assert_not_awaited()
        self.status(0)
        await self.settle()
        self.read.assert_awaited_once()

    async def test_held_recording_finishes_before_idle_reconnect_probe(self):
        self.connect()
        self.status(1, elapsed_cs=200, distance_dm=50)
        self.assertTrue(self.coordinator.recorder.recording)
        self.session._set_connected(False)
        self.connect(early_idle=True)
        await self.settle()
        self.read.assert_not_awaited()
        # The recorder's grace timer expires without any further status packets.
        self.coordinator.recorder.finish()
        await self.settle()
        self.read.assert_awaited_once()
        self.assertCountEqual(
            [row["distance_m"] for row in self.manager.list_workouts()], [5, 159],
        )

    async def test_manual_and_automatic_probes_wait_for_each_other(self):
        started, release = asyncio.Event(), asyncio.Event()
        response = self.read.return_value

        async def wait_for_release(_coordinator):
            started.set()
            await release.wait()
            return response

        self.read.side_effect = wait_for_release
        self.connect(early_idle=True)
        async with asyncio.timeout(2):
            await started.wait()
            manual = asyncio.create_task(self.coordinator.async_read_latest())
            try:
                await asyncio.sleep(0)
                self.read.assert_awaited_once()
            finally:
                release.set()
                result = await manual
        self.assertEqual(result, response)
        self.assertEqual(self.read.await_count, 2)
        await self.settle()
        self.assertEqual(len(self.manager.list_workouts()), 1)

    async def test_probe_yields_to_rowing_and_disconnect_then_recovers_on_reconnect(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def wait_for_interruption(_coordinator):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.read.side_effect = wait_for_interruption
        self.connect(early_idle=True)
        async with asyncio.timeout(2):
            await started.wait()
            self.status(1)
            await cancelled.wait()
            self.assertFalse(self.manager.list_workouts())
            started.clear()
            cancelled.clear()
            self.status(0)
            await started.wait()
            self.session._set_connected(False)
            await cancelled.wait()
        self.assertFalse(self.manager.list_workouts())

        self.read.side_effect = None
        self.connect(early_idle=True)
        await self.settle()
        self.assertEqual(len(self.manager.list_workouts()), 1)

    async def test_failed_and_unsupported_reads_retry_only_on_new_connection(self):
        self.read.side_effect = [
            HomeAssistantError("PM5 cannot read the log"),
            {"address": 1, "length": 1, "record_hex": "00"},
            self.read.return_value,
        ]
        for tries in (1, 2):
            self.connect(early_idle=True)
            await self.settle()
            self.status(0)
            await self.settle()
            self.assertEqual(self.read.await_count, tries)
            self.assertFalse(self.manager.list_workouts())
            self.session._set_connected(False)
        self.connect(early_idle=True)
        await self.settle()
        self.assertEqual(len(self.manager.list_workouts()), 1)
