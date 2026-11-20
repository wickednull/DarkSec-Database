/* SDR / spectrum: HackRF, RTL-SDR, network rtl_tcp and WebSDR. No synthetic spectrum is drawn. */
import { api } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, stat, btn, badge, pageHead, alertStrip, emptyState, loading, fmt, toast,
  field, input, select, checkbox, modal, confirmDialog, icon,
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

function directValue(settings) {
  const value = String(settings?.sdr_direct_sampling ?? "auto").toLowerCase();
  return value === "2" ? "2" : value === "0" ? "0" : "auto";
}

function directOptions() {
  return [
    { value: "auto", label: "AUTO · HF/VHF" },
    { value: "0", label: "TUNER 24M+" },
    { value: "2", label: "HF Q" },
  ];
}

function accentColor() {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue("--acc").trim() || "#5fe0a0";
  } catch { return "#5fe0a0"; }
}
function accentRgb() {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue("--acc-rgb").trim() || "95,224,160";
  } catch { return "95,224,160"; }
}
function cssColor(name, fallback) {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  } catch { return fallback; }
}
function cssRgb(name, fallback) {
  try {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  } catch { return fallback; }
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
    desc: "Live receiver, wideband scan, IQ capture and public WebSDR. Click the spectrum to tune; every local value comes from the radio.",
    actions: [
      btn("CHECK HARDWARE", { icon: "refresh", onClick: () => rescan() }),
      btn("DIAGNOSE", { icon: "terminal", onClick: () => runDiagnose() }),
      btn("FIELD MANUAL", { icon: "book", onClick: () => { location.hash = "#/manual?doc=sdr"; } }),
      btn("SETTINGS", { icon: "settings", tone: "ghost", onClick: () => { location.hash = "#/settings"; } }),
    ],
  }));

  const legalNote = alertStrip(
    "Receive first. Transmitting without a licence, on the wrong frequency, or near sensitive systems can be illegal and dangerous. "
    + "HackRF is not a calibrated instrument. Use the spectrum display to find signals, then verify with a dedicated receiver.",
    "",
  );

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

  const presetsPanel = panel({
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
  });

  // ------------------------------------------------------------- tabs
  const tabDefs = [
    ["receiver", "RECEIVER", "signal"],
    ["scan", "SCAN", "search"],
    ["capture", "CAPTURE", "radio"],
    ["network", "NETWORK", "network"],
    ["websdr", "WEBSDR", "globe"],
    ["adsb", "ADS-B", "satellite"],
    ["system", "SYSTEM", "settings"],
  ];
  const tabBar = h("div", { class: "sdr-tabs", role: "tablist" });
  const tabButtons = {};
  const paneReceiver = h("div", { class: "sdr-pane sdr-pane-receiver active", id: "sdr-pane-receiver" });
  const paneScan = h("div", { class: "sdr-pane panel-grid grid-2", id: "sdr-pane-scan" });
  const paneCapture = h("div", { class: "sdr-pane", id: "sdr-pane-capture" });
  const paneNetwork = h("div", { class: "sdr-pane panel-grid grid-2", id: "sdr-pane-network" });
  const paneWeb = h("div", { class: "sdr-pane", id: "sdr-pane-websdr" });
  const paneAdsb = h("div", { class: "sdr-pane", id: "sdr-pane-adsb" });
  const paneSystem = h("div", { class: "sdr-pane panel-grid grid-2", id: "sdr-pane-system" });
  const panes = { receiver: paneReceiver, scan: paneScan, capture: paneCapture,
    network: paneNetwork, websdr: paneWeb, adsb: paneAdsb, system: paneSystem };
  let activeTab = "receiver";
  function showTab(name) {
    activeTab = panes[name] ? name : "receiver";
    for (const [id, button] of Object.entries(tabButtons)) {
      button.classList.toggle("active", id === activeTab);
      button.setAttribute("aria-selected", id === activeTab ? "true" : "false");
    }
    for (const [id, pane] of Object.entries(panes)) {
      pane.classList.toggle("active", id === activeTab);
    }
    try { localStorage.setItem("darksec.sdr.tab", activeTab); } catch {}
    if (activeTab === "receiver") setTimeout(drawLiveSpectrum, 20);
  }
  for (const [id, label, iconName] of tabDefs) {
    const button = h("button", { class: "sdr-tab", role: "tab", "data-tab": id,
      onClick: () => showTab(id) }, icon(iconName), h("span", { text: label }));
    tabButtons[id] = button;
    tabBar.appendChild(button);
  }
  stats.classList.add("span-2");
  legalNote.classList.add("span-2");
  paneSystem.prepend(legalNote);
  paneSystem.prepend(stats);
  root.appendChild(tabBar);
  root.appendChild(h("div", { class: "sdr-panes" }, ...Object.values(panes)));
  let storedTab = "receiver";
  try { storedTab = localStorage.getItem("darksec.sdr.tab") || "receiver"; } catch {}
  showTab(storedTab);

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
    ["RTL-SDR", rtl.connected ? [rtl.device_name || "RTL-SDR", rtl.tuner].filter(Boolean).join(" · ") : rtl.present || tools.rtl_test ? "tools installed / no device" : "not installed"],
    ["HF mode", directValue(store.settings) === "auto"
      ? "AUTO · HF Q <24 MHz"
      : (directValue(store.settings) === "2" || rtl.direct_sampling === 2) ? "DIRECT Q / HF" : "TUNER"],
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
  const sweepDirect = select(directOptions(), directValue(store.settings));
  controls.append(
    field("START (MHz)", startInput),
    field("END (MHz)", endInput),
    field("BIN (kHz)", binInput),
    field("BACKEND", sweepBackend),
    field("LNA (0-40)", lnaInput),
    field("VGA (0-62)", vgaInput),
    field("RTL GAIN", sweepGain),
    field("PPM", sweepPpm),
    field("HF MODE", sweepDirect),
    h("div", { class: "field" }, h("label", { text: "AMP" }), ampInput),
  );
  const sweepBtn = btn("START SWEEP", { tone: "primary", icon: "play" });
  const stopSweepBtn = btn("STOP", { icon: "stop", tone: "danger", disabled: true });
  let sweepAbort = null;
  let sweepRunning = false;
  let sweepHistory = [];
  let sweepBounds = { min: -110, max: -10 };

  function paintSweepStatus(left, right = "—") {
    spectrumReadout.replaceChildren(
      h("span", { text: left }),
      h("span", { text: right }),
    );
  }

  function acceptSweep(frame) {
    const db = frame.db || [];
    if (!db.length) return;
    const lo = Math.min(...db), hi = Math.max(...db);
    sweepBounds.min = sweepBounds.min * 0.85 + (lo - 6) * 0.15;
    sweepBounds.max = sweepBounds.max * 0.85 + (hi + 6) * 0.15;
    sweepHistory.push(db);
    if (sweepHistory.length > 90) sweepHistory.shift();
    drawSpectrum(spectrumCanvas, frame, sweepBounds);
    drawWaterfall(waterfallCanvas, sweepHistory, sweepBounds);
    const peak = peakFrequency(frame);
    paintSweepStatus(
      `SCANNING // ${fmt.num(frame.start_hz / 1e6, 3)}–${fmt.num(frame.end_hz / 1e6, 3)} MHz · ${frame.count || db.length} bins`,
      `PEAK ${peak == null ? "—" : `${fmt.num(peak / 1e6, 4)} MHz / ${fmt.num(frame.peak_db, 1)} dB`}`,
    );
  }

  function stopSweep(message = "") {
    stopSweep();
    sweepAbort = null;
    sweepRunning = false;
    sweepBtn.disabled = false;
    stopSweepBtn.disabled = true;
    if (message) paintSweepStatus(`IDLE // ${message}`);
  }

  async function startSweep() {
    if (sweepRunning) return;
    const s1 = Number(startInput.value);
    const s2 = Number(endInput.value);
    if (!(s1 > 0 && s2 > s1)) { toast("Sweep start must be lower than sweep end", "warn"); return; }
    sweepRunning = true;
    sweepBtn.disabled = true;
    stopSweepBtn.disabled = false;
    sweepHistory = [];
    sweepBounds = { min: -110, max: -10 };
    paintSweepStatus("CONNECTING // starting sweep");
    drawSpectrum(spectrumCanvas, null, sweepBounds);
    drawWaterfall(waterfallCanvas, [], sweepBounds);
    const ac = new AbortController();
    sweepAbort = ac;
    const qs = new URLSearchParams({
      start_mhz: String(s1), end_mhz: String(s2),
      bin_khz: String(Number(binInput.value) || 100),
      backend: sweepBackend.value,
      gain: String(Number(sweepGain.value) || 40),
      ppm: String(Number(sweepPpm.value) || 0),
      device: "0",
      direct: sweepDirect.value,
      lna: String(Number(lnaInput.value) || 32),
      vga: String(Number(vgaInput.value) || 40),
      amp: String(!!ampInput.querySelector("input")?.checked),
    });
    let gotData = false;
    let lastError = "";
    try {
      const res = await fetch(`/api/sdr/sweep?${qs}`, {
        signal: ac.signal,
        headers: { Accept: "application/x-ndjson" },
        cache: "no-store",
      });
      if (!res.ok) {
        let detail = res.statusText || `HTTP ${res.status}`;
        try { const body = await res.json(); detail = body.detail || detail; } catch {}
        throw new Error(detail);
      }
      if (!res.body) throw new Error("The browser cannot stream this response");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let cut;
        while ((cut = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, cut).trim();
          buffer = buffer.slice(cut + 1);
          if (!line) continue;
          let frame;
          try { frame = JSON.parse(line); } catch { continue; }
          if (frame.type === "sweep") { gotData = true; acceptSweep(frame); }
          else if (frame.type === "error") {
            lastError = frame.error || "Sweep failed";
            toast(lastError, "error", 8000);
            paintSweepStatus(`ERROR // ${lastError}`);
          } else if (frame.type === "notice" && frame.message) {
            toast(frame.message, "warn", 7000);
          }
        }
      }
      if (!gotData && !lastError) paintSweepStatus("IDLE // no sweep data; check the radio and range");
    } catch (err) {
      if (err.name !== "AbortError") {
        lastError = err.message || "Sweep failed";
        toast(lastError, "error", 8000);
        paintSweepStatus(`ERROR // ${lastError}`);
      }
    } finally {
      if (sweepAbort === ac) {
        sweepAbort = null;
        sweepRunning = false;
        sweepBtn.disabled = false;
        stopSweepBtn.disabled = true;
      }
    }
  }

  sweepBtn.addEventListener("click", startSweep);
  stopSweepBtn.addEventListener("click", () => stopSweep("sweep stopped"));
  spectrumBody.append(
    spectrumShell,
    waterfallShell,
    spectrumReadout,
    controls,
    h("div", { class: "row wrap mt-10" }, sweepBtn, stopSweepBtn,
      h("span", { class: "tiny muted", text: "Sweep uses hackrf_sweep or rtl_power. A full 1–6000 MHz sweep can take time and congest USB." })),
  );
  paneScan.appendChild(panel({
    title: "Wideband spectrum",
    sub: "hackrf_sweep / rtl_power · live NDJSON",
    icon: "signal",
    cls: "span-2",
    body: spectrumBody,
  }));
  paneScan.appendChild(presetsPanel);
  paneSystem.appendChild(hardwarePanel);

  /* ------------------------------------------------------- live receiver */
  const liveCenterStart = (Number(paramFreq) || 100) * 1e6;
  const liveModes = [["am","AM"],["nfm","NFM"],["wfm","WFM"],["usb","USB"],["lsb","LSB"]];
  let liveCenter = liveCenterStart;
  let liveStep = 1000;
  let liveModeValue = DEMODS.some((d) => d.value === paramMode)
    && ["nfm","wfm","am","usb","lsb"].includes(paramMode) ? paramMode : "nfm";
  let liveBackendValue = String(store.settings?.sdr_backend || "auto");
  let liveDirectMode = null;
  let liveFatal = false;
  let liveAudioRate = 48000;
  let liveRunning = false;
  let liveWs = null;
  let liveCtx = null;
  let liveGainNode = null;
  let liveNext = 0;
  let liveSources = [];
  let liveHistory = [];
  let liveBounds = { min: -120, max: -10 };
  let liveSpec = null;
  let liveRecording = false;
  let liveRecChunks = [];
  let liveRecBytes = 0;
  let liveRecStarted = 0;
  let liveRecTimer = null;
  let liveScanActive = false;
  let liveScanTimer = null;

  const liveVfo = h("div", { class: "sdr-vfo-digits" });
  const liveSub = h("div", { class: "sdr-vfo-sub" });
  const liveBackendSel = select([
    { value: "auto", label: "AUTO" },
    { value: "hackrf", label: "HACKRF" },
    { value: "rtl", label: "RTL-SDR" },
    { value: "rtl_tcp", label: "NETWORK" },
  ], liveBackendValue);
  liveBackendSel.classList.add("sdr-inline-select");
  const liveDirect = select(directOptions(), directValue(store.settings));
  liveDirect.classList.add("sdr-inline-select");
  const liveRateIn = input({ type: "number", value: String(store.settings?.sdr_live_rate ?? 1.024), step: "0.025", min: "0.25", max: "3.2" });
  liveRateIn.classList.add("sdr-small-input");
  const liveGainIn = input({ type: "number", value: String(store.settings?.sdr_rtl_gain ?? 40), min: "0", max: "62" });
  liveGainIn.classList.add("sdr-small-input");
  const liveSquelchIn = input({ type: "number", value: String(store.settings?.sdr_live_squelch ?? 0), min: "0", max: "100" });
  liveSquelchIn.classList.add("sdr-small-input");
  const liveVolumeIn = input({ type: "range", value: "0.8", min: "0", max: "1", step: "0.01" });
  const liveStepSel = select([
    { value: "1", label: "1 Hz" }, { value: "10", label: "10 Hz" },
    { value: "100", label: "100 Hz" }, { value: "1000", label: "1 kHz" },
    { value: "5000", label: "5 kHz" }, { value: "12500", label: "12.5 kHz" },
    { value: "25000", label: "25 kHz" }, { value: "100000", label: "100 kHz" },
  ], "1000");
  liveStepSel.classList.add("sdr-inline-select");
  const livePlay = btn("PLAY", { tone: "primary", icon: "play" });
  const liveStopBtn = btn("STOP", { icon: "stop", tone: "danger", disabled: true });
  const liveRecBtn = btn("REC", { icon: "mic", tone: "ghost" });
  const liveFull = btn("FULL", { icon: "maximize", tone: "ghost" });
  const liveScanBtn = btn("SCAN", { icon: "search", tone: "ghost" });
  const liveStatus = h("span", { class: "tiny muted sdr-live-status", text: "IDLE" });
  const liveWarning = h("div", { class: "callout warn small mt-8 sdr-live-warning", style: "display:none" });
  const liveModeRow = h("div", { class: "sdr-mode-row" });
  const liveBandRow = h("div", { class: "sdr-band-row" });
  const liveSpectrum = h("canvas", { class: "sdr-live-spectrum" });
  const liveWaterfall = h("canvas", { class: "sdr-live-waterfall" });
  const liveGrid = h("div", { class: "sdr-live-grid" }, liveSpectrum, liveWaterfall);
  const liveSMeter = h("i");
  const liveSBox = h("div", { class: "sdr-smeter" }, liveSMeter);
  const liveReadout = h("div", { class: "sdr-live-readout" });

  function formatVfo(hz) {
    const mhz = (Number(hz) || 0) / 1e6;
    const text = mhz.toFixed(6);
    const [whole, frac] = text.split(".");
    return `${whole}.${frac.slice(0, 3)} ${frac.slice(3)}`;
  }
  function directLabel() {
    const chosen = liveDirect.value === "2" ? "HF Q" : liveDirect.value === "0" ? "TUNER" : "AUTO";
    if (liveDirectMode == null) return chosen;
    const effective = liveDirectMode ? "HF Q" : "TUNER";
    if (liveDirect.value === "auto") return `AUTO · ${effective}`;
    return chosen === effective ? chosen : `${chosen} → ${effective}`;
  }
  function paintVfo() {
    liveVfo.textContent = formatVfo(liveCenter);
    liveSub.textContent = `${backendName(liveBackendValue)} · ${liveModeValue.toUpperCase()} · ${directLabel()} · step ${fmt.num(liveStep, 0)} Hz`;
  }
  function paintModeButtons() {
    for (const button of liveModeRow.querySelectorAll("button")) {
      button.classList.toggle("active", button.dataset.mode === liveModeValue);
    }
  }
  function setMode(mode) {
    liveModeValue = mode;
    if (["wfm","nfm"].includes(mode)) {
      liveRateIn.value = mode === "wfm" ? "2.4" : "1.024";
    }
    paintModeButtons();
    paintVfo();
    liveSend({ cmd: "tune", freq_mhz: liveCenter / 1e6, mode: liveModeValue,
      rate: Number(liveRateIn.value) || 1.024,
      squelch: Number(liveSquelchIn.value) || 0, gain: Number(liveGainIn.value) || 40 });
  }
  for (const [value, label] of liveModes) {
    const button = h("button", { class: "sdr-mode-btn", "data-mode": value, text: label,
      onClick: () => setMode(value) });
    liveModeRow.appendChild(button);
  }
  const bands = [
    ["160m", 1_900_000, "lsb"], ["80m", 3_700_000, "lsb"], ["40m", 7_100_000, "lsb"],
    ["20m", 14_200_000, "usb"], ["15m", 21_200_000, "usb"], ["10m", 28_500_000, "usb"],
    ["6m", 50_150_000, "usb"], ["FM", 100_100_000, "wfm"], ["AIR", 121_500_000, "am"],
    ["MAR", 156_800_000, "nfm"], ["2m", 145_500_000, "nfm"], ["70cm", 433_500_000, "nfm"],
  ];
  for (const [label, hz, mode] of bands) {
    liveBandRow.appendChild(h("button", { class: "sdr-band-btn", text: label, onClick: () => {
      liveCenter = hz;
      setMode(mode);
      paintVfo();
    } }));
  }

  function liveSend(payload) {
    if (liveWs && liveWs.readyState === WebSocket.OPEN) {
      try { liveWs.send(JSON.stringify(payload)); } catch {}
    }
  }
  function tuneBy(delta) {
    liveCenter = Math.max(100_000, Math.min(6_000_000_000, liveCenter + delta));
    paintVfo();
    liveSend({ cmd: "tune", freq_mhz: liveCenter / 1e6, mode: liveModeValue,
      squelch: Number(liveSquelchIn.value) || 0, gain: Number(liveGainIn.value) || 40 });
  }
  function tuneTo(hz) {
    liveCenter = Math.max(100_000, Math.min(6_000_000_000, hz));
    paintVfo();
    liveSend({ cmd: "tune", freq_mhz: liveCenter / 1e6, mode: liveModeValue,
      squelch: Number(liveSquelchIn.value) || 0, gain: Number(liveGainIn.value) || 40 });
  }

  function openTuneDialog() {
    const value = input({ type: "number", value: (liveCenter / 1e6).toFixed(6),
      step: "0.000001", min: "0.1", max: "6000" });
    const m = modal({
      title: "Tune receiver",
      sub: "Enter a frequency in MHz",
      narrow: true,
      body: field("FREQUENCY (MHz)", value),
      actions: [
        btn("CANCEL", { tone: "ghost", onClick: () => m.close() }),
        btn("TUNE", { tone: "primary", icon: "target", onClick: () => {
          const hz = Number(value.value) * 1e6;
          if (!(hz >= 100_000 && hz <= 6_000_000_000)) { toast("Frequency must be 0.1–6000 MHz", "warn"); return; }
          tuneTo(hz);
          m.close();
        } }),
      ],
    });
    setTimeout(() => { try { value.focus(); value.select(); } catch {} }, 30);
  }
  function stopScan(message = "") {
    liveScanActive = false;
    if (liveScanTimer) { clearTimeout(liveScanTimer); liveScanTimer = null; }
    liveScanBtn.replaceChildren(h("span", { text: "SCAN" }));
    if (message) toast(message, "warn", 5000);
  }
  function startScan() {
    if (liveScanActive) { stopScan(); return; }
    if (!liveRunning) { toast("Press PLAY before scanning", "warn"); return; }
    liveScanActive = true;
    liveScanBtn.replaceChildren(h("span", { text: "STOP SCAN" }));
    let steps = 0;
    const step = Math.max(1000, liveStep);
    const threshold = Number(liveSquelchIn.value) > 0 ? -75 : -80;
    const tick = () => {
      if (!liveScanActive) return;
      if (steps++ > 600 || liveCenter > 6_000_000_000) {
        stopScan("Scan finished: no signal above threshold");
        return;
      }
      tuneTo(liveCenter + step);
      liveScanTimer = setTimeout(() => {
        if (!liveScanActive) return;
        const peak = Number(liveSpec?.peak_db);
        if (Number.isFinite(peak) && peak > threshold) {
          stopScan(`Signal found at ${fmt.num(liveCenter / 1e6, 5)} MHz (${fmt.num(peak, 1)} dB)`);
        } else {
          tick();
        }
      }, 420);
    };
    tick();
  }

  function schedulePcm(arrayBuffer) {
    if (!liveCtx || !liveGainNode) return;
    const int16 = new Int16Array(arrayBuffer);
    if (!int16.length) return;
    const buffer = liveCtx.createBuffer(1, int16.length, liveAudioRate);
    const channel = buffer.getChannelData(0);
    let sum = 0;
    for (let i = 0; i < int16.length; i++) {
      const v = int16[i] / 32768;
      channel[i] = v;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / Math.max(1, int16.length));
    liveSMeter.style.width = `${Math.min(100, rms * 420)}%`;
    if (liveRecording) {
      liveRecChunks.push(new Int16Array(int16));
      liveRecBytes += int16.length * 2;
      if (liveRecBytes > 120 * 1024 * 1024) stopRecording();
    }
    const src = liveCtx.createBufferSource();
    src.buffer = buffer;
    src.connect(liveGainNode);
    const now = liveCtx.currentTime;
    if (liveNext < now + 0.05) liveNext = now + 0.05;
    if (liveNext > now + 0.7) liveNext = now + 0.1;
    src.start(liveNext);
    liveNext += buffer.duration;
    liveSources.push(src);
    src.onended = () => { liveSources = liveSources.filter((item) => item !== src); };
  }

  function drawLiveSpectrum() {
    const rect = liveSpectrum.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(280, rect.width || 900);
    const hh = Math.max(140, rect.height || 240);
    liveSpectrum.width = Math.round(w * dpr);
    liveSpectrum.height = Math.round(hh * dpr);
    const ctx = liveSpectrum.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, hh);
    ctx.fillStyle = "#020604";
    ctx.fillRect(0, 0, w, hh);
    ctx.strokeStyle = `rgba(${accentRgb()},.09)`;
    for (let i = 1; i < 10; i++) {
      ctx.beginPath(); ctx.moveTo((w / 10) * i, 0); ctx.lineTo((w / 10) * i, hh); ctx.stroke();
    }
    for (let i = 1; i < 5; i++) {
      const y = (hh / 5) * i;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }
    if (!liveSpec || !liveSpec.bins?.length) {
      ctx.fillStyle = "rgba(123,154,136,.72)";
      ctx.font = "10px 'JetBrains Mono', monospace";
      ctx.fillText("PRESS PLAY TO START THE RECEIVER", 14, hh / 2);
      return;
    }
    const bins = liveSpec.bins;
    const lo = liveBounds.min, hi = liveBounds.max;
    ctx.beginPath();
    for (let i = 0; i < bins.length; i++) {
      const x = 4 + (i / Math.max(1, bins.length - 1)) * (w - 8);
      const y = hh - 18 - ((bins[i] - lo) / Math.max(0.001, hi - lo)) * (hh - 30);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.lineTo(w - 4, hh - 18); ctx.lineTo(4, hh - 18); ctx.closePath();
    const grad = ctx.createLinearGradient(0, 0, 0, hh);
    grad.addColorStop(0, `rgba(${accentRgb()},.24)`);
    grad.addColorStop(1, `rgba(${accentRgb()},.015)`);
    ctx.fillStyle = grad; ctx.fill();
    ctx.beginPath();
    for (let i = 0; i < bins.length; i++) {
      const x = 4 + (i / Math.max(1, bins.length - 1)) * (w - 8);
      const y = hh - 18 - ((bins[i] - lo) / Math.max(0.001, hi - lo)) * (hh - 30);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = accentColor(); ctx.lineWidth = 1.3; ctx.stroke();
    if (liveSpec.peak_hz) {
      const px = 4 + ((liveSpec.peak_hz - liveSpec.start_hz) / Math.max(1, liveSpec.end_hz - liveSpec.start_hz)) * (w - 8);
      ctx.strokeStyle = `rgba(${cssRgb("--amber-rgb","234,180,95")},.8)`;
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, hh - 18); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = cssColor("--amber", "#eab45f");
      ctx.font = "9px 'JetBrains Mono', monospace";
      ctx.fillText(`${fmt.num(liveSpec.peak_hz / 1e6, 4)}`, Math.min(w - 60, px + 4), 12);
    }
    ctx.fillStyle = "rgba(205,236,218,.78)";
    ctx.font = "9px 'JetBrains Mono', monospace";
    ctx.fillText(`${fmt.num(liveSpec.start_hz / 1e6, 3)} MHz`, 6, hh - 5);
    ctx.textAlign = "center";
    ctx.fillText(`${fmt.num(liveSpec.center_hz / 1e6, 4)}`, w / 2, hh - 5);
    ctx.textAlign = "right";
    ctx.fillText(`${fmt.num(liveSpec.end_hz / 1e6, 3)} MHz`, w - 6, hh - 5);
    ctx.textAlign = "left";
    ctx.fillStyle = "rgba(123,154,136,.8)";
    ctx.fillText(`${Math.round(hi)} dB`, 6, 12);
  }

  function drawLiveWaterfall() {
    const width = 512;
    if (!liveHistory.length) {
      const ctx0 = liveWaterfall.getContext("2d");
      liveWaterfall.width = width; liveWaterfall.height = 60;
      ctx0.fillStyle = "#020604"; ctx0.fillRect(0, 0, width, 60);
      return;
    }
    const rows = liveHistory.length;
    liveWaterfall.width = width;
    liveWaterfall.height = Math.max(60, rows);
    const ctx = liveWaterfall.getContext("2d");
    const img = ctx.createImageData(width, liveWaterfall.height);
    const lo = liveBounds.min, hi = liveBounds.max;
    for (let row = 0; row < rows; row++) {
      const frame = liveHistory[row];
      const y = rows - 1 - row;
      for (let x = 0; x < width; x++) {
        const idx = Math.min(frame.length - 1, Math.floor((x / width) * frame.length));
        let t = (frame[idx] - lo) / Math.max(0.001, hi - lo);
        t = Math.max(0, Math.min(1, t));
        let r, g, b;
        if (t < .25) { const k = t / .25; r = 2 + 4 * k; g = 10 + 30 * k; b = 18 + 44 * k; }
        else if (t < .5) { const k = (t - .25) / .25; r = 6 + 20 * k; g = 40 + 100 * k; b = 62 + 46 * k; }
        else if (t < .75) { const k = (t - .5) / .25; r = 26 + 110 * k; g = 140 + 80 * k; b = 108 - 40 * k; }
        else { const k = (t - .75) / .25; r = 136 + 119 * k; g = 220 + 35 * k; b = 68 + 120 * k; }
        const p = (y * width + x) * 4;
        img.data[p] = r; img.data[p + 1] = g; img.data[p + 2] = b; img.data[p + 3] = 255;
      }
    }
    ctx.putImageData(img, 0, 0);
  }

  function acceptLiveSpectrum(frame) {
    liveSpec = frame;
    const values = frame.bins || [];
    if (values.length) {
      const lo = Math.min(...values), hi = Math.max(...values);
      liveBounds.min = liveBounds.min * .9 + (lo - 6) * .1;
      liveBounds.max = liveBounds.max * .9 + (hi + 6) * .1;
      liveHistory.push(values);
      if (liveHistory.length > 110) liveHistory.shift();
      drawLiveWaterfall();
    }
    liveReadout.textContent = `${fmt.num(frame.center_hz / 1e6, 4)} MHz · span ${fmt.num((frame.end_hz - frame.start_hz) / 1e6, 3)} MHz · peak ${fmt.num(frame.peak_hz / 1e6, 4)} MHz / ${fmt.num(frame.peak_db, 1)} dB`;
    drawLiveSpectrum();
  }

  function makeWav(chunks, rate) {
    let total = 0;
    for (const c of chunks) total += c.length;
    const buffer = new ArrayBuffer(44 + total * 2);
    const view = new DataView(buffer);
    const write = (offset, text) => { for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i)); };
    write(0, "RIFF"); view.setUint32(4, 36 + total * 2, true); write(8, "WAVE");
    write(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    write(36, "data"); view.setUint32(40, total * 2, true);
    let offset = 44;
    for (const chunk of chunks) {
      for (let i = 0; i < chunk.length; i++) { view.setInt16(offset, chunk[i], true); offset += 2; }
    }
    return URL.createObjectURL(new Blob([buffer], { type: "audio/wav" }));
  }

  function startRecording() {
    liveRecording = true;
    liveRecChunks = [];
    liveRecBytes = 0;
    liveRecStarted = Date.now();
    liveRecBtn.classList.add("recording");
    liveRecBtn.replaceChildren(h("span", { text: "REC ●" }));
    liveRecTimer = setInterval(() => {
      const seconds = Math.floor((Date.now() - liveRecStarted) / 1000);
      liveStatus.textContent = `LIVE · REC ${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
    }, 1000);
  }
  function stopRecording() {
    if (!liveRecording) return;
    liveRecording = false;
    clearInterval(liveRecTimer);
    liveRecTimer = null;
    liveRecBtn.classList.remove("recording");
    liveRecBtn.replaceChildren(h("span", { text: "REC" }));
    if (!liveRecChunks.length) return;
    const url = makeWav(liveRecChunks, liveAudioRate);
    const a = h("a", { href: url, download: `darksec-${Date.now()}.wav` });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 8000);
    toast("Recording saved as WAV");
    liveStatus.textContent = "LIVE";
  }

  function startLive() {
    if (liveRunning) return;
    const AudioCtor = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtor) { toast("This browser has no Web Audio support", "error"); return; }
    try { liveCtx = new AudioCtor(); } catch (err) { toast(`Audio unavailable: ${err.message || err}`, "error"); return; }
    liveGainNode = liveCtx.createGain();
    liveGainNode.gain.value = Number(liveVolumeIn.value) || 0.8;
    liveGainNode.connect(liveCtx.destination);
    liveCtx.resume().catch(() => {});
    liveHistory = [];
    liveSpec = null;
    liveBounds = { min: -120, max: -10 };
    liveDirectMode = null;
    liveFatal = false;
    liveWarning.style.display = "none";
    liveWarning.textContent = "";
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    const qs = new URLSearchParams({
      backend: liveBackendValue, freq_mhz: String(liveCenter / 1e6), mode: liveModeValue,
      rate: String(Number(liveRateIn.value) || 1.024),
      gain: String(Number(liveGainIn.value) || 40),
      squelch: String(Number(liveSquelchIn.value) || 0),
      host: String(netHost?.value || "").trim(), port: String(Number(netPort?.value) || 1234),
      direct: liveDirect.value,
    });
    liveWs = new WebSocket(`${scheme}://${location.host}/api/sdr/ws?${qs}`);
    liveWs.binaryType = "arraybuffer";
    liveRunning = true;
    livePlay.disabled = true;
    liveStopBtn.disabled = false;
    livePlay.replaceChildren(h("span", { text: "LIVE" }));
    liveStatus.textContent = "CONNECTING…";
    liveWs.onopen = () => { liveStatus.textContent = "LIVE · waiting for audio"; };
    liveWs.onmessage = (event) => {
      if (typeof event.data === "string") {
        let frame;
        try { frame = JSON.parse(event.data); } catch { return; }
        if (frame.type === "status") {
          liveAudioRate = Number(frame.audio_rate) || 48000;
          liveBackendValue = frame.backend || liveBackendValue;
          if (frame.direct_mode !== undefined && frame.direct_mode !== null) {
            liveDirectMode = Number(frame.direct_mode) || 0;
          }
          if (frame.status === "retuning") {
            liveStatus.textContent = "TUNING…";
          } else if (frame.status === "reconnecting") {
            liveStatus.textContent = "RECONNECTING…";
          } else if (frame.status === "stopped") {
            stopLive();
            liveStatus.textContent = liveFatal ? "STOPPED · receiver error" : "STOPPED";
          } else {
            liveStatus.textContent = `LIVE · ${backendName(liveBackendValue)} · ${fmt.num(frame.rate / 1e6, 3)} Msps`;
          }
          if (frame.warning) {
            liveWarning.textContent = frame.warning;
            liveWarning.style.display = "";
          } else if (!liveFatal) {
            liveWarning.style.display = "none";
          }
          paintVfo();
        } else if (frame.type === "spectrum") {
          acceptLiveSpectrum(frame);
        } else if (frame.type === "error") {
          liveFatal = !!frame.fatal;
          const shortError = String(frame.error || "SDR error");
          liveStatus.textContent = "ERROR";
          liveWarning.textContent = shortError + (frame.fatal ? " Open DIAGNOSE for the full report." : "");
          liveWarning.style.display = "";
          toast(frame.error || "SDR error", "error");
        }
      } else {
        schedulePcm(event.data);
      }
    };
    liveWs.onerror = () => { liveStatus.textContent = "CONNECTION ERROR"; };
    liveWs.onclose = () => {
      liveRunning = false;
      livePlay.disabled = false;
      liveStopBtn.disabled = true;
      livePlay.replaceChildren(h("span", { text: "PLAY" }));
      if (liveStatus.textContent.startsWith("LIVE")) liveStatus.textContent = "DISCONNECTED";
      liveWs = null;
    };
  }
  function stopLive() {
    if (liveWs) { try { liveWs.close(); } catch {} liveWs = null; }
    for (const src of liveSources) { try { src.stop(); } catch {} }
    liveSources = [];
    if (liveCtx) { try { liveCtx.close(); } catch {} liveCtx = null; }
    liveGainNode = null;
    liveNext = 0;
    liveRunning = false;
    livePlay.disabled = false;
    liveStopBtn.disabled = true;
    livePlay.replaceChildren(h("span", { text: "PLAY" }));
    liveStatus.textContent = "IDLE";
    liveSMeter.style.width = "0%";
    stopScan();
    stopRecording();
  }

  liveVfo.addEventListener("click", openTuneDialog);
  liveVfo.style.cursor = "pointer";
  livePlay.addEventListener("click", startLive);
  liveStopBtn.addEventListener("click", stopLive);
  liveScanBtn.addEventListener("click", startScan);
  liveRecBtn.addEventListener("click", () => { liveRecording ? stopRecording() : startRecording(); });
  liveBackendSel.addEventListener("change", () => {
    liveBackendValue = liveBackendSel.value;
    if (liveRunning) { stopLive(); setTimeout(startLive, 250); }
    paintVfo();
  });
  liveDirect.addEventListener("change", () => {
    liveDirectMode = null;
    paintVfo();
    if (liveRunning) { stopLive(); setTimeout(startLive, 250); }
    toast(liveDirect.value === "auto"
      ? "AUTO: HF direct-sampling Q below 24 MHz, R820T tuner at 24 MHz and above"
      : liveDirect.value === "2"
        ? "HF direct sampling Q selected — 0.1–24 MHz with an HF antenna on the Q branch"
        : "Tuner mode selected — 24 MHz and above");
  });
  liveStepSel.addEventListener("change", () => { liveStep = Number(liveStepSel.value) || 1000; paintVfo(); });
  liveVolumeIn.addEventListener("input", () => {
    if (liveGainNode) { try { liveGainNode.gain.value = Number(liveVolumeIn.value) || 0.8; } catch {} }
  });
  liveSquelchIn.addEventListener("change", () => liveSend({ cmd: "set", squelch: Number(liveSquelchIn.value) || 0 }));
  liveGainIn.addEventListener("change", () => { if (liveRunning) { stopLive(); setTimeout(startLive, 250); } });
  liveRateIn.addEventListener("change", () => { if (liveRunning) { stopLive(); setTimeout(startLive, 250); } });
  liveFull.addEventListener("click", () => {
    const target = liveSpectrum.closest(".panel") || liveSpectrum;
    target.requestFullscreen?.().catch(() => {});
  });
  liveSpectrum.addEventListener("click", (event) => {
    if (!liveSpec) return;
    const rect = liveSpectrum.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width)));
    tuneTo(liveSpec.start_hz + ratio * (liveSpec.end_hz - liveSpec.start_hz));
  });
  liveSpectrum.addEventListener("wheel", (event) => {
    event.preventDefault();
    tuneBy(event.deltaY < 0 ? liveStep : -liveStep);
  }, { passive: false });
  function liveKeyHandler(event) {
    if (["INPUT","SELECT","TEXTAREA"].includes(document.activeElement?.tagName)) return;
    if (!liveRunning) return;
    if (event.key === "ArrowUp" || event.key === "ArrowRight") { event.preventDefault(); tuneBy(liveStep); }
    else if (event.key === "ArrowDown" || event.key === "ArrowLeft") { event.preventDefault(); tuneBy(-liveStep); }
    else if (event.key.toLowerCase() === "m") {
      const idx = liveModes.findIndex(([value]) => value === liveModeValue);
      setMode(liveModes[(idx + 1) % liveModes.length][0]);
    } else if (event.code === "Space") { event.preventDefault(); stopLive(); }
  }
  window.addEventListener("keydown", liveKeyHandler);
  paintModeButtons();
  paintVfo();
  setTimeout(() => { drawLiveSpectrum(); drawLiveWaterfall(); }, 40);

  const liveBody = h("div", { class: "sdr-cockpit" },
    h("div", { class: "sdr-vfo-row" },
      h("div", { class: "sdr-vfo-main" },
        liveVfo,
        liveSub,
        h("div", { class: "sdr-vfo-actions" },
          btn("◀", { size: "sm", tone: "ghost", onClick: () => tuneBy(-liveStep) }),
          btn("▶", { size: "sm", tone: "ghost", onClick: () => tuneBy(liveStep) }),
          liveStepSel,
          liveBackendSel,
          liveDirect)),
      h("div", { class: "sdr-vfo-side" },
        h("div", { class: "row between" }, h("span", { class: "tiny muted", text: "S-METER" }), liveStatus),
        liveSBox,
        h("div", { class: "row wrap gap-6 mt-6" }, livePlay, liveStopBtn, liveScanBtn, liveRecBtn, liveFull))),
    liveModeRow,
    liveBandRow,
    liveGrid,
    liveReadout,
    liveWarning,
    h("div", { class: "sdr-cockpit-controls" },
      field("RATE (Msps)", liveRateIn),
      field("RTL GAIN", liveGainIn),
      field("SQUELCH", liveSquelchIn),
      h("div", { class: "field" }, h("label", { text: "VOLUME" }), liveVolumeIn),
      h("div", { class: "field" }, h("label", { text: "DEFAULTS" }),
        btn("SAVE", { size: "sm", tone: "ghost", icon: "check", onClick: async () => {
          try {
            await store.saveSettings({
              sdr_backend: liveBackendSel.value,
              sdr_ppm: Number(sweepPpm.value) || 0,
              sdr_rtl_gain: Number(liveGainIn.value) || 40,
              sdr_live_rate: Number(liveRateIn.value) || 1.024,
              sdr_live_squelch: Number(liveSquelchIn.value) || 0,
              sdr_server_port: Number(srvPort.value) || 1234,
              sdr_server_bind: srvBind.value.trim() || "0.0.0.0",
              sdr_direct_sampling: liveDirect.value,
            });
            toast("SDR defaults saved");
          } catch (err) { toast(err.message, "error"); }
        } }))),
    h("div", { class: "sdr-keyhint tiny muted" },
      "CLICK FREQUENCY TO TYPE · CLICK SPECTRUM TO TUNE · WHEEL = STEP · ↑/↓ OR ←/→ = STEP · M = MODE · SPACE = STOP"),
    h("div", { class: "tiny muted" },
      "AUTO selects HF Q below 24 MHz and the R820T tuner at 24 MHz and above. HF Q needs an HF antenna on the Q branch; AIO V2 uses that path."),
  );
  paneReceiver.appendChild(panel({
    title: "Receiver",
    sub: "Spectrum + waterfall + audio · click to tune",
    icon: "signal",
    cls: "span-2 sdr-live-panel",
    body: liveBody,
  }));

  /* -------------------------------------------------------------- capture */
  const captureBody = h("div", { class: "col" });
  const capFreq = input({ type: "number", value: paramFreq ? String(paramFreq) : "146.520", step: "0.0001", min: "0.1", max: "6000" });
  const capSeconds = input({ type: "number", value: "5", step: "0.5", min: "0.2", max: "180" });
  const capRate = input({ type: "number", value: "2.4", step: "0.05", min: "0.9", max: "20" });
  const capBackend = select([
    { value: "auto", label: "Auto" },
    { value: "hackrf", label: "HackRF (2–20 Msps)" },
    { value: "rtl", label: "RTL-SDR (0.9–3.2 Msps)" },
    { value: "rtl_tcp", label: "Network rtl_tcp" },
  ], String(store.settings?.sdr_backend || "auto"));
  const capDirect = select(directOptions(), directValue(store.settings));
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
    field("HF MODE", capDirect, "Tuner for 24 MHz+, HF Q for shortwave"),
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
  paneCapture.appendChild(panel({
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
  paneNetwork.appendChild(panel({ title: "Network SDR", sub: "rtl_tcp client", icon: "network", body: netBody }));

  const srvPort = input({ type: "number", value: String(store.settings?.sdr_server_port || 1234), min: "1", max: "65535" });
  const srvBind = input({ type: "text", value: store.settings?.sdr_server_bind || "0.0.0.0" });
  const srvFreq = input({ type: "number", value: "100.0", step: "0.0001", min: "0.1", max: "6000" });
  const srvRate = input({ type: "number", value: "2.4", step: "0.05", min: "0.9", max: "3.2" });
  const srvGain = input({ type: "number", value: String(store.settings?.sdr_rtl_gain ?? 40), min: "0", max: "62" });
  const srvDirect = select(directOptions(), directValue(store.settings));
  const srvStatus = h("div", { class: "tiny muted", text: "Not running." });
  const srvStart = btn("SHARE RTL-SDR", { size: "sm", tone: "primary", icon: "play", onClick: async () => {
    try {
      const out = await api.post("/api/sdr/server/start", {
        bind: srvBind.value.trim() || "0.0.0.0", port: Number(srvPort.value) || 1234,
        freq_hz: Number(srvFreq.value) * 1e6, rate: Number(srvRate.value) * 1e6,
        gain: Number(srvGain.value) || 40, ppm: Number(store.settings?.sdr_ppm) || 0,
        bias: !!store.settings?.sdr_bias_tee,
        direct: srvDirect.value,
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
      field("GAIN", srvGain),
      field("HF MODE", srvDirect)),
    h("div", { class: "row wrap mt-8" }, srvStart, srvStop),
    srvStatus,
    h("div", { class: "tiny muted mt-6", text: "rtl_tcp has no authentication. Use only a trusted private LAN. Another DarkSec instance can connect from the Network SDR panel." }),
  );
  paneNetwork.appendChild(panel({ title: "Share this SDR", sub: "rtl_tcp server", icon: "upload", body: srvBody }));

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
    let tuned;
    try {
      const out = await api.get(`/api/sdr/websdr/tune?url=${encodeURIComponent(rec.url)}&freq_hz=${encodeURIComponent(freq * 1e6)}&mode=${encodeURIComponent(wsMode.value)}`);
      tuned = out.url;
    } catch (err) {
      toast(err.message || "Could not build receiver URL", "error");
      return;
    }
    const mixed = location.protocol === "https:" && tuned.startsWith("http:");
    const frame = h("iframe", {
      src: tuned, title: rec.name || "WebSDR receiver",
      class: "sdr-websdr-frame", allow: "autoplay; fullscreen; microphone",
      referrerpolicy: "no-referrer",
    });
    const m = modal({
      title: rec.name || "WebSDR receiver",
      sub: `${rec.loc || "public receiver"} · ${rec.band || ""} · ${tuned}`,
      wide: true, flush: true,
      body: h("div", { class: "sdr-websdr-body" },
        mixed ? h("div", { class: "callout warn small", text: "This receiver is HTTP and DarkSec is HTTPS. The browser may block the embedded view; use OPEN EXTERNALLY if it stays blank." }) : null,
        frame),
      actions: [
        btn("OPEN EXTERNALLY", { tone: "ghost", icon: "upload", onClick: () => window.open(tuned, "_blank", "noopener,noreferrer") }),
        btn("CLOSE", { tone: "ghost", onClick: () => m.close() }),
      ],
      onClose: () => { try { frame.src = "about:blank"; } catch {} },
    });
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
  paneWeb.appendChild(panel({ title: "WebSDR / remote receivers", sub: "No local radio required · opens in-app", icon: "globe", body: wsBody }));
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
        direct: capDirect.value,
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
  paneCapture.appendChild(recPanel);
  const lower = paneSystem;

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
  const adsb0 = adsb.status || {};
  const adsbPort = input({ type: "number", value: String(adsb0.port || 8081), min: "1", max: "65535" });
  const adsbGain = input({ type: "number", value: String(adsb0.gain ?? 40), min: "0", max: "62" });
  const adsbDevice = input({ type: "number", value: String(adsb0.device ?? 0), min: "0", max: "8" });
  const adsbStatus = h("div", { class: "tiny muted", text: "Checking ADS-B manager…" });
  const adsbTable = h("div", { class: "col" });
  const adsbStart = btn("START DUMP1090", { tone: "primary", icon: "play" });
  const adsbStopBtn = btn("STOP", { icon: "stop", tone: "danger" });
  let adsbPoll = null;

  function paintAdsb(data) {
    const status = data?.status || {};
    const running = !!status.running;
    adsbStatus.className = `tiny ${status.warning ? "warn-text" : status.available ? "muted" : "danger-text"}`;
    if (status.port && document.activeElement !== adsbPort) adsbPort.value = String(status.port);
    const mode = status.managed ? "MANAGED" : status.external ? "EXTERNAL FEED" : running ? "RUNNING" : "STOPPED";
    const binaryLabel = status.binary ? PathlessName(status.binary)
      : status.external ? "external feed" : "dump1090/readsb not installed";
    const feedLabel = status.managed && status.json_dir
      ? "local JSON" : `${status.host || "127.0.0.1"}:${status.port || adsbPort.value}`;
    adsbStatus.textContent = `${mode} · ${binaryLabel} · ${feedLabel} · ${status.aircraft_count ?? (data?.aircraft || []).length} aircraft`
      + (status.warning ? ` · ${status.warning}` : "");
    adsbStart.disabled = running;
    adsbStopBtn.disabled = !status.managed;
    adsbTable.replaceChildren();
    const aircraft = data?.aircraft || [];
    if (!status.available) {
      adsbTable.appendChild(emptyState("ADS-B feed unavailable", status.note || "Start dump1090 or point DarkSec at an existing feed.", "satellite"));
      return;
    }
    if (!aircraft.length) {
      adsbTable.appendChild(emptyState("No aircraft heard", "dump1090 is reachable; waiting for ADS-B messages.", "satellite"));
      return;
    }
    const table = h("table", { class: "data" });
    table.innerHTML = "<thead><tr><th>FLIGHT</th><th>HEX</th><th>ALT</th><th>GS</th><th>TRK</th></tr></thead>";
    const tbody = h("tbody");
    for (const a of aircraft) {
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
    adsbTable.appendChild(h("div", { class: "scroll-x" }, table));
  }
  function PathlessName(path) {
    return String(path || "").split("/").pop() || "receiver";
  }
  async function loadAdsb() {
    try {
      const data = await api.get("/api/sdr/adsb");
      paintAdsb(data);
      if (data?.status?.running && !adsbPoll) {
        adsbPoll = setInterval(loadAdsb, 5000);
      }
      if (!data?.status?.running && adsbPoll) {
        clearInterval(adsbPoll); adsbPoll = null;
      }
    } catch (err) { adsbStatus.textContent = err.message; }
  }
  async function startAdsb() {
    adsbStart.disabled = true;
    try {
      const settings = {
        adsb_port: Number(adsbPort.value) || 8081,
        adsb_gain: Number(adsbGain.value) || 40,
        adsb_device: Number(adsbDevice.value) || 0,
      };
      try { await store.saveSettings(settings); } catch {}
      const out = await api.post("/api/sdr/adsb/start", {
        port: settings.adsb_port,
        gain: settings.adsb_gain,
        device: settings.adsb_device,
      });
      if (out?.port && Number(adsbPort.value) !== Number(out.port)) {
        adsbPort.value = String(out.port);
        try { await store.saveSettings({ adsb_port: out.port }); } catch {}
      }
      toast(out?.external ? "Existing ADS-B feed detected; showing it"
        : out?.warning || "dump1090 started");
      await loadAdsb();
    } catch (err) {
      toast(err.message || "Could not start dump1090", "error", 9000);
      await loadAdsb();
    } finally {
      adsbStart.disabled = false;
    }
  }
  async function stopAdsb() {
    try {
      await api.post("/api/sdr/adsb/stop", {});
      toast("dump1090 stopped");
      await loadAdsb();
    } catch (err) { toast(err.message, "error"); }
  }
  adsbStart.addEventListener("click", startAdsb);
  adsbStopBtn.addEventListener("click", stopAdsb);
  paintAdsb(adsb);
  if (adsb?.status?.running) adsbPoll = setInterval(loadAdsb, 5000);

  const adsbBody = h("div", { class: "col" },
    h("div", { class: "form-grid" },
      field("EXTERNAL PORT", adsbPort, "Optional feed port. Managed dump1090 uses local JSON, so it cannot conflict with the LAN server."),
      field("GAIN", adsbGain),
      field("DEVICE", adsbDevice)),
    h("div", { class: "row wrap mt-8" }, adsbStart, adsbStopBtn),
    adsbStatus,
    h("div", { class: "mt-8" }, adsbTable),
    h("div", { class: "hint mt-10",
      text: "ADS-B is receive-only. Stop the live receiver and rtl_tcp before starting dump1090 — one RTL-SDR can only serve one program at a time." }),
  );
  paneAdsb.appendChild(panel({ title: "ADS-B / air picture", sub: "dump1090 / readsb · managed locally", icon: "satellite", body: adsbBody }));

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
  /* ------------------------------------------------------------ diagnose */
  const diagBtn = btn("RUN SDR DIAGNOSE", { tone: "primary", icon: "terminal" });
  const diagOut = h("div", { class: "tiny muted mt-8",
    text: "Collects the exact RTL/HackRF error: process user and groups, USB nodes, rtl_test output, a short real capture, busy SDR processes and the effective HF/tuner mode." });
  let diagBusy = false;

  function showDiagnose(out) {
    const errors = (out.sections || []).filter((s) => s.status === "error").length;
    const warnings = (out.sections || []).filter((s) => s.status === "warn").length;
    const pre = h("pre", {
      style: "white-space:pre-wrap;word-break:break-word;max-height:62vh;overflow:auto;font-size:11px;line-height:1.45;margin:0",
      text: out.text || "No diagnostic output",
    });
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const m = modal({
      title: "SDR diagnostic report",
      sub: `${out.backend || "auto"} · ${fmt.num((out.frequency_hz || 0) / 1e6, 4)} MHz · ${errors} error(s), ${warnings} warning(s)`,
      wide: true,
      body: h("div", { class: "col" },
        h("div", { class: "row wrap gap-6" },
          badge(`${errors} ERRORS`, errors ? "red" : "green"),
          badge(`${warnings} WARNINGS`, warnings ? "amber" : "muted")),
        pre),
      actions: [
        btn("COPY", { icon: "copy", onClick: async () => {
          try { await navigator.clipboard.writeText(out.text || ""); toast("Diagnostic report copied"); }
          catch { toast("Clipboard unavailable; use SAVE TXT", "warn"); }
        } }),
        btn("SAVE TXT", { icon: "download", onClick: () => {
          const url = URL.createObjectURL(new Blob([out.text || ""], { type: "text/plain" }));
          const a = h("a", { href: url, download: `darksec-sdr-diagnose-${stamp}.txt` });
          document.body.appendChild(a); a.click(); a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 5000);
        } }),
        btn("CLOSE", { tone: "ghost", onClick: () => m.close() }),
      ],
    });
  }

  async function runDiagnose() {
    if (diagBusy) return;
    diagBusy = true;
    diagBtn.disabled = true;
    diagOut.className = "tiny muted mt-8";
    diagOut.textContent = "Running rtl_test and a short capture…";
    try {
      const out = await api.get(`/api/sdr/diagnose?freq_mhz=${encodeURIComponent(liveCenter / 1e6)}`
        + `&backend=${encodeURIComponent(liveBackendValue)}&direct=${encodeURIComponent(liveDirect.value)}`);
      const errors = (out.sections || []).filter((s) => s.status === "error").length;
      const warnings = (out.sections || []).filter((s) => s.status === "warn").length;
      diagOut.className = `tiny mt-8 ${errors ? "danger-text" : "muted"}`;
      diagOut.textContent = `${errors} error(s), ${warnings} warning(s) · ${out.backend || "auto"} · ${fmt.num((out.frequency_hz || 0) / 1e6, 4)} MHz · ${out.direct?.mode ? "HF Q" : "tuner"}`;
      showDiagnose(out);
    } catch (err) {
      diagOut.className = "tiny danger-text mt-8";
      diagOut.textContent = err.message || "Diagnose failed";
      toast(err.message || "Diagnose failed", "error");
    } finally {
      diagBusy = false;
      diagBtn.disabled = false;
    }
  }
  diagBtn.addEventListener("click", runDiagnose);
  lower.appendChild(panel({
    title: "Diagnose receiver",
    sub: "Exact rtl_sdr / rtl_test / USB failure report",
    icon: "terminal",
    body: h("div", { class: "col" },
      h("div", { class: "row wrap" }, diagBtn),
      diagOut),
  }));

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
    paneCapture.appendChild(jobsPanel);
  }

  const cards = await referenceSection("COMMS", { limit: 6 });
  if (cards) paneSystem.appendChild(panel({ title: "Radio field cards", sub: "Receive and procedure reference", icon: "book", body: cards }));

  loadRecordings();

  return () => {
    if (sweepAbort) { try { sweepAbort.abort(); } catch {} }
    if (pollTimer) clearInterval(pollTimer);
    window.removeEventListener("keydown", liveKeyHandler);
    if (adsbPoll) { clearInterval(adsbPoll); adsbPoll = null; }
    stopLive();
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
  ctx.strokeStyle = `rgba(${accentRgb()},.09)`;
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
  grad.addColorStop(0, `rgba(${accentRgb()},.24)`);
  grad.addColorStop(1, `rgba(${accentRgb()},.015)`);
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.beginPath();
  for (let i = 0; i < db.length; i++) {
    const x = 4 + (i / (db.length - 1)) * (w - 8);
    const y = hh - 18 - ((db[i] - lo) / Math.max(0.001, hi - lo)) * (hh - 30);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = accentColor();
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
      if (t < .25) { const k = t / .25; r = 2 + 4 * k; g = 10 + 30 * k; b = 18 + 44 * k; }
      else if (t < .5) { const k = (t - .25) / .25; r = 6 + 20 * k; g = 40 + 100 * k; b = 62 + 46 * k; }
      else if (t < .75) { const k = (t - .5) / .25; r = 26 + 110 * k; g = 140 + 80 * k; b = 108 - 40 * k; }
      else { const k = (t - .75) / .25; r = 136 + 119 * k; g = 220 + 35 * k; b = 68 + 120 * k; }
      const p = (y * width + x) * 4;
      img.data[p] = r; img.data[p + 1] = g; img.data[p + 2] = b; img.data[p + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
}
