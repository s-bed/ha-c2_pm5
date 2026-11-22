# Concept2 PM5 – Home Assistant Integration

This integration connects **Concept2 PM5 monitors** (RowErg) to **Home Assistant** over Bluetooth using the official Concept2 protocol. See the [s-bed fork](https://github.com/s-bed/ha-c2_pm5) for the source and [published releases](https://github.com/s-bed/ha-c2_pm5/releases).

---

## Features

- Automatic Bluetooth discovery of PM5 monitors
- Live workout metrics (elapsed time, distance, pace, stroke rate, power, heart rate, etc.)
- End-of-workout summaries (splits, averages, calories, drag factor)
- A **Concept2 PM5** sidebar page with live status, saved workouts, FIT downloads, and Garmin Connect upload
- PM5 clock is synchronized to Home Assistant with an accuracy of +/- 30 seconds
- Automatic idle detection and safe workout termination
- Works with **ESPHome Bluetooth Proxy**

---

## Requirements

- Concept2 **PM5** monitor
- Bluetooth support via:
  - ESPHome Bluetooth Proxy **(recommended)**, or
  - Local Bluetooth adapter

---

## Enabling Bluetooth on the PM5

Before Home Assistant can detect the PM5:

1. On the PM5, press **Menu**
2. Select **Connect**
3. Leave the PM5 on the **Connect** screen

> The PM5 only advertises for app connections when this screen is active.

---

## Setup

### Install from Home Assistant Marketplace

On Home Assistant 2026.11 or later, open **Settings → Marketplace**, connect GitHub if prompted, and add `https://github.com/s-bed/ha-c2_pm5` as an integration using **Add from link**. Install the published release, then add **Concept2 PM5** under **Settings → Devices & services**. Updates and the release changelog appear under **Settings → Updates**.

To install a development build, open the repository in Marketplace and choose **⋮ → Include pre-releases**. Choose **Install** when the desired `-dev.N` version is displayed, or use **⋮ → Install another version** to select a specific prerelease.

### Automatic Discovery (Recommended)

1. Enable the PM5 **app connection** (see above)
2. Open **Home Assistant**
3. Navigate to:
   - **Settings → Devices & Services**
4. Select **Add** under **Concept2 PM5**

### Manual Setup

If the device is not discovered automatically:

1. Go to **Settings → Devices & Services → Add Integration**
2. Select **Concept2 PM5**
3. Enter the **Bluetooth MAC address** of the PM5

> 💡 You can find the MAC address on the PM5 under *More Options → Utilities → Product ID*, or via Bluetooth scanning tools.

---

## Recording and uploading workouts

1. Open **Concept2 PM5** in Home Assistant's sidebar as an administrator. Wake the PM5, open **Connect**, and wait for the page to show **Connected** before rowing.
2. To include heart rate, pair a compatible Bluetooth Smart or ANT+ sensor through **Connect → Connect Heart Rate** on the PM5. Check that the live page shows a BPM reading. [Concept2 pairing instructions](https://www.concept2.com/support/heart-rate-equipment/garmin-suunto/how-to-use)
3. Row the workout, then press **Menu** on the PM5 to end it. Home Assistant saves it automatically. The page labels captures with missing portions as **Partial recording**.
4. Select the workout and choose **Download FIT**, **Upload to Garmin**, or **Keep locally**. Upload uses the same `garmin_connect.upload_activity` action as King Smith and reuses your existing Garmin Connect account. [Garmin Connect upload action](https://github.com/cyberjunky/home-assistant-garmin_connect#garmin_connectupload_activity)

The FIT activity includes rowing time, distance, pace, stroke rate, power, calories, and heart-rate samples when the PM5 reports them. It stores one lap for the full workout. Heart-rate gaps remain empty. Recordings are saved in Home Assistant and survive restarts.

For newly registered entities, Home Assistant enables sensors for saved workout metrics by default. You can enable additional sensors such as battery level, drag factor, and interval diagnostics from the PM5 device page. Workout recording uses Bluetooth data directly and works with any sensor selection.

If Home Assistant receives only the finish, it saves the PM5's final totals and available averages as **Partial recording · PM5 summary only**.

If Home Assistant missed even the finish, leave the PM5 idle when it next connects. The integration checks the latest workout in PM5 Memory and can recover a **Just Row** summary with its logged time, duration, and distance. Reconnect before finishing another workout, since the check reads only the newest entry. A missed row appears in the workout review list as **Recovered from PM5 Memory · Summary only**. If the row already has a recording, the integration keeps its live samples and Garmin review decision.

### Default Garmin account and phone approval

1. Open **Settings → Devices & services → Concept2 PM5 → Configure** for your monitor.
2. Choose a **Default Garmin Connect account** and a **Phone for workout review**, then save. Use a device registered with the Home Assistant Companion App. You can set a default without enabling phone notifications.
3. When a row is saved, the phone shows the duration and distance. Choose **Upload to Garmin** to send it to the configured account or **Keep locally** to skip the upload and retain the FIT download. The sidebar preselects the default account and lets you choose another for an individual row.

Pending rows are eligible for notification when you enable phone review and after a Home Assistant restart. If an upload result is uncertain, open the workout in Home Assistant and check Garmin before explicitly retrying.

### Inspect the latest workout in PM5 Memory

The integration includes a manual, read-only diagnostic action for administrators. It uses the monitor's existing Bluetooth connection, including a Home Assistant Bluetooth proxy.

1. Wake the PM5 and wait for **Connected** with the monitor idle.
2. Open **Developer tools → Actions**, select **Concept2 PM5: Read latest stored workout** (`c2_pm5.read_latest_stored_workout`), and choose your monitor.
3. Run the action. The response includes the record's start address, byte length, and hexadecimal contents. To check the result against the PM5 display, follow the [record comparison guide](tools/README.md).

The diagnostic action returns the raw bytes for inspection. If you previously installed the standalone development probe, remove its `pm5_memory_probe:` configuration and component directory when you update.

---

## Recommended Usage Practices

### Ending Workouts Properly

For the **most accurate end-of-workout data**:

**Press the `Menu` button on the PM5 to end workouts manually**

This ensures:
- End-of-workout summary messages are fully emitted
- Final splits are logged
- Accurate calories, averages, and stroke data

### Battery Conservation

The PM5 runs on batteries and Bluetooth connections will keep the device awake.

After finishing your session, it's best to manually disable the App Connection on the PM5.
- Press **Menu**
- Select **Connect**
- Select **Disconnect app**

---

## Automatic Idle Handling

The idle disconnect timeout defaults to **5 minutes**. To change it, go to **Settings → Devices & services → Devices**, open your PM5, and set **Idle disconnect timeout** under **Configuration**. Each monitor has its own setting, from 1 to 60 minutes.

If a workout is left running and the machine becomes idle:

- The integration detects prolonged inactivity
- It *attempts* to safely terminate the workout
- A short grace period allows final workout summaries to be received
- The integration disconnects long enough for the PM5 to enter sleep


**Important:** While waiting for the PM5 to sleep, new Bluetooth connections are intentionally blocked.

---

## Known Limitations

- Only **one active Bluetooth connection** to the PM5 is supported at a time

---

## Publishing a release

Set the new version in `custom_components/c2_pm5/manifest.json` and add a matching `## x.y.z` section to [CHANGELOG.md](CHANGELOG.md). When those changes reach `main`, the Release workflow automatically tags the commit with the manifest version and publishes `c2_pm5.zip` with the matching changelog. Marketplace shows those release notes under **Settings → Updates**.

Pushes to `dev` and branches under `codex/` automatically publish prereleases with the next `-dev.N` suffix for the manifest version. The workflow sets the version inside the ZIP to that tag. Add a matching `## x.y.z-dev.N` changelog section for preview-specific notes. Otherwise, the workflow uses the stable version's notes. Pushing a version tag manually also triggers the Release workflow.

---

## Disclaimer

This integration is not affiliated with or endorsed by Concept2. Use at your own risk.

