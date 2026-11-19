/* SDR / spectrum: HackRF, RTL-SDR, network rtl_tcp and WebSDR. No synthetic spectrum is drawn. */
import { api } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, stat, btn, badge, pageHead, alertStrip, emptyState, loading, fmt, toast,
  field, input, select, checkbox, modal, confirmDialog,
} from "../components.js";
import { kv, progressBar, referenceSection, statusTone } from "./shared.js?v=opsec-1";

const DEMODS = [
  { value: "nfm", label: "NFM — narrowband voice" },
  { value: "am", label: "AM — aviation / broadcast" },
  { value: "wfm", label: "WFM — broadcast FM" },
  { value: "usb", label: "USB — upper sideband" },
  { value: "lsb", label: "LSB — lower sideband" },
  { value: "apt", label: "APT — NOAA weather satellite (experimental)" },
  { value: "iq", label: "IQ only — retain raw samples" },
];

const PRESET_LS_FAV = "darksec.sdr.favorites";
const PRESET_LS_CUSTOM = "darksec.sdr.customPresets";

function loadJsonLS(key, fallback) {
  try { const v = JSON.parse(localStorage.getItem(key) || ""); return v ?? fallback; } catch { return fallback; }
}
function saveJsonLS(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
}
function uid() {
  return "custom-" + Math.random().toString(36).slice(2, 8) + Date.now().toString(36).slice(-4);
}

function mhz(hz) {
  return hz == null ? "—" : `${fmt.num(Number(hz) / 1e6, 4)} MHz`;
}

function backendName(id) {
  return ({ hackrf: "HackRF", rtl: "RTL-SDR", rtl_tcp: "rtl_tcp", soapy: "SoapySDR",
    websdr: "WebSDR", none: "none", auto: "AUTO" })[id] || String(id || "auto").toUpperCase();
}

function noteFields(note) {
  const out = {};
  for (const part of String(note || "").split(";")) {
    const [k, v] = part.split("=");
    if (k && v) out[k.trim()] = v.trim();
  }
  return out;
}

function platformLabel(p) {
  if (!p) return "UNKNOWN";
  if (p.uconsole) return "UCONSOLE";
  if (p.raspberry_pi) return "RASPBERRY PI";
  return String(p.arch || p.system || "LINUX").toUpperCase();
}

