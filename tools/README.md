# Compare the latest stored workout with PM5 Memory

The built-in `c2_pm5.read_latest_stored_workout` action reads the most recent internal workout record from a connected, idle PM5. [Home Assistant instructions](../README.md#inspect-the-latest-workout-in-pm5-memory)

1. Finish a known workout and note the local end date and minute, total duration, distance, and number of splits shown in **Memory** on the PM5 or in Concept2 Utility.
2. Run the action in Home Assistant. Save its response as `probe.json`, a JSON object containing the numeric `length` and complete `record_hex` string. The raw record can contain workout and device identifiers.
3. From this repository's root, run the comparator with the values from your reference workout.

```sh
python tools/compare_pm5_record.py probe.json --pm-time 2016-05-23T20:18 --duration-s 1607.3 --distance-m 5500 --splits 5
```

The comparison prints each decoded value beside your reference. Exit code 0 means all four match, 1 means they differ, and 2 indicates invalid input or an unsupported layout. To verify persistence, let the monitor sleep, wake it, reconnect, and repeat the read before completing another row.

The decoder recognizes candidate USB record layouts for Just Row, fixed-distance, and fixed-time RowErg workouts with 1–15 splits. Its output was checked against published USB examples. A physical PM5 comparison is still needed to establish whether a particular internal Bluetooth record uses the same encoding. [Reference fixtures](https://pypi.org/project/c2log/0.1.1/#files)

The action requests the latest descriptor with `GET_INTERNALLOGPARAMS` (`0x99`), reads up to 4 KiB in 64-byte blocks with `GET_INTERNALLOGMEMORY` (`0x6A`), and checks the descriptor again. A changed descriptor, incomplete block, active workout, disconnected monitor, or 30-second timeout ends the read. The `address` response field is the monitor's internal record address. Where available, compare it and the length with the PM5's `0x3F` logged-workout notification. Concept2 describes developer licensing for this command set on page 65 of the specification. [Concept2 protocol](https://cms.concept2.com/sites/default/files/2026-03/Concept2%20PM%20CSAFE%20Communication%20Definition.pdf#page=65)
