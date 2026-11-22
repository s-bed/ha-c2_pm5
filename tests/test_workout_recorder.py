"""Check recording and recovery against the PM5's minute-resolution log time."""

from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from custom_components.c2_pm5.workout_recorder import WorkoutRecorder


_ZONE = ZoneInfo("America/Toronto")
_RECEIVED = datetime(2026, 11, 21, 19, 27, 54, tzinfo=_ZONE).timestamp()
# The log-entry bytes 5b 35 1b 13 represent Nov 21, 2026 at 19:27 local time.
_DATE = 0x355B
_TIME = 0x131B


def summary(elapsed=20.5, distance=100, date=_DATE, time=_TIME):
    return {
        "0039_log_entry_date": date,
        "0039_log_entry_time": time,
        "0039_elapsed_time_s": elapsed,
        "0039_distance_m": distance,
        "0039_workout_type": "Just Row",
        "0039_avg_heart_rate_bpm": 150,
    }


def additional(time=_TIME):
    return {
        "003a_log_entry_date": _DATE,
        "003a_log_entry_time": time,
        "003a_avg_power_w": 165,
        "003a_total_calories": 10,
    }


def status(elapsed, distance, state=1):
    return {
        "0031_elapsed_time_s": elapsed,
        "0031_distance_m": distance,
        "0031_workout_state_raw": state,
    }


