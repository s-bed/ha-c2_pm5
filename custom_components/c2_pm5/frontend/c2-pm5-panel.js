/* Home Assistant's authenticated Concept2 PM5 panel. */

const DOMAIN = "c2_pm5";
const HISTORY_REFRESH_MS = 10_000;
const WORKOUT_STATUS = {
  ready: "Ready to upload",
  uploading: "Uploading",
  uploaded: "Uploaded",
  kept_local: "Kept locally",
  needs_review: "Check Garmin",
};

const styles = `
  :host {
    display: block;
    color: var(--primary-text-color, #1c2730);
    background: var(--primary-background-color, #f2f5f6);
    height: 100%;
    overflow: auto;
    font-family: var(--ha-font-family-body, system-ui, sans-serif);
    --card: var(--ha-card-background, var(--card-background-color, #fff));
    --border: var(--divider-color, #dce3e6);
    --muted: var(--secondary-text-color, #5b6470);
    --accent: var(--primary-color, #287a94);
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  header {
    position: sticky; top: 0; z-index: 1;
    background: var(--app-header-background-color, var(--card));
    color: var(--app-header-text-color, inherit);
    border-bottom: 1px solid var(--border);
  }
  .masthead { min-height: 60px; padding: 0 clamp(16px, 4vw, 36px); display: flex; align-items: center; gap: 12px; }
  .masthead h1 { font-size: 1.27rem; margin: 0; font-weight: 660; }
  .menu { padding: 6px; border: 0; background: transparent; line-height: 0; }
  main { width: min(100%, 1160px); margin: auto; padding: clamp(16px, 4vw, 36px); }
  h2 { font-size: 1.2rem; margin: 0; }
  h3 { font-size: 1.05rem; margin: 0; }
  p { margin: 0; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 14px; }
  .section-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  #live { padding: clamp(14px, 3vw, 24px); margin-bottom: 24px; }
  .line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
  .device-info { margin-top: 14px; }
  .muted { color: var(--muted); font-size: .9rem; }
  .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(120px, 100%), 1fr)); gap: 10px; margin: 16px 0 0; }
  .metric { min-width: 0; border: 1px solid var(--border); border-radius: 10px; padding: 12px; }
  .metric dt { color: var(--muted); font-size: .78rem; margin: 0 0 6px; }
  .metric dd { margin: 0; font-size: clamp(1rem, 2.45vw, 1.35rem); line-height: 1.2; font-weight: 650; font-variant-numeric: tabular-nums; }
  .pill { display: inline-block; border: 1px solid var(--border); border-radius: 18px; padding: 4px 9px; white-space: nowrap; font-size: .76rem; font-weight: 620; }
  .pill[data-status="connected"], .pill[data-status="uploaded"] { color: var(--success-color, #357a45); }
  .pill[data-status="recording"], .pill[data-status="uploading"] { color: var(--accent); }
  .pill[data-status="needs_review"] { color: var(--error-color, #b74731); }
  .history { margin-top: 12px; display: grid; grid-template-columns: minmax(215px, .8fr) minmax(0, 1.75fr); gap: 14px; align-items: start; }
  #workout-list { padding: 0; margin: 0; list-style: none; max-height: 440px; overflow: auto; }
  #workout-list li + li { border-top: 1px solid var(--border); }
  .workout-row { display: grid; gap: 7px; width: 100%; padding: 14px 16px; text-align: left; border: 0; border-left: 3px solid transparent; border-radius: 0; background: transparent; }
  .workout-row[aria-pressed="true"] { border-left-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, var(--card)); }
  .workout-row .line { justify-content: space-between; }
  #workout { padding: clamp(14px, 3vw, 22px); min-width: 0; }
  #workout .metrics { margin: 14px 0; }
  .actions { display: flex; flex-wrap: wrap; align-items: end; gap: 10px; margin-top: 16px; }
  .stack { display: grid; gap: 8px; }
  .notice { margin-top: 10px; font-size: .9rem; }
  .error { color: var(--error-color, #b74731); }
  button, select {
    font: inherit; color: inherit; border: 1px solid var(--border); border-radius: 8px;
    background: var(--card); padding: 7px 12px; min-height: 39px; max-width: 100%;
  }
  button { cursor: pointer; }
  button:disabled { opacity: .55; cursor: default; }
  button:focus-visible, select:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .primary { background: var(--accent); border-color: var(--accent); color: var(--text-primary-color, #fff); }
  label { font-size: .88rem; }
  @media (max-width: 650px) {
    .history { grid-template-columns: minmax(0, 1fr); }
    #workout-list { max-height: 240px; }
  }
`;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, handler, className) {
  const node = element("button", text, className);
  node.type = "button";
  node.addEventListener("click", handler);
  return node;
}