export async function render(root, params = {}) {
  root.replaceChildren(loading("INITIALISING SDR CHAIN"));
  let devices, jobs, recordings, adsb, presetData;
  try {
    [devices, jobs, recordings, adsb, presetData] = await Promise.all([
      api.get("/api/sdr/devices"),
      api.get("/api/sdr/jobs?limit=15"),
      api.get("/api/sdr/recordings"),
      api.get("/api/sdr/adsb").catch(() => ({ available: false, aircraft: [] })),
      api.get("/api/sdr/presets").catch(() => ({ presets: [], note: "Presets unavailable" })),
    ]);
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }

  const txEnabled = !!store.state.status?.tx_enabled;
  const hackrf = devices.hackrf || {};
  const rtl = devices.rtl || {};
  const soapy = devices.soapy || {};
  const platform = devices.platform || {};
  const network = devices.rtl_tcp || {};
  const tools = devices.tools || {};
  const activeBackend = devices.active_backend || "none";
  const recs = recordings.items || [];
  const recentJobs = jobs.items || [];
  const paramFreq = Number(params.freq || 0);
  const paramMode = String(params.mode || "").toLowerCase();

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // RADIO SPECTRUM",
    title: "SDR / Spectrum",
    desc: "HackRF, RTL-SDR, network rtl_tcp and public WebSDR/KiwiSDR receive. Live audio, wideband sweep, IQ capture, demodulation, recordings and gated transmit. Every local value comes from the radio or the filesystem.",
    actions: [
      btn("CHECK HARDWARE", { icon: "refresh", onClick: () => rescan() }),
      btn("FIELD MANUAL", { icon: "book", onClick: () => { location.hash = "#/manual?doc=sdr"; } }),
      btn("SETTINGS", { icon: "settings", tone: "ghost", onClick: () => { location.hash = "#/settings"; } }),
    ],
  }));

  root.appendChild(alertStrip(
    "Receive first. Transmitting without a licence, on the wrong frequency, or near sensitive systems can be illegal and dangerous. "
    + "HackRF is not a calibrated instrument. Use the spectrum display to find signals, then verify with a dedicated receiver.",
    "",
  ));

  const stats = h("div", { class: "grid grid-4 mt-14" });
  const activeLabel = activeBackend === "hackrf" ? (hackrf.board_id || "HACKRF")
    : activeBackend === "rtl" ? (rtl.tuner || "RTL-SDR")
    : activeBackend === "rtl_tcp" ? `${network.host || "network"}:${network.port || 1234}`
    : "NO LOCAL RADIO";
  stats.appendChild(stat("Active RX", activeBackend === "none" ? "WEB ONLY" : backendName(activeBackend).toUpperCase(),
    activeBackend === "none" ? "WebSDR works without hardware" : activeLabel,
    activeBackend === "none" ? "warn" : "acc"));
  stats.appendChild(stat("Platform", platformLabel(platform),
    `${platform.model || "unknown board"} · ${platform.thermal_c != null ? platform.thermal_c + " °C" : "thermal n/a"}`,
    (platform.throttled_flags || []).length ? "warn" : ""));
  stats.appendChild(stat("Tools", String(Object.values(tools).filter(Boolean).length).padStart(2, "0"),
    "rtl_sdr / rtl_power / hackrf / SoapySDR"));
  stats.appendChild(stat("Transmit", txEnabled ? "ARMED" : "HARD LOCK",
    txEnabled ? "legal attestation recorded" : "settings gate closed", txEnabled ? "warn" : ""));
  root.appendChild(stats);

  // ---------------------------------------------------------- presets
  const presetCtl = { tune: null, sweep: null };
  const presetState = {
    q: "",
    region: "ALL",
    favorites: new Set(loadJsonLS(PRESET_LS_FAV, [])),
    custom: loadJsonLS(PRESET_LS_CUSTOM, []),
  };
  function allPresets() {
    const server = Array.isArray(presetData?.presets) ? presetData.presets : [];
    const custom = (Array.isArray(presetState.custom) ? presetState.custom : [])
      .map((p) => ({ ...p, custom: true }));
    return [...custom, ...server];
  }
  function savePresetState() {
    saveJsonLS(PRESET_LS_FAV, Array.from(presetState.favorites));
    saveJsonLS(PRESET_LS_CUSTOM, presetState.custom);
  }
  function regionMatch(p) {
    if (presetState.region === "ALL") return true;
    const r = String(p.region || "Worldwide").toUpper();
    if (presetState.region === "WORLDWIDE") return r === "WORLDWIDE" || r === "GLOBAL";
    return r === presetState.region;
  }
  function presetTone(p) {
    const pri = String(p.priority || "").toUpperCase();
    const g = String(p.group || "").toUpperCase();
    if (pri === "EMERGENCY" || g === "EMERGENCY") return "red";
    if (pri === "HIGH" || g === "WEATHER") return "amber";
    return "muted";
  }

  const presetSearch = input({ type: "search", placeholder: "Search presets…", autocomplete: "off" });
  presetSearch.classList.add("sdr-preset-search");
  const regionSel = select([
    { value: "ALL", label: "All regions" },
    { value: "Worldwide", label: "Worldwide" },
    { value: "US", label: "US" },
    { value: "EU", label: "EU" },
  ], "ALL");
  const presetList = h("div", { class: "sdr-preset-list" });
  const presetCount = h("span", { class: "tiny muted mono" });

  function openCustomPreset(existing = null) {
    const p = existing || {};
    const nameIn = input({ type: "text", value: p.name || "", placeholder: "e.g. Local Repeater", maxlength: "80" });
    const freqIn = input({ type: "number", value: p.freq_mhz != null ? String(p.freq_mhz) : "", step: "0.0001", min: "0.1", max: "6000" });
    const demodIn = select(DEMODS, p.demod || "nfm");
    const regionIn = select([
      { value: "Worldwide", label: "Worldwide" }, { value: "US", label: "US" }, { value: "EU", label: "EU" }, { value: "Other", label: "Other" },
    ], p.region || "Worldwide");
    const groupIn = input({ type: "text", value: p.group || "CUSTOM", placeholder: "Group", maxlength: "40" });
    const swStart = input({ type: "number", value: p.sweep?.start_mhz != null ? String(p.sweep.start_mhz) : "", step: "0.1", placeholder: "Sweep start MHz" });
    const swEnd = input({ type: "number", value: p.sweep?.end_mhz != null ? String(p.sweep.end_mhz) : "", step: "0.1", placeholder: "Sweep end MHz" });
    const noteIn = input({ type: "text", value: p.note || "", placeholder: "Optional note", maxlength: "200" });
    const body = h("div", { class: "col gap-8" },
      field("NAME", nameIn),
      h("div", { class: "form-grid" }, field("FREQ (MHz)", freqIn), field("DEMOD", demodIn), field("REGION", regionIn), field("GROUP", groupIn)),
      h("div", { class: "form-grid" }, field("SWEEP START (MHz)", swStart, "Optional: Tune + Sweep range"), field("SWEEP END (MHz)", swEnd)),
      field("NOTE", noteIn),
    );
    const m = modal({
      title: existing ? "Edit custom preset" : "Add custom preset",
      sub: "Stored locally on this device",
      body,
      actions: [
        btn("CANCEL", { tone: "ghost", onClick: () => m.close() }),
        ...(existing ? [btn("DELETE", { tone: "danger", onClick: () => {
          presetState.custom = (presetState.custom || []).filter((x) => x.id !== existing.id);
          presetState.favorites.delete(existing.id);
          savePresetState(); paintPresets(); m.close(); toast("Custom preset deleted");
        } })] : []),
        btn(existing ? "SAVE" : "ADD", { tone: "primary", icon: "plus" }),
      ],
    });
    const save = Array.from(m.box.querySelectorAll(".modal-foot .btn")).pop();
    save.addEventListener("click", () => {
      const freq = Number(freqIn.value);
      if (!nameIn.value.trim()) { toast("Name required", "warn"); return; }
      if (!(freq > 0 && freq <= 6000)) { toast("Valid frequency required (0.1–6000 MHz)", "warn"); return; }
      const s1 = Number(swStart.value), s2 = Number(swEnd.value);
      const sweep = (s1 > 0 && s2 > s1) ? { start_mhz: s1, end_mhz: s2 } : null;
      const next = {
        id: existing?.id || uid(),
        name: nameIn.value.trim(),
        freq_mhz: freq,
        demod: demodIn.value,
        region: regionIn.value,
        group: (groupIn.value || "CUSTOM").trim().toUpperCase(),
        priority: "CUSTOM",
        sweep,
        note: noteIn.value.trim(),
      };
      if (existing) presetState.custom = (presetState.custom || []).map((x) => x.id === existing.id ? next : x);
      else presetState.custom = [next, ...(presetState.custom || [])];
      savePresetState(); paintPresets(); m.close(); toast(existing ? "Preset saved" : "Preset added");
    });
  }

  function paintPresets() {
    const q = presetState.q.toLowerCase();
    const favs = presetState.favorites;
    let items = allPresets().filter(regionMatch);
    if (q) items = items.filter((p) => `${p.name} ${p.group} ${p.freq_mhz} ${p.demod} ${p.note || ""} ${p.region || ""}`.toLowerCase().includes(q));
    items.sort((a, b) => {
      const af = favs.has(a.id) ? 0 : 1, bf = favs.has(b.id) ? 0 : 1;
      if (af !== bf) return af - bf;
      const ae = a.priority === "EMERGENCY" ? 0 : 1, be = b.priority === "EMERGENCY" ? 0 : 1;
      if (ae !== be) return ae - be;
      return String(a.group).localeCompare(String(b.group)) || Number(a.freq_mhz) - Number(b.freq_mhz);
    });
    presetCount.textContent = `${items.length} preset${items.length === 1 ? "" : "s"}`;
    presetList.replaceChildren();
    if (!items.length) {
      presetList.appendChild(h("div", { class: "small muted", style: { padding: "14px 4px" }, text: "No presets match. Try another region/search, or add a custom preset." }));
      return;
    }
    let lastGroup = null;
    let lastFav = null;
    for (const p of items) {
      const isFav = favs.has(p.id);
      if (isFav !== lastFav) {
        presetList.appendChild(h("div", { class: "sdr-preset-section", text: isFav ? "PINNED" : "PRESETS" }));
        lastFav = isFav; lastGroup = null;
      }
      const g = String(p.group || "OTHER");
      if (g !== lastGroup && !isFav) {
        presetList.appendChild(h("div", { class: "sdr-preset-group", text: g }));
        lastGroup = g;
      }
      const row = h("div", { class: "sdr-preset-row" + (presetTone(p) === "red" ? " is-emergency" : "") });
      const star = h("button", {
        type: "button",
        class: "sdr-preset-star" + (isFav ? " active" : ""),
        title: isFav ? "Unpin preset" : "Pin preset",
        text: isFav ? "★" : "☆",
        on: { click: (e) => {
          e.stopPropagation();
          if (favs.has(p.id)) favs.delete(p.id); else favs.add(p.id);
          savePresetState(); paintPresets();
        } },
      });
      const main = h("div", { class: "sdr-preset-main" });
      main.appendChild(h("div", { class: "sdr-preset-name", text: p.name }));
      main.appendChild(h("div", { class: "sdr-preset-meta" },
        h("span", { class: "mono acc", text: `${fmt.num(p.freq_mhz, p.freq_mhz < 30 ? 3 : 4)} MHz` }),
        h("span", { class: "sdr-preset-dot", text: "·" }),
        h("span", { text: String(p.demod || "nfm").toUpperCase() }),
        h("span", { class: "sdr-preset-dot", text: "·" }),
        h("span", { text: p.region || "Worldwide" }),
        ...(p.custom ? [h("span", { class: "sdr-preset-dot", text: "·" }), h("span", { class: "amber", text: "CUSTOM" })] : []),
      ));
      if (p.note) main.title = p.note;
      const actions = h("div", { class: "sdr-preset-actions" },
        btn("TUNE", { size: "sm", tone: "ghost", title: "Fill capture controls", onClick: () => presetCtl.tune && presetCtl.tune(p) }),
        btn("SWEEP", { size: "sm", tone: presetTone(p) === "red" ? "amber" : "ghost", title: p.sweep ? "Tune + set sweep range" : "Tune + center sweep", onClick: () => presetCtl.sweep && presetCtl.sweep(p) }),
      );
      if (p.custom) actions.appendChild(btn("EDIT", { size: "sm", tone: "ghost", onClick: () => openCustomPreset(p) }));
      row.append(star, main, actions);
      presetList.appendChild(row);
    }
  }

  presetSearch.addEventListener("input", () => { presetState.q = presetSearch.value.trim(); paintPresets(); });
  regionSel.addEventListener("change", () => { presetState.region = regionSel.value; paintPresets(); });
  paintPresets();

  root.appendChild(panel({
    title: "Quick signal presets",
    sub: "Tune capture or set a sweep range — no auto-transmit",
    icon: "antenna",
    cls: "mt-14 sdr-presets-panel",
    tools: [presetCount, btn("ADD", { size: "sm", icon: "plus", tone: "ghost", title: "Add custom preset", onClick: () => openCustomPreset(null) })],
    body: h("div", { class: "sdr-presets-body" },
      h("div", { class: "sdr-presets-toolbar" },
        presetSearch,
        regionSel,
      ),
      h("div", { class: "sdr-presets-hint tiny muted" },
        "Presets fill controls only. Review frequency, mode, gain and legal constraints before starting. ",
        presetData?.note ? "" : ""),
      presetList,
    ),
  }));

  const topGrid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(topGrid);

  /* ------------------------------------------------------------- hardware */
  const devWrap = h("div", { class: "col" });
  const platformRow = h("div", { class: "device-card" });
  platformRow.appendChild(h("div", { class: "row between" },
    h("div", { class: "dev-name", text: platformLabel(platform) }),
    badge(platform.uconsole ? "HANDHELD" : platform.raspberry_pi ? "SBC" : "DESKTOP",
      platform.uconsole ? "cyan" : platform.raspberry_pi ? "amber" : "")));
  platformRow.appendChild(kv([
    ["Board", platform.model || "unknown"],
    ["Arch", platform.arch || "—"],
    ["Memory", platform.memory_mb ? `${fmt.num(platform.memory_mb / 1024, 1)} GB` : "—"],
    ["CPU temp", platform.thermal_c != null ? `${platform.thermal_c} °C` : "—"],
    ["Power flags", (platform.throttled_flags || []).length ? platform.throttled_flags.join(", ") : "none"],
  ]));
  devWrap.appendChild(platformRow);
  if (platform.uconsole) {
    devWrap.appendChild(h("div", { class: "row wrap gap-6 mt-8" },
      btn("ENABLE AIO V2 SDR (GPIO 7)", { size: "sm", icon: "power", onClick: async () => {
        try {
          const out = await api.post("/api/sdr/platform/uconsole-sdr", {});
          toast(out.note || "AIO V2 SDR rail enabled");
          setTimeout(() => rescan(true), 1200);
        } catch (err) { toast(err.message, "error"); }
      } }),
      h("span", { class: "tiny muted", text: "Requires pinctrl permission (often root)." })));
  }

  const deviceGrid = h("div", { class: "col gap-6 mt-10" });
  const rf = h("div", { class: "device-card" });
  rf.appendChild(h("div", { class: "row between" },
    h("div", { class: "dev-name", text: "RECEIVERS" }),
    badge(`${(devices.backends || []).filter((b) => b.connected && b.rx).length} RX`, "green")));
  rf.appendChild(kv([
    ["HackRF", hackrf.connected ? (hackrf.board_id || "connected") : tools.hackrf_info ? "tools installed / no device" : "not installed"],
    ["RTL-SDR", rtl.connected ? (rtl.tuner || "connected") : rtl.present || tools.rtl_test ? "tools installed / no device" : "not installed"],
    ["RTL gains", (rtl.gains || []).length ? `${rtl.gains.length} steps` : "—"],
    ["SoapySDR", soapy.connected ? `${(soapy.devices || []).length} device(s)` : soapy.present ? "no device" : "not installed"],
    ["Network", network.configured ? `${network.host}:${network.port}` : "not configured"],
  ]));
  deviceGrid.appendChild(rf);
  devWrap.appendChild(deviceGrid);

  if ((devices.guidance || []).length) {
    const guide = h("details", { class: "mt-10", open: !hackrf.connected && !rtl.connected });
    guide.appendChild(h("summary", { class: "tiny muted", text: "SETUP / FIXES" }));
    for (const item of devices.guidance) {
      guide.appendChild(h("div", { class: `tiny ${item.level === "warn" ? "danger-text" : "muted"} mt-6`, text: `• ${item.text}` }));
    }
    devWrap.appendChild(guide);
  }
  if (hackrf.raw) {
    const raw = h("details", { class: "mt-10" });
    raw.appendChild(h("summary", { class: "tiny muted", text: "hackrf_info output" }));
    raw.appendChild(h("pre", { class: "tiny mono wrap-any", text: hackrf.raw }));
    devWrap.appendChild(raw);
  }
  if (rtl.raw) {
    const raw = h("details", { class: "mt-10" });
    raw.appendChild(h("summary", { class: "tiny muted", text: "rtl_test output" }));
    raw.appendChild(h("pre", { class: "tiny mono wrap-any", text: rtl.raw }));
    devWrap.appendChild(raw);
  }
  const toolWrap = h("div", { class: "col gap-4 mt-10" });
  for (const [name, path] of Object.entries(tools)) {
    toolWrap.appendChild(h("div", { class: "row between small" },
      h("span", { class: "mono", text: name }),
      badge(path ? "OK" : "MISSING", path ? "green" : "muted")));
  }
  devWrap.appendChild(h("div", { class: "side-label mt-10", text: "TOOLCHAIN" }));
  devWrap.appendChild(toolWrap);
  const hardwarePanel = panel({
    title: "Radio hardware",
    sub: "Local inventory, board health and fixes",
    icon: "antenna",
    tools: [btn("RESCAN", { size: "sm", icon: "refresh", onClick: () => rescan(true) })],
    body: devWrap,
  });

  /* ------------------------------------------------------------- spectrum */
  const spectrumBody = h("div", { class: "col" });
  const spectrumShell = h("div", { class: "spectrum-shell" });
  const spectrumCanvas = h("canvas");
  spectrumShell.appendChild(spectrumCanvas);
  const waterfallShell = h("div", { class: "waterfall-shell" });
  const waterfallCanvas = h("canvas");
  waterfallShell.appendChild(waterfallCanvas);
  const spectrumReadout = h("div", { class: "row between tiny mono muted mt-6" },
    h("span", { text: "IDLE // no sweep running" }),
    h("span", { text: "—" }));
  const controls = h("div", { class: "sdr-controls mt-10" });
  const startInput = input({ type: "number", value: paramFreq ? Math.max(1, paramFreq - 1).toFixed(3) : "88", step: "0.1", min: "0.1", max: "6000" });
  const endInput = input({ type: "number", value: paramFreq ? Math.min(6000, paramFreq + 1).toFixed(3) : "108", step: "0.1", min: "0.1", max: "6000" });
  const binInput = input({ type: "number", value: "100", step: "10", min: "10", max: "5000" });
  const lnaInput = input({ type: "number", value: String(store.settings?.sdr_lna_gain ?? 32), min: "0", max: "40" });
  const vgaInput = input({ type: "number", value: String(store.settings?.sdr_rx_gain ?? 40), min: "0", max: "62" });
  const ampInput = checkbox("RF amp +14 dB", !!store.settings?.sdr_amp);
  const sweepBackend = select([
    { value: "auto", label: "Auto" },
    { value: "hackrf", label: "HackRF" },
    { value: "rtl", label: "RTL-SDR" },
  ], String(store.settings?.sdr_backend || "auto"));
  const sweepGain = input({ type: "number", value: String(store.settings?.sdr_rtl_gain ?? 40), min: "0", max: "62" });
  const sweepPpm = input({ type: "number", value: String(store.settings?.sdr_ppm ?? 0), min: "-1000", max: "1000" });
  controls.append(
    field("START (MHz)", startInput),
    field("END (MHz)", endInput),
    field("BIN (kHz)", binInput),
    field("BACKEND", sweepBackend),
    field("LNA (0-40)", lnaInput),
    field("VGA (0-62)", vgaInput),
    field("RTL GAIN", sweepGain),
    field("PPM", sweepPpm),
    h("div", { class: "field" }, h("label", { text: "AMP" }), ampInput),
  );
  const sweepBtn = btn("START SWEEP", { tone: "primary", icon: "play" });
  const stopSweepBtn = btn("STOP", { icon: "stop", tone: "danger", disabled: true });
  spectrumBody.append(
    spectrumShell,
    waterfallShell,
    spectrumReadout,
    controls,
    h("div", { class: "row wrap mt-10" }, sweepBtn, stopSweepBtn,
      h("span", { class: "tiny muted", text: "Sweep uses hackrf_sweep or rtl_power. A full 1–6000 MHz sweep can take time and congest USB." })),
  );
  topGrid.appendChild(panel({
    title: "Wideband spectrum",
    sub: "hackrf_sweep · live NDJSON",
    icon: "signal",
    cls: "span-2",
    body: spectrumBody,
  }));
  topGrid.appendChild(hardwarePanel);

  /* ----------------------------------------------------------- live audio */
  const liveFreq = input({ type: "number", value: paramFreq ? String(paramFreq) : "100.0", step: "0.0001", min: "0.1", max: "6000" });
  const liveMode = select([
    { value: "nfm", label: "NFM — voice" },
    { value: "wfm", label: "WFM — broadcast" },
    { value: "am", label: "AM — aviation/broadcast" },
    { value: "usb", label: "USB" },
    { value: "lsb", label: "LSB" },
  ], DEMODS.some((d) => d.value === paramMode) && ["nfm", "wfm", "am", "usb", "lsb"].includes(paramMode) ? paramMode : "nfm");
  const liveBackend = select([
    { value: "auto", label: "Auto" },
    { value: "hackrf", label: "HackRF" },
    { value: "rtl", label: "RTL-SDR" },
    { value: "rtl_tcp", label: "Network rtl_tcp" },
  ], String(store.settings?.sdr_backend || "auto"));
  const liveRate = input({ type: "number", value: String(store.settings?.sdr_live_rate ?? 1.024), step: "0.025", min: "0.25", max: "3.2" });
  const liveGain = input({ type: "number", value: String(store.settings?.sdr_rtl_gain ?? 40), min: "0", max: "62" });
  const liveSquelch = input({ type: "number", value: String(store.settings?.sdr_live_squelch ?? 0), min: "0", max: "100" });
  const liveVolume = input({ type: "range", value: "0.8", min: "0", max: "1", step: "0.01" });
  const livePlay = btn("LISTEN", { tone: "primary", icon: "play" });
  const liveStopBtn = btn("STOP", { icon: "stop", tone: "danger", disabled: true });
  const liveStatus = h("span", { class: "tiny muted", text: "IDLE // no stream" });
  const liveLevelBar = h("i", { style: { width: "0%" } });
  const liveLevel = h("div", { class: "sdr-level", title: "audio level" }, liveLevelBar);
  const liveBody = h("div", { class: "col" },
    h("div", { class: "form-grid" },
      field("FREQUENCY (MHz)", liveFreq),
      field("MODE", liveMode),
      field("BACKEND", liveBackend),
      field("SAMPLE RATE (Msps)", liveRate, "Lower is easier on a Pi/uConsole"),
      field("RTL GAIN", liveGain),
      field("SQUELCH (0=off)", liveSquelch),
    ),
    h("div", { class: "row between mt-8" },
      h("div", { class: "row gap-8" }, livePlay, liveStopBtn, liveStatus),
      h("div", { class: "row gap-8 wrap" },
        btn("SAVE DEFAULTS", { size: "sm", tone: "ghost", icon: "check", onClick: async () => {
          try {
            await store.saveSettings({
              sdr_backend: liveBackend.value,
              sdr_ppm: Number(sweepPpm.value) || 0,
              sdr_rtl_gain: Number(liveGain.value) || 40,
              sdr_live_rate: Number(liveRate.value) || 1.024,
              sdr_live_squelch: Number(liveSquelch.value) || 0,
              sdr_server_port: Number(srvPort.value) || 1234,
              sdr_server_bind: srvBind.value.trim() || "0.0.0.0",
            });
            toast("SDR defaults saved");
          } catch (err) { toast(err.message, "error"); }
        } }),
        h("span", { class: "tiny muted", text: "VOL" }), liveVolume)),
    liveLevel,
    h("div", { class: "tiny muted mt-6" },
      "Live audio is demodulated on the server and streamed as 16-bit PCM. On a uConsole, use USB-A and keep the sample rate at 1.024–2.4 Msps. Public WebSDR receivers do not use this stream — see the WebSDR panel."),
  );
  topGrid.appendChild(panel({
    title: "Live audio",
    sub: "HackRF / RTL-SDR / rtl_tcp · server-side DSP",
    icon: "signal",
    cls: "span-2",
    body: liveBody,
  }));

  let liveCtx = null;
  let liveGainNode = null;
  let liveSession = "";
  let liveAbort = null;
  let liveSources = [];
  let liveNext = 0;
  let liveRunning = false;

  function setLiveRunning(running) {
    liveRunning = running;
    livePlay.disabled = running;
    liveStopBtn.disabled = !running;
    livePlay.replaceChildren(h("span", { text: running ? "LIVE…" : "LISTEN" }));
  }

  function stopLive() {
    if (liveSession) {
      const sid = liveSession;
      liveSession = "";
      api.post(`/api/sdr/live/stop?session=${encodeURIComponent(sid)}`, {}).catch(() => {});
    }
    if (liveAbort) { try { liveAbort.abort(); } catch {} liveAbort = null; }
    for (const src of liveSources) { try { src.stop(); } catch {} }
    liveSources = [];
    if (liveCtx) {
      try { liveCtx.close(); } catch {}
      liveCtx = null;
    }
    liveGainNode = null;
    liveNext = 0;
    liveLevelBar.style.width = "0%";
    if (liveRunning) liveStatus.textContent = "STOPPED // operator";
    setLiveRunning(false);
  }

  async function startLive() {
    if (liveRunning) return;
    const freq = Number(liveFreq.value);
    if (!(freq >= 1 && freq <= 6000)) { toast("Frequency must be 1–6000 MHz", "warn"); return; }
    const AudioCtor = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtor) { toast("This browser has no Web Audio support", "error"); return; }
    let ctx;
    try { ctx = new AudioCtor(); } catch (err) { toast(`Audio unavailable: ${err.message || err}`, "error"); return; }
    liveCtx = ctx;
    liveGainNode = ctx.createGain();
    liveGainNode.gain.value = Number(liveVolume.value) || 0.8;
    liveGainNode.connect(ctx.destination);
    const playThrough = (buffer) => {
      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(liveGainNode);
      return src;
    };
    await ctx.resume();
    const controller = new AbortController();
    liveAbort = controller;
    setLiveRunning(true);
    liveStatus.textContent = "CONNECTING // waiting for audio";
    const qs = new URLSearchParams({
      freq_mhz: String(freq), mode: liveMode.value, backend: liveBackend.value,
      rate: String(Number(liveRate.value) || 1.024),
      gain: String(Number(liveGain.value) || 40),
      squelch: String(Number(liveSquelch.value) || 0),
      host: String(netHost?.value || "").trim(),
      port: String(Number(netPort?.value) || 1234),
    });
    try {
      const res = await fetch(`/api/sdr/live?${qs}`, { credentials: "same-origin", signal: controller.signal });
      if (!res.ok) {
        let detail = `Live audio failed (${res.status})`;
        try { const data = await res.json(); detail = data.detail || data.error || detail; } catch {}
        throw new Error(detail);
      }
      liveSession = res.headers.get("x-sdr-session") || "";
      const rate = Number(res.headers.get("x-audio-rate")) || 48000;
      const backendUsed = res.headers.get("x-sdr-backend") || liveBackend.value;
      liveStatus.textContent = `LIVE // ${String(backendUsed).toUpperCase()} · ${Math.round(rate / 1000)} kHz`;
      if (!res.body) throw new Error("Streaming response is unavailable in this browser");
      const reader = res.body.getReader();
      let carry = new Uint8Array(0);
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        if (!value) continue;
        const merged = new Uint8Array(carry.length + value.length);
        merged.set(carry); merged.set(value, carry.length);
        carry = merged;
        while (carry.length >= 4096) {
          const chunk = carry.slice(0, 4096);
          carry = carry.slice(4096);
          const int16 = new Int16Array(chunk.buffer, 0, chunk.length / 2);
          const buffer = ctx.createBuffer(1, int16.length, rate);
          const channel = buffer.getChannelData(0);
          let sum = 0;
          for (let i = 0; i < int16.length; i++) {
            const v = int16[i] / 32768;
            channel[i] = v; sum += v * v;
          }
          liveLevelBar.style.width = `${Math.min(100, Math.sqrt(sum / Math.max(1, int16.length)) * 420)}%`;
          const src = playThrough(buffer);
          const now = ctx.currentTime;
          if (liveNext < now + 0.05) liveNext = now + 0.05;
          if (liveNext > now + 0.7) liveNext = now + 0.1;
          src.start(liveNext);
          liveNext += buffer.duration;
          liveSources.push(src);
          src.onended = () => { liveSources = liveSources.filter((item) => item !== src); };
        }
      }
      if (liveRunning) liveStatus.textContent = "STREAM ENDED // restart to listen";
      liveSession = "";
    } catch (err) {
      if (err.name !== "AbortError") {
        liveStatus.textContent = `ERROR // ${err.message}`;
        toast(err.message || "Live audio failed", "error");
      }
    } finally {
      if (liveAbort === controller) liveAbort = null;
      if (!controller.signal.aborted && liveCtx === ctx) {
        try { ctx.close(); } catch {}
        liveCtx = null;
        liveGainNode = null;
      }
      setLiveRunning(false);
    }
  }

  livePlay.addEventListener("click", startLive);
  liveStopBtn.addEventListener("click", stopLive);
  liveVolume.addEventListener("input", () => {
    if (liveGainNode) {
      try { liveGainNode.gain.value = Number(liveVolume.value) || 0.8; } catch {}
    }
  });
  liveMode.addEventListener("change", () => {
    if (["wfm", "nfm"].includes(liveMode.value)) liveRate.value = liveMode.value === "wfm" ? "2.4" : "1.024";
  });

  let sweepAbort = null;
  let sweepHistory = [];
  let bounds = null;
  function setSweepRunning(running) {
    sweepBtn.disabled = running;
    stopSweepBtn.disabled = !running;
    sweepBtn.replaceChildren(h("span", { text: running ? "SWEEPING…" : "START SWEEP" }));
  }
  async function startSweep() {
    if (sweepAbort) return;
    const start = Number(startInput.value), end = Number(endInput.value);
    if (!(start >= 0.1 && end <= 6000 && start < end)) {
      toast("Enter a valid start/end range within 0.1–6000 MHz", "warn");
      return;
    }
    sweepHistory = [];
    bounds = null;
    const controller = new AbortController();
    sweepAbort = controller;
    setSweepRunning(true);
    spectrumReadout.firstChild.textContent = "CONNECTING // waiting for first frame";
    try {
      const qs = new URLSearchParams({
        start_mhz: String(start), end_mhz: String(end), bin_khz: String(Number(binInput.value) || 100),
        lna: String(Number(lnaInput.value) || 32), vga: String(Number(vgaInput.value) || 40),
        amp: ampInput.querySelector("input")?.checked ? "true" : "false",
        backend: sweepBackend.value, gain: String(Number(sweepGain.value) || 40),
        ppm: String(Number(sweepPpm.value) || 0),
      });
      const res = await fetch(`/api/sdr/sweep?${qs}`, {
        credentials: "same-origin", signal: controller.signal,
        headers: { Accept: "application/x-ndjson" },
      });
      if (!res.ok) {
        let detail = `Sweep failed (${res.status})`;
        try { const data = await res.json(); detail = data.detail || data.error || detail; } catch {}
        throw new Error(detail);
      }
      if (!res.body) throw new Error("Streaming response is unavailable in this browser");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.trim()) continue;
          let frame;
          try { frame = JSON.parse(line); } catch { continue; }
          if (frame.type === "error") throw new Error(frame.error || "Sweep error");
          if (frame.type === "sweep") acceptSweep(frame);
        }
      }
      spectrumReadout.firstChild.textContent = "SWEEP STOPPED // stream closed";
    } catch (err) {
      if (err.name === "AbortError") {
        spectrumReadout.firstChild.textContent = "SWEEP STOPPED // operator";
      } else {
        spectrumReadout.firstChild.textContent = `ERROR // ${err.message}`;
        toast(err.message || "Sweep failed", "error");
      }
    } finally {
      if (sweepAbort === controller) sweepAbort = null;
      setSweepRunning(false);
      drawSpectrum(spectrumCanvas, null, bounds);
    }
  }
  function acceptSweep(frame) {
    const db = Array.isArray(frame.db) ? frame.db : [];
    if (!db.length) return;
    const localMin = Math.min(...db), localMax = Math.max(...db);
    if (!bounds) bounds = { min: localMin - 7, max: localMax + 7 };
    else {
      bounds.min = bounds.min * .88 + (localMin - 7) * .12;
      bounds.max = bounds.max * .88 + (localMax + 7) * .12;
    }
    drawSpectrum(spectrumCanvas, frame, bounds);
    sweepHistory.push(db);
    if (sweepHistory.length > 150) sweepHistory.shift();
    drawWaterfall(waterfallCanvas, sweepHistory, bounds);
    spectrumReadout.firstChild.textContent =
      `LIVE // ${fmt.num(frame.start_hz / 1e6, 3)}–${fmt.num(frame.end_hz / 1e6, 3)} MHz`;
    const peakHz = peakFrequency(frame);
    spectrumReadout.lastChild.textContent =
      `PEAK ${peakHz ? `${fmt.num(peakHz / 1e6, 4)} MHz` : "—"} / ${fmt.num(frame.peak_db, 1)} dB`;
  }
  sweepBtn.addEventListener("click", startSweep);
  stopSweepBtn.addEventListener("click", () => { if (sweepAbort) sweepAbort.abort(); });
  setTimeout(() => drawSpectrum(spectrumCanvas, null, null), 30);

  /* -------------------------------------------------------------- capture */
  const captureBody = h("div", { class: "col" });
  const capFreq = input({ type: "number", value: paramFreq ? String(paramFreq) : "146.520", step: "0.0001", min: "0.1", max: "6000" });
  const capSeconds = input({ type: "number", value: "5", step: "0.5", min: "0.2", max: "180" });
  const capRate = input({ type: "number", value: "2.4", step: "0.05", min: "0.9", max: "3.2" });
  const capBackend = select([
    { value: "auto", label: "Auto" },
    { value: "hackrf", label: "HackRF (2–20 Msps)" },
    { value: "rtl", label: "RTL-SDR (0.9–3.2 Msps)" },
    { value: "rtl_tcp", label: "Network rtl_tcp" },
  ], String(store.settings?.sdr_backend || "auto"));
  const capDemod = select(DEMODS, DEMODS.some((d) => d.value === paramMode) ? paramMode : "nfm");
  const capName = input({ type: "text", placeholder: "optional label", maxlength: "120" });
  const capKeepIq = checkbox("Keep IQ file (large; enables TX replay)", false);

  function tuneToPreset(p, { setSweep = false } = {}) {
    if (!p) return;
    capFreq.value = String(p.freq_mhz);
    if (p.demod) capDemod.value = p.demod;
    if (p.rate_msps != null) capRate.value = String(p.rate_msps);
    if (p.seconds != null) capSeconds.value = String(p.seconds);
    if (!capName.value.trim()) capName.value = p.name || "";
    let sweepMsg = "";
    if (setSweep) {
      let s1, s2;
      if (p.sweep && Number(p.sweep.start_mhz) < Number(p.sweep.end_mhz)) {
        s1 = Number(p.sweep.start_mhz); s2 = Number(p.sweep.end_mhz);
      } else {
        const span = p.freq_mhz >= 1000 ? 4 : p.freq_mhz >= 100 ? 2 : p.freq_mhz >= 30 ? 4 : 2;
        s1 = Math.max(0.1, p.freq_mhz - span / 2);
        s2 = Math.min(6000, p.freq_mhz + span / 2);
      }
      startInput.value = String(Number(s1.toFixed(4)));
      endInput.value = String(Number(s2.toFixed(4)));
      sweepMsg = ` Sweep ${fmt.num(s1, 3)}–${fmt.num(s2, 3)} MHz ready.`;
    }
    toast(`Preset: ${p.name} — ${fmt.num(p.freq_mhz, 4)} MHz ${String(p.demod || "").toUpperCase()}.${sweepMsg}`);
    (setSweep ? spectrumBody : captureBody).scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  presetCtl.tune = (p) => tuneToPreset(p, { setSweep: false });
  presetCtl.sweep = (p) => tuneToPreset(p, { setSweep: true });

  const capGrid = h("div", { class: "form-grid" });
  capGrid.append(
    field("FREQUENCY (MHz)", capFreq),
    field("DURATION (s)", capSeconds),
    field("BACKEND", capBackend),
    field("SAMPLE RATE (Msps)", capRate, "HackRF 2–20; RTL-SDR 0.9–3.2"),
    field("DEMOD", capDemod),
    field("LABEL", capName),
    h("div", { class: "wide" }, capKeepIq),
  );
  const captureBtn = btn("START CAPTURE", { tone: "primary", icon: "play" });
  const captureStatus = h("div", { class: "col mt-10" });
  captureBody.append(capGrid, h("div", { class: "row mt-10" }, captureBtn), captureStatus);
  if (paramFreq) {
    captureBody.insertBefore(h("div", { class: "callout small" },
      `Tuned from Communications: ${fmt.num(paramFreq, 4)} MHz ${paramMode ? `// ${paramMode.toUpperCase()}` : ""}`), capGrid);
  }
  topGrid.appendChild(panel({
    title: "Capture & demodulate",
    sub: "HackRF / RTL-SDR / rtl_tcp → local DSP",
    icon: "radio",
    body: captureBody,
  }));

  /* --------------------------------------------------------- network SDR */
  const netHost = input({ type: "text", value: network.host || "", placeholder: "192.168.1.50" });
  const netPort = input({ type: "number", value: String(network.port || 1234), min: "1", max: "65535" });
  const netStatus = h("div", { class: "tiny muted", text: network.configured ? "Configured; press PROBE to verify." : "Not configured." });
  const netProbe = btn("PROBE", { size: "sm", icon: "search", onClick: async () => {
    netStatus.textContent = "Probing…";
    try {
      const out = await api.get(`/api/sdr/network/probe?host=${encodeURIComponent(netHost.value.trim())}&port=${Number(netPort.value) || 1234}`);
      netStatus.className = `tiny ${out.ok ? "muted" : "danger-text"}`;
      netStatus.textContent = out.ok ? `OK · RTL0 tuner ${out.tuner_type} · ${out.gain_count} gain steps` : out.error || "No rtl_tcp server answered";
    } catch (err) { netStatus.className = "tiny danger-text"; netStatus.textContent = err.message; }
  } });
  const netSave = btn("SAVE AS DEFAULT", { size: "sm", icon: "check", onClick: async () => {
    try {
      await store.saveSettings({ sdr_rtl_tcp_host: netHost.value.trim(), sdr_rtl_tcp_port: Number(netPort.value) || 1234 });
      toast("rtl_tcp address saved");
    } catch (err) { toast(err.message, "error"); }
  } });
  const netBody = h("div", { class: "col" },
    h("div", { class: "form-grid" }, field("HOST", netHost), field("PORT", netPort)),
    h("div", { class: "row wrap mt-8" }, netProbe, netSave),
    netStatus,
    h("div", { class: "tiny muted mt-6", text: "Connect DarkSec to a Raspberry Pi/uConsole or another host running rtl_tcp. Select “Network rtl_tcp” in Live audio or Capture after probing." }),
  );
  topGrid.appendChild(panel({ title: "Network SDR", sub: "rtl_tcp client", icon: "network", body: netBody }));

  const srvPort = input({ type: "number", value: String(store.settings?.sdr_server_port || 1234), min: "1", max: "65535" });
  const srvBind = input({ type: "text", value: store.settings?.sdr_server_bind || "0.0.0.0" });
  const srvFreq = input({ type: "number", value: "100.0", step: "0.0001", min: "0.1", max: "6000" });
  const srvRate = input({ type: "number", value: "2.4", step: "0.05", min: "0.9", max: "3.2" });
  const srvGain = input({ type: "number", value: String(store.settings?.sdr_rtl_gain ?? 40), min: "0", max: "62" });
  const srvStatus = h("div", { class: "tiny muted", text: "Not running." });
  const srvStart = btn("SHARE RTL-SDR", { size: "sm", tone: "primary", icon: "play", onClick: async () => {
    try {
      const out = await api.post("/api/sdr/server/start", {
        bind: srvBind.value.trim() || "0.0.0.0", port: Number(srvPort.value) || 1234,
        freq_hz: Number(srvFreq.value) * 1e6, rate: Number(srvRate.value) * 1e6,
        gain: Number(srvGain.value) || 40, ppm: Number(store.settings?.sdr_ppm) || 0,
        bias: !!store.settings?.sdr_bias_tee,
      });
      paintServer(out);
    } catch (err) { toast(err.message, "error"); }
  } });
  const srvStop = btn("STOP", { size: "sm", tone: "danger", icon: "stop", onClick: async () => {
    try { paintServer(await api.post("/api/sdr/server/stop", {})); } catch (err) { toast(err.message, "error"); }
  } });
  const srvBody = h("div", { class: "col" },
    h("div", { class: "form-grid" },
      field("BIND", srvBind, "Use the LAN IP to limit exposure"),
      field("PORT", srvPort),
      field("FREQ (MHz)", srvFreq),
      field("RATE (Msps)", srvRate),
      field("GAIN", srvGain)),
    h("div", { class: "row wrap mt-8" }, srvStart, srvStop),
    srvStatus,
    h("div", { class: "tiny muted mt-6", text: "rtl_tcp has no authentication. Use only a trusted private LAN. Another DarkSec instance can connect from the Network SDR panel." }),
  );
  topGrid.appendChild(panel({ title: "Share this SDR", sub: "rtl_tcp server", icon: "upload", body: srvBody }));

  function paintServer(out) {
    srvStatus.className = `tiny ${out?.running ? "muted" : "muted"}`;
    if (!out?.running) { srvStatus.textContent = "Not running."; return; }
    srvStatus.replaceChildren(
      h("span", { text: `RUNNING · ${out.bind}:${out.port}` }),
      ...(out.urls || []).map((url) => h("div", { class: "row between tiny mono mt-4" },
        h("span", { text: url }),
        btn("COPY", { size: "sm", tone: "ghost", onClick: () => navigator.clipboard?.writeText(url).then(() => toast("Copied")) }))),
    );
  }
  api.get("/api/sdr/server").then(paintServer).catch(() => {});

  /* --------------------------------------------------------------- WebSDR */
  const wsFreq = input({ type: "number", value: "7.100", step: "0.0001", min: "0.1", max: "6000" });
  const wsMode = select([
    { value: "lsb", label: "LSB" }, { value: "usb", label: "USB" },
    { value: "am", label: "AM" }, { value: "nfm", label: "NFM" }, { value: "wfm", label: "WFM" },
  ], "lsb");
  const wsBand = select([
    { value: "ALL", label: "All bands" }, { value: "HF", label: "HF" },
    { value: "VHF", label: "VHF" }, { value: "UHF", label: "UHF" },
  ], "ALL");
  const wsSearch = input({ type: "search", placeholder: "Filter receiver, location or host…" });
  const wsList = h("div", { class: "col gap-6" }, loading("LOADING PUBLIC RECEIVERS"));
  const wsStatus = h("div", { class: "tiny muted", text: "Public WebSDR/KiwiSDR directory" });
  const wsCustom = input({ type: "text", placeholder: "https://receiver.example:8073/" });
  let wsData = { receivers: [] };

  async function openWebsdr(rec) {
    const freq = Number(wsFreq.value);
    if (!(freq >= 0.1 && freq <= 6000)) { toast("Enter a frequency within 0.1–6000 MHz", "warn"); return; }
    const win = window.open("about:blank", "_blank");
    try {
      const out = await api.get(`/api/sdr/websdr/tune?url=${encodeURIComponent(rec.url)}&freq_hz=${encodeURIComponent(freq * 1e6)}&mode=${encodeURIComponent(wsMode.value)}`);
      if (win) {
        try { win.opener = null; } catch {}
        win.location.href = out.url;
      } else {
        window.open(out.url, "_blank", "noopener,noreferrer");
      }
    } catch (err) {
      try { win?.close(); } catch {}
      toast(err.message || "Could not build receiver URL", "error");
    }
  }

  function paintWebsdr(out) {
    wsData = out || { receivers: [] };
    const q = wsSearch.value.trim().toLowerCase();
    const band = wsBand.value;
    const items = (wsData.receivers || []).filter((r) => {
      if (band !== "ALL" && r.band !== band) return false;
      if (!q) return true;
      return `${r.name} ${r.loc} ${r.url}`.toLowerCase().includes(q);
    }).slice(0, 80);
    wsStatus.className = "tiny muted";
    wsStatus.textContent = `${wsData.count ?? items.length} receiver(s) · ${wsData.source === "curated" ? "curated fallback" : "KiwiSDR mirror"}${wsData.stale ? " · cached" : ""}${wsData.error ? ` · ${wsData.error}` : ""}`;
    wsList.replaceChildren();
    if (!items.length) {
      wsList.appendChild(emptyState("No receivers match", "Clear the filter, change band, or refresh the directory.", "radio"));
      return;
    }
    for (const rec of items) {
      const users = rec.users != null ? `${rec.users}/${rec.users_max ?? "?"} users` : "users n/a";
      wsList.appendChild(h("div", { class: "list-item" },
        h("div", { class: "li-main" },
          h("div", { class: "li-title truncate", text: rec.name }),
          h("div", { class: "li-sub", text: `${rec.loc || "unknown"} · ${rec.band} · ${users}${rec.ext_api === 0 ? " · web only" : ""}` }),
          h("div", { class: "li-sub mono truncate", text: rec.url, title: rec.url })),
        badge(rec.kind === "kiwi" ? "KIWI" : "WEBSDR", rec.kind === "kiwi" ? "cyan" : "green"),
        btn("LISTEN", { size: "sm", tone: "primary", icon: "play", onClick: () => openWebsdr(rec) }),
        btn("COPY", { size: "sm", tone: "ghost", onClick: async () => {
          try {
            const out = await api.get(`/api/sdr/websdr/tune?url=${encodeURIComponent(rec.url)}&freq_hz=${encodeURIComponent(Number(wsFreq.value) * 1e6)}&mode=${encodeURIComponent(wsMode.value)}`);
            await navigator.clipboard?.writeText(out.url); toast("Tuned URL copied");
          } catch (err) { toast(err.message, "error"); }
        } })));
    }
  }

  async function loadWebsdr(refresh = false) {
    wsStatus.textContent = refresh ? "Refreshing public directory…" : "Loading public receivers…";
    try {
      const out = await api.get(`/api/sdr/websdr?refresh=${refresh ? "true" : "false"}`);
      paintWebsdr(out);
    } catch (err) {
      wsStatus.className = "tiny danger-text";
      wsStatus.textContent = err.message || "WebSDR directory unavailable";
    }
  }
  wsSearch.addEventListener("input", () => paintWebsdr(wsData));
  wsBand.addEventListener("change", () => paintWebsdr(wsData));
  const wsBody = h("div", { class: "col" },
    h("div", { class: "form-grid" },
      field("FREQUENCY (MHz)", wsFreq),
      field("MODE", wsMode),
      field("BAND", wsBand),
      field("FILTER", wsSearch)),
    h("div", { class: "row wrap mt-8" },
      btn("REFRESH DIRECTORY", { size: "sm", icon: "refresh", onClick: () => loadWebsdr(true) }),
      h("div", { class: "row gap-6 grow" }, h("div", { class: "grow" }, wsCustom),
        btn("OPEN URL", { size: "sm", icon: "upload", onClick: () => openWebsdr({ url: wsCustom.value.trim() }) }))),
    wsStatus,
    h("div", { class: "scroll-y mt-8", style: { maxHeight: "46vh" } }, wsList),
    h("div", { class: "tiny muted mt-8", text: "LISTEN opens the receiver's own web interface tuned to this frequency. DarkSec does not proxy or record public receivers; respect operator limits and terms." }),
  );
  root.appendChild(panel({ title: "WebSDR / remote receivers", sub: "No local radio required", icon: "globe", cls: "mt-14", body: wsBody }));
  loadWebsdr(false);

  let pollTimer = null;
  let activeJob = null;
  function showJob(job) {
    activeJob = job;
    captureStatus.replaceChildren();
    const wrap = h("div", { class: "device-card" });
    wrap.appendChild(h("div", { class: "row between" },
      h("div", { class: "dev-name", text: job.name || job.id }),
      badge(String(job.status || "queued").toUpperCase(), statusTone(job.status))));
    wrap.appendChild(kv([
      ["Frequency", mhz(job.freq_hz)],
      ["Rate", job.rate ? `${fmt.num(Number(job.rate) / 1e6, 2)} Msps` : "—"],
      ["Duration", job.seconds != null ? `${fmt.num(job.seconds, 1)} s` : "—"],
      ["Mode", String(job.demod || job.kind || "—").toUpperCase()],
      ["Backend", backendName(job.backend || "hackrf")],
      ["Job ID", job.id],
    ]));
    const pct = Math.round((Number(job.progress) || 0) * 100);
    wrap.appendChild(h("div", { class: "row between tiny muted mt-6" },
      h("span", { text: pct >= 100 ? "COMPLETE" : "CAPTURING" }),
      h("span", { text: `${pct}%` })));
    wrap.appendChild(progressBar(pct, job.status === "error" ? "bad" : ""));
    if (job.error) wrap.appendChild(h("div", { class: "callout danger small mt-10", text: job.error }));
    if (job.status === "complete") {
      if (job.audio_path) {
        wrap.appendChild(h("div", { class: "side-label mt-10", text: "AUDIO" }));
        const audio = h("audio", { controls: true, src: `/api/sdr/jobs/${encodeURIComponent(job.id)}/audio` });
        audio.style.width = "100%";
        wrap.appendChild(audio);
      }
      if (job.image_path) {
        wrap.appendChild(h("div", { class: "side-label mt-10", text: "APT IMAGE // EXPERIMENTAL" }));
        wrap.appendChild(h("img", { src: `/api/sdr/jobs/${encodeURIComponent(job.id)}/image`, alt: "APT decode", style: { width: "100%", border: "1px solid var(--line)" } }));
      }
      if (!job.audio_path && !job.image_path) {
        wrap.appendChild(h("div", { class: "small muted mt-10", text: "Job complete. See the recording list below." }));
      }
      loadRecordings();
    } else if (job.status === "running" || job.status === "processing" || job.status === "queued") {
      const stop = btn("STOP JOB", { size: "sm", tone: "danger", icon: "stop", onClick: async () => {
        try { await api.post(`/api/sdr/jobs/${encodeURIComponent(job.id)}/stop`, {}); toast("Stop requested"); }
        catch (err) { toast(err.message, "error"); }
      } });
      wrap.appendChild(h("div", { class: "row mt-10" }, stop));
    }
    captureStatus.appendChild(wrap);
  }
  async function startCapture() {
    const freqMhz = Number(capFreq.value);
    if (!(freqMhz >= 1 && freqMhz <= 6000)) { toast("Frequency must be 1–6000 MHz", "warn"); return; }
    captureBtn.disabled = true;
    captureStatus.replaceChildren(loading("STARTING CAPTURE"));
    try {
      const job = await api.post("/api/sdr/capture", {
        freq_hz: freqMhz * 1e6,
        seconds: Number(capSeconds.value),
        rate: Number(capRate.value) * 1e6,
        demod: capDemod.value,
        lna: Number(lnaInput.value) || 32,
        vga: Number(vgaInput.value) || 40,
        amp: !!ampInput.querySelector("input")?.checked,
        keep_iq: !!capKeepIq.querySelector("input")?.checked,
        name: capName.value.trim(),
        backend: capBackend.value,
        host: netHost.value.trim(), port: Number(netPort.value) || 1234,
      });
      showJob(job);
      clearInterval(pollTimer);
      pollTimer = setInterval(async () => {
        if (!activeJob) return;
        try {
          const j = await api.get(`/api/sdr/jobs/${encodeURIComponent(activeJob.id)}`);
          showJob(j);
          if (["complete", "error", "stopped"].includes(j.status)) {
            clearInterval(pollTimer);
            pollTimer = null;
            if (j.status === "complete") toast("Capture complete");
            if (j.status === "error") toast(j.error || "Capture failed", "error");
          }
        } catch (err) {
          clearInterval(pollTimer); pollTimer = null;
          captureStatus.appendChild(h("div", { class: "callout danger small mt-10", text: err.message }));
        }
      }, 700);
    } catch (err) {
      captureStatus.replaceChildren(h("div", { class: "callout danger small", text: err.message }));
      toast(err.message || "Capture failed", "error");
    } finally {
      captureBtn.disabled = false;
    }
  }
  captureBtn.addEventListener("click", startCapture);

  /* ----------------------------------------------------------- recordings */
  const recPanel = panel({
    title: "Recordings",
    sub: "Local audio, images and retained IQ",
    icon: "database",
    cls: "span-3",
    tools: [btn("REFRESH", { size: "sm", icon: "refresh", onClick: () => loadRecordings() })],
    body: h("div", { class: "col", id: "sdr-rec-list" }),
  });
  root.appendChild(recPanel);
  root.appendChild(h("div", { class: "grid grid-2 mt-14", id: "sdr-lower" }));
  const lower = root.querySelector("#sdr-lower");

  async function loadRecordings() {
    try {
      const out = await api.get("/api/sdr/recordings");
      paintRecordings(out.items || []);
    } catch (err) { toast(err.message, "error"); }
  }
  function paintRecordings(items) {
    const target = recPanel.querySelector("#sdr-rec-list");
    target.replaceChildren();
    if (!items.length) {
      target.appendChild(emptyState("No recordings", "Capture a signal to create a real local recording.", "radio"));
      return;
    }
    for (const rec of items) {
      const wrap = h("div", { class: "rec-item" });
      const info = h("div", { class: "grow" });
      info.appendChild(h("div", { class: "rec-name", text: rec.name || `Recording #${rec.id}` }));
      const meta = noteFields(rec.note);
      info.appendChild(h("div", { class: "rec-sub",
        text: `${mhz(rec.freq_hz)} · ${String(rec.kind || "").toUpperCase()} · ${backendName(meta.backend || "hackrf")} · ${fmt.bytes(rec.size || 0)} · ${fmt.ago(rec.ts)}` }));
      wrap.appendChild(info);
      if (rec.audio_path) {
        const audio = h("audio", { controls: true, preload: "none", src: `/api/sdr/recordings/${rec.id}/audio` });
        wrap.appendChild(audio);
      }
      if (rec.image_path) {
        wrap.appendChild(h("img", {
          src: `/api/sdr/recordings/${rec.id}/image`, alt: rec.name || "recording",
          style: { height: "38px", border: "1px solid var(--line)" },
        }));
      }
      if (rec.iq_path) {
        wrap.appendChild(btn("IQ", { size: "sm", tone: "ghost", icon: "download", title: "Download IQ",
          onClick: () => { location.href = `/api/sdr/recordings/${rec.id}/iq?download=true`; } }));
        const fmt = noteFields(rec.note).format;
        if (!fmt || fmt === "i8") {
          wrap.appendChild(btn("TX", { size: "sm", tone: "danger", title: "Replay this IQ",
            onClick: () => openTx(rec) }));
        }
      }
      wrap.appendChild(btn("DEL", { size: "sm", tone: "ghost", onClick: async () => {
        const ok = await confirmDialog({
          title: "Delete recording", confirmText: "DELETE", danger: true,
          message: `Delete recording #${rec.id} and all of its local files? This cannot be undone.`,
        });
        if (!ok) return;
        try { await api.del(`/api/sdr/recordings/${rec.id}`); toast("Recording deleted"); loadRecordings(); }
        catch (err) { toast(err.message, "error"); }
      } }));
      target.appendChild(wrap);
    }
  }

  /* ---------------------------------------------------------------- ADS-B */
  const adsbBody = h("div", { class: "col" });
  if (!adsb.available) {
    adsbBody.appendChild(emptyState("ADS-B feed unavailable", adsb.note || "Start dump1090 and expose its JSON feed on 127.0.0.1:8080.", "satellite"));
  } else if (!(adsb.aircraft || []).length) {
    adsbBody.appendChild(emptyState("No aircraft heard", "dump1090 is reachable but reports no aircraft.", "satellite"));
  } else {
    const table = h("table", { class: "data" });
    table.innerHTML = "<thead><tr><th>FLIGHT</th><th>HEX</th><th>ALT</th><th>GS</th><th>TRK</th></tr></thead>";
    const tbody = h("tbody");
    for (const a of adsb.aircraft) {
      const tr = h("tr");
      tr.append(
        h("td", { text: a.flight || "—" }),
        h("td", { class: "mono", text: a.hex || "—" }),
        h("td", { class: "num", text: a.alt_baro != null ? String(a.alt_baro) : "—" }),
        h("td", { class: "num", text: a.gs != null ? fmt.num(a.gs, 0) : "—" }),
        h("td", { class: "num", text: a.track != null ? `${fmt.num(a.track, 0)}°` : "—" }),
      );
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    adsbBody.appendChild(h("div", { class: "scroll-x" }, table));
  }
  adsbBody.appendChild(h("div", { class: "hint mt-10",
    text: "ADS-B is receive-only. dump1090 typically needs an RTL-SDR; a HackRF IQ capture can be fed to dump1090 --ifile separately." }));
  lower.appendChild(panel({ title: "ADS-B / air picture", sub: "dump1090 local feed", icon: "satellite", body: adsbBody }));

  /* ------------------------------------------------------------------- TX */
  const txBody = h("div", { class: "col" });
  const iqRecs = recs.filter((r) => r.iq_path);
  if (!txEnabled) {
    txBody.appendChild(h("div", { class: "tx-warning",
      text: "TRANSMIT IS HARD-LOCKED. Enable it in Settings only after you have read the legal and RF-safety warnings and recorded the legal attestation." }));
    txBody.appendChild(btn("OPEN TX GATE SETTINGS", { tone: "amber", icon: "settings", onClick: () => { location.hash = "#/settings"; } }));
  } else {
    txBody.appendChild(h("div", { class: "tx-warning",
      text: "TX is armed. Replay only IQ you are authorised to transmit. Use a dummy load or shielded setup for testing, never an antenna indoors." }));
    if (!iqRecs.length) {
      txBody.appendChild(h("div", { class: "small muted", text: "No recordings with retained IQ. Enable “Keep IQ” when capturing." }));
    } else {
      const sel = select(iqRecs.map((r) => ({ value: String(r.id), label: `${r.name || `#${r.id}`} — ${mhz(r.freq_hz)}` })), String(iqRecs[0].id));
      txBody.appendChild(field("RETAINED IQ RECORDING", sel));
      txBody.appendChild(btn("ARM REPLAY", { tone: "danger", icon: "send", onClick: () => openTx(iqRecs.find((r) => String(r.id) === sel.value) || iqRecs[0]) }));
    }
  }
  lower.appendChild(panel({ title: "Transmit gate", sub: "Audited and confirmation-gated", icon: "warning", cls: "tx-gate", body: txBody }));

  function openTx(rec) {
    if (!rec) return;
    if (!txEnabled) { toast("Transmit is disabled in Settings", "warn"); return; }
    const attest = checkbox("I am licensed/authorised for this frequency and accept responsibility for this transmission.", false);
    const phrase = input({ placeholder: "Type TRANSMIT", autocomplete: "off" });
    const amp = checkbox("Enable RF amplifier (+14 dB) — only with proper shielding/load", false);
    const gain = input({ type: "number", value: "40", min: "0", max: "47" });
    const body = h("div", { class: "col" },
      h("div", { class: "tx-warning", text:
        "This will key the HackRF and replay raw IQ into whatever is connected. Confirm the frequency, filter, antenna/load and legal authority before proceeding." }),
      kv([["Recording", rec.name || `#${rec.id}`], ["Frequency", mhz(rec.freq_hz)], ["Rate", rec.rate ? `${fmt.num(rec.rate / 1e6, 2)} Msps` : "—"]]),
      attest,
      field("Confirmation", phrase),
      field("TX VGA gain (0-47)", gain),
      amp,
    );
    const m = modal({
      title: "Transmit confirmation",
      sub: "Hard gate // audited",
      body,
      actions: [btn("CANCEL", { tone: "ghost", onClick: () => m.close() }), btn("TRANSMIT", { tone: "danger", icon: "send" })],
    });
    const go = m.box.querySelector(".modal-foot .btn-danger");
    go.addEventListener("click", async () => {
      if (phrase.value.trim().toUpperCase() !== "TRANSMIT") { toast("Type TRANSMIT exactly", "warn"); return; }
      if (!attest.querySelector("input")?.checked) { toast("Legal attestation is required", "warn"); return; }
      go.disabled = true;
      try {
        const out = await api.post("/api/sdr/tx", {
          recording_id: rec.id, vga: Number(gain.value) || 40,
          amp: !!amp.querySelector("input")?.checked, confirm: "TRANSMIT", attest: true,
        });
        toast(out.note || "Transmission complete");
        m.close();
      } catch (err) {
        toast(err.message || "Transmit failed", "error");
      } finally { go.disabled = false; }
    });
  }

  paintRecordings(recs);

  /* ---------------------------------------------------------------- jobs */
  if (recentJobs.length) {
    const jobsPanel = panel({
      title: "Recent jobs",
      sub: "Capture history",
      icon: "clock",
      cls: "span-3",
      body: h("div", { class: "scroll-x" }, (() => {
        const table = h("table", { class: "data" });
        table.innerHTML = "<thead><tr><th>ID</th><th>NAME</th><th>FREQ</th><th>MODE</th><th>STATUS</th><th>CREATED</th></tr></thead>";
        const tbody = h("tbody");
        for (const j of recentJobs) {
          const tr = h("tr");
          tr.append(
            h("td", { class: "mono", text: j.id }),
            h("td", { text: j.name || "—" }),
            h("td", { class: "num", text: mhz(j.freq_hz) }),
            h("td", { text: String(j.demod || j.kind || "—").toUpperCase() }),
            h("td", {}, badge(String(j.status || "").toUpperCase(), statusTone(j.status))),
            h("td", { text: fmt.date(j.created) }),
          );
          tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        return table;
      })()),
    });
    root.appendChild(jobsPanel);
  }

  const cards = await referenceSection("COMMS", { limit: 6 });
  if (cards) root.appendChild(panel({ title: "Radio field cards", sub: "Receive and procedure reference", icon: "book", cls: "mt-14", body: cards }));

  loadRecordings();

  return () => {
    if (sweepAbort) { try { sweepAbort.abort(); } catch {} }
    if (pollTimer) clearInterval(pollTimer);
  };

  async function rescan(force = false) {
    try {
      const d = await api.get(`/api/sdr/devices?force=${force ? "true" : "false"}`);
      const found = d.active_backend && d.active_backend !== "none";
      toast(found ? `${backendName(d.active_backend)} detected` : "No local SDR detected",
        found ? "" : "warn");
      render(root, params);
    } catch (err) { toast(err.message, "error"); }
  }
}

/* --------------------------------------------------------------- rendering */
function peakFrequency(frame) {
  const db = frame.db || [];
  if (!db.length) return null;
  let idx = 0;
  for (let i = 1; i < db.length; i++) if (db[i] > db[idx]) idx = i;
  const span = Number(frame.end_hz) - Number(frame.start_hz);
  return Number(frame.start_hz) + (idx / Math.max(1, db.length - 1)) * span;
}

function chartSize(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(280, rect.width || 800);
  const hh = Math.max(100, rect.height || 220);
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(hh * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: hh };
}

function drawSpectrum(canvas, frame, bounds) {
  const { ctx, w, h: hh } = chartSize(canvas);
  ctx.clearRect(0, 0, w, hh);
  ctx.fillStyle = "#020604";
  ctx.fillRect(0, 0, w, hh);
  ctx.strokeStyle = "rgba(93,255,154,.09)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 8; i++) {
    const x = (w / 8) * i;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, hh); ctx.stroke();
  }
  for (let i = 1; i < 5; i++) {
    const y = (hh / 5) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  if (!frame || !frame.db?.length || !bounds) {
    ctx.fillStyle = "rgba(123,154,136,.7)";
    ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.fillText("NO SWEEP DATA", 12, hh / 2);
    return;
  }
  const db = frame.db;
  const lo = bounds.min, hi = bounds.max;
  ctx.beginPath();
  for (let i = 0; i < db.length; i++) {
    const x = 4 + (i / (db.length - 1)) * (w - 8);
    const y = hh - 18 - ((db[i] - lo) / Math.max(0.001, hi - lo)) * (hh - 30);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.lineTo(w - 4, hh - 18);
  ctx.lineTo(4, hh - 18);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, hh);
  grad.addColorStop(0, "rgba(93,255,154,.24)");
  grad.addColorStop(1, "rgba(93,255,154,.015)");
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.beginPath();
  for (let i = 0; i < db.length; i++) {
    const x = 4 + (i / (db.length - 1)) * (w - 8);
    const y = hh - 18 - ((db[i] - lo) / Math.max(0.001, hi - lo)) * (hh - 30);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = "#5dff9a";
  ctx.lineWidth = 1.4;
  ctx.stroke();
  ctx.fillStyle = "rgba(205,236,218,.72)";
  ctx.font = "9px 'JetBrains Mono', monospace";
  ctx.fillText(`${(frame.start_hz / 1e6).toFixed(3)} MHz`, 6, hh - 5);
  const mid = (frame.start_hz + frame.end_hz) / 2 / 1e6;
  ctx.textAlign = "center";
  ctx.fillText(`${mid.toFixed(3)} MHz`, w / 2, hh - 5);
  ctx.textAlign = "right";
  ctx.fillText(`${(frame.end_hz / 1e6).toFixed(3)} MHz`, w - 6, hh - 5);
  ctx.textAlign = "left";
  ctx.fillStyle = "rgba(123,154,136,.78)";
  ctx.fillText(`${Math.round(hi)} dB`, 6, 12);
  ctx.fillStyle = "rgba(123,154,136,.55)";
  ctx.fillText(`${Math.round(lo)} dB`, 6, hh - 21);
}

function drawWaterfall(canvas, history, bounds) {
  if (!history.length) {
    const { ctx, w, h: hh } = chartSize(canvas);
    ctx.fillStyle = "#020604";
    ctx.fillRect(0, 0, w, hh);
    return;
  }
  const width = Math.min(1024, Math.max(64, history[0].length));
  canvas.width = width;
  canvas.height = Math.max(40, history.length);
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(width, canvas.height);
  const lo = bounds?.min ?? -110, hi = bounds?.max ?? -10;
  for (let row = 0; row < history.length; row++) {
    const frame = history[row];
    const src = frame.length;
    const y = history.length - 1 - row;
    for (let x = 0; x < width; x++) {
      const idx = Math.min(src - 1, Math.floor((x / width) * src));
      let t = (frame[idx] - lo) / Math.max(0.001, hi - lo);
      t = Math.max(0, Math.min(1, t));
      let r, g, b;
      if (t < .45) { const k = t / .45; r = 2 + 10 * k; g = 8 + 80 * k; b = 6 + 24 * k; }
      else if (t < .8) { const k = (t - .45) / .35; r = 12 + 80 * k; g = 88 + 167 * k; b = 30 + 40 * k; }
      else { const k = (t - .8) / .2; r = 92 + 163 * k; g = 255; b = 70 + 150 * k; }
      const p = (y * width + x) * 4;
      img.data[p] = r; img.data[p + 1] = g; img.data[p + 2] = b; img.data[p + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
}
