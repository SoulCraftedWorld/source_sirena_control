"use strict";

const $ = (id) => document.getElementById(id);
let catalog = [];
let activeStarted = null;
let journalNext = null;
let journalLines = [];
let nextSessionNumber = "1";
let sessionNumberManual = false;
let currentSessionRunning = false;
let lastAppliedEgoSyncVersion = 0;

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function jsonPost(path, body = {}) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function cell(row, value) {
  const td = document.createElement("td");
  if (value instanceof HTMLElement) td.appendChild(value);
  else td.textContent = String(value);
  row.appendChild(td);
}

function bytes(value) {
  let n = Number(value) || 0;
  for (const unit of ["B", "KiB", "MiB", "GiB"]) {
    if (n < 1024 || unit === "GiB") {
      return `${n.toFixed(unit === "B" ? 0 : 1)} ${unit}`;
    }
    n /= 1024;
  }
}

function duration(value) {
  const n = Math.max(0, Math.floor(Number(value) || 0));
  return [Math.floor(n / 3600), Math.floor((n % 3600) / 60), n % 60]
    .map((part) => String(part).padStart(2, "0")).join(":");
}

function number(id) {
  const value = Number($(id).value);
  return Number.isFinite(value) ? value : 0;
}

function text(value, fallback = "-") {
  return value === undefined || value === null || value === "" ? fallback : String(value);
}

function fixed(value, digits = 2, suffix = "") {
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? `${numberValue.toFixed(digits)}${suffix}` : "-";
}

function statusText(status) {
  return {
    local: "локально",
    queued: "в очереди",
    sending: "отправка",
    retrying: "повтор",
    sent: "отправлено",
    error: "ошибка",
  }[status] || status || "локально";
}

function setToggleButtonState(onButton, offButton, active) {
  onButton.disabled = false;
  offButton.disabled = false;
  onButton.classList.toggle("state-active", Boolean(active));
  onButton.classList.toggle("state-muted", !active);
  offButton.classList.toggle("state-active", !active);
  offButton.classList.toggle("state-muted", Boolean(active));
}

function renderCatalog() {
  const groups = [...new Set(catalog.map((item) => item.group))];
  $("testGroup").innerHTML = groups.map((group) => `<option>${group}</option>`).join("");
  renderTests();
}

function renderTests() {
  const tests = catalog.filter((item) => item.group === $("testGroup").value);
  $("testId").innerHTML = tests
    .map((item) => `<option value="${item.id}">${item.id} - ${item.name}</option>`)
    .join("");
}

function updateSessionNumberLock() {
  const input = $("sessionNumber");
  const manual = $("sessionNumberManual").checked;
  sessionNumberManual = manual;
  input.disabled = !manual || currentSessionRunning;
  if (!manual && nextSessionNumber && !currentSessionRunning) {
    input.value = nextSessionNumber;
  }
}

async function loadCatalog() {
  const data = await api("/api/sessions/catalog");
  catalog = data.tests || [];
  nextSessionNumber = data.next_session_number || "1";
  $("sessionNumbers").innerHTML = (data.session_numbers || [])
    .map((value) => `<option value="${value}"></option>`)
    .join("");
  renderCatalog();
  updateSessionNumberLock();
}

function sessionPayload() {
  const selected = catalog.find((item) => item.id === $("testId").value) || {};
  return {
    session_number: $("sessionNumber").value,
    test_group: $("testGroup").value,
    test_id: $("testId").value,
    test_name: selected.name || "",
    custom_name: $("customName").value,
    repeat_number: number("repeatNumber"),
    siren_type: $("sirenType").value,
    operator: $("operator").value,
    comment: $("comment").value,
  };
}

function applyEgoSyncFields(sync = {}) {
  if (!sync.enabled || currentSessionRunning) return;
  const fields = sync.applied_fields || {};
  const version = Number(sync.applied_version) || 0;
  if (!version || version === lastAppliedEgoSyncVersion) return;
  lastAppliedEgoSyncVersion = version;
  if (fields.test_group && $("testGroup").value !== fields.test_group) {
    $("testGroup").value = fields.test_group;
    renderTests();
  }
  if (fields.test_id) $("testId").value = fields.test_id;
  if (fields.session_number) {
    $("sessionNumberManual").checked = true;
    $("sessionNumber").disabled = false;
    $("sessionNumber").value = fields.session_number;
  }
  if (fields.repeat_number) $("repeatNumber").value = fields.repeat_number;
  if (fields.siren_type !== undefined) $("sirenType").value = fields.siren_type || "";
  $("sessionFormState").textContent = "Синхронизация EGO: задано";
}