function number(value, unit, digits = 0) {
  if (!Number.isFinite(value)) return "—";
  return value.toLocaleString(undefined, { maximumFractionDigits: digits }) + " " + unit;
}

function duration(value) {
  if (!Number.isFinite(value) || value < 0) return "—";
  const seconds = Math.round(value);
  const minutes = Math.floor(seconds / 60);
  return minutes + ":" + String(seconds % 60).padStart(2, "0");
}

function metric(label, value) {
  const item = element("div", undefined, "metric");
  item.append(element("dt", label), element("dd", value));
  return item;
}

function displayStatus(label, status) {
  const node = element("span", label, "pill");
  node.dataset.status = status;
  return node;
}

function errorMessage(error) {
  return error?.message || "The request failed. Try again.";
}

class Concept2Panel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>${styles}</style>
      <header><div class="masthead">
        <button type="button" class="menu" id="menu" aria-label="Open Home Assistant menu" hidden>
          <ha-icon icon="mdi:menu"></ha-icon>
        </button>
        <h1>Concept2 PM5</h1>
      </div></header>
      <main>
        <section class="card" id="live" aria-labelledby="live-heading">
          <div class="section-header">
            <h2 id="live-heading">Monitor status</h2>
            <select id="monitor-picker" aria-label="PM5 monitor" hidden></select>
          </div>
          <div class="line device-info" id="device-info"></div>
          <p id="live-notice" class="notice muted" role="status">Loading monitor status…</p>
          <dl id="live-metrics" class="metrics" hidden></dl>
          <p id="heart-hint" class="notice muted" hidden>Pair your heart-rate strap with the PM5 to include heart rate in the recording.</p>
        </section>
        <section aria-labelledby="history-heading">
          <h2 id="history-heading">Workout history</h2>
          <p id="history-notice" class="notice muted" role="status">Loading saved workouts…</p>
          <div class="history">
            <ul id="workout-list" class="card" aria-label="Saved workouts" hidden></ul>
            <article id="workout" class="card" aria-label="Workout details" hidden></article>
          </div>
        </section>
      </main>
    `;
    this._devices = [];
    this._workouts = [];
    this._accounts = [];
    this._accountSelections = new Map();
    this._workoutId = new URLSearchParams(window.location.search).get("workout");
    this._lastHistoryRefresh = 0;
    this._ref("menu").addEventListener("click", () => {
      this.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true }));
    });
    this._ref("monitor-picker").addEventListener("change", (event) => {
      this._deviceAddress = event.target.value;
      this._renderMonitor();
    });
  }

  set hass(value) {
    this._hass = value;
    this._startPolling();
  }

  set narrow(value) {
    this._ref("menu").hidden = !value;
  }

  connectedCallback() {
    this._startPolling();
  }

  disconnectedCallback() {
    window.clearInterval(this._timer);
    this._timer = null;
  }

  _ref(id) {
    return this.shadowRoot.getElementById(id);
  }

  _startPolling() {
    if (!this.isConnected || !this._hass || this._timer) return;
    this._timer = window.setInterval(() => {
      if (document.hidden) return;
      this._refreshStatus();
      if (Date.now() - this._lastHistoryRefresh >= HISTORY_REFRESH_MS) this._refreshHistory();
    }, 2_000);
    this._refreshStatus();
    this._refreshHistory();
  }

  async _refreshStatus() {
    if (this._statusLoading) return;
    this._statusLoading = true;
    try {
      const response = await this._hass.callWS({ type: DOMAIN + "/status" });
      this._devices = response.devices;
      this._liveError = null;
    } catch (error) {
      this._liveError = errorMessage(error);
    } finally {
      this._statusLoading = false;
      if (this.isConnected) this._renderMonitor();
    }
  }

  async _refreshHistory() {
    if (this._historyLoading) return;
    this._historyLoading = true;
    this._lastHistoryRefresh = Date.now();
    try {
      const response = await this._hass.callWS({ type: DOMAIN + "/workouts/list" });
      this._historyError = response.storage_error;
      const signature = JSON.stringify(response);
      if (signature === this._historySignature) return;
      this._historySignature = signature;
      this._workouts = response.workouts;
      this._accounts = response.garmin_accounts;
      this._garminUploadAvailable = response.garmin_upload_available;
      if (!this._workouts.some((workout) => workout.workout_id === this._workoutId)) {
        this._workoutId = this._workouts[0]?.workout_id;
      }
      if (this.isConnected) this._renderWorkouts();
    } catch (error) {
      this._historyError = errorMessage(error);
    } finally {
      this._historyLoading = false;
      if (this.isConnected) {
        this._ref("history-notice").textContent = this._historyError
          || (this._workouts.length ? "" : "Completed rows will appear here.");
        this._ref("history-notice").classList.toggle("error", Boolean(this._historyError));
      }
    }
  }

  _renderMonitor() {
    const picker = this._ref("monitor-picker");
    const device = this._devices.find((candidate) => candidate.address === this._deviceAddress)
      || this._devices[0];
    this._deviceAddress = device?.address;
    picker.hidden = this._devices.length < 2;
    const choices = this._devices.map(({ address, name }) => [address, name]);
    const signature = JSON.stringify(choices);
    if (signature !== this._deviceChoices) {
      this._deviceChoices = signature;
      picker.replaceChildren(...choices.map(([address, name]) => new Option(name, address)));
    }
    if (device) picker.value = device.address;

    const info = this._ref("device-info");
    const notice = this._ref("live-notice");
    const metrics = this._ref("live-metrics");
    const heartHint = this._ref("heart-hint");
    if (!device || this._liveError) {
      info.replaceChildren();
      metrics.hidden = true;
      heartHint.hidden = true;
      notice.hidden = false;
      notice.textContent = this._liveError || "Add a Concept2 PM5 in Home Assistant to see its status.";
      notice.classList.toggle("error", Boolean(this._liveError));
      return;
    }
    const values = device.connected ? device.values : {};
    const connection = device.connected ? "Connected" : device.available ? "Detected" : "Offline";
    const signatureLive = JSON.stringify([device.name, connection, device.recording, values]);
    if (signatureLive === this._liveSignature && !metrics.hidden) return;
    this._liveSignature = signatureLive;
    const state = values["0031_workout_state"];
    info.replaceChildren(
      element("strong", device.name),
      displayStatus(connection, device.connected ? "connected" : "offline"),
    );
    if (device.recording) info.append(displayStatus("Recording", "recording"));
    if (state) info.append(element("span", state, "muted"));
    notice.textContent = "Open Connect on the PM5 to make it available over Bluetooth.";
    notice.classList.remove("error");
    notice.hidden = device.connected;
    const pace = duration(values["0032_current_pace_s_per_500m"]);
    const heartRate = values["0032_heart_rate_bpm"];
    heartHint.hidden = !device.connected || heartRate > 0;
    metrics.hidden = false;
    metrics.replaceChildren(
      metric("Elapsed time", duration(values["0031_elapsed_time_s"])),
      metric("Distance", number(values["0031_distance_m"], "m")),
      metric("Pace / 500 m", pace === "0:00" ? "—" : pace),
      metric("Stroke rate", number(values["0032_stroke_rate_spm"], "spm")),
      metric("Stroke power", number(values["0036_stroke_power_w"], "W")),
      metric("Heart rate", number(heartRate > 0 ? heartRate : null, "bpm")),
    );
  }

  _formatDate(value) {
    if (!Number.isFinite(value)) return "Workout";
    const date = new Date(value * 1000);
    return date.toLocaleString(this._hass.locale?.language, {
      dateStyle: "medium",
      timeStyle: "short",
      timeZone: this._hass.config?.time_zone,
    });
  }

  _renderWorkouts() {
    const list = this._ref("workout-list");
    list.hidden = this._workouts.length === 0;
    list.replaceChildren(...this._workouts.map((workout) => {
      const id = workout.workout_id;
      const row = button("", () => this._selectWorkout(id), "workout-row");
      row.dataset.workoutId = id;
      row.setAttribute("aria-pressed", String(id === this._workoutId));
      const title = element("span", undefined, "line");
      title.append(element("strong", this._formatDate(workout.start_time)));
      const label = WORKOUT_STATUS[workout.status];
      if (label) title.append(displayStatus(label, workout.status));
      const totals = [number(workout.distance_m, "m"), duration(workout.elapsed_seconds)];
      row.append(title, element("span", totals.join(" · "), "muted"));
      const item = element("li");
      item.append(row);
      return item;
    }));
    this._renderWorkout();
  }

  _selectWorkout(id) {
    this._workoutId = id;
    for (const row of this._ref("workout-list").querySelectorAll("button")) {
      row.setAttribute("aria-pressed", String(row.dataset.workoutId === id));
    }
    this._renderWorkout();
  }

  _renderWorkout() {
    const panel = this._ref("workout");
    const workout = this._workouts.find(({ workout_id }) => workout_id === this._workoutId);
    panel.hidden = !workout;
    if (!workout) return;

    const title = element("h3", this._formatDate(workout.start_time));
    const monitor = element("p", workout.name || workout.address, "muted");
    const metrics = element("dl", undefined, "metrics");
    metrics.append(
      metric("Elapsed time", duration(workout.elapsed_seconds)),
      metric("Distance", number(workout.distance_m, "m")),
      metric("Avg. power", number(workout.avg_power_w, "W")),
      metric("Avg. stroke rate", number(workout.avg_stroke_rate_spm, "spm", 1)),
      metric("Calories", number(workout.total_calories, "kcal")),
      metric("Avg. heart rate", number(workout.avg_heart_rate_bpm, "bpm")),
      metric("Max. heart rate", number(workout.max_heart_rate_bpm, "bpm")),
    );
    panel.replaceChildren(title, monitor, metrics);
    if (workout.partial) {
      panel.append(element("p", "Partial recording", "notice muted"));
    }
    const heartRateSamples = workout.heart_rate_sample_count || 0;
    const sampleCount = workout.sample_count || 0;
    const sampleText = heartRateSamples
      ? number(heartRateSamples, "of") + " " + number(sampleCount, "samples have heart rate.")
      : "No heart-rate samples were recorded.";
    panel.append(element("p", sampleText, "muted"));
    if (workout.error) panel.append(element("p", workout.error, "notice error"));
    if (this._actionNotice?.id === workout.workout_id && this._actionNotice.text !== workout.error) {
      const notice = element("p", this._actionNotice.text, "notice");
      notice.classList.toggle("error", Boolean(this._actionNotice.error));
      notice.setAttribute("role", "status");
      panel.append(notice);
    }

    const actions = element("div", undefined, "actions");
    const download = button("Download FIT", () => this._download(workout));
    download.disabled = Boolean(this._busy);
    actions.append(download);
    if (workout.status === "ready" || workout.status === "needs_review") {
      this._addUploadControls(panel, actions, workout);
      const keep = button("Keep locally", () => this._review(workout, "keep_workout", {}));
      keep.disabled = Boolean(this._busy);
      actions.append(keep);
    }
    panel.append(actions);
  }

  _addUploadControls(panel, actions, workout) {
    const retrying = workout.status === "needs_review";
    const field = element("div", undefined, "stack");
    const label = element("label", "Garmin account");
    const select = element("select");
    select.id = "garmin-account";
    label.htmlFor = select.id;
    select.add(new Option("Select Garmin account", ""));
    for (const account of this._accounts) {
      const option = new Option(account.name, account.entity_id);
      option.disabled = !account.available;
      select.add(option);
    }
    const available = this._garminUploadAvailable && this._accounts.some((account) => account.available);
    select.disabled = !available || Boolean(this._busy);
    const preferred = this._accounts.find((account) => retrying
      ? account.entity_ids.includes(workout.garmin_entity_id)
      : account.entry_id === workout.default_garmin_account);
    const selected = this._accountSelections.get(workout.workout_id) ?? preferred?.entity_id;
    select.value = this._accounts.some((account) => account.entity_id === selected) ? selected : "";
    field.append(label, select);
    const upload = button(
      retrying ? "Retry upload" : "Upload to Garmin",
      () => this._upload(workout, select.value),
      "primary",
    );
    const retry = element("input");
    retry.type = "checkbox";
    const retryKey = workout.workout_id + ":" + workout.upload_attempt;
    retry.checked = this._confirmedRetry === retryKey;
    retry.disabled = Boolean(this._busy);
    const updateControls = () => {
      const selectedAvailable = this._accounts.some((account) => account.entity_id === select.value && account.available);
      upload.disabled = !available || Boolean(this._busy) || !selectedAvailable || (retrying && !retry.checked);
    };
    select.addEventListener("change", () => {
      this._accountSelections.set(workout.workout_id, select.value);
      updateControls();
    });
    retry.addEventListener("change", () => {
      this._confirmedRetry = retry.checked ? retryKey : null;
      updateControls();
    });
    updateControls();
    if (!available) {
      const reason = this._garminUploadAvailable
        ? "No Garmin Connect account is available for uploads."
        : "The Garmin Connect upload action is unavailable.";
      panel.append(element("p", reason, "notice muted"));
    } else if (preferred && !preferred.available) {
      panel.append(element("p", "The preselected Garmin account is unavailable. Select another account to upload now.", "notice muted"));
    }
    if (retrying) {
      const confirmation = element("label", undefined, "line notice");
      confirmation.append(retry, element("span", "I checked Garmin Connect and this workout is absent."));
      panel.append(confirmation);
    }
    actions.append(field, upload);
  }

  async _upload(workout, account) {
    if (this._busy || !account) return;
    const id = workout.workout_id;
    const data = { garmin_entity_id: account };
    if (workout.status === "needs_review") {
      if (this._confirmedRetry !== id + ":" + workout.upload_attempt) return;
      data.retry = true;
      data.upload_attempt = workout.upload_attempt;
      this._confirmedRetry = null;
    }
    await this._review(workout, "upload_workout", data);
  }

  async _review(workout, service, data) {
    if (this._busy) return;
    const id = workout.workout_id;
    this._busy = true;
    this._actionNotice = { id, text: service === "upload_workout" ? "Sending workout to Garmin…" : "Saving review decision…" };
    this._renderWorkout();
    try {
      await this._hass.callService(DOMAIN, service, { ...data, workout_id: id });
      this._actionNotice = null;
    } catch (error) {
      this._actionNotice = { id, text: errorMessage(error), error: true };
    } finally {
      this._busy = false;
      this._lastHistoryRefresh = 0;
      await this._refreshHistory();
      if (this.isConnected) this._renderWorkout();
    }
  }

  async _download(workout) {
    if (this._busy) return;
    const id = workout.workout_id;
    this._busy = true;
    this._actionNotice = null;
    this._renderWorkout();
    try {
      const file = await this._hass.callWS({
        type: DOMAIN + "/workouts/download",
        workout_id: id,
      });
      const bytes = Uint8Array.from(window.atob(file.content), (char) => char.charCodeAt(0));
      const url = URL.createObjectURL(new Blob([bytes], { type: "application/octet-stream" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = file.filename;
      this.shadowRoot.append(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 30_000);
    } catch (error) {
      this._actionNotice = { id, text: errorMessage(error), error: true };
    } finally {
      this._busy = false;
      if (this.isConnected) this._renderWorkout();
    }
  }
}

if (!customElements.get("c2-pm5-panel")) {
  customElements.define("c2-pm5-panel", Concept2Panel);
}
