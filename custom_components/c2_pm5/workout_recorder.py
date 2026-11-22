"""Collect PM5 workout notifications into a single activity."""

from __future__ import annotations

from asyncio import AbstractEventLoop, TimerHandle
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging
from math import isfinite
from statistics import mean
from uuid import uuid4
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)
_ACTIVE_STATES = range(1, 10)
_FINISHED_STATES = (10, 11, 12)
_RESET_STATES = (0, 13)
_SAMPLE_GAP_SECONDS = 5
_SUMMARY_GRACE_SECONDS = 4
_RECONNECT_GRACE_SECONDS = 90
_DISTANCE_TOLERANCE_M = 2
# Leave headroom above rowing speeds when status packets were lost before the finish.
_MAX_DISTANCE_PER_MISSING_SECOND = 15

_STATUS_FIELDS = {
    "0032": {
        "0032_speed_m_s": "speed_mps",
        "0032_current_pace_s_per_500m": "pace_s_per_500m",
        "0032_stroke_rate_spm": "stroke_rate_spm",
        "0032_heart_rate_bpm": "heart_rate_bpm",
    },
    "0033": {"0033_total_calories": "total_calories"},
    "0036": {"0036_stroke_power_w": "power_w", "0036_stroke_count": "stroke_count"},
}
_SUMMARY_FIELDS = {
    "0039_avg_stroke_rate_spm": "avg_stroke_rate_spm",
    "0039_avg_heart_rate_bpm": "avg_heart_rate_bpm",
    "0039_min_heart_rate_bpm": "min_heart_rate_bpm",
    "0039_max_heart_rate_bpm": "max_heart_rate_bpm",
    "0039_avg_pace_s_per_500m": "avg_pace_s_per_500m",
}
_ADDITIONAL_SUMMARY_FIELDS = {
    "003a_avg_power_w": "avg_power_w",
    "003a_total_calories": "total_calories",
}
_LIVE_AVERAGES = {
    "0032_avg_power_w": "avg_power_w",
    "0033_average_power_w": "avg_power_w",
    "0033_total_calories": "total_calories",
}


def _metrics(values: dict, fields: dict) -> dict:
    return {
        target: values[source]
        for source, target in fields.items()
        if values.get(source) is not None
    }


def _log_key(values: dict, prefix: str) -> tuple[int, int]:
    return values[f"{prefix}_log_entry_date"], values[f"{prefix}_log_entry_time"]


def summary_identity(
    address: str, date: int, time: int, elapsed: float, distance: float
) -> tuple[str, int, int, int, int]:
    """Identify a PM5 result at the precision of its on-wire totals."""
    return address.upper(), date, time, round(elapsed * 100), round(distance * 10)


def _recent_end_minute(key: tuple[int, int], now: float, zone: ZoneInfo) -> int | None:
    """Match the receipt minute, the two replay minutes, or a rounded-up PM clock."""
    current_minute = int(now) // 60 * 60
    # The PM clock is set to the nearest minute and can run 30 seconds ahead.
    latest_minute = int(now + 30) // 60 * 60
    oldest_minute = current_minute - 120
    for end in range(latest_minute, oldest_minute - 1, -60):
        # Checking real UTC minutes also selects the right fold at a DST change.
        local = datetime.fromtimestamp(end, zone)
        date = ((local.year - 2000) << 9) | (local.day << 4) | local.month
        if key == (date, (local.hour << 8) | local.minute):
            return end
    return None


@dataclass
class _Recording:
    start_time: float
    elapsed: float = 0
    distance: float = 0
    partial: bool = False
    finished: bool = False
    summary_key: tuple[int, int] | None = None
    summary_only: bool = False
    additional_summary: dict | None = None
    metadata: dict = field(default_factory=dict)
    samples: dict[int, dict] = field(default_factory=dict)

    def sample(self, elapsed: float) -> dict:
        # FIT record timestamps have second resolution. Coalesce contemporaneous
        # status and stroke packets without carrying an older HR value forward.
        return self.samples.setdefault(int(elapsed), {})


