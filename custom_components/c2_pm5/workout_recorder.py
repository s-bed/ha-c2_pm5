"""Collect PM5 workout notifications into a single activity."""

from __future__ import annotations

from asyncio import AbstractEventLoop, TimerHandle
from collections.abc import Callable
from dataclasses import dataclass, field
from statistics import mean
from uuid import uuid4

_ACTIVE_STATES = range(1, 10)
_FINISHED_STATES = (10, 11, 12)
_RESET_STATES = (0, 13)
_SAMPLE_GAP_SECONDS = 5
_SUMMARY_GRACE_SECONDS = 4
_RECONNECT_GRACE_SECONDS = 90

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


@dataclass
class _Recording:
    start_time: float
    elapsed: float = 0
    distance: float = 0
    partial: bool = False
    finished: bool = False
    summary_key: tuple[int, int] | None = None
    additional_summary: tuple[tuple[int, int], dict] | None = None
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
    ) -> None:
        self._loop = loop
        self._address = address
        self._name = name
        self._on_complete = on_complete
        self._workout: _Recording | None = None
        self._finish_timer: TimerHandle | None = None
        self._disconnect_timer: TimerHandle | None = None
        self._last_summary_key: tuple[int, int] | None = None
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

        workout = self._workout
        if workout is None:
            return
        if "0039_log_entry_date" in values:
            self._summary(workout, values)
        elif "003a_log_entry_date" in values:
            key = _log_key(values, "003a")
            if key != self._last_summary_key:
                fields = _metrics(values, _ADDITIONAL_SUMMARY_FIELDS)
                workout.additional_summary = (key, fields)
                if key == workout.summary_key:
                    workout.metadata.update(fields)
        else:
            self._live_metrics(workout, values)

    def _general_status(self, values: dict, now: float) -> None:
        state = values["0031_workout_state_raw"]
        elapsed = values["0031_elapsed_time_s"]
        distance = values["0031_distance_m"]
        workout = self._workout

        if state in _ACTIVE_STATES:
            if workout and (
                self._finish_timer is not None or elapsed + 1 < workout.elapsed
            ):
                self.finish()
                workout = None
            if workout is None:
                if elapsed <= 0 or distance <= 0:
                    return
                workout = self._workout = _Recording(
                    now - elapsed, partial=elapsed > _SAMPLE_GAP_SECONDS
                )
                workout.metadata["workout_type"] = values.get("0031_workout_type")
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

    def _summary(self, workout: _Recording, values: dict) -> None:
        key = _log_key(values, "0039")
        elapsed = values["0039_elapsed_time_s"]
        distance = values["0039_distance_m"]
        if elapsed <= 0 or distance <= 0:
            return
        if key == self._last_summary_key or (
            workout.summary_key is not None and workout.summary_key != key
        ):
            return
        if elapsed + 2 < workout.elapsed or (
            elapsed > workout.elapsed + 2 * _SAMPLE_GAP_SECONDS
            and not (workout.finished or self._disconnect_timer)
        ):
            return
        if elapsed - workout.elapsed > _SAMPLE_GAP_SECONDS:
            workout.partial = True
        workout.elapsed = elapsed
        workout.distance = distance
        workout.sample(elapsed)["distance_m"] = distance
        workout.finished = True
        workout.summary_key = key
        workout.metadata.update(_metrics(values, _SUMMARY_FIELDS))
        if workout.additional_summary and workout.additional_summary[0] == key:
            workout.metadata.update(workout.additional_summary[1])
        self._clear_disconnect_timer()
        self._schedule_finish()

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
        if workout is None or workout.elapsed < 1 or workout.distance <= 0:
            return
        if workout.summary_key is not None:
            self._last_summary_key = workout.summary_key
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