function renderEgoSync(sync = {}) {
  const enabled = sync.enabled !== false;
  $("egoSyncEnabled").checked = enabled;
  $("egoStartWithEgo").checked = sync.start_with_ego !== false;
  const status = sync.status || {};
  const ok = Boolean(status.available);
  $("egoSyncStatus").textContent = enabled
    ? (ok ? `EGO доступен, ${status.status || "связь есть"}` : `EGO недоступен: ${status.last_error || "нет связи"}`)
    : "Синхронизация с EGO выключена";
  applyEgoSyncFields(sync);
}

async function postEgoSyncLocalFields() {
  try {
    await jsonPost("/api/ego-sync/local", sessionPayload());
  } catch (_) {
  }
}

async function saveEgoSyncEnabled() {
  try {
    const sync = await jsonPost("/api/ego-sync/config", {
      enabled: $("egoSyncEnabled").checked,
      start_with_ego: $("egoStartWithEgo").checked,
    });
    renderEgoSync(sync);
  } catch (error) {
    $("egoSyncStatus").textContent = error.message;
  }
}

async function startSession() {
  if (!$("sessionNumber").value.trim()) {
    $("sessionFormState").textContent = "Укажите номер сессии";
    $("sessionNumber").focus();
    return;
  }
  try {
    $("sessionFormState").textContent = "Запуск...";
    await jsonPost("/api/sessions/start", sessionPayload());
    await updateState(true);
  } catch (error) {
    $("sessionFormState").textContent = error.message;
  }
}

async function stopSession() {
  try {
    $("sessionFormState").textContent = "Остановка...";
    await jsonPost("/api/sessions/stop");
    await loadCatalog();
    await updateState(true);
    await updateLogs(true);
  } catch (error) {
    $("sessionFormState").textContent = error.message;
  }
}

async function updateState(force = false) {
  if (!force && !$("sessions").classList.contains("active") && !$("interfaces").classList.contains("active")) {
    return;
  }
  try {
    const state = await api("/api/state");
    const session = state.session;
    const active = session.active;
    currentSessionRunning = Boolean(active);
    $("sourceBadge").textContent = `Source ${state.source_id}`;
    $("title").textContent = `Источник сирены - ${state.source_name}`;
    $("pageState").textContent = state.interfaces.nmea.running
      ? "NMEA интерфейс активен"
      : "NMEA не активен";
    $("recordIndicator").classList.toggle("active", Boolean(active));
    $("recordText").textContent = active ? "Запись" : "Остановлено";
    $("start").disabled = Boolean(active);
    $("stop").disabled = !active;
    if (active && !activeStarted) activeStarted = Date.parse(active.started_utc);
    if (!active) activeStarted = null;
    $("timer").textContent = duration(activeStarted ? (Date.now() - activeStarted) / 1000 : 0);
    $("activeFile").textContent = active?.log_name || "-";
    $("activeSize").textContent = bytes(session.size);
    $("activeTrigger").textContent = session.trigger_active ? "ON" : "OFF";
    $("activeAudio").textContent = session.audio.blocks;
    $("activeNmea").textContent = `${state.interfaces.nmea.valid} / err ${state.interfaces.nmea.errors}`;
    updateSessionNumberLock();
    renderInterfacesState(state.interfaces);
    renderEgoSync(state.interfaces.ego_sync || {});
    await postEgoSyncLocalFields();
  } catch (error) {
    $("pageState").textContent = error.message;
  }
}

function action(text, handler, cls = "") {
  const button = document.createElement("button");
  button.textContent = text;
  button.className = cls;
  button.onclick = handler;
  return button;
}

function logNameButton(log) {
  const button = document.createElement("button");
  button.className = "link-button";
  button.textContent = log.name;
  button.onclick = () => analyzeLog(log.name);
  return button;
}

