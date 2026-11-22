"""Compare a candidate PM5 memory record with the same row on the PM5 Memory screen."""

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
from runpy import run_path

# Load the integration's standalone decoder without importing Home Assistant.
_DECODER = Path(__file__).resolve().parents[1] / "custom_components/c2_pm5/memory_record.py"
_decode_stored_workout = run_path(str(_DECODER))["decode_stored_workout"]


def decode_candidate(response: dict) -> dict:
    """Apply the three observed RowErg USB layouts to the probe bytes."""
    row = _decode_stored_workout(response, allow_usb_candidates=True)
    return {
        "workout_type": row.workout_type,
        "pm_time": row.pm_time.isoformat(timespec="minutes"),
        "duration_s": row.duration_s,
        "distance_m": row.distance_m,
        "splits": row.splits,
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