class WorkoutRecorderTests(TestCase):
    def setUp(self):
        self.loop = Mock()
        self.saved = []
        self.has_summary = Mock(return_value=False)
        self.recorder = WorkoutRecorder(
            self.loop, "erg", "RowErg", self.saved.append, _ZONE.key,
            has_saved_summary=self.has_summary,
        )

    def expire_summary_grace(self):
        delay, callback = self.loop.call_later.call_args.args
        self.assertEqual(delay, 4)
        callback()

    def test_standalone_summary_has_reported_totals_and_zero_live_samples(self):
        self.recorder.receive(summary(), _RECEIVED)
        self.recorder.receive(additional(), _RECEIVED + 0.4)
        self.recorder.receive(
            {"0032_elapsed_time_s": 20, "0032_heart_rate_bpm": 140}, _RECEIVED + 1
        )
        self.assertFalse(self.recorder.recording)
        self.expire_summary_grace()
        row, = self.saved
        self.assertTrue(row["partial"] and row["summary_only"])
        self.assertEqual((row["pm5_log_date"], row["pm5_log_time"]), (_DATE, _TIME))
        self.assertEqual(
            (row["start_time"], row["end_time"]), (_RECEIVED - 20.5, _RECEIVED)
        )
        self.assertEqual((row["elapsed_seconds"], row["distance_m"]), (20.5, 100))
        self.assertEqual(
            (row["samples"], row["sample_count"], row["heart_rate_sample_count"]),
            ([], 0, 0),
        )
        self.assertEqual((row["avg_power_w"], row["avg_heart_rate_bpm"]), (165, 150))
        self.assertEqual(row["workout_type"], "Just Row")
        self.recorder.receive(summary(), _RECEIVED + 60)
        self.recorder.close()
        self.assertEqual(len(self.saved), 1)

    def test_additional_can_precede_the_primary_by_up_to_four_seconds(self):
        self.recorder.receive(additional(), _RECEIVED - 3)
        self.recorder.receive(summary(), _RECEIVED)
        self.expire_summary_grace()
        self.assertEqual(self.saved[0]["total_calories"], 10)

    def test_supplement_never_creates_a_row_and_old_supplement_is_ignored(self):
        self.recorder.receive(additional(), _RECEIVED - 10)
        self.loop.call_later.assert_not_called()
        self.recorder.receive(summary(), _RECEIVED)
        self.expire_summary_grace()
        self.assertNotIn("avg_power_w", self.saved[0])

    def test_invalid_future_and_stale_summaries_do_not_open_a_row(self):
        for values in (
            summary(date=0), summary(time=0x1340), summary(time=0x131D),
            summary(time=0x1318), summary(elapsed=0), summary(distance=0),
            summary(elapsed=float("nan")), summary(distance=float("inf")),
        ):
            with self.subTest(values=values):
                self.recorder.receive(values, _RECEIVED)
        self.loop.call_later.assert_not_called()
        self.assertFalse(self.saved)

    def test_standalone_summary_accepts_next_minute_within_pm_clock_rounding(self):
        received = datetime(2026, 11, 21, 19, 26, 30, tzinfo=_ZONE).timestamp()
        self.recorder.receive(summary(), received - 0.001)
        self.loop.call_later.assert_not_called()
        self.recorder.receive(summary(), received)
        self.expire_summary_grace()
        row, = self.saved
        self.assertTrue(row["summary_only"])
        self.assertEqual(row["end_time"], received)
        self.assertEqual(row["pm5_log_time"], _TIME)

    def test_rounded_clock_can_show_tomorrow_when_a_workout_finishes(self):
        received = datetime(2026, 11, 21, 23, 59, 48, tzinfo=_ZONE).timestamp()
        self.recorder.receive(summary(date=0x356B, time=0), received)
        self.expire_summary_grace()
        row, = self.saved
        self.assertEqual(row["end_time"], received)
        self.assertEqual((row["pm5_log_date"], row["pm5_log_time"]), (0x356B, 0))

    def test_delayed_summary_uses_monitor_end_minute_in_local_timezone(self):
        self.recorder.receive(summary(), _RECEIVED + 120)
        self.expire_summary_grace()
        self.assertEqual(self.saved[0]["end_time"], _RECEIVED + 5)

    def test_duplicate_local_minutes_at_dst_fall_back_use_the_receipt_fold(self):
        for fold in (0, 1):
            with self.subTest(fold=fold):
                now = datetime(2026, 11, 1, 1, 30, 44, tzinfo=_ZONE, fold=fold).timestamp()
                saved = []
                recorder = WorkoutRecorder(
                    self.loop, "erg", "RowErg", saved.append, _ZONE.key
                )
                recorder.receive(summary(date=0x341B, time=0x011E), now)
                recorder.close()
                self.assertEqual(saved[0]["end_time"], now)

    def test_two_same_minute_rows_survive_with_ambiguous_additional_ignored(self):
        first, second = summary(10, 50), summary(12, 65)
        self.recorder.receive(first, _RECEIVED - 2)
        self.recorder.receive(additional(), _RECEIVED - 1)
        self.recorder.receive(second, _RECEIVED)
        self.recorder.receive(additional(), _RECEIVED + 1)
        self.expire_summary_grace()
        self.recorder.receive(first, _RECEIVED + 60)
        self.recorder.receive(second, _RECEIVED + 60)
        self.recorder.close()
        self.assertEqual([row["distance_m"] for row in self.saved], [50, 65])
        self.assertTrue(all("avg_power_w" not in row for row in self.saved))

    def test_live_record_keeps_track_and_persists_primary_summary_key(self):
        self.recorder.receive(status(1, 2), _RECEIVED - 4)
        self.assertTrue(self.recorder.recording)
        self.recorder.receive(status(5, 15, state=10), _RECEIVED)
        self.recorder.receive(summary(5, 15), _RECEIVED + 1)
        self.expire_summary_grace()
        row, = self.saved
        self.assertFalse(row["partial"])
        self.assertNotIn("summary_only", row)
        self.assertEqual(row["sample_count"], 2)
        self.assertEqual((row["pm5_log_date"], row["pm5_log_time"]), (_DATE, _TIME))

    def test_persisted_replay_does_not_finalize_current_live_workout(self):
        self.recorder.receive(status(2, 3), _RECEIVED)
        self.has_summary.return_value = True
        self.recorder.receive(summary(3, 5), _RECEIVED)
        self.has_summary.assert_called_once_with("erg", _DATE, _TIME, 3, 5)
        self.loop.call_later.assert_not_called()
        self.recorder.receive(status(5, 10), _RECEIVED + 3)
        self.recorder.close()
        self.assertEqual(self.saved[0]["distance_m"], 10)
        self.assertTrue(self.saved[0]["partial"])
        self.assertNotIn("pm5_log_date", self.saved[0])

    def test_delayed_prior_summary_leaves_current_workout_recording(self):
        self.recorder.receive(status(2, 5), _RECEIVED - 1)
        # Similar elapsed counts alone would match the current row. Its distance
        # and the minute on the PM show that this is a separate result.
        self.recorder.receive(summary(2.5, 100, time=_TIME - 1), _RECEIVED)
        self.recorder.receive(additional(time=_TIME - 1), _RECEIVED + 0.1)
        self.expire_summary_grace()
        prior, = self.saved
        self.assertTrue(prior["summary_only"])
        self.assertEqual(prior["avg_power_w"], 165)
        self.assertTrue(self.recorder.recording)
        self.recorder.receive(status(6, 18, state=10), _RECEIVED + 3)
        self.recorder.receive(summary(6, 18), _RECEIVED + 3.1)
        self.expire_summary_grace()
        self.recorder.close()
        self.assertEqual([w["distance_m"] for w in self.saved], [100, 18])
        self.assertNotIn("summary_only", self.saved[1])
        self.assertEqual(self.saved[1]["sample_count"], 2)

    def test_correlated_live_row_uses_totals_with_an_inaccurate_pm_clock(self):
        self.recorder.receive(status(5, 12), _RECEIVED)
        self.recorder.receive(summary(6, 14, time=0x1400), _RECEIVED + 1)
        self.expire_summary_grace()
        self.assertNotIn("summary_only", self.saved[0])
        self.assertEqual(self.saved[0]["distance_m"], 14)

    def test_lost_tail_is_still_attached_to_a_link_interrupted_live_capture(self):
        self.recorder.receive(status(5, 12), _RECEIVED - 20)
        self.recorder.set_connected(False)
        self.recorder.receive(summary(25, 80), _RECEIVED)
        self.expire_summary_grace()
        row, = self.saved
        self.assertNotIn("summary_only", row)
        self.assertTrue(row["partial"])
        self.assertEqual((row["elapsed_seconds"], row["distance_m"]), (25, 80))
