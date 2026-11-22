"""Compare a candidate PM5 memory record with the same row on the PM5 Memory screen."""

import argparse
from datetime import datetime
import json
import math
from pathlib import Path

_USB_WORKOUT_TYPES = {1: "Just Row", 3: "Fixed distance", 5: "Fixed time"}


def decode_candidate(response: dict) -> dict:
    """Apply the three observed RowErg USB layouts to the probe bytes."""
    if not isinstance(response, dict) or not isinstance(response.get("record_hex"), str):
        raise ValueError("Expected the probe response object with length and record_hex")
    record = bytes.fromhex(response["record_hex"])
    if type(response.get("length")) is not int or response["length"] != len(record):
        raise ValueError("The reported record length differs from the supplied bytes")
    if (
        len(record) < 50
        or record[0] != 0x95
        or record[1] not in _USB_WORKOUT_TYPES
        or record[18:20] != b"\x96\x00"
    ):
        raise ValueError("Unrecognized fixed-workout USB header")

    # Cross-check the four-bit count in the header with the actual frames.
    splits = record[29] & 0x0F
    if splits == 0 or len(record) != 50 + 32 * splits:
        raise ValueError("Split count and fixed-size USB layout do not agree")
    date = int.from_bytes(record[8:10], "big")
    logged = datetime(
        2000 + (date >> 9), date & 0x0F, (date >> 4) & 0x1F, record[10], record[11]
    )
    duration = int.from_bytes(record[20:24], "big") / 10
    distance = int.from_bytes(record[24:28], "big")
    if not duration or not distance:
        raise ValueError("The candidate record has empty workout totals")
    return {
        "workout_type": _USB_WORKOUT_TYPES[record[1]],
        "pm_time": logged.isoformat(timespec="minutes"),
        "duration_s": duration,
        "distance_m": distance,
        "splits": splits,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("probe", type=Path, help="JSON response from c2_pm5.read_latest_stored_workout")
    parser.add_argument(
        "--pm-time", required=True, help="PM local date and time, YYYY-MM-DDTHH:MM"
    )
    parser.add_argument(
        "--duration-s", required=True, type=float, help="Total seconds to the nearest tenth"
    )
    parser.add_argument(
        "--distance-m", required=True, type=int, help="Total meters from PM Memory"
    )
    parser.add_argument(
        "--splits", required=True, type=int, help="Number of splits in PM Memory"
    )
    args = parser.parse_args(argv)
    try:
        logged = datetime.strptime(args.pm_time, "%Y-%m-%dT%H:%M")
        if not math.isfinite(args.duration_s) or min(
            args.duration_s, args.distance_m, args.splits
        ) <= 0:
            raise ValueError("Reference duration, distance and split count must be positive")
        response = json.loads(args.probe.read_text(encoding="utf-8-sig"))
        candidate = decode_candidate(response)
    except (OSError, ValueError) as err:
        parser.error(str(err))

    print(f"Candidate USB layout for {candidate['workout_type']}")
    expected = {
        "pm_time": logged.isoformat(timespec="minutes"),
        "duration_s": args.duration_s,
        "distance_m": args.distance_m,
        "splits": args.splits,
    }
    matched = True
    for field, reference in expected.items():
        observed = candidate[field]
        same = observed == reference
        result = "match" if same else "DIFFERENT"
        print(f"{field}   decoded {observed}   reference {reference}   {result}")
        matched = matched and same
    return 0 if matched else 1


if __name__ == "__main__":
    raise SystemExit(main())