function renderLogs(logs) {
  $("logs").innerHTML = "";
  logs.forEach((log) => {
    const row = document.createElement("tr");
    cell(row, logNameButton(log));
    cell(row, bytes(log.size));
    cell(row, log.modified_utc || "-");
    cell(row, `${text(log.session_number)}/${text(log.test_id)}/R${text(log.repeat_number)}`);
    cell(row, statusText(log.status));
    cell(row, bytes(log.progress_bytes));
    cell(row, log.error || "-");
    const actions = document.createElement("div");
    actions.className = "upload-actions";
    const download = document.createElement("a");
    download.href = `/api/logs/local?name=${encodeURIComponent(log.name)}`;
    download.textContent = "Скачать";
    actions.append(download);
    actions.append(action("В EGO", () => sendLog(log.name)));
    actions.append(action("Удалить", () => deleteLog(log.name), "danger"));
    cell(row, actions);
    $("logs").appendChild(row);
  });
}

async function updateLogs(force = false) {
  if (!force && !$("sessions").classList.contains("active")) return;
  try {
    const state = await api("/api/logs/state");
    renderLogs(state.logs || []);
  } catch (error) {
    $("sessionFormState").textContent = error.message;
  }
}

async function sendLog(name) {
  try {
    await jsonPost("/api/logs/send", { name });
    await updateLogs(true);
  } catch (error) {
    $("sessionFormState").textContent = error.message;
  }
}

async function deleteLog(name) {
  if (!confirm(`Удалить ${name}?`)) return;
  try {
    await jsonPost("/api/logs/delete", { name });
    await updateLogs(true);
  } catch (error) {
    $("sessionFormState").textContent = error.message;
  }
}

function analysisStatusText(status) {
  return { ok: "OK", warn: "Есть замечания", error: "Нужна проверка" }[status] || status;
}

async function analyzeLog(name) {
  try {
    const data = await api(`/api/logs/analyze?name=${encodeURIComponent(name)}`);
    const gps = data.gps || {};
    const last = gps.last || {};
    $("logAnalysisPanel").hidden = false;
    $("analysisTitle").textContent = `Анализ GPS - ${name}`;
    $("analysisStatus").textContent = analysisStatusText(data.status);
    $("analysisGpsFixes").textContent = text(gps.fixes);
    $("analysisValidFixes").textContent = text(gps.valid_fixes);
    $("analysisGpsGap").textContent = fixed(gps.max_fix_gap_s, 2, " с");
    $("analysisRtk").textContent = `${text(gps.rtk_float, "0")} / ${text(gps.rtk_fixed, "0")}`;
    $("analysisSatellites").textContent = text(gps.max_satellites);
    $("analysisAccuracy").textContent = `${fixed(last.h_accuracy_m, 2, " м")} / ${fixed(last.v_accuracy_m, 2, " м")}`;
    $("analysisDop").textContent = `HDOP ${fixed(last.hdop, 2)}, PDOP ${fixed(last.pdop, 2)}, VDOP ${fixed(last.vdop, 2)}`;
    $("analysisGst").textContent =
      `RMS ${fixed(last.gst_rms_error_m, 2, " м")}, lat ${fixed(last.gst_latitude_error_m, 2, " м")}, lon ${fixed(last.gst_longitude_error_m, 2, " м")}, alt ${fixed(last.gst_altitude_error_m, 2, " м")}`;
    $("analysisDetails").textContent = (data.reasons || []).join("\n");
    $("logAnalysisPanel").scrollIntoView({ block: "nearest" });
  } catch (error) {
    $("sessionFormState").textContent = error.message;
  }
}