class WorkoutRecorder:
    """Keep one recording open across both interval rests and short link drops."""

    def __init__(
        self,
        loop: AbstractEventLoop,
        address: str,
        name: str,
        on_complete: Callable[[dict], None],
        time_zone: str = "UTC",
        *,
        has_saved_summary: Callable[[str, int, int, float, float], bool] | None = None,
    ) -> None:
        self._loop = loop
        self._address = address
        self._name = name
        self._on_complete = on_complete
        self._zone = ZoneInfo(time_zone)
        self._has_saved_summary = has_saved_summary
        self._workout: _Recording | None = None
        self._recovered: _Recording | None = None
        self._recovery_timer: TimerHandle | None = None
        self._finish_timer: TimerHandle | None = None
        self._disconnect_timer: TimerHandle | None = None
        self._recent_summaries: deque[tuple[str, int, int, int, int]] = deque(maxlen=16)
        self._pending_additional: tuple[tuple[int, int], float, dict] | None = None
        self._closed = False

    @property
    def recording(self) -> bool:
        return self._workout is not None

    def set_connected(self, connected: bool) -> None:
        if not connected and self._workout and not self._workout.finished:
            self._clear_disconnect_timer()
            self._disconnect_timer = self._loop.call_later(
                _RECONNECT_GRACE_SECONDS, self.finish
            )

    def receive(self, values: dict, now: float) -> None:
        """Process each notification using the PM's own elapsed-time counter."""
        if self._closed:
            return
        if "0031_workout_state_raw" in values:
            self._general_status(values, now)
            return

        if "0039_log_entry_date" in values:
            self._summary(values, now)
        elif "003a_log_entry_date" in values:
            self._additional(values, now)
        elif self._workout:
            self._live_metrics(self._workout, values)

    def _general_status(self, values: dict, now: float) -> None:
        state = values["0031_workout_state_raw"]
        elapsed = values["0031_elapsed_time_s"]
        distance = values["0031_distance_m"]
        workout = self._workout

        if state in _ACTIVE_STATES:
            if workout and (
                self._finish_timer is not None or elapsed + 1 < workout.elapsed
            ):
                if (
                    workout.summary_key is not None
                    and abs(elapsed - workout.elapsed) < 0.01
                    and abs(distance - workout.distance) < 0.1
                ):
                    return
                self.finish()
                workout = None
            if workout is None:
                if elapsed <= 0 or distance <= 0:
                    return
                workout = self._workout = _Recording(
                    now - elapsed, partial=elapsed > _SAMPLE_GAP_SECONDS
                )
                workout.metadata["workout_type"] = values.get("0031_workout_type")
                _LOGGER.info(
                    "PM5 recording started (%s): first active status %.2f s, %.1f m",
                    self._address, elapsed, distance,
                )
        elif workout is None:
            return
        elif state in _RESET_STATES:
            if not workout.finished:
                self._schedule_finish()
            return
        elif state not in _FINISHED_STATES:
            return

        self._clear_disconnect_timer()
        if workout.summary_key is None and elapsed >= workout.elapsed:
            if workout.elapsed and elapsed - workout.elapsed > _SAMPLE_GAP_SECONDS:
                workout.partial = True
            workout.elapsed = elapsed
            workout.distance = distance
            workout.sample(elapsed)["distance_m"] = distance
        if state in _FINISHED_STATES:
            workout.finished = True
            self._schedule_finish()

    def _live_metrics(self, workout: _Recording, values: dict) -> None:
        for prefix, fields in _STATUS_FIELDS.items():
            elapsed = values.get(f"{prefix}_elapsed_time_s")
            if elapsed is None or abs(elapsed - workout.elapsed) > _SAMPLE_GAP_SECONDS:
                continue
            if workout.summary_key is not None and elapsed > workout.elapsed:
                continue
            sample = workout.sample(elapsed)
            sample.update(_metrics(values, fields))
            if prefix == "0032" and values.get("0032_heart_rate_bpm") in (None, 0, 255):
                sample.pop("heart_rate_bpm", None)
            if "0036_stroke_count" in values:
                workout.metadata["stroke_count"] = max(
                    values["0036_stroke_count"], workout.metadata.get("stroke_count", 0)
                )

            if workout.summary_key is None:
                workout.metadata.update(_metrics(values, _LIVE_AVERAGES))

    def _recent_additional(self, key: tuple[int, int], now: float) -> dict | None:
        pending = self._pending_additional
        if (
            pending and pending[0] == key
            and 0 <= now - pending[1] <= _SUMMARY_GRACE_SECONDS
        ):
            return pending[2]
        return None

    def _pending_for_minute(self, key: tuple[int, int]) -> list[_Recording]:
        return [
            row for row in (self._workout, self._recovered)
            if row is not None and row.summary_key == key
        ]

    def _finalized_in_minute(self, key: tuple[int, int]) -> bool:
        return any(identity[1:3] == key for identity in self._recent_summaries)

    def _additional(self, values: dict, now: float) -> None:
        key = _log_key(values, "003a")
        # Additional packets lack totals. Two rows ending in the same minute
        # cannot be distinguished using this packet alone.
        candidates = self._pending_for_minute(key)
        if len(candidates) > 1 or self._finalized_in_minute(key):
            return
        fields = _metrics(values, _ADDITIONAL_SUMMARY_FIELDS)
        previous = (
            candidates[0].additional_summary
            if candidates else self._recent_additional(key, now)
        )
        if previous is not None and previous != fields:
            fields = {}
        if candidates:
            candidates[0].additional_summary = fields
        else:
            self._pending_additional = key, now, fields

    def _matches_live(
        self, workout: _Recording, elapsed: float, distance: float, now: float,
        end_minute: int | None,
    ) -> bool:
        elapsed_gap = elapsed - workout.elapsed
        distance_gap = distance - workout.distance
        if elapsed_gap < -2 or distance_gap < -_DISTANCE_TOLERANCE_M:
            return False
        if elapsed > now - workout.start_time + 2:
            return False
        if (
            elapsed_gap > 2 * _SAMPLE_GAP_SECONDS
            and not (workout.finished or self._disconnect_timer)
        ):
            return False
        if end_minute is not None and end_minute + 60 < workout.start_time + elapsed - 2:
            # The PM's clock can drift. Strong agreement on both live counters
            # can still identify a finish even if the reported minute is early.
            return abs(elapsed_gap) <= 2 and abs(distance_gap) <= _DISTANCE_TOLERANCE_M
        max_distance_gain = max(0, elapsed_gap) * _MAX_DISTANCE_PER_MISSING_SECOND
        return distance_gap <= _DISTANCE_TOLERANCE_M + max_distance_gain

    def _summary(self, values: dict, now: float) -> None:
        key = _log_key(values, "0039")
        elapsed = values["0039_elapsed_time_s"]
        distance = values["0039_distance_m"]
        if not (
            isfinite(elapsed) and elapsed > 0 and isfinite(distance) and distance > 0
        ):
            return
        identity = summary_identity(self._address, *key, elapsed, distance)
        candidates = self._pending_for_minute(key)
        if any(
            identity == summary_identity(self._address, *key, row.elapsed, row.distance)
            for row in candidates
        ):
            return
        if identity in self._recent_summaries:
            return
        if self._has_saved_summary and self._has_saved_summary(
            self._address, *key, elapsed, distance
        ):
            self._recent_summaries.append(identity)
            if self._pending_additional and self._pending_additional[0] == key:
                self._pending_additional = None
            for row in candidates:
                row.additional_summary = None
            return
        workout = self._workout
        end = _recent_end_minute(key, now, self._zone)
        if (
            workout is None or workout.summary_key is not None
            or not self._matches_live(workout, elapsed, distance, now, end)
        ):
            if end is None:
                _LOGGER.debug(
                    "Ignoring stale or invalid standalone PM5 summary (%s)", self._address
                )
                return
            for row in candidates:
                row.additional_summary = None
            self._finish_recovered()
            # The PM reports whole minutes. Cap the estimated end at receipt
            # even when its rounded clock already shows the next minute.
            workout = self._recovered = _Recording(
                min(now, end + 59) - elapsed, partial=True, summary_only=True
            )
            workout.metadata.update(
                pm5_end_minute=end, workout_type=values.get("0039_workout_type")
            )
            _LOGGER.info(
                "PM5 recovered end summary (%s): %.2f s, %.1f m",
                self._address, elapsed, distance,
            )
            self._recovery_timer = self._loop.call_later(
                _SUMMARY_GRACE_SECONDS, self._finish_recovered
            )
        else:
            for row in candidates:
                row.additional_summary = None
            self._clear_disconnect_timer()
            self._schedule_finish()
        if elapsed - workout.elapsed > _SAMPLE_GAP_SECONDS:
            workout.partial = True
        workout.elapsed = elapsed
        workout.distance = distance
        if not workout.summary_only:
            workout.sample(elapsed)["distance_m"] = distance
        workout.finished = True
        workout.summary_key = key
        workout.metadata.update(_metrics(values, _SUMMARY_FIELDS))
        if not candidates and not self._finalized_in_minute(key):
            workout.additional_summary = self._recent_additional(key, now)
        self._pending_additional = None

    def _finish_recovered(self) -> None:
        if self._recovery_timer is not None:
            self._recovery_timer.cancel()
            self._recovery_timer = None
        recovered, self._recovered = self._recovered, None
        if recovered:
            self._emit(recovered)

    def _schedule_finish(self) -> None:
        if self._finish_timer is None:
            self._finish_timer = self._loop.call_later(
                _SUMMARY_GRACE_SECONDS, self.finish
            )

    def _clear_disconnect_timer(self) -> None:
        if self._disconnect_timer is not None:
            self._disconnect_timer.cancel()
            self._disconnect_timer = None

    def finish(self) -> None:
        """Emit a completed activity or the captured portion after a link loss."""
        self._clear_disconnect_timer()
        if self._finish_timer is not None:
            self._finish_timer.cancel()
            self._finish_timer = None
        workout, self._workout = self._workout, None
        if workout and workout.elapsed >= 1 and workout.distance > 0:
            self._emit(workout)

    def _emit(self, workout: _Recording) -> None:
        if workout.summary_key is not None:
            date, time = workout.summary_key
            self._recent_summaries.append(
                summary_identity(
                    self._address, date, time, workout.elapsed, workout.distance
                )
            )
            workout.metadata.update(pm5_log_date=date, pm5_log_time=time)
            if workout.additional_summary:
                workout.metadata.update(workout.additional_summary)
        if workout.summary_only:
            workout.metadata["summary_only"] = True
        samples = [
            {"timestamp": workout.start_time + second, **sample}
            for second, sample in sorted(workout.samples.items())
            if sample and second <= workout.elapsed
        ]
        for field_name, sample_field, aggregate in (
            ("avg_stroke_rate_spm", "stroke_rate_spm", mean),
            ("avg_power_w", "power_w", mean),
            ("avg_heart_rate_bpm", "heart_rate_bpm", mean),
            ("min_heart_rate_bpm", "heart_rate_bpm", min),
            ("max_heart_rate_bpm", "heart_rate_bpm", max),
        ):
            if field_name not in workout.metadata:
                observed = [
                    sample[sample_field] for sample in samples if sample_field in sample
                ]
                if observed:
                    workout.metadata[field_name] = aggregate(observed)
        _LOGGER.info(
            "PM5 recording finalized (%s): %.2f s, %.1f m, %d samples, partial=%s",
            self._address, workout.elapsed, workout.distance, len(samples),
            workout.partial or not workout.finished,
        )
        self._on_complete(
            {
                "workout_id": str(uuid4()),
                "address": self._address,
                "name": self._name,
                "start_time": workout.start_time,
                "end_time": workout.start_time + workout.elapsed,
                "elapsed_seconds": workout.elapsed,
                "distance_m": workout.distance,
                "partial": workout.partial or not workout.finished,
                "sample_count": len(samples),
                "heart_rate_sample_count": sum("heart_rate_bpm" in s for s in samples),
                "samples": samples,
                **workout.metadata,
            }
        )

    def close(self) -> None:
        """Save the final captured portion and ignore any further packets."""
        self._closed = True
        self.finish()
        self._finish_recovered()
