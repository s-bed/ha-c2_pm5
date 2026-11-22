"""Decode RowErg workout totals from a PM5 memory record."""

from dataclasses import dataclass
from datetime import datetime

_WORKOUT_TYPES = {1: "Just Row", 3: "Fixed distance", 5: "Fixed time"}


@dataclass(frozen=True, slots=True)
class StoredWorkout:
    """Workout header values in monitor-local time and the PM's stored units."""

    workout_type: str
    pm_time: datetime
    duration_s: float
    distance_m: int
    splits: int
    record_address: int | None = None


def decode_stored_workout(
    response: dict, *, allow_usb_candidates: bool = False
) -> StoredWorkout:
    """Decode Just Row, or also try USB-only layouts for offline comparison."""
    if not isinstance(response, dict) or not isinstance(response.get("record_hex"), str):
        raise ValueError("Expected the probe response object with length and record_hex")
    record = bytes.fromhex(response["record_hex"])
    if type(response.get("length")) is not int or response["length"] != len(record):
        raise ValueError("The reported record length differs from the supplied bytes")
    address = response.get("address")
    if address is not None and (
        type(address) is not int or not 0 <= address <= 2**32 - len(record)
    ):
        raise ValueError("Invalid PM5 memory record address")
    if len(record) < 50 or record[0] != 0x95 or record[18:20] != b"\x96\x00":
        raise ValueError("Unrecognized PM5 workout header")
    workout_type = _WORKOUT_TYPES.get(record[1])
    if workout_type is None or (record[1] != 1 and not allow_usb_candidates):
        raise ValueError("Unsupported PM5 memory workout type")

    # The header carries the split count in the low four bits. The totals
    # precede every split and can be read without interpreting the split data.
    splits = record[29] & 0x0F
    if splits == 0 or len(record) != 50 + 32 * splits:
        raise ValueError("Split count and PM5 record length do not agree")
    date = int.from_bytes(record[8:10], "big")
    logged = datetime(
        2000 + (date >> 9), date & 0x0F, (date >> 4) & 0x1F, record[10], record[11]
    )
    duration = int.from_bytes(record[20:24], "big") / 10
    distance = int.from_bytes(record[24:28], "big")
    if not duration or not distance:
        raise ValueError("The PM5 record has empty workout totals")
    return StoredWorkout(workout_type, logged, duration, distance, splits, address)
