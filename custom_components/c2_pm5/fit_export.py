"""Encode a recorded PM5 workout as a Garmin FIT activity."""

from datetime import datetime
import math
from statistics import mean
from uuid import UUID
from zoneinfo import ZoneInfo

from garmin_fit_sdk import Encoder, FIT_EPOCH_S, Profile


def _measurement(value, maximum=None):
    """Return a measurement that fits its unsigned field."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value if maximum is None or value <= maximum else None


def _required(value, name):
    number = _measurement(value)
    if number is None:
        raise ValueError(f"Invalid workout {name}.")
    return number


def _timestamp(unix_seconds):
    timestamp = int(_required(unix_seconds, "timestamp")) - FIT_EPOCH_S
    if not 0 <= timestamp < 0xFFFFFFFF:
        raise ValueError("Workout timestamp is outside the FIT date range.")
    return timestamp


def _heart_rate(value):
    # The PM5 uses 255 for an invalid reading. Zero is also unavailable.
    value = _measurement(value, 254)
    return value if value is not None and value >= 1 else None


def _speed_from_pace(pace):
    pace = _measurement(pace)
    return 500 / pace if pace else None


def _record_fields(sample):
    speed = _measurement(sample.get("speed_mps"))
    if speed is None:
        speed = _speed_from_pace(sample.get("pace_s_per_500m"))
    fields = {
        "distance": _measurement(sample.get("distance_m")),
        "enhanced_speed": speed,
    }
    integers = {
        "cadence": _measurement(sample.get("stroke_rate_spm"), 254),
        "power": _measurement(sample.get("power_w"), 65534),
        "heart_rate": _heart_rate(sample.get("heart_rate_bpm")),
        "total_cycles": _measurement(sample.get("stroke_count"), 0xFFFFFFFE),
        "calories": _measurement(sample.get("total_calories"), 65534),
    }
    fields.update(
        {key: round(value) for key, value in integers.items() if value is not None}
    )
    return {key: value for key, value in fields.items() if value is not None}


def _record_statistic(records, field, statistic):
    values = [record[field] for record in records if field in record]
    return statistic(values) if values else None


def _timer_events(workout, start, end):
    events = []
    for event in workout.get("timer_events", []):
        timestamp = _timestamp(event["timestamp"])
        if start <= timestamp <= end:
            kind = event["event"]
            if kind not in {"start", "stop"}:
                raise ValueError(f"Invalid workout timer event: {kind}.")
            events.append((timestamp, kind))
    events.sort(key=lambda event: event[0])
    if not events or events[0][1] != "start":
        events.insert(0, (start, "start"))
    if events[-1][1] != "stop":
        events.append((end, "stop"))
    return events


def encode_workout(workout: dict, time_zone: str) -> bytes:
    """Return an indoor rowing activity with the PM5's observed measurements."""
    start = _timestamp(workout["start_time"])
    end = _timestamp(workout["end_time"])
    if workout["end_time"] < workout["start_time"]:
        raise ValueError("Workout ends before it starts.")
    elapsed = _required(workout["elapsed_seconds"], "elapsed time")
    timer = _required(workout.get("timer_seconds", elapsed), "timer time")
    if timer > elapsed:
        raise ValueError("Workout timer time exceeds elapsed time.")
    distance = _required(workout["distance_m"], "distance")
    serial = UUID(workout["workout_id"]).int % 0xFFFFFFFE + 1

    records_by_time = {}
    for sample in sorted(workout["samples"], key=lambda sample: sample["timestamp"]):
        timestamp = _timestamp(sample["timestamp"])
        fields = _record_fields(sample)
        if start <= timestamp <= end and fields:
            records_by_time.setdefault(timestamp, {}).update(fields)
    if not records_by_time:
        raise ValueError("Workout has no recorded measurements in its time range.")

    encoder = Encoder()

    def write(kind, **fields):
        encoder.write_mesg({"mesg_num": Profile["mesg_num"][kind], **fields})

    write(
        "FILE_ID",
        type="activity",
        manufacturer="development",
        product=0,
        time_created=start,
        serial_number=serial,
    )
    write(
        "DEVICE_INFO",
        device_index="creator",
        manufacturer="development",
        product=0,
        product_name="Concept2 PM5",
        serial_number=serial,
        timestamp=start,
    )

    timeline = [
        (timestamp, 1, "RECORD", {"timestamp": timestamp, **fields})
        for timestamp, fields in records_by_time.items()
    ]
    timer_events = _timer_events(workout, start, end)
    for index, (timestamp, kind) in enumerate(timer_events):
        final = index == len(timer_events) - 1
        timeline.append(
            (
                timestamp,
                2 if final else 0,
                "EVENT",
                {
                    "timestamp": timestamp,
                    "event": "timer",
                    "event_type": "stop_all" if final else kind,
                },
            )
        )
    for _, _, kind, fields in sorted(timeline, key=lambda item: (item[0], item[1])):
        write(kind, **fields)

    records = records_by_time.values()
    summary = {
        "message_index": 0,
        "timestamp": end,
        "start_time": start,
        "total_elapsed_time": elapsed,
        "total_timer_time": timer,
        "total_distance": distance,
        "sport": "fitness_equipment",
        "sub_sport": "indoor_rowing",
    }
    average_speed = _speed_from_pace(workout.get("avg_pace_s_per_500m"))
    if average_speed is None and timer:
        average_speed = distance / timer
    maximum_speed = _record_statistic(records, "enhanced_speed", max)
    if average_speed is not None:
        summary["enhanced_avg_speed"] = average_speed
    if maximum_speed is not None:
        summary["enhanced_max_speed"] = maximum_speed

    # Prefer the workout summary from the monitor, then the available samples.
    stats = (
        (
            "avg_cadence",
            "cadence",
            _measurement(workout.get("avg_stroke_rate_spm"), 254),
            mean,
        ),
        ("max_cadence", "cadence", None, max),
        ("avg_power", "power", _measurement(workout.get("avg_power_w"), 65534), mean),
        ("max_power", "power", None, max),
        (
            "avg_heart_rate",
            "heart_rate",
            _heart_rate(workout.get("avg_heart_rate_bpm")),
            mean,
        ),
        (
            "max_heart_rate",
            "heart_rate",
            _heart_rate(workout.get("max_heart_rate_bpm")),
            max,
        ),
        (
            "min_heart_rate",
            "heart_rate",
            _heart_rate(workout.get("min_heart_rate_bpm")),
            min,
        ),
        (
            "total_cycles",
            "total_cycles",
            _measurement(workout.get("stroke_count"), 0xFFFFFFFE),
            max,
        ),
    )
    for key, field, value, statistic in stats:
        if value is None:
            value = _record_statistic(records, field, statistic)
        if value is not None:
            summary[key] = round(value)
    if "total_cycles" in summary and summary["total_cycles"] < 65535:
        summary["stroke_count"] = summary["total_cycles"]
    calories = _measurement(workout.get("total_calories"), 65534)
    if calories is not None:
        summary["total_calories"] = round(calories)

    write("LAP", **summary)
    write("SESSION", **summary, first_lap_index=0, num_laps=1)
    utc_offset = datetime.fromtimestamp(
        workout["end_time"], ZoneInfo(time_zone)
    ).utcoffset()
    write(
        "ACTIVITY",
        timestamp=end,
        num_sessions=1,
        type="manual",
        total_timer_time=timer,
        local_timestamp=end + int(utc_offset.total_seconds()),
    )
    return bytes(encoder.close())
