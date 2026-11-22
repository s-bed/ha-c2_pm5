"""The offline tool only reports matches for an independently identified row."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from custom_components.c2_pm5.memory_record import decode_stored_workout

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "compare_pm5_record.py"
_SPEC = importlib.util.spec_from_file_location("compare_pm5_record", _TOOL)
compare = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(compare)


def example_record():
    # Use an anonymous synthetic row in the 50-byte header / 32-byte split
    # format. Dates, units and header markers match published PM5 USB examples.
    raw = bytearray(50 + 32 * 5)
    raw[:2] = bytes.fromhex("9503")
    raw[8:12] = bytes.fromhex("21751412")
    raw[18:28] = bytes.fromhex("960000003ec90000157c")
    raw[29] = 0x85
    return raw


def response(data):
    return {"length": len(data), "record_hex": data.hex()}


class PM5RecordComparisonTests(TestCase):
    def test_runtime_accepts_just_row_and_returns_the_physical_record_address(self):
        raw = example_record()
        raw[1] = 1
        raw[29] = 0x81
        body = response(raw[:82])
        fields = decode_stored_workout({**body, "address": 0x12000})
        self.assertEqual(fields.record_address, 0x12000)
        self.assertEqual(fields.pm_time.isoformat(timespec="minutes"), "2016-05-23T20:18")
        self.assertEqual((fields.duration_s, fields.distance_m, fields.splits), (1607.3, 5500, 1))
        for invalid_address in (-1, 2**32 - 81, False, "unknown"):
            with self.subTest(address=invalid_address), self.assertRaises(ValueError):
                decode_stored_workout({**body, "address": invalid_address})

    def test_runtime_excludes_usb_only_and_unknown_record_types(self):
        raw = example_record()
        for workout_type in (0, 3, 5, 7):
            raw[1] = workout_type
            with self.subTest(workout_type=workout_type), self.assertRaises(ValueError):
                decode_stored_workout(response(raw))

    def test_known_headers_and_full_width_duration(self):
        for workout_type in (1, 3, 5):
            with self.subTest(workout_type=workout_type):
                raw = example_record()
                raw[1] = workout_type
                raw[20:24] = (72032).to_bytes(4, "big")
                fields = compare.decode_candidate(response(raw))
                self.assertEqual(fields["duration_s"], 7203.2)
                self.assertEqual(fields["pm_time"], "2016-05-23T20:18")
                self.assertEqual(fields["distance_m"], 5500)
                self.assertEqual(fields["splits"], 5)

    def test_incompatible_shapes_are_rejected(self):
        for index, value in ((0, 0), (1, 7), (18, 0x98), (29, 0), (29, 4), (10, 24)):
            with self.subTest(index=index, value=value):
                raw = example_record()
                raw[index] = value
                with self.assertRaises(ValueError):
                    compare.decode_candidate(response(raw))
        raw = example_record()
        for invalid in (
            response(raw[:-1]),
            {"length": 10, "record_hex": raw.hex()},
            {"length": 210, "record_hex": "bad hex"},
        ):
            with self.assertRaises(ValueError):
                compare.decode_candidate(invalid)

    def test_cli_reports_mismatch_and_invalid_input_separately(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "probe.json"
            path.write_text(json.dumps(response(example_record())), encoding="utf-8")
            flags = [
                str(path), "--pm-time", "2016-05-23T20:18", "--duration-s", "1607.3",
                "--splits", "5", "--distance-m",
            ]
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(compare.main([*flags, "5500"]), 0)
                self.assertEqual(compare.main([*flags, "5000"]), 1)
            self.assertIn("distance_m   decoded 5500   reference 5000   DIFFERENT", output.getvalue())
            path.write_text("{}", encoding="utf-8")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                compare.main([*flags, "5500"])
            self.assertEqual(error.exception.code, 2)