function renderInterfacesState(state) {
  const c = state.config;
  const n = c.nmea;
  const g = c.siren_trigger;
  const a = c.audio;
  const l = c.localpc || {};
  if (document.activeElement?.tagName !== "INPUT" && document.activeElement?.tagName !== "SELECT") {
    $("sourceId").value = c.source_id || 1;
    $("nmeaType").value = n.type;
    $("usbDevice").value = n.usb_device;
    $("usbBaud").value = n.usb_baud;
    $("uartDevice").value = n.uart_device;
    $("uartBaud").value = n.uart_baud;
    $("tcpPort").value = n.tcp_port || n.udp_port || 10110;
    $("triggerMode").value = g.mode || (g.mock ? "mock" : "auto");
    $("gpioPin").value = g.gpio_bcm;
    $("gpioPull").value = g.pull;
    $("gpioDebounce").value = g.debounce_ms;
    $("audioEnabled").checked = Boolean(a.enabled);
    $("audioBackend").value = a.backend || "auto";
    $("alsaDevice").value = a.alsa_device || "";
    $("windowsDevice").value = a.windows_device || "default";
    $("ffmpegPath").value = a.ffmpeg_path || "ffmpeg";
    $("audioRate").value = a.sample_rate_hz;
    $("audioChannels").value = a.channels;
    $("audioFormat").value = a.sample_format;
    $("audioBytes").value = a.bytes_per_sample;
    $("audioBlock").value = a.block_frames;
    $("localpcEnabled").checked = Boolean(l.enabled);
    $("localpcHost").value = l.host || "";
    $("localpcPort").value = l.port || 10201;
    $("localpcTimeout").value = l.connect_timeout_s || 3;
    $("localpcRetryWindow").value = l.retry_window_s || 20;
    $("localpcRetryInterval").value = l.retry_interval_s || 2;
  }
  setToggleButtonState($("sessionTriggerOn"), $("sessionTriggerOff"), state.trigger.active);
  setToggleButtonState($("triggerOn"), $("triggerOff"), state.trigger.active);
  $("triggerLamp").parentElement.classList.toggle("active", state.trigger.active);
  $("triggerState").textContent = state.trigger.active ? "ON" : "OFF";
  $("triggerBackendState").textContent =
    state.trigger.error || state.trigger.warning || `Backend: ${state.trigger.mode || "-"}`;
  const fix = state.nmea.last_fix || {};
  $("fixPosition").textContent = fix.latitude_deg === undefined
    ? "-"
    : `${fix.latitude_deg.toFixed(8)}, ${fix.longitude_deg.toFixed(8)}`;
  $("fixAltitude").textContent = fixed(fix.altitude_m, 2, " м");
  $("fixSpeed").textContent = fixed((fix.speed_mps || 0) * 3.6, 2, " км/ч");
  $("fixHeading").textContent = fix.heading_rad === undefined
    ? "-"
    : `${(fix.heading_rad * 180 / Math.PI).toFixed(1)}°`;
  $("fixSatellites").textContent = text(fix.satellites);
  $("fixRtk").textContent = `${text(fix.rtk_status)} / ${text(fix.fix_type)}`;
  $("fixAccuracy").textContent = `${fixed(fix.h_accuracy_m, 2, " м")} / ${fixed(fix.v_accuracy_m, 2, " м")}`;
  $("fixDop").textContent = `HDOP ${fixed(fix.hdop, 2)}, PDOP ${fixed(fix.pdop, 2)}, VDOP ${fixed(fix.vdop, 2)}`;
  $("fixDiff").textContent = `${fixed(fix.age_of_diff_s, 1, " с")} / ${text(fix.base_station_id)}`;
  $("fixGst").textContent =
    `RMS ${fixed(fix.gst_rms_error_m, 2, " м")}, lat ${fixed(fix.gst_latitude_error_m, 2, " м")}, lon ${fixed(fix.gst_longitude_error_m, 2, " м")}, alt ${fixed(fix.gst_altitude_error_m, 2, " м")}`;
  $("fixUtc").textContent = fix.utc_ns || fix.utc_time_ns || "-";
  $("nmeaCounters").textContent = `${state.nmea.valid} / ${state.nmea.errors}`;
  const tcpPeer = state.nmea.source_type === "tcp"
    ? (state.nmea.connected ? `, TCP ${state.nmea.peer}` : `, TCP ждёт клиента ${state.nmea.listen || ""}`)
    : "";
  $("nmeaStatus").textContent =
    state.nmea.error || `${state.nmea.running ? "работает" : "остановлено"}${tcpPeer}, ${state.nmea.bytes} байт`;
}

function interfacesPayload() {
  return {
    source_id: Math.max(1, Math.min(3, number("sourceId") || 1)),
    nmea: {
      type: $("nmeaType").value,
      usb_device: $("usbDevice").value,
      usb_baud: number("usbBaud"),
      uart_device: $("uartDevice").value,
      uart_baud: number("uartBaud"),
      tcp_bind: "0.0.0.0",
      tcp_port: number("tcpPort"),
    },
    siren_trigger: {
      mode: $("triggerMode").value,
      gpio_bcm: number("gpioPin"),
      pull: $("gpioPull").value,
      debounce_ms: number("gpioDebounce"),
    },
    audio: {
      enabled: $("audioEnabled").checked,
      backend: $("audioBackend").value,
      alsa_device: $("alsaDevice").value,
      windows_device: $("windowsDevice").value,
      ffmpeg_path: $("ffmpegPath").value,
      sample_rate_hz: number("audioRate"),
      channels: number("audioChannels"),
      sample_format: $("audioFormat").value,
      bytes_per_sample: number("audioBytes"),
      block_frames: number("audioBlock"),
    },
    localpc: {
      enabled: $("localpcEnabled").checked,
      host: $("localpcHost").value,
      port: number("localpcPort"),
      connect_timeout_s: number("localpcTimeout"),
      retry_window_s: number("localpcRetryWindow"),
      retry_interval_s: number("localpcRetryInterval"),
    },
  };
}

