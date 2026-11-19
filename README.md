# Concept2 PM5 – Home Assistant Integration

This integration connects **Concept2 PM5 monitors** (RowErg) to **Home Assistant** over Bluetooth using the official Concept2 protocol.

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
4. Select the workout and choose **Download FIT** or select an account and choose **Upload to Garmin**. Upload uses the same `garmin_connect.upload_activity` action as King Smith and reuses your existing Garmin Connect account. [Garmin Connect upload action](https://github.com/cyberjunky/home-assistant-garmin_connect#garmin_connectupload_activity)

The FIT activity includes rowing time, distance, pace, stroke rate, power, calories, and heart-rate samples when the PM5 reports them. It stores one lap for the full workout. Heart-rate gaps remain empty. Recordings are saved in Home Assistant and survive restarts. If the result of an upload is uncertain, check Garmin Connect before retrying from the page.

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

Set the new version in `custom_components/c2_pm5/manifest.json` and add a matching `## x.y.z` section to [CHANGELOG.md](CHANGELOG.md). After merging those changes, create and push a tag that matches the version exactly, such as `2026.11.0`. The Release workflow builds `c2_pm5.zip` and publishes it with that version's changelog as the GitHub release body. Marketplace uses that body in **Settings → Updates**.

For development builds, tag the commit you want to test with the manifest version followed by `-dev.N`, for example `2026.11.0-dev.1`, and push the tag from your working branch. The workflow publishes it as a GitHub prerelease and sets the version inside the ZIP to the tag. Add a matching `## x.y.z-dev.N` changelog section for preview-specific notes. If that section is absent, the workflow uses the target release notes.

---

## Disclaimer

This integration is not affiliated with or endorsed by Concept2. Use at your own risk.

