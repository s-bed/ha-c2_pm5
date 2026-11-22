"""Exercise PM5 summary recovery through local storage and FIT export."""

import asyncio
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch
from uuid import uuid4

from garmin_fit_sdk import Decoder, Stream
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.c2_pm5 import PM5Coordinator
from custom_components.c2_pm5.workout_manager import WorkoutManager


class SummaryStorageTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant(self.enterContext(TemporaryDirectory()))
        self.manager = WorkoutManager(self.hass)
        self.address = "AA:BB:CC:DD:EE:FF"
        self.now = datetime(2026, 11, 21, 19, 27, 54, tzinfo=timezone.utc).timestamp()
        self.signal = self.enterContext(
            patch("custom_components.c2_pm5.workout_manager.async_dispatcher_send")
        )

    def summary(self, duration=120, distance=500):
        return {
            "workout_id": str(uuid4()),
            "name": "RowErg",
            "address": self.address,
            "start_time": self.now - duration,
            "end_time": self.now,
            "elapsed_seconds": duration,
            "distance_m": distance,
            "pm5_log_date": 0x355B,
            "pm5_log_time": 0x131B,
            "avg_heart_rate_bpm": 120,
            "partial": True,
            "summary_only": True,
            "samples": [],
            "sample_count": 0,
            "heart_rate_sample_count": 0,
        }

    def capture(self, duration=100, distance=400):
        start = self.now - 120
        return {
            "workout_id": str(uuid4()),
            "address": self.address,
            "name": "RowErg",
            "start_time": start,
            "end_time": start + duration,
            "elapsed_seconds": duration,
            "distance_m": distance,
            "partial": True,
            "sample_count": 1,
            "heart_rate_sample_count": 1,
            "samples": [{"timestamp": start + duration, "distance_m": distance, "heart_rate_bpm": 112}],
        }

    async def test_direct_and_multiplexed_packets_through_coordinator(self):
        coordinator = PM5Coordinator(
            self.hass, Mock(spec=ConfigEntry, title="PM5"), self.address, "RowErg", self.manager
        )
        session = coordinator.session
        # 0x3A can precede 0x39 and use a different notification stream.
        extra = bytearray.fromhex("5b351b13") + bytearray(14)
        extra[9:11] = (180).to_bytes(2, "little")
        primary = bytearray.fromhex("5b351b13") + bytearray(16)
        primary[4:7] = (12000).to_bytes(3, "little")
        primary[7:10] = (5000).to_bytes(3, "little")
        with patch("custom_components.c2_pm5.time", return_value=self.now):
            session._handle_0080(session._uuid80, bytearray([0x3A]) + extra)
            session._handle_0039(session._uuid39, primary)
        coordinator.recorder.close()
        await self.hass.async_block_till_done()
        [row] = self.manager.list_workouts()
        self.assertEqual((row["elapsed_seconds"], row["distance_m"]), (120, 500))
        self.assertTrue(row["summary_only"])
        self.assertEqual(row["avg_power_w"], 180)
        self.assertEqual(row["sample_count"], 0)
        self.signal.assert_called_once()

    async def test_summary_dedup_before_write_and_after_restart_and_fit(self):
        row = self.summary()
        self.manager.record(row)
        self.manager.record(self.summary())
        await self.hass.async_block_till_done()
        self.signal.assert_called_once()
        self.manager = WorkoutManager(self.hass)
        await self.manager.async_initialize()
        self.assertTrue(self.manager.has_saved_summary(self.address, 0x355B, 0x131B, 120, 500))
        self.manager.record(self.summary())
        self.manager.record(self.summary(22, 63))  # another finish in the same minute
        await self.hass.async_block_till_done()
        self.assertEqual(len(self.manager.list_workouts()), 2)
        self.assertEqual(self.signal.call_count, 2)
        fit = await self.manager.async_export(row["workout_id"])
        messages, errors = Decoder(Stream.from_byte_array(bytearray(fit))).read()
        self.assertFalse(errors)
        [endpoint] = messages["record_mesgs"]
        self.assertEqual(endpoint["distance"], 500)
        for field in ("heart_rate", "power", "cadence"):
            self.assertNotIn(field, endpoint)
        self.assertEqual(messages["session_mesgs"][0]["avg_heart_rate"], 120)
        self.assertEqual(messages["lap_mesgs"][0]["total_elapsed_time"], 120)

    async def test_reconcile_saved_partial_preserves_samples_and_review(self):
        capture = self.capture()
        workout_id = capture["workout_id"]
        self.manager.record(capture)
        await self.hass.async_block_till_done()
        await self.manager.async_keep_workout(workout_id)
        self.signal.reset_mock()
        self.manager.record(self.summary())
        await self.hass.async_block_till_done()
        self.manager = WorkoutManager(self.hass)
        await self.manager.async_initialize()
        [row] = self.manager.list_workouts()
        self.assertEqual(row["workout_id"], workout_id)
        self.assertEqual(row["elapsed_seconds"], 120)
        self.assertEqual(row["distance_m"], 500)
        self.assertEqual(row["status"], "kept_local")
        self.assertTrue(row["partial"])
        self.assertFalse(row["summary_only"])
        self.assertEqual((await self.manager.async_get_workout(workout_id))["samples"], capture["samples"])
        self.signal.assert_not_called()

    async def test_rounded_pm_minute_keeps_an_earlier_identical_capture_separate(self):
        capture = self.capture(duration=10, distance=40)  # Ends at 19:26:04.
        self.manager.record(capture)
        await self.hass.async_block_till_done()
        coordinator = PM5Coordinator(
            self.hass, Mock(spec=ConfigEntry, title="PM5"), self.address, "RowErg", self.manager
        )
        # A second row finishes at 19:26:50 with the faster PM clock at 19:27.
        received = self.now - 64
        coordinator.recorder.receive(
            {
                "0039_log_entry_date": 0x355B,
                "0039_log_entry_time": 0x131B,
                "0039_elapsed_time_s": 10,
                "0039_distance_m": 40,
            },
            received,
        )
        coordinator.recorder.close()
        await self.hass.async_block_till_done()
        recovered, earlier = self.manager.list_workouts()
        self.assertEqual(earlier["workout_id"], capture["workout_id"])
        self.assertTrue(recovered["summary_only"])
        self.assertEqual(recovered["end_time"], received)
        self.assertEqual(recovered["pm5_end_minute"], received + 10)

    async def test_summary_arrives_during_first_write(self):
        started = asyncio.Event()
        release = asyncio.Event()
        real_executor = self.hass.async_add_executor_job

        async def hold_first_write(target, *args):
            if target == self.manager._write_recording and not started.is_set():
                started.set()
                await release.wait()
            return await real_executor(target, *args)

        capture = self.capture()
        with patch.object(self.hass, "async_add_executor_job", hold_first_write):
            self.manager.record(capture)
            await asyncio.wait_for(started.wait(), 5)
            self.manager.record(self.summary())
            release.set()
            await self.hass.async_block_till_done()
        [row] = self.manager.list_workouts()
        self.assertEqual(row["workout_id"], capture["workout_id"])
        self.assertEqual(row["distance_m"], 500)
        self.signal.assert_called_once()

    async def test_live_track_finishes_after_its_summary_was_saved(self):
        summary = self.summary()
        self.manager.record(summary)
        await self.hass.async_block_till_done()
        capture = self.capture()
        self.manager.record(capture)
        await self.hass.async_block_till_done()
        [row] = self.manager.list_workouts()
        self.assertEqual(row["workout_id"], summary["workout_id"])
        self.assertEqual(row["sample_count"], 1)
        self.assertFalse(row["summary_only"])
        self.assertEqual(row["distance_m"], 500)
        self.signal.assert_called_once()

    async def test_failed_write_can_retry_without_losing_summary_or_duplicating(self):
        with patch.object(self.manager, "_write_recording", side_effect=OSError("disk full")):
            with self.assertLogs("custom_components.c2_pm5.workout_manager", level="ERROR"):
                self.manager.record(self.summary())
                await self.hass.async_block_till_done()
        self.assertFalse(self.manager.list_workouts())
        self.manager.record(self.summary())
        await self.manager.async_flush()
        self.assertEqual(len(self.manager.list_workouts()), 1)
        self.signal.assert_called_once()