async function refreshAudioDevices() {
  try {
    $("audioDevicesState").textContent = "Поиск...";
    const data = await jsonPost("/api/audio/devices", {
      audio: { ffmpeg_path: $("ffmpegPath").value || "ffmpeg" },
    });
    $("windowsDevices").innerHTML = (data.devices || [])
      .map((device) => `<option value="${device.name}"></option>`)
      .join("");
    if ((!$("windowsDevice").value || $("windowsDevice").value === "default") && data.devices?.length) {
      $("windowsDevice").value = data.devices[0].name;
    }
    $("audioDevicesState").textContent = data.ok
      ? `${data.devices.length} устройств`
      : (data.error || "устройства не найдены");
  } catch (error) {
    $("audioDevicesState").textContent = error.message;
  }
}

async function saveInterfaces() {
  try {
    $("interfacesState").textContent = "Применение...";
    await jsonPost("/api/interfaces", interfacesPayload());
    $("interfacesState").textContent = "Применено";
    await updateState(true);
  } catch (error) {
    $("interfacesState").textContent = error.message;
  }
}

async function simulate(active) {
  try {
    await jsonPost("/api/interfaces/simulate-trigger", { active });
    await updateState(true);
  } catch (error) {
    $("interfacesState").textContent = error.message;
    $("sessionFormState").textContent = error.message;
  }
}

async function updateJournal(force = false) {
  if (!force && (!$("journal").classList.contains("active") || !$("journalAuto").checked)) return;
  try {
    const path = journalNext === null ? "/api/journal" : `/api/journal?after=${journalNext - 1}`;
    const data = await api(path);
    journalNext = data.next_sequence;
    journalLines.push(...data.lines);
    journalLines = journalLines.slice(-500);
    $("journalOutput").textContent = journalLines
      .map((line) => `${line.time} ${line.level.padEnd(7)} ${line.message}`)
      .join("\n");
    $("journalOutput").scrollTop = $("journalOutput").scrollHeight;
  } catch (_) {
  }
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.onclick = () => {
    document.querySelectorAll(".tab,.view").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    $(tab.dataset.view).classList.add("active");
    if (tab.dataset.view === "sessions") updateLogs(true);
    if (tab.dataset.view === "interfaces") updateState(true);
    if (tab.dataset.view === "journal") updateJournal(true);
  };
});

$("testGroup").onchange = renderTests;
$("sessionNumberManual").onchange = () => {
  updateSessionNumberLock();
  if (sessionNumberManual) $("sessionNumber").focus();
};
$("start").onclick = startSession;
$("stop").onclick = stopSession;
$("egoSyncEnabled").onchange = saveEgoSyncEnabled;
$("egoStartWithEgo").onchange = saveEgoSyncEnabled;
$("sessionTriggerOn").onclick = () => simulate(true);
$("sessionTriggerOff").onclick = () => simulate(false);
$("logsRefresh").onclick = () => updateLogs(true);
$("analysisClose").onclick = () => { $("logAnalysisPanel").hidden = true; };
$("interfacesRefresh").onclick = () => updateState(true);
$("interfacesSave").onclick = saveInterfaces;
$("audioDevicesRefresh").onclick = refreshAudioDevices;
$("triggerOn").onclick = () => simulate(true);
$("triggerOff").onclick = () => simulate(false);
$("journalRefresh").onclick = () => updateJournal(true);
$("journalCopy").onclick = () => navigator.clipboard.writeText($("journalOutput").textContent);
$("journalClear").onclick = async () => {
  await jsonPost("/api/journal/clear");
  journalLines = [];
  journalNext = null;
  $("journalOutput").textContent = "";
};

(async () => {
  await loadCatalog();
  await updateState(true);
  await updateLogs(true);
})();

setInterval(() => updateState(), 1000);
setInterval(() => updateLogs(), 1500);
setInterval(() => updateJournal(), 1000);
