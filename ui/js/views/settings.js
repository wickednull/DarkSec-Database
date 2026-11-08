import { api, download } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, pageHead, btn, field, input, select, checkbox, badge, toast,
  modal, alertStrip, fmt, loading, emptyState,
} from "../components.js";
import {
  kv, progressBar, statusTone, positionConfidence, manualPositionMetadata, gpsPositionMetadata,
  geocodedPositionMetadata,
} from "./shared.js";

export async function render(root) {
  root.replaceChildren(loading("LOADING CONFIGURATION"));
  let status;
  try {
    status = await store.loadStatus({ force: true });
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }
  const settings = { ...store.settings };
  let disposed = false;
  let pollTimer = null;
  const cleanups = [];
  let gpsCandidate = null;
  let phoneSessionId = null;
  let phonePollTimer = null;
  let libStats = status.library || {};

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "SYSTEM // CONFIGURATION",
    title: "System Settings",
    desc: "Local identity, position, radio limits, offline maps and data custody. Nothing here is sent to a remote service.",
    actions: [
      btn("REFRESH", { icon: "refresh", onClick: () => render(root) }),
      btn("BACKUP DATA", { icon: "save", tone: "primary", onClick: backupNow }),
    ],
  }));

  if (!settings.vault_timeout_min) {
    root.appendChild(alertStrip("Vault timeout is unset. Set a short auto-lock window before carrying this device.", ""));
  }

  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);

  /* ------------------------------------------------------------ identity */
  const callsign = input({ value: settings.callsign || "", maxlength: 40 });
  const team = input({ value: settings.team || "", maxlength: 80 });
  const operator = input({ value: settings.operator || "", maxlength: 80, placeholder: "Optional — avoid real names in the field" });
  const accent = select([
    { value: "green", label: "Green phosphor" },
    { value: "amber", label: "Amber CRT" },
    { value: "cyan", label: "Cyan night" },
    { value: "red", label: "Red alert" },
  ], settings.accent || "green");
  const gridFormat = select([
    { value: "mgrs", label: "MGRS" },
    { value: "utm", label: "UTM" },
    { value: "dd", label: "Decimal degrees" },
  ], settings.grid_format || "mgrs");
  const units = select([
    { value: "metric", label: "Metric" },
    { value: "imperial", label: "Imperial" },
  ], settings.units || "metric");
  const dateFormat = select([
    { value: "iso", label: "ISO 8601" },
    { value: "local", label: "Local" },
  ], settings.date_format || "iso");
  const timezone = input({ value: settings.timezone || "auto", placeholder: "auto or IANA zone" });
  const epoch = input({ value: settings.epoch || "", placeholder: "ISO date/time, optional mission epoch" });
  const scanlines = checkbox("Scanline overlay", !!settings.scanlines);
  const sound = checkbox("Interface sound cues", !!settings.sound);
  const opsec = checkbox("OPSEC blackout on start", !!settings.opsec_mode);

  const identity = panel({
    title: "Identity & display",
    icon: "settings",
    body: h("div", { class: "col" },
      h("div", { class: "form-grid" },
        field("Callsign", callsign), field("Team", team),
        field("Operator", operator, "Optional. Stored only in the local database."),
        field("Accent", accent),
        field("Default grid", gridFormat), field("Units", units),
        field("Date format", dateFormat), field("Timezone", timezone, "auto uses the host clock."),
        h("div", { class: "wide" }, field("Mission epoch", epoch, "Shown as H+ / H- in the status bar."))),
      h("div", { class: "row wrap" }, scanlines, sound, opsec),
      h("div", { class: "row mt-10" }, btn("SAVE IDENTITY", { tone: "primary", icon: "save", onClick: saveIdentity }))),
  });
  grid.appendChild(identity);

  /* ------------------------------------------------------------ position */
  const lat = input({ type: "number", step: "0.000001", min: -90, max: 90, value: settings.lat ?? "" });
  const lon = input({ type: "number", step: "0.000001", min: -180, max: 180, value: settings.lon ?? "" });
  const elevation = input({ type: "number", step: "1", value: settings.elevation_m ?? 0 });
  const declination = input({ type: "number", step: "0.1", value: settings.declination ?? 0 });
  const coordPreview = h("div", { class: "coord-block" });
  const addressQuery = input({ type: "search", maxlength: 200, placeholder: "Street, town/region, country", autocomplete: "off" });
  const geocodeResults = h("div", { class: "col gap-6" });
  const geocodeButton = btn("LOOK UP ADDRESS", { icon: "search", onClick: lookupAddress });
  const phoneInterface = select([{ value: "", label: "Loading local interfaces…" }], "");
  const positionInfo = positionConfidence(settings);
  const positionStatus = h("div", {
    class: `callout ${positionInfo.tone} small`,
    text: positionInfo.text,
  });
  const posPanel = panel({
    title: "Position",
    sub: "Used by navigation, almanac and map",
    icon: "compass",
    body: h("div", { class: "col" },
      h("div", { class: "form-grid" },
        field("Latitude", lat), field("Longitude", lon),
        field("Elevation (m)", elevation), field("Magnetic declination (°)", declination, "Positive east. Verify against a current model or map.")),
      positionStatus,
      h("div", { class: "sep" }),
      h("div", { class: "side-label", text: "SET HOME LOCATION FROM ADDRESS" }),
      field("Address", addressQuery, "One-shot lookup; not saved after selecting a result."),
      h("div", { class: "callout warn small", text: "Clicking LOOK UP sends the full address to OpenStreetMap Nominatim. Do not use it for a sensitive location. Lookup is disabled in OPSEC mode; the address is not saved to disk but may remain in memory cache for up to 24 hours. Only selected coordinates are saved. Verify the result on the map; elevation is not supplied." }),
      h("div", { class: "row wrap" }, geocodeButton),
      geocodeResults,
      coordPreview,
      h("div", { class: "row wrap mt-10" },
        btn("SAVE POSITION", { tone: "primary", icon: "save", onClick: savePosition }),
        btn("GPS FIX", { icon: "satellite", onClick: gpsFix }),
        btn("OPEN MAP", { icon: "map", onClick: () => { location.hash = "#/map"; } }),
        btn("NAV TOOLS", { icon: "route", onClick: () => { location.hash = "#/nav"; } }))),
  });
  grid.appendChild(posPanel);

  const phoneQrWrap = h("div", { class: "phone-gps-qr-wrap" });
  const phoneState = h("div", { class: "callout small", text: settings.opsec_mode
    ? "Phone GPS sharing is disabled in OPSEC mode."
    : "No phone session. The temporary HTTPS listener is off until you start a session." });
  const phoneFix = h("div", { class: "tiny muted", text: "No phone fix received." });
  const phoneUrl = h("div", { class: "small mono wrap-text" });
  const phoneStartButton = btn("START PHONE GPS", { icon: "satellite", tone: "primary", onClick: startPhoneGps });
  const phoneStopButton = btn("STOP SESSION", { icon: "close", tone: "ghost", onClick: stopPhoneGps, disabled: true });
  const phoneAcceptButton = btn("ACCEPT FIX AS POSITION", { icon: "check", tone: "primary", onClick: acceptPhoneGps, disabled: true });
  const phoneGpsPanel = panel({
    title: "Phone GPS · Browser", icon: "satellite",
    sub: "Temporary HTTPS on one selected local interface · no cloud relay",
    body: h("div", { class: "col gap-8" },
      field("Computer interface", phoneInterface, "Connect the phone and computer to the same trusted Wi-Fi or phone hotspot."),
      h("div", { class: "callout warn small", text: "The phone uses its normal browser—no external app. This creates a separate HTTPS listener bound only to the selected local IP; the main API remains loopback-only. Keep the phone page open — the session stays connected while active (not a fixed 5-minute cutoff), and you can switch DarkSec tabs without stopping it. Use a trusted AP that allows device-to-device traffic; some phone hotspots isolate clients. iOS/Android may show a self-signed-certificate warning: continue only if you started this session, the URL IP matches this interface, and you are on your private network. The phone must grant location permission. OPSEC mode or STOP ends the listener. Accepting a fresh fix updates the map, navigation, almanac, weather/intel references and the status-bar GPS source/age; accepting saves the fix but keeps the listener open for newer fixes until you stop it." }),
      h("div", { class: "row wrap gap-6" }, phoneStartButton, phoneStopButton, phoneAcceptButton),
      phoneQrWrap, phoneUrl, phoneState, phoneFix),
  });
  phoneStartButton.disabled = true;
  grid.appendChild(phoneGpsPanel);
  loadPhoneInterfaces();
  reattachPhoneGps();
  cleanups.push(() => {
    // Keep the backend phone GPS listener/session alive across tab navigation.
    // Only stop it via explicit STOP, phone leave/stop, OPSEC, or idle expiry.
    if (phonePollTimer) clearInterval(phonePollTimer);
    phonePollTimer = null;
  });
  cleanups.push(store.subscribe((state, type) => {
    if (!["opsec", "settings", "status"].includes(type)) return;
    const opsec = !!state.opsec || !!state.settings?.opsec_mode;
    phoneStartButton.disabled = opsec || !!phoneSessionId;
    if (opsec && phoneSessionId) stopPhoneGps();
    else if (!opsec) loadPhoneInterfaces();
  }));

  /* --------------------------------------------------------------- radio */
  const lna = input({ type: "number", min: 0, max: 40, value: settings.sdr_lna_gain ?? 32 });
  const vga = input({ type: "number", min: 0, max: 62, value: settings.sdr_rx_gain ?? 40 });
  const amp = checkbox("Enable HackRF RF amplifier (+14 dB, more noise, more battery)", !!settings.sdr_amp);

  const radioPanel = panel({
    title: "SDR receiver",
    sub: "HackRF gain defaults",
    icon: "antenna",
    body: h("div", { class: "col" },
      h("div", { class: "form-grid" },
        field("LNA gain (dB)", lna), field("VGA gain (dB)", vga)),
      amp,
      h("div", { class: "hint", text: "Start with LNA 24–32 and VGA 30–40. Strong local signals may require less gain. Never connect a transmit antenna to the receive input while transmitting with another radio." }),
      h("div", { class: "row mt-10" },
        btn("SAVE RADIO", { tone: "primary", icon: "save", onClick: saveRadio }),
        btn("OPEN SDR", { icon: "antenna", onClick: () => { location.hash = "#/sdr"; } }))),
  });
  grid.appendChild(radioPanel);

  /* ------------------------------------------------------------ transmit */
  const txState = status.tx_enabled;
  const txEnabled = checkbox("Enable transmit capability", !!settings.sdr_tx_enabled);
  const txAttested = checkbox("I have recorded the legal attestation", !!settings.sdr_tx_attested);
  const txPanel = panel({
    title: "Transmit gate",
    sub: "Hard interlock · audited",
    icon: "warning",
    cls: "tx-gate",
    body: h("div", { class: "col" },
      h("div", { class: "tx-warning" },
        h("strong", { text: "TRANSMITTING IS A LEGAL ACT. " }),
        "You must hold a valid licence or operate under an applicable emergency exception, use the correct power and bandwidth, and avoid interfering with distress, aviation, marine, public safety and protected bands. DarkSec logs every transmit attempt."),
      h("div", { class: "row wrap" },
        badge(txState ? "TX ENABLED" : "TX DISABLED", txState ? "red" : "green"),
        badge(settings.sdr_tx_attested ? "ATTESTED" : "NOT ATTESTED", settings.sdr_tx_attested ? "amber" : "muted")),
      txEnabled, txAttested,
      h("div", { class: "row mt-10" },
        btn("APPLY TX SETTINGS", { tone: "danger", icon: "lock", onClick: () => saveTx(txEnabled, txAttested) })),
      h("div", { class: "tiny muted", text: "Every individual transmission still requires the exact word TRANSMIT plus a fresh attestation in the SDR module." })),
  });
  grid.appendChild(txPanel);

  /* ---------------------------------------------------------------- map */
  const tileSource = select([
    { value: "esri_imagery", label: "Satellite imagery (Esri)" },
    { value: "esri_street", label: "Street map (Esri)" },
    { value: "carto_dark", label: "Dark tactical (CARTO)" },
    { value: "usgs_topo", label: "USGS topo (US, public domain)" },
    { value: "usgs_imagery", label: "USGS imagery topo (US)" },
    { value: "opentopomap", label: "OpenTopoMap" },
  ], settings.tile_source || "esri_imagery");
  api.get("/api/map/cache/sources").then((r) => {
    const opts = (r.sources || []).map((s) => ({
      value: s.id,
      label: s.name + (s.tile_count ? ` · ${s.tile_count} cached` : ""),
    }));
    if (!opts.length) return;
    const current = tileSource.value;
    tileSource.replaceChildren();
    for (const o of opts) tileSource.appendChild(h("option", { value: o.value, text: o.label }));
    tileSource.value = opts.some((o) => o.value === current) ? current : opts[0].value;
  }).catch(() => {});
  const mbtiles = input({ value: settings.mbtiles_path || "", placeholder: "/path/to/region.mbtiles" });
  const mapVector = checkbox("Render offline vector basemap", settings.map_vector !== false);
  const layersBox = h("div", { class: "col gap-4" }, h("div", { class: "tiny muted", text: "Loading layers…" }));
  const mapPanel = panel({
    title: "Offline maps",
    icon: "layers",
    body: h("div", { class: "col" },
      h("div", { class: "form-grid" },
        field("Tile source", tileSource, "Used for live view and for new offline downloads. Bundled and cached tiles are always preferred."),
        field("MBTiles path", mbtiles, "Must exist on this machine; read-only.")),
      mapVector,
      layersBox,
      h("div", { class: "row mt-10" },
        btn("SAVE MAP", { tone: "primary", icon: "save", onClick: saveMap }),
        btn("OPEN MAP", { icon: "map", onClick: () => { location.hash = "#/map"; } }))),
  });
  grid.appendChild(mapPanel);
  loadLayers(layersBox);

  /* ------------------------------------------------------------ library */
  const autoDeep = checkbox("Extract full text automatically when indexing", settings.auto_deep_index !== false);
  const deep = checkbox("Deep text extraction (slower, enables full-text search)", true);
  const force = checkbox("Re-index unchanged files", false);
  const libBox = h("div", { class: "col" });
  const libPanel = panel({
    title: "Document library",
    sub: "5.6 GB local archive",
    icon: "library",
    tools: [btn("OPEN", { size: "sm", onClick: () => { location.hash = "#/library"; } })],
    body: h("div", { class: "col" },
      libBox, autoDeep,
      h("div", { class: "row wrap" }, deep, force),
      h("div", { class: "row mt-10" },
        btn("START INDEX", { tone: "primary", icon: "database", onClick: startIndex }),
        btn("REFRESH", { icon: "refresh", onClick: refreshLibrary })),
      h("div", { class: "hint", text: "Deep indexing reads every PDF and can take hours on a large archive. It runs in the background; the console remains usable." })),
  });
  grid.appendChild(libPanel);
  renderLibrary(libBox, libStats);

  /* ------------------------------------------------------------- feeds */
  const feedAuto = checkbox("Automatically fetch public feeds while online", !!settings.feed_auto_refresh);
  const feedInterval = select([
    { value: 5, label: "Every 5 minutes" },
    { value: 10, label: "Every 10 minutes (recommended)" },
    { value: 15, label: "Every 15 minutes" },
    { value: 30, label: "Every 30 minutes" },
    { value: 60, label: "Every 60 minutes" },
  ], settings.feed_refresh_min ?? 10);
  const feedPanel = panel({
    title: "Intelligence feeds",
    sub: "Automatic while online · cached offline",
    icon: "globe",
    body: h("div", { class: "col" },
      feedAuto,
      field("Check cadence", feedInterval, "Fast-moving safety sources still use their own conservative minimum intervals to respect upstream rate limits."),
      h("div", { class: "callout small", text: "When online and OPSEC is off, the console refreshes public feeds in the background: space weather, earthquakes, NWS alerts, GDACS/EONET hazards, Safecast radiation reports, volcanic activity, weather, aircraft, satellites and world news. OPSEC blackout pauses all automatic fetching. Offline, the last successful cache remains readable with its age shown. Treat all feed content as unverified until confirmed with an official source." }),
      h("div", { class: "row mt-10" },
        btn("SAVE SYNC SETTINGS", { icon: "save", onClick: saveFeed }),
        btn("SYNC NOW", { icon: "refresh", onClick: refreshFeeds }))),
  });
  grid.appendChild(feedPanel);

  /* ------------------------------------------------------------ security */
  const vaultTimeout = input({ type: "number", min: 0, max: 240, value: settings.vault_timeout_min ?? 15 });
  const secPanel = panel({
    title: "Data custody",
    icon: "shield",
    body: h("div", { class: "col" },
      field("Vault auto-lock (minutes)", vaultTimeout, "0 disables auto-lock. Shorter is safer."),
      h("div", { class: "row wrap mt-10" },
        btn("SAVE", { icon: "save", onClick: () => saveSimple({ vault_timeout_min: vaultTimeout.value }, "Vault timeout saved") }),
        btn("VAULT & NOTES", { icon: "lock", onClick: () => { location.hash = "#/security?tab=vault"; } }),
        btn("EXPORT JSON", { icon: "download", onClick: () => download("/api/logistics/export?fmt=json", "darksec-export.json") }),
        btn("EXPORT CSV", { icon: "download", onClick: () => download("/api/logistics/export?fmt=csv", "darksec-export.csv") })),
      h("div", { class: "callout small", text: "Encrypted notes are never included in exports or backups in plaintext. Losing the vault passphrase means losing the notes; there is no recovery key." })),
  });
  grid.appendChild(secPanel);

  /* ---------------------------------------------------------- assistant */
  const assistantEnabled = checkbox("Enable Delamain (local AI / Ollama)", !!settings.assistant_enabled);
  const assistantThink = checkbox("Enable model thinking/reasoning", !!settings.assistant_think);
  const ollamaBase = input({ value: settings.ollama_base_url || "http://127.0.0.1:11434", placeholder: "http://127.0.0.1:11434" });
  const ollamaModel = input({ value: settings.ollama_model || "", placeholder: "e.g. llama3.2, mistral, qwen2.5:7b" });
  const assistantTemp = input({ type: "number", step: "0.1", min: "0", max: "2", value: settings.assistant_temperature ?? 0.2 });
  const assistantTimeout = input({ type: "number", step: "1", min: "5", max: "1800", value: settings.assistant_timeout_sec ?? 300 });
  const assistantCtx = input({ type: "number", step: "512", min: "512", max: "32768", value: settings.assistant_num_ctx ?? 4096 });
  const assistantMaxTokens = input({ type: "number", step: "512", min: "128", max: "8192", value: settings.assistant_max_tokens ?? 2048 });
  const assistantPrompt = h("textarea", { class: "input mono", rows: "5", style: { width: "100%", resize: "vertical" } });
  assistantPrompt.value = settings.assistant_system_prompt || "";
  const assistantPanel = panel({
    title: "Delamain / Ollama",
    sub: "Local AI model integration for assistant chat and field support",
    icon: "cpu",
    body: h("div", { class: "col gap-8" },
      h("div", { class: "row wrap gap-16" }, assistantEnabled, assistantThink),
      h("div", { class: "form-grid" },
        field("Ollama base URL", ollamaBase, "Local endpoint. Default: http://127.0.0.1:11434"),
        field("Model", ollamaModel, "Must be pulled in Ollama (e.g. `ollama pull llama3.2`).")),
      h("div", { class: "form-grid cols-3" },
        field("Temperature", assistantTemp), field("Timeout (sec)", assistantTimeout, "CPU inference can be slow; 300–900s may be needed."), field("Context tokens", assistantCtx),
        field("Max reply tokens", assistantMaxTokens, "Raise for long guides. 2048 is the default; higher is slower on CPU. If answers stop mid-sentence, increase this.")),
      field("System prompt", assistantPrompt, "Guides behavior/safety. Keep it practical; avoid secrets."),
      h("div", { class: "row wrap gap-8" },
        btn("SAVE ASSISTANT", { tone: "primary", icon: "save", onClick: saveAssistant }),
        btn("TEST CONNECTION", { icon: "refresh", onClick: testAssistant }),
        btn("OPEN ASSISTANT", { icon: "cpu", tone: "ghost", onClick: () => { location.hash = "#/assistant"; } })),
      h("div", { class: "callout small", text: "Thinking/reasoning can improve complex answers but is much slower on CPU-only systems (especially Qwen 3.5). If responses stall, disable thinking or increase timeout." }),
      h("div", { class: "callout small amber", text: "The app calls Ollama through the local backend. AI output can be wrong or incomplete—verify critical medical, navigation, legal, and hazard decisions." })),
  });
  grid.appendChild(assistantPanel);

  async function saveAssistant() {
    await saveSimple({
      assistant_enabled: assistantEnabled.querySelector("input").checked,
      assistant_think: assistantThink.querySelector("input").checked,
      ollama_base_url: ollamaBase.value.trim(),
      ollama_model: ollamaModel.value.trim(),
      assistant_temperature: Number(assistantTemp.value),
      assistant_timeout_sec: Number(assistantTimeout.value),
      assistant_num_ctx: Number(assistantCtx.value),
      assistant_max_tokens: Number(assistantMaxTokens.value),
      assistant_system_prompt: assistantPrompt.value,
    }, "Assistant settings saved");
  }
  async function testAssistant() {
    toast("Testing Ollama connection…", "warn");
    try {
      const r = await api.get("/api/assistant/status");
      if (r.reachable) toast(`Ollama reachable · ${(r.models||[]).length} model(s)${r.warning ? " · " + r.warning : ""}`, "ok");
      else toast(r.error || "Ollama not reachable", "error");
    } catch (e) { toast(e.message, "error"); }
  }

  /* ------------------------------------------------------------- system */
  const sys = status.system || {};
  const sysBox = h("div", { class: "col" });
  sysBox.appendChild(kv([
    ["Host", sys.hostname || "—"],
    ["Platform", sys.platform || "—"],
    ["CPU", `${sys.cpu_count ?? "—"} cores · load ${sys.load_1m ?? "—"}`],
    ["Memory", `${fmt.num(sys.memory_pct, 1)}% used`],
    ["Disk", `${fmt.num(sys.disk_pct, 1)}% used · ${fmt.bytes(sys.disk_free)} free`],
    ["Data directory", sys.data_dir || "—"],
    ["Database", fmt.bytes(sys.db_bytes)],
    ["Uptime", fmt.duration(sys.uptime_s)],
    ["Version", `${status.app || "DarkSec"} ${status.version || ""} · ${status.codename || ""}`],
  ]));
  const auditBox = h("div", { class: "list" });
  const sysPanel = panel({
    title: "Platform & audit",
    icon: "cpu",
    cls: "span-2",
    tools: [btn("REFRESH", { size: "sm", icon: "refresh", onClick: () => loadAudit(auditBox) })],
    body: h("div", { class: "col" }, sysBox, h("div", { class: "sep" }), auditBox),
  });
  grid.appendChild(sysPanel);
  loadAudit(auditBox);

  /* ------------------------------------------------------------ backups */
  const backupsBox = h("div", { class: "list" });
  const backupPanel = panel({
    title: "Local backups",
    icon: "save",
    body: h("div", { class: "col" },
      h("div", { class: "callout small", text: "Backups contain operator data and settings, not document files or plaintext notes. Copy them to removable media and protect them." }),
      h("div", { class: "row wrap mt-10" }, btn("CREATE BACKUP", { tone: "primary", icon: "save", onClick: backupNow })),
      backupsBox),
  });
  grid.appendChild(backupPanel);
  loadBackups(backupsBox);

  /* -------------------------------------------------------------- actions */
  async function saveIdentity() {
    await saveSimple({
      callsign: callsign.value.trim() || "NIGHTWATCH",
      team: team.value.trim(),
      operator: operator.value.trim(),
      accent: accent.value,
      grid_format: gridFormat.value,
      units: units.value,
      date_format: dateFormat.value,
      timezone: timezone.value.trim() || "auto",
      epoch: epoch.value.trim(),
      scanlines: scanlines.querySelector("input").checked,
      sound: sound.querySelector("input").checked,
      opsec_mode: opsec.querySelector("input").checked,
    }, "Identity saved");
  }

  async function savePosition() {
    const la = Number(lat.value), lo = Number(lon.value);
    if (!Number.isFinite(la) || la < -90 || la > 90) { toast("Latitude must be -90 to 90", "warn"); return; }
    if (!Number.isFinite(lo) || lo < -180 || lo > 180) { toast("Longitude must be -180 to 180", "warn"); return; }
    const isGpsCandidate = gpsCandidate
      && Math.abs(la - Number(gpsCandidate.lat)) < 1e-7
      && Math.abs(lo - Number(gpsCandidate.lon)) < 1e-7;
    const metadata = isGpsCandidate ? gpsPositionMetadata(gpsCandidate) : manualPositionMetadata();
    await saveSimple({
      lat: la, lon: lo, elevation_m: Number(elevation.value) || 0,
      declination: Number(declination.value) || 0, ...metadata,
    }, "Position saved");
    gpsCandidate = null;
    render(root);
  }

  async function saveRadio() {
    await saveSimple({ sdr_lna_gain: Number(lna.value), sdr_rx_gain: Number(vga.value), sdr_amp: amp.querySelector("input").checked }, "Radio defaults saved");
  }

  async function saveMap() {
    const patch = { tile_source: tileSource.value, mbtiles_path: mbtiles.value.trim(), map_vector: mapVector.querySelector("input").checked };
    await saveSimple(patch, "Map settings saved");
  }

  async function saveFeed() {
    await saveSimple({
      feed_auto_refresh: feedAuto.querySelector("input").checked,
      feed_refresh_min: Number(feedInterval.value) || 10,
    }, "Sync settings saved");
  }

  async function saveSimple(patch, label) {
    try {
      await store.saveSettings(patch);
      toast(label || "Settings saved");
      status = await store.loadStatus({ force: true });
    } catch (err) {
      toast(err.message || "Could not save settings", "error");
    }
  }

  function saveTx(enabledCtl, attestedCtl) {
    const enabled = enabledCtl.querySelector("input").checked;
    const attested = attestedCtl.querySelector("input").checked;
    if (!enabled && !attested) {
      saveSimple({ sdr_tx_enabled: false, sdr_tx_attested: false }, "Transmit disabled").then(() => render(root));
      return;
    }
    if (enabled && !attested) { toast("Record the legal attestation before enabling transmit", "warn"); return; }
    const confirmInput = input({ placeholder: "I AM RESPONSIBLE", autocomplete: "off" });
    const m = modal({
      title: "Transmit legal gate",
      sub: "This action is logged",
      narrow: true,
      body: h("div", { class: "col" },
        h("div", { class: "tx-warning", text: "You are about to enable a radio transmitter. Confirm that you are licensed or operating under a lawful emergency exception, that you will use the minimum power necessary, and that you will not transmit on distress, aviation, marine, public-safety or protected frequencies." }),
        field("Type I AM RESPONSIBLE to continue", confirmInput)),
      actions: [
        btn("CANCEL", { tone: "ghost", onClick: () => m.close() }),
        btn("ENABLE TX", { tone: "danger", onClick: async () => {
          if (confirmInput.value.trim().toUpperCase() !== "I AM RESPONSIBLE") {
            toast("Type I AM RESPONSIBLE exactly", "warn");
            return;
          }
          try {
            await store.saveSettings({ sdr_tx_enabled: enabled, sdr_tx_attested: attested, tx_ack: "I AM RESPONSIBLE" });
            toast(enabled ? "Transmit enabled — still requires TRANSMIT per action" : "Attestation recorded", enabled ? "warn" : "");
            m.close();
            render(root);
          } catch (err) {
            toast(err.message || "TX settings rejected", "error");
          }
        } }),
      ],
    });
  }

  async function gpsFix() {
    try {
      const g = await api.get("/api/nav/gps");
      if (!g.fix) { toast(g.note || "No GPS fix available", "warn"); return; }
      gpsCandidate = g;
      lat.value = g.lat; lon.value = g.lon;
      if (g.alt_m != null) elevation.value = Math.round(g.alt_m);
      const accuracy = g.accuracy_m != null && Number.isFinite(Number(g.accuracy_m))
        ? ` · estimated ±${fmt.num(g.accuracy_m, 1)} m` : " · accuracy unavailable";
      positionStatus.className = "callout warn small";
      positionStatus.textContent = `GPS fix captured ${fmt.ago(g.time || new Date().toISOString())}${accuracy}. Review the coordinate and SAVE POSITION to apply it; this is not live tracking.`;
      toast("GPS fix copied — review and save");
      updateCoordPreview();
    } catch (err) { toast(err.message || "GPS unavailable", "error"); }
  }

  async function loadPhoneInterfaces() {
    if (store.state.opsec || store.settings?.opsec_mode) return;
    try {
      const out = await api.get("/api/phone-gps/interfaces");
      const items = out.interfaces || [];
      phoneInterface.replaceChildren(
        h("option", { value: "", text: items.length ? "— select computer interface —" : "No active private IPv4 interface" }),
        ...items.map((item) => h("option", {
          value: item.ip,
          text: `${item.interface} · ${item.ip}`,
        })),
      );
      if (items.length) phoneInterface.value = items[0].ip;
      phoneStartButton.disabled = !items.length || !!phoneSessionId;
    } catch (err) {
      phoneInterface.replaceChildren(h("option", { value: "", text: "Could not list local interfaces" }));
      phoneStartButton.disabled = true;
      phoneState.className = "callout danger small";
      phoneState.textContent = err.message || "Could not list local interfaces.";
    }
  }

  async function reattachPhoneGps() {
    if (store.state.opsec || store.settings?.opsec_mode || disposed) return;
    try {
      const status = await api.get("/api/phone-gps/status");
      if (!status || !status.session_id) return;
      if (!["waiting", "receiving", "starting"].includes(status.state)) return;
      phoneSessionId = status.session_id;
      phoneStartButton.disabled = true;
      phoneStopButton.disabled = false;
      if (status.url) {
        try {
          const u = new URL(status.url);
          phoneUrl.textContent = `Phone URL: ${u.protocol}//${u.host}/ · active session`;
        } catch { phoneUrl.textContent = `Phone URL: active session`; }
      }
      const image = h("img", {
        class: "phone-gps-qr",
        alt: "Temporary local HTTPS phone GPS QR code",
        src: `/api/phone-gps/qr.svg?session_id=${encodeURIComponent(phoneSessionId)}`,
      });
      image.addEventListener("error", () => {}, { once: true });
      phoneQrWrap.replaceChildren(image);
      if (!phonePollTimer) phonePollTimer = setInterval(refreshPhoneGps, 1000);
      await refreshPhoneGps();
    } catch {}
  }

  async function startPhoneGps() {
    if (store.state.opsec || store.settings?.opsec_mode) {
      toast("Phone GPS pairing is disabled in OPSEC mode", "warn");
      return;
    }
    if (!phoneInterface.value) {
      toast("Choose an active private network interface first", "warn");
      return;
    }
    phoneStartButton.disabled = true;
    phoneState.className = "callout small";
    phoneState.textContent = "Starting the temporary HTTPS listener on the selected interface…";
    phoneQrWrap.replaceChildren(loading("CREATING ONE-TIME QR"));
    try {
      const session = await api.post("/api/phone-gps/pair", { ip: phoneInterface.value });
      phoneSessionId = session.session_id;
      const image = h("img", {
        class: "phone-gps-qr",
        alt: "Temporary local HTTPS phone GPS QR code",
        src: `/api/phone-gps/qr.svg?session_id=${encodeURIComponent(phoneSessionId)}`,
      });
      image.addEventListener("error", () => {
        phoneQrWrap.replaceChildren(h("div", { class: "callout danger small", text: "Could not load the pairing QR. Stop this session and try again." }));
      }, { once: true });
      phoneQrWrap.replaceChildren(image);
      phoneStartButton.disabled = true;
      phoneStopButton.disabled = false;
      phoneAcceptButton.disabled = true;
      phoneState.className = "callout warn small";
      phoneUrl.textContent = `Phone URL: https://${session.ip}:${session.port}/ · QR expires ${fmt.date(session.expires_at)}`;
      phoneState.textContent = "Scan with the iPhone camera, review the certificate warning, continue only if the IP matches, then tap START GPS in Safari.";
      phonePollTimer = setInterval(refreshPhoneGps, 1000);
      await refreshPhoneGps();
    } catch (err) {
      phoneStartButton.disabled = false;
      phoneQrWrap.replaceChildren();
      phoneState.className = "callout danger small";
      phoneState.textContent = err.message || "Could not start local HTTPS phone GPS.";
      toast(phoneState.textContent, "error");
    }
  }

  async function refreshPhoneGps() {
    if (!phoneSessionId || disposed) return;
    try {
      const status = await api.get("/api/phone-gps/status");
      if (status.state === "disabled") {
        phoneState.className = "callout danger small";
        phoneState.textContent = "Phone GPS listener stopped because OPSEC mode is active.";
        phoneAcceptButton.disabled = true;
        phoneStopButton.disabled = true;
        phoneStartButton.disabled = true;
        clearInterval(phonePollTimer); phonePollTimer = null; phoneSessionId = null;
        phoneQrWrap.replaceChildren();
        phoneUrl.textContent = "";
        return;
      }
      const tone = status.state === "error" || status.state === "expired" ? "danger"
        : status.latest_fix ? "" : "warn";
      phoneState.className = `callout ${tone} small`;
      phoneState.textContent = status.error
        || (status.state === "receiving"
          ? "Phone browser is sending location updates. Review the fix; it is not yet saved."
          : status.state === "expired" ? "Phone GPS session expired. Start a new session."
            : "Waiting for the phone browser to open the QR page and start GPS sharing." );
      const fix = status.latest_fix;
      if (fix) {
        phoneFix.replaceChildren(kv([
          ["PHONE POSITION", fmt.coord(fix.lat, fix.lon, 6)],
          ["REPORTED ACCURACY", `±${fmt.num(fix.accuracy_m, 1)} m`],
          ["FIX TIME", fmt.date(fix.timestamp)],
          ["RECEIVED", `${fmt.ago(fix.received_at)} · ${fix.age_seconds}s old`],
          ["ALTITUDE", fix.alt_m != null ? `${fmt.num(fix.alt_m, 1)} m (not applied)` : "not reported"],
        ]));
        phoneAcceptButton.disabled = Number(fix.age_seconds) > 30;
      } else {
        phoneFix.textContent = "No phone fix received.";
        phoneAcceptButton.disabled = true;
      }
      if (status.state === "expired") {
        phoneStartButton.disabled = false;
        phoneStopButton.disabled = true;
        phoneAcceptButton.disabled = true;
        clearInterval(phonePollTimer); phonePollTimer = null; phoneSessionId = null;
        phoneQrWrap.replaceChildren();
        phoneUrl.textContent = "";
      }
    } catch (err) {
      phoneState.className = "callout danger small";
      phoneState.textContent = err.message || "Phone GPS status unavailable.";
    }
  }

  async function acceptPhoneGps() {
    if (!phoneSessionId) return;
    phoneAcceptButton.disabled = true;
    try {
      const fix = await api.post("/api/phone-gps/accept", {});
      await store.loadStatus({ force: true });
      lat.value = store.settings.lat;
      lon.value = store.settings.lon;
      const quality = positionConfidence(store.settings);
      positionStatus.className = `callout ${quality.tone} small`;
      positionStatus.textContent = quality.text;
      phoneState.className = "callout small";
      phoneState.textContent = `Phone fix accepted: ±${fmt.num(fix.accuracy_m, 1)} m · ${fmt.ago(fix.timestamp)}. Listener still open — keep the phone page open to send fresh fixes, or tap STOP to close it.`;
      await updateCoordPreview();
      toast("Phone fix saved; listener still open for fresh fixes", "warn");
      // Keep session/polling active; refreshPhoneGps() will manage accept button state.
      if (phoneSessionId && !phonePollTimer) phonePollTimer = setInterval(refreshPhoneGps, 1000);
      phoneStartButton.disabled = true;
      phoneStopButton.disabled = false;
      await refreshPhoneGps();
    } catch (err) {
      phoneAcceptButton.disabled = false;
      toast(err.message || "Could not accept phone GPS fix", "error");
    }
  }

  async function stopPhoneGps() {
    try { await api.post("/api/phone-gps/stop", {}); }
    catch (err) { toast(err.message || "Could not stop phone GPS listener", "error"); }
    phoneSessionId = null;
    if (phonePollTimer) clearInterval(phonePollTimer);
    phonePollTimer = null;
    phoneQrWrap.replaceChildren();
    phoneFix.textContent = "No phone fix received.";
    phoneState.className = "callout small";
    phoneUrl.textContent = "";
    phoneState.textContent = "Phone GPS listener stopped. The QR is no longer valid.";
      phoneStartButton.disabled = !!(store.state.opsec || store.settings?.opsec_mode || !phoneInterface.value);
    phoneStopButton.disabled = true;
    phoneAcceptButton.disabled = true;
  }

  async function lookupAddress() {
    const query = addressQuery.value.trim();
    if (!query) { toast("Enter an address to look up", "warn"); addressQuery.focus(); return; }
    if (store.state.opsec || store.settings?.opsec_mode) {
      toast("Address lookup is disabled in OPSEC mode", "warn");
      return;
    }
    geocodeButton.disabled = true;
    geocodeResults.replaceChildren(loading("LOOKING UP ADDRESS"));
    try {
      const response = await api.post("/api/nav/geocode", { address: query });
      geocodeResults.replaceChildren();
      if (!(response.results || []).length) {
        geocodeResults.appendChild(emptyState("No matching address", "Try adding the town, region and country, or enter coordinates manually.", "search"));
      } else {
        for (const result of response.results) {
          const row = h("div", { class: "list-item" });
          const details = h("div", { class: "li-main" });
          details.appendChild(h("div", { class: "li-title", text: result.label }));
          details.appendChild(h("div", { class: "li-sub", text: `${Number(result.lat).toFixed(6)}, ${Number(result.lon).toFixed(6)} · ${[result.category, result.type].filter(Boolean).join(" / ") || "place"}` }));
          row.append(details, btn("SET HOME", { size: "sm", tone: "primary", onClick: () => applyGeocodedPosition(result) }));
          geocodeResults.appendChild(row);
        }
      }
      geocodeResults.appendChild(h("div", { class: "tiny muted", text: `${response.attribution || "© OpenStreetMap contributors"} · ${response.cached ? "24-hour in-memory cache" : "one-shot public lookup"}. Address queries are not saved to disk.` }));
    } catch (err) {
      geocodeResults.replaceChildren(h("div", { class: "callout danger small", text: err.message || "Address lookup failed. Enter coordinates manually or try again when online." }));
    } finally {
      geocodeButton.disabled = false;
    }
  }

  async function applyGeocodedPosition(result) {
    try {
      await store.saveSettings({
        lat: Number(result.lat), lon: Number(result.lon),
        ...geocodedPositionMetadata(),
      });
      toast("Home reference coordinates saved; verify the map point and elevation", "warn");
      render(root);
    } catch (err) {
      toast(err.message || "Could not save the selected location", "error");
    }
  }

  async function startIndex() {
    try {
      await api.post("/api/library/index", {
        deep: deep.querySelector("input").checked,
        force: force.querySelector("input").checked,
      });
      toast("Library index started in the background");
      refreshLibrary();
      if (!pollTimer) pollTimer = setInterval(refreshLibrary, 2000);
    } catch (err) { toast(err.message || "Could not start index", "error"); }
  }

  async function refreshLibrary() {
    if (disposed) return;
    try {
      libStats = await api.get("/api/library/stats");
      renderLibrary(libBox, libStats);
      if (!libStats.job?.running && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    } catch {}
  }

  function renderLibrary(target, stats) {
    target.replaceChildren();
    target.appendChild(kv([
      ["Documents", fmt.num(stats.documents || 0, 0)],
      ["Full text", fmt.num(stats.text_indexed || 0, 0)],
      ["Text extracted", fmt.bytes(stats.chars || 0)],
      ["Index size", fmt.bytes(stats.db_bytes || 0)],
    ]));
    const job = stats.job || {};
    if (job.running) {
      const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
      target.appendChild(h("div", { class: "small muted mt-10", text: `${job.message || "Indexing"} — ${job.done}/${job.total} (${pct}%)${job.errors ? ` · ${job.errors} errors` : ""}` }));
      target.appendChild(progressBar(pct));
      if (job.current) target.appendChild(h("div", { class: "tiny dim truncate", text: job.current }));
    } else if (job.finished) {
      target.appendChild(h("div", { class: "small muted mt-10", text: `Last index: ${job.message || "complete"} · ${fmt.date(job.finished)}` }));
    }
  }

  async function loadLayers(target) {
    try {
      const layers = await api.get("/api/map/layers");
      target.replaceChildren();
      const vec = layers.vector || [];
      const ras = layers.raster || [];
      target.appendChild(h("div", { class: "tiny muted", text: `Vector: ${vec.length ? vec.map((v) => v.name).join(", ") : "none"}` }));
      target.appendChild(h("div", { class: "tiny muted", text: `Raster layers: ${ras.length ? ras.map((r) => r.name).join(", ") : "none"}${ras.length ? "" : " — add tiles under maps/tiles or point at an MBTiles archive"}` }));
    } catch (err) {
      target.replaceChildren(h("div", { class: "tiny danger-text", text: err.message }));
    }
  }

  async function loadAudit(target) {
    try {
      const out = await api.get("/api/audit?limit=30");
      const list = out.items || [];
      target.replaceChildren(h("div", { class: "side-label", text: "RECENT AUDIT EVENTS" }));
      if (!list.length) { target.appendChild(h("div", { class: "small muted", text: "No audit events." })); return; }
      const wrap = h("div", { class: "list" });
      for (const row of list) {
        const item = h("div", { class: "list-item" });
        item.appendChild(h("span", { class: "badge muted", text: String(row.action || "EVENT").slice(0, 16) }));
        const main = h("div", { class: "li-main" });
        main.appendChild(h("div", { class: "li-title truncate", text: row.detail || row.action }));
        main.appendChild(h("div", { class: "li-sub", text: `${fmt.date(row.ts)}${row.host ? ` · ${row.host}` : ""}` }));
        item.appendChild(main);
        wrap.appendChild(item);
      }
      target.appendChild(wrap);
    } catch (err) {
      target.replaceChildren(h("div", { class: "small danger-text", text: err.message }));
    }
  }

  async function loadBackups(target) {
    try {
      const out = await api.get("/api/logistics/backups");
      target.replaceChildren(h("div", { class: "side-label", text: "ARCHIVES" }));
      if (!(out.items || []).length) { target.appendChild(h("div", { class: "small muted", text: "No local backups yet." })); return; }
      const list = h("div", { class: "list" });
      for (const b of out.items) {
        const item = h("div", { class: "list-item" });
        const main = h("div", { class: "li-main" });
        main.appendChild(h("div", { class: "li-title truncate", text: b.name }));
        main.appendChild(h("div", { class: "li-sub", text: `${fmt.bytes(b.bytes)} · ${fmt.date(b.mtime)}` }));
        item.appendChild(main);
        item.appendChild(btn("DOWNLOAD", { size: "sm", tone: "ghost", onClick: () => download(`/api/logistics/backups/${encodeURIComponent(b.name)}`, b.name) }));
        list.appendChild(item);
      }
      target.appendChild(list);
    } catch (err) {
      target.replaceChildren(h("div", { class: "small danger-text", text: err.message }));
    }
  }

  async function backupNow() {
    try {
      const out = await api.post("/api/logistics/backup", {});
      toast(`Backup written: ${out.file}`);
      loadBackups(backupsBox);
    } catch (err) { toast(err.message || "Backup failed", "error"); }
  }

  async function refreshFeeds() {
    toast("Refreshing public feeds — network required", "warn");
    try {
      const out = await api.post("/api/feeds/refresh", {});
      const ok = (out.results || []).filter((r) => r.status === "OK").length;
      toast(`Feeds refreshed: ${ok}/${(out.results || []).length} OK`);
    } catch (err) { toast(err.message || "Feed refresh failed", "error"); }
  }

  async function updateCoordPreview() {
    const la = Number(lat.value), lo = Number(lon.value);
    if (!Number.isFinite(la) || !Number.isFinite(lo)) return;
    try {
      const c = await api.get(`/api/nav/coords?lat=${la}&lon=${lo}`);
      coordPreview.replaceChildren();
      for (const [k, v] of [["DD", c.dd], ["UTM", c.utm], ["MGRS", c.mgrs]]) {
        coordPreview.appendChild(h("div", { class: "coord-line" }, h("span", { text: k }), h("b", { text: v })));
      }
    } catch {}
  }

  const debounce = setTimeout(updateCoordPreview, 60);
  const onPos = () => { clearTimeout(debounce); setTimeout(updateCoordPreview, 300); };
  lat.addEventListener("input", onPos);
  lon.addEventListener("input", onPos);
  updateCoordPreview();

  return () => {
    disposed = true;
    if (pollTimer) clearInterval(pollTimer);
    for (const fn of cleanups) try { fn(); } catch {}
  };
}
