# Changelog

## 2026.11.1

- Connect immediately when a configured PM5 reappears. Debug logs show the connection attempts, first workout status, recording start, and save.
- Log the reason for Bluetooth teardown and whether the adapter confirms disconnection.
- Enable sensors for saved workout metrics by default. Additional PM5 sensors are available from the device's entity list.
- Recover a partial row from a recent PM5 finish summary when Home Assistant missed the live strokes. Repeated summaries are recognized across restarts.
- Check PM5 Memory when the monitor connects and becomes idle. Recover the latest supported Just Row summary even if Home Assistant missed the entire workout, and keep existing samples and review decisions when a matching row is already saved.
- Inspect the latest stored workout on an idle monitor through the built-in administrator action `c2_pm5.read_latest_stored_workout`. The action returns the raw record for comparison with PM5 Memory.

## 2026.11.0

Restart Home Assistant after updating. Open **Concept2 PM5** in the sidebar as an administrator. To include heart rate, pair a compatible Bluetooth or ANT+ sensor with the PM5 and confirm a BPM reading before rowing.

- View live PM5 connection status, distance, pace, stroke rate, power, and heart rate.
- Save completed rows across Home Assistant restarts. Captures with missing portions are labeled **Partial recording**.
- Download each row as an indoor rowing FIT activity with its recorded metrics and heart rate. The FIT activity uses one lap for the full row.
- Upload a saved row to a selected account through the Garmin Connect integration. If an upload outcome is uncertain, check Garmin Connect before retrying.
- Set a default Garmin account for each monitor and choose a Companion App device to approve uploads or keep rows locally. Pending rows can be reviewed when notifications are enabled.
- Use the Home Assistant 2026.11 Marketplace to install the release and read these notes under **Settings → Updates**.
