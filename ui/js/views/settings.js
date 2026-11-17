import { api, download } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, pageHead, btn, field, input, select, checkbox, badge, toast,
  modal, fmt, loading, emptyState, confirmDialog, icon,
} from "../components.js";
import {
  kv, progressBar, statusTone, positionConfidence, manualPositionMetadata, gpsPositionMetadata,
  geocodedPositionMetadata, opsecActive,
} from "./shared.js?v=opsec-1";
import { soundCue, setSoundEnabled } from "../sound.js";

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
  let bundlePoll = null;
  let lastBundlePhase = "";
  let phonePollTimer = null;
  let autoAcceptedReceivedAt = null;
  let libStats = status.library || {};

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "SYSTEM // CONFIGURATION",
    title: "System Settings",
    desc: "Local identity, position, radio limits, offline maps and data custody. Network actions are explicit and labeled.",
    actions: [
      btn("REFRESH", { icon: "refresh", onClick: () => render(root) }),
      btn("BACKUP DATA", { icon: "save", tone: "primary", onClick: backupNow }),
    ],
  }));

  const SETTINGS_SECTIONS = [
    { id: "identity", num: "01", label: "Identity", icon: "settings", title: "Identity & display",
      desc: "Callsign, operator profile, theme, units and interface behavior." },
    { id: "position", num: "02", label: "Position", icon: "compass", title: "Position & GPS",
      desc: "Manual coordinates, verified fixes and browser-assisted phone GPS." },
    { id: "radio", num: "03", label: "Radio & network", icon: "radio", title: "Radio & network",
      desc: "LAN/mobile listeners, SDR gain defaults and the transmit interlock." },
    { id: "maps", num: "04", label: "Maps & library", icon: "map", title: "Maps & library",
      desc: "Offline tile sources, vector layers and the document archive." },
    { id: "intel", num: "05", label: "Feeds & overlays", icon: "globe", title: "Feeds & overlays",
      desc: "Public intelligence sources, CCTV catalogs and ALPR overlays." },
    { id: "assistant", num: "06", label: "Assistant", icon: "cpu", title: "Delamain / local AI",
      desc: "Local model connection, tool access, generation limits and privacy boundaries." },
    { id: "system", num: "07", label: "System", icon: "database", title: "System & maintenance",
      desc: "Host status, vault custody, audit trail, program updates and local backups." },
  ];
  const sectionsById = Object.fromEntries(SETTINGS_SECTIONS.map((s) => [s.id, s]));
  const nav = h("nav", { class: "settings-nav", "aria-label": "Settings sections" });
  const navButtons = {};
  const scrollHost = document.querySelector(".main");

  function settingsDetails(summary, ...children) {
    return h("details", { class: "settings-details" },
      h("summary", {}, typeof summary === "string" ? h("span", { text: summary }) : summary),
      h("div", { class: "sd-body" }, ...children));
  }

  function setActiveSection(id) {
    for (const [key, button] of Object.entries(navButtons)) {
      button.classList.toggle("active", key === id);
    }
  }

  function jumpToSection(id) {
    const target = document.getElementById(`set-${id}`);
    if (!target) return;
    setActiveSection(id);
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  for (const section of SETTINGS_SECTIONS) {
    const button = h("button", {
      type: "button", class: "settings-nav-btn", title: section.title,
      onClick: () => jumpToSection(section.id),
    }, icon(section.icon), h("span", { text: section.label }));
    navButtons[section.id] = button;
    nav.appendChild(button);
  }
  root.appendChild(nav);
  setActiveSection(SETTINGS_SECTIONS[0].id);

  if (scrollHost) {
    const onScroll = () => {
      const threshold = scrollHost.getBoundingClientRect().top + 130;
      let active = SETTINGS_SECTIONS[0].id;
      for (const section of SETTINGS_SECTIONS) {
        const head = document.getElementById(`set-${section.id}`);
        if (head && head.getBoundingClientRect().top <= threshold) active = section.id;
      }
      setActiveSection(active);
    };
    scrollHost.addEventListener("scroll", onScroll, { passive: true });
    cleanups.push(() => scrollHost.removeEventListener("scroll", onScroll));
  }

  const grid = h("div", { class: "panel-grid grid-2 settings-grid mt-14" });
  root.appendChild(grid);

  function addSectionHead(id) {
    const section = sectionsById[id];
    if (!section) return;
    grid.appendChild(h("div", { class: "settings-section-head", id: `set-${id}` },
      h("div", { class: "ss-index", text: section.num }),
      h("div", { class: "ss-copy" },
        h("h2", { text: section.title }),
        h("p", { text: section.desc }))));
  }

  /* ------------------------------------------------------------ identity */
  const callsign = input({ value: settings.callsign || "", maxlength: 40 });
  const team = input({ value: settings.team || "", maxlength: 80 });
  const operator = input({ value: settings.operator || "", maxlength: 80, placeholder: "Optional — avoid real names in the field" });
  const themeMode = select([
    { value: "dark", label: "Dark background" },
    { value: "light", label: "Light background" },
    { value: "system", label: "Follow system" },
  ], settings.theme_mode || "dark");
  const darkTheme = select([
    { value: "nightwatch", label: "Nightwatch (default)" },
    { value: "midnight", label: "Midnight blue" },
    { value: "blackout", label: "Blackout" },
    { value: "forest", label: "Forest" },
    { value: "slate", label: "Slate" },
    { value: "ocean", label: "Ocean" },
    { value: "ember", label: "Ember" },
    { value: "plum", label: "Plum" },
    { value: "contrast", label: "High contrast" },
  ], settings.dark_theme || settings.theme || "nightwatch");
  const lightTheme = select([
    { value: "daylight", label: "Daylight (default)" },
    { value: "paper", label: "Paper" },
    { value: "solarized", label: "Solarized light" },
    { value: "cloud", label: "Cloud" },
    { value: "sand", label: "Sand" },
    { value: "mint", label: "Mint" },
    { value: "blueprint", label: "Blueprint" },
    { value: "lavender", label: "Lavender" },
    { value: "contrast-light", label: "High contrast light" },
  ], settings.light_theme || "daylight");
  const accent = select([
    { value: "green", label: "Green phosphor" },
    { value: "amber", label: "Amber CRT" },
    { value: "cyan", label: "Cyan night" },
    { value: "red", label: "Red alert" },
    { value: "violet", label: "Violet" },
    { value: "blue", label: "Ice blue" },
    { value: "orange", label: "Tactical orange" },
    { value: "magenta", label: "Magenta" },
    { value: "mono", label: "Monochrome" },
  ], settings.accent || "green");
  const fontScale = select([
    { value: "0.9", label: "Small (90%)" },
    { value: "1", label: "Normal (100%)" },
    { value: "1.1", label: "Large (110%)" },
    { value: "1.2", label: "Extra large (120%)" },
    { value: "1.35", label: "Maximum (135%)" },
  ], String(settings.font_scale ?? 1));
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
  const soundTestBtn = btn("TEST CUE", { size: "sm", tone: "ghost", icon: "signal", onClick: () => {
    const on = sound.querySelector("input").checked;
    setSoundEnabled(on);
    if (on) soundCue("ok");
    else toast("Enable interface sound cues first", "warn");
  } });
  scanlines.querySelector("input").addEventListener("change", () => {
    const on = scanlines.querySelector("input").checked;
    document.body.classList.toggle("no-scan", !on);
    const el = document.getElementById("scanlines");
    if (el) el.style.display = on ? "" : "none";
  });
  sound.querySelector("input").addEventListener("change", () => {
    const on = sound.querySelector("input").checked;
    setSoundEnabled(on);
    if (on) soundCue("ok");
  });
  const opsec = checkbox("OPSEC blackout on start", !!settings.opsec_mode);

  const identity = panel({
    title: "Identity & display",
    icon: "settings",
    cls: "span-2",
    body: h("div", { class: "col" },
      h("div", { class: "settings-identity-layout" },
        h("div", { class: "col" },
          h("div", { class: "settings-subhead", text: "Operator" }),
          h("div", { class: "form-grid" },
            field("Callsign", callsign), field("Team", team),
            h("div", { class: "wide" }, field("Operator", operator, "Optional. Stored only in the local database."))),
          h("div", { class: "col gap-6" },
            scanlines, h("div", { class: "row gap-8" }, sound, soundTestBtn), opsec)),
        h("div", { class: "col" },
          h("div", { class: "settings-subhead", text: "Appearance" }),
          h("div", { class: "form-grid" },
            field("Background", themeMode), field("Accent", accent),
            field("Dark theme", darkTheme), field("Light theme", lightTheme),
            h("div", { class: "wide" }, field("Font size", fontScale, "Scales the whole interface."))),
          h("div", { class: "hint", text: "The sun/moon button in the top bar switches between the selected dark and light themes." })),
        h("div", { class: "col" },
          h("div", { class: "settings-subhead", text: "Display & region" }),
          h("div", { class: "form-grid" },
            field("Default grid", gridFormat), field("Units", units),
            field("Date format", dateFormat), field("Timezone", timezone, "auto uses the host clock."),
            h("div", { class: "wide" }, field("Mission epoch", epoch, "Shown as H+ / H- in the status bar."))))),
      h("div", { class: "settings-actions" },
        btn("SAVE IDENTITY", { tone: "primary", icon: "save", onClick: saveIdentity }))),
  });
  addSectionHead("identity");
  grid.appendChild(identity);

  /* ------------------------------------------------------------ position */
  const lat = input({ type: "number", step: "0.000001", min: -90, max: 90, value: settings.lat ?? "", class: "input opsec-mask" });
  const lon = input({ type: "number", step: "0.000001", min: -180, max: 180, value: settings.lon ?? "", class: "input opsec-mask" });
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
      h("div", { class: "settings-subhead", text: "Coordinates" }),
      h("div", { class: "form-grid" },
        field("Latitude", lat), field("Longitude", lon),
        field("Elevation (m)", elevation), field("Magnetic declination (°)", declination, "Positive east. Verify against a current model or map.")),
      positionStatus,
      h("div", { class: "settings-subhead", text: "Set home location from address" }),
      field("Address", addressQuery, "One-shot lookup; not saved after selecting a result."),
      settingsDetails("Address lookup privacy and behavior",
        h("p", { class: "settings-note-text", text: "Clicking LOOK UP sends the full address to OpenStreetMap Nominatim. Do not use it for a sensitive location. Lookup is disabled in OPSEC mode; the address is not saved to disk but may remain in memory cache for up to 24 hours. Only selected coordinates are saved. Verify the result on the map; elevation is not supplied." })),
      h("div", { class: "row wrap" }, geocodeButton),
      geocodeResults,
      coordPreview,
      h("div", { class: "settings-actions" },
        btn("SAVE POSITION", { tone: "primary", icon: "save", onClick: savePosition }),
        btn("GPS FIX", { icon: "satellite", onClick: gpsFix }),
        btn("PHONE GPS (BROWSER)", { icon: "smartphone", onClick: browserGpsFix }),
        btn("OPEN MAP", { icon: "map", onClick: () => { location.hash = "#/map"; } }),
        btn("NAV TOOLS", { icon: "route", onClick: () => { location.hash = "#/nav"; } }))),
  });
  addSectionHead("position");
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
      h("div", { class: "settings-subhead", text: "Link" }),
      field("Computer interface", phoneInterface, "Connect the phone and computer to the same trusted Wi-Fi or phone hotspot."),
      settingsDetails("How phone GPS works · privacy and limits",
        h("p", { class: "settings-note-text", text: "The phone uses its normal browser—no external app. This creates a separate HTTPS listener bound only to the selected local IP; the main API remains loopback-only. Keep the phone page open — the session stays connected while active (not a fixed 5-minute cutoff), and you can switch DarkSec tabs without stopping it. Use a trusted AP that allows device-to-device traffic; some phone hotspots isolate clients. iOS/Android may show a self-signed-certificate warning: continue only if you started this session, the URL IP matches this interface, and you are on your private network. The phone must grant location permission. OPSEC mode or STOP ends the listener. Accepting a fresh fix updates the map, navigation, almanac, weather/intel references and the status-bar GPS source/age; accepting saves the fix but keeps the listener open for newer fixes until you stop it." })),
      h("div", { class: "settings-actions" }, phoneStartButton, phoneStopButton, phoneAcceptButton),
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
    updateCoordPreview();
  }));

  /* ---------------------------------------------------- LAN / mobile access */
  const lanToggle = checkbox("Enable LAN / mobile access", !!settings.lan_access_enabled);
  const lanInput = lanToggle.querySelector("input");
  const lanStatus = h("div", { class: "small muted", text: "Checking..." });
  const lanUrl = h("div", { class: "small mono wrap-any", text: "" });
  const lanQr = h("img", { alt: "LAN access QR code", style: { display: "none", maxWidth: "320px", height: "auto", border: "1px solid var(--line)", background: "#fff" } });
  const lanCopy = btn("COPY URL", { size: "sm", icon: "copy", onClick: async () => {
    const url = lanUrl.textContent.trim();
    if (!url) return;
    try { await navigator.clipboard.writeText(url); toast("LAN URL copied", "ok"); }
    catch { toast("Could not copy; select the URL manually", "warn"); }
  } });
  const lanPanel = panel({
    title: "LAN / mobile access", icon: "wifi",
    sub: "Optional second HTTPS listener for phones/tablets on the trusted local network",
    body: h("div", { class: "col gap-8" },
      h("div", { class: "settings-subhead", text: "Listener" }),
      settingsDetails("Privacy and exposure",
        h("p", { class: "settings-note-text", text: "Binds a separate HTTPS listener to all interfaces using a self-signed certificate. The main API stays loopback-only. OPSEC mode disables LAN access. Do not expose to the public internet." })),
      lanToggle,
      h("div", { class: "row gap-6" }, h("span", { class: "small muted", text: "Status:" }), lanStatus),
      lanUrl,
      lanQr,
      h("div", { class: "settings-actions" }, lanCopy),
      h("div", { class: "tiny muted", text: "Scan the QR (or open the URL) on the phone, accept the certificate warning, and the session cookie is set." }),
    ),
  });
  addSectionHead("radio");
  grid.appendChild(lanPanel);
  lanInput.disabled = !!settings.opsec_mode;

  async function refreshLan() {
    try {
      const st = await api.get("/api/lan/status");
      lanInput.checked = !!st.enabled;
      lanStatus.textContent = st.enabled ? `ACTIVE · ${st.ip}:${st.port}` : "INACTIVE";
      if (st.enabled && st.url) {
        lanUrl.textContent = st.url;
        lanQr.src = `/api/lan/qr?${Date.now()}`;
        lanQr.style.display = "block";
      } else {
        lanUrl.textContent = "";
        lanQr.style.display = "none";
      }
    } catch (err) {
      lanStatus.textContent = err.message || "unavailable";
    }
  }
  lanInput.addEventListener("change", async () => {
    try {
      if (lanInput.checked) await api.post("/api/lan/start");
      else await api.post("/api/lan/stop");
      await refreshLan();
      // When LAN/mobile access is enabled, also start phone GPS on the LAN IP
      // if possible, so a mobile browser can provide a position fix.
      if (lanInput.checked) {
        try {
          const st = await api.get("/api/lan/status");
          if (st?.ip && !phoneSessionId) {
            if (![...phoneInterface.options].some(o => o.value === st.ip)) {
              const opt = document.createElement("option");
              opt.value = st.ip; opt.textContent = `${st.ip} (LAN)`;
              phoneInterface.appendChild(opt);
            }
            phoneInterface.value = st.ip;
            await startPhoneGps();
          }
        } catch {}
      }
      toast(lanInput.checked ? "LAN access started" : "LAN access stopped", "warn");
    } catch (err) {
      toast(err.message || "LAN toggle failed", "error");
      lanInput.checked = !lanInput.checked;
      await refreshLan();
    }
  });
  refreshLan();
  cleanups.push(store.subscribe((state, type) => {
    if (!["opsec", "settings"].includes(type)) return;
    const opsec = !!state.opsec || !!state.settings?.opsec_mode;
    lanInput.disabled = opsec;
    if (opsec) { api.post("/api/lan/stop").then(refreshLan).catch(() => {}); }
    else refreshLan();
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
      h("div", { class: "settings-subhead", text: "Gain defaults" }),
      h("div", { class: "form-grid" },
        field("LNA gain (dB)", lna), field("VGA gain (dB)", vga)),
      amp,
      h("div", { class: "hint", text: "Start with LNA 24–32 and VGA 30–40. Strong local signals may require less gain. Never connect a transmit antenna to the receive input while transmitting with another radio." }),
      h("div", { class: "settings-actions" },
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
    cls: "tx-gate span-2",
    body: h("div", { class: "col" },
      h("div", { class: "tx-warning" },
        h("strong", { text: "TRANSMITTING IS A LEGAL ACT. " }),
        "You must hold a valid licence or operate under an applicable emergency exception, use the correct power and bandwidth, and avoid interfering with distress, aviation, marine, public safety and protected bands. DarkSec logs every transmit attempt."),
      h("div", { class: "row wrap" },
        badge(txState ? "TX ENABLED" : "TX DISABLED", txState ? "red" : "green"),
        badge(settings.sdr_tx_attested ? "ATTESTED" : "NOT ATTESTED", settings.sdr_tx_attested ? "amber" : "muted")),
      txEnabled, txAttested,
      h("div", { class: "settings-actions" },
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
      h("div", { class: "settings-subhead", text: "Tile source" }),
      h("div", { class: "form-grid" },
        field("Tile source", tileSource, "Used for live view and for new offline downloads. Bundled and cached tiles are always preferred."),
        field("MBTiles path", mbtiles, "Must exist on this machine; read-only.")),
      mapVector,
      h("div", { class: "settings-subhead", text: "Layers" }),
      layersBox,
      h("div", { class: "settings-actions" },
        btn("SAVE MAP", { tone: "primary", icon: "save", onClick: saveMap }),
        btn("OPEN MAP", { icon: "map", onClick: () => { location.hash = "#/map"; } }))),
  });
  addSectionHead("maps");
  grid.appendChild(mapPanel);
  loadLayers(layersBox);

  /* ------------------------------------------------------------ library */
  const autoDeep = checkbox("Extract full text automatically when indexing", settings.auto_deep_index !== false);
  const deep = checkbox("Deep text extraction (slower, enables full-text search)", true);
  const force = checkbox("Re-index unchanged files", false);
  const libBox = h("div", { class: "col" });
  const bundleUrl = input({ value: settings.document_bundle_manifest_url || "", placeholder: "https://…/document-library.bundle.json" });
  const bundleProgress = progressBar(0);
  const bundleStatus = h("div", { class: "tiny muted", text: "Bundle status unavailable" });
  const bundleJobText = h("div", { class: "tiny muted", text: "" });
  const bundleDownloadBtn = btn("DOWNLOAD DOCUMENT LIBRARY", { tone: "primary", icon: "download", onClick: downloadBundle });
  const bundleCancelBtn = btn("CANCEL", { size: "sm", tone: "ghost", onClick: cancelBundle, disabled: true });
  const libPanel = panel({
    title: "Document library",
    sub: "Local archive + optional downloadable bundle",
    icon: "library",
    tools: [btn("OPEN", { size: "sm", onClick: () => { location.hash = "#/library"; } })],
    body: h("div", { class: "col" },
      libBox,
      h("div", { class: "settings-subhead", text: "Index" }),
      autoDeep,
      h("div", { class: "settings-check-grid" }, deep, force),
      h("div", { class: "settings-actions" },
        btn("START INDEX", { tone: "primary", icon: "database", onClick: startIndex }),
        btn("REFRESH", { icon: "refresh", onClick: refreshLibrary })),
      h("div", { class: "hint", text: "Deep indexing reads every PDF and can take hours on a large archive. It runs in the background; the console remains usable." }),
      h("div", { class: "settings-subhead", text: "Bundle" }),
      field("Manifest URL", bundleUrl, "Official bundle manifest from the DarkSec GitHub release. Leave this URL unless a newer release provides a different manifest."),
      h("div", { class: "settings-actions" }, bundleDownloadBtn, bundleCancelBtn),
      bundleProgress,
      bundleJobText,
      bundleStatus,
      settingsDetails("Redistribution and installer behavior",
        h("p", { class: "settings-note-text", text: "Only install a bundle you are legally allowed to redistribute. The installer verifies SHA-256, extracts document files into fielddata/library, and starts a metadata index. Existing files are kept." }))),
  });
  grid.appendChild(libPanel);
  renderLibrary(libBox, libStats);
  loadBundleStatus();

  /* ------------------------------------------------------------- feeds */
  const feedAuto = checkbox("Automatically fetch public feeds while online", !!settings.feed_auto_refresh);
  const feedInterval = select([
    { value: 5, label: "Every 5 minutes" },
    { value: 10, label: "Every 10 minutes (recommended)" },
    { value: 15, label: "Every 15 minutes" },
    { value: 30, label: "Every 30 minutes" },
    { value: 60, label: "Every 60 minutes" },
  ], settings.feed_refresh_min ?? 10);
  const feedHealthBox = h("div", { class: "col gap-6" });
  const feedHealthSummary = h("span", { text: "Source health & editing" });
  const feedHealthDetails = settingsDetails(feedHealthSummary,
    h("div", { class: "tiny muted mb-10", text: "Refresh tests the live source. Editing a URL or parser clears that feed's cached payload so old items are not shown under the new source." }),
    feedHealthBox);
  const feedPanel = panel({
    title: "Intelligence feeds",
    sub: "Automatic while online · cached offline",
    icon: "globe",
    body: h("div", { class: "col" },
      h("div", { class: "settings-subhead", text: "Automatic refresh" }),
      feedAuto,
      field("Check cadence", feedInterval, "Fast-moving safety sources still use their own conservative minimum intervals to respect upstream rate limits."),
      settingsDetails("What is fetched and when",
        h("p", { class: "settings-note-text", text: "When online and OPSEC is off, the console refreshes public feeds in the background: space weather, earthquakes, NWS alerts, GDACS/EONET hazards, Safecast radiation reports, volcanic activity, weather, aircraft, satellites and world news. OPSEC blackout pauses all automatic fetching. Offline, the last successful cache remains readable with its age shown. Treat all feed content as unverified until confirmed with an official source." })),
      h("div", { class: "settings-actions" },
        btn("SAVE SYNC SETTINGS", { icon: "save", onClick: saveFeed }),
        btn("SYNC NOW", { icon: "refresh", onClick: refreshFeeds })),
      feedHealthDetails),
  });
  addSectionHead("intel");
  grid.appendChild(feedPanel);
  loadFeedHealth(feedHealthBox).then((count) => {
    if (count) feedHealthSummary.textContent = `Source health & editing · ${count} feeds`;
  });

  /* ------------------------------------------------------- cctv catalog */
  const cctvCatalogBox = h("div", { class: "col gap-6 mt-10" });
  const cctvCatalogPanel = panel({
    title: "CCTV / camera catalog",
    sub: "Local/imported packs · cached global layer",
    icon: "camera",
    body: h("div", { class: "col" },
      h("div", { class: "settings-subhead", text: "Catalog" }),
      settingsDetails("Sources and attribution",
        h("p", { class: "settings-note-text", text: "Merges cached CCTV layers on the map: OSM surveillance locations (Overpass, operator-triggered) and local/imported camera catalogs. Imported items preserve provider credit/license metadata when available. Verify source terms before redistributing data or fetching snapshots. See docs/DATA_SOURCES.md." })),
      h("div", { class: "settings-actions" },
        btn("IMPORT/REFRESH CATALOG", { icon: "download", tone: "primary", onClick: () => importCctvCatalog(cctvCatalogBox) }),
        btn("REFRESH OSM CCTV", { icon: "refresh", onClick: () => refreshOsmCctv(cctvCatalogBox) }),
        btn("STATUS", { icon: "info", onClick: () => loadCctvCatalog(cctvCatalogBox) })),
      cctvCatalogBox),
  });
  grid.appendChild(cctvCatalogPanel);
  loadCctvCatalog(cctvCatalogBox);

  const alprGlobalBox = h("div", { class: "col gap-6" });
  const alprGlobalPanel = panel({
    title: "Global ALPR cameras",
    sub: "DeFlock / OpenStreetMap · ODbL",
    icon: "camera",
    cls: "span-2",
    body: h("div", { class: "col" },
      h("div", { class: "settings-subhead", text: "Overlay" }),
      settingsDetails("Coverage and license",
        h("p", { class: "settings-note-text", text: "Downloads the US and Canada DeFlock ALPR GeoJSON (~39 MB) into fielddata/maps/alpr. The map shows individual cameras when zoomed in and clusters at low zoom. Manual refresh only; locations are community-mapped — verify on OpenStreetMap." })),
      h("div", { class: "settings-actions" },
        btn("REFRESH GLOBAL ALPR", { icon: "download", tone: "primary", onClick: () => refreshGlobalAlpr(alprGlobalBox) }),
        btn("STATUS", { icon: "info", onClick: () => loadAlprStatus(alprGlobalBox) })),
      alprGlobalBox),
  });
  grid.appendChild(alprGlobalPanel);
  loadAlprStatus(alprGlobalBox);

  /* ------------------------------------------------------------ security */
  const secPanel = panel({
    title: "Data custody",
    icon: "shield",
    body: h("div", { class: "col" },
      h("div", { class: "settings-subhead", text: "Vault & exports" }),
      h("p", { class: "settings-note-text", text: "The vault remains unlocked until you manually lock it or the app exits. Lock it when you finish handling sensitive notes." }),
      h("div", { class: "settings-actions" },
        btn("VAULT & NOTES", { icon: "lock", onClick: () => { location.hash = "#/security?tab=vault"; } }),
        btn("EXPORT JSON", { icon: "download", onClick: () => download("/api/logistics/export?fmt=json", "darksec-export.json") }),
        btn("EXPORT CSV", { icon: "download", onClick: () => download("/api/logistics/export?fmt=csv", "darksec-export.csv") })),
      settingsDetails("Custody and recovery",
        h("p", { class: "settings-note-text", text: "Encrypted notes are never included in exports or backups in plaintext. Losing the vault passphrase means losing the notes; there is no recovery key." }))),
  });
  /* ---------------------------------------------------------- assistant */
  const assistantEnabled = checkbox("Enable Delamain (local AI / Ollama)", !!settings.assistant_enabled);
  const assistantThink = checkbox("Enable model thinking/reasoning", !!settings.assistant_think);
  const assistantTools = checkbox("Allow Delamain tools (live status, records, library, vault notes)", settings.assistant_tools !== false);
  const assistantToolsAuto = checkbox("Load tools only when needed (faster)", settings.assistant_tools_auto !== false);
  const assistantSystemTools = checkbox("Allow Delamain system file tools (list/read/write, non-sudo)", !!settings.assistant_system_tools);
  const assistantSystemRoot = input({ value: settings.assistant_system_root || "~", placeholder: "~" });
  const assistantMedicalTools = checkbox("Allow Delamain medical record reads (patients, vitals, meds)", !!settings.assistant_medical_tools);
  const assistantHistoryEncrypted = checkbox("Encrypt chat history with the vault (requires unlocked vault)", !!settings.assistant_history_encrypted);
  const assistantLibrary = checkbox("Auto-include library excerpts for manual/document questions", settings.assistant_library_context !== false);
  const assistantHistoryFilter = checkbox("Filter risky/refused topics from chat history", settings.assistant_history_filter !== false);
  const ollamaBase = input({ value: settings.ollama_base_url || "http://127.0.0.1:11434", placeholder: "http://127.0.0.1:11434" });
  const ollamaModel = input({ value: settings.ollama_model || "", placeholder: "e.g. llama3.2, mistral, qwen2.5:7b" });
  const assistantTemp = input({ type: "number", step: "0.1", min: "0", max: "2", value: settings.assistant_temperature ?? 0.2 });
  const assistantTimeout = input({ type: "number", step: "1", min: "5", max: "1800", value: settings.assistant_timeout_sec ?? 300 });
  const assistantCtx = input({ type: "number", step: "512", min: "512", max: "32768", value: settings.assistant_num_ctx ?? 4096 });
  const assistantMaxTokens = input({ type: "number", step: "512", min: "128", max: "8192", value: settings.assistant_max_tokens ?? 2048 });
  const assistantPrompt = h("textarea", { class: "input mono", rows: "5", style: { width: "100%", resize: "vertical" } });
  assistantPrompt.value = settings.assistant_system_prompt || "";
  const assistantSafety = settingsDetails("Safety, privacy and tool boundaries",
    h("div", { class: "col gap-6" },
      h("div", { class: "callout small", text: "Thinking/reasoning can improve complex answers but is much slower on CPU-only systems (especially Qwen 3.5). If responses stall, disable thinking or increase timeout." }),
      h("div", { class: "callout small amber", text: "The app calls Ollama through the local backend. AI output can be wrong or incomplete—verify critical medical, navigation, legal, and hazard decisions." }),
      h("div", { class: "callout small amber", text: "System file tools run as your user with no sudo. Writes are restricted to the configured root, existing files are backed up, and credential/key paths are blocked. Leave this off unless you need it." }),
      h("div", { class: "callout small amber", text: "Medical record tools are read-only and expose patient identities, vitals and medication history to the local model. Verify patient identity and do not treat AI output as diagnosis or prescription." }),
      h("div", { class: "callout small amber", text: "Encrypted chat history stores conversation titles and messages with the vault key. The vault must be configured and unlocked; when it is locked, chats are not saved and history cannot be read. Toggling the setting migrates existing history while the vault is unlocked." })));
  assistantSafety.classList.add("warn");
  const assistantPanel = panel({
    title: "Delamain / Ollama",
    sub: "Local AI model integration for assistant chat and field support",
    icon: "cpu",
    cls: "span-2",
    body: h("div", { class: "settings-assistant-layout" },
      h("div", { class: "col gap-10" },
        assistantEnabled,
        h("div", { class: "settings-subhead", text: "Tools & behavior" }),
        h("div", { class: "settings-check-grid" },
          assistantTools, assistantToolsAuto, assistantLibrary, assistantThink,
          assistantSystemTools, assistantMedicalTools, assistantHistoryEncrypted, assistantHistoryFilter),
        h("div", { class: "settings-subhead", text: "Model connection" }),
        h("div", { class: "form-grid" },
          field("Ollama base URL", ollamaBase, "Local endpoint. Default: http://127.0.0.1:11434"),
          field("Model", ollamaModel, "Must be pulled in Ollama (e.g. `ollama pull llama3.2`)."),
          h("div", { class: "wide" }, field("System tools root", assistantSystemRoot, "Paths are restricted to this root. Sensitive credential paths are blocked. Runs as your user; never sudo.")))),
      h("div", { class: "col gap-10" },
        h("div", { class: "settings-subhead", text: "Generation limits" }),
        h("div", { class: "form-grid" },
          field("Temperature", assistantTemp), field("Timeout (sec)", assistantTimeout, "Per-request Ollama read timeout. The chat waits this long for activity before reporting a stall."),
          field("Context tokens", assistantCtx), field("Max reply tokens", assistantMaxTokens, "Raise for long guides. 2048 is the default; higher is slower on CPU. If answers stop mid-sentence, increase this.")),
        field("System prompt", assistantPrompt, "Guides behavior/safety. Keep it practical; avoid secrets."),
        h("div", { class: "settings-actions" },
          btn("SAVE ASSISTANT", { tone: "primary", icon: "save", onClick: saveAssistant }),
          btn("TEST CONNECTION", { icon: "refresh", onClick: testAssistant }),
          btn("OPEN ASSISTANT", { icon: "cpu", tone: "ghost", onClick: () => { location.hash = "#/assistant"; } })),
        assistantSafety)),
  });
  addSectionHead("assistant");
  grid.appendChild(assistantPanel);

  async function saveAssistant() {
    await saveSimple({
      assistant_enabled: assistantEnabled.querySelector("input").checked,
      assistant_think: assistantThink.querySelector("input").checked,
      assistant_tools: assistantTools.querySelector("input").checked,
      assistant_tools_auto: assistantToolsAuto.querySelector("input").checked,
      assistant_system_tools: assistantSystemTools.querySelector("input").checked,
      assistant_system_root: assistantSystemRoot.value.trim() || "~",
      assistant_medical_tools: assistantMedicalTools.querySelector("input").checked,
      assistant_history_encrypted: assistantHistoryEncrypted.querySelector("input").checked,
      assistant_library_context: assistantLibrary.querySelector("input").checked,
      assistant_history_filter: assistantHistoryFilter.querySelector("input").checked,
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
  const auditSummary = h("span", { text: "Recent audit events" });
  const auditDetails = settingsDetails(auditSummary, auditBox);
  const sysPanel = panel({
    title: "Platform & audit",
    icon: "cpu",
    cls: "span-2",
    tools: [btn("REFRESH", { size: "sm", icon: "refresh", onClick: () => loadAudit(auditBox).then((n) => { auditSummary.textContent = `Recent audit events · ${n}`; }) })],
    body: h("div", { class: "col" },
      h("div", { class: "settings-subhead", text: "Host" }),
      sysBox,
      h("div", { class: "settings-subhead", text: "Audit trail" }),
      auditDetails),
  });
  addSectionHead("system");
  grid.appendChild(sysPanel);
  grid.appendChild(secPanel);
  loadAudit(auditBox).then((n) => { auditSummary.textContent = `Recent audit events · ${n}`; });

  /* ------------------------------------------------------ program updates */
  const updateBox = h("div", { class: "col" });
  const updateMsg = h("div", { class: "callout small", text: "Checking local update state…" });
  const updateCheckBtn = btn("CHECK FOR UPDATES", { icon: "refresh", onClick: checkUpdates });
  const updateApplyBtn = btn("UPDATE NOW", { tone: "primary", icon: "download", onClick: applyUpdate, disabled: true });
  const updatePanel = panel({
    title: "Program updates",
    icon: "download",
    sub: "Fast-forward only · from the configured git remote",
    body: h("div", { class: "col gap-8" },
      h("div", { class: "settings-subhead", text: "Git remote" }),
      updateBox,
      updateMsg,
      h("div", { class: "settings-actions" }, updateCheckBtn, updateApplyBtn),
      settingsDetails("What an update changes",
        h("p", { class: "settings-note-text", text: "Updating fetches and fast-forwards program files from origin. Operator data in fielddata/ is ignored by git and is never changed. Uncommitted changes to tracked files block the update; untracked files are kept. Restart DarkSec after a successful update to run the new code. Checking for updates contacts the configured remote." }))),
  });
  grid.appendChild(updatePanel);
  loadUpdateStatus();

  /* ------------------------------------------------------------ backups */
  const backupsBox = h("div", { class: "list" });
  const backupPanel = panel({
    title: "Local backups",
    icon: "save",
    cls: "span-2",
    body: h("div", { class: "col" },
      h("div", { class: "settings-subhead", text: "Archives" }),
      h("p", { class: "settings-note-text", text: "Backups contain operator data and settings, not document files or plaintext notes. Copy them to removable media and protect them." }),
      h("div", { class: "settings-actions" }, btn("CREATE BACKUP", { tone: "primary", icon: "save", onClick: backupNow })),
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
      theme_mode: themeMode.value,
      dark_theme: darkTheme.value,
      light_theme: lightTheme.value,
      theme: themeMode.value === "light" ? lightTheme.value : darkTheme.value,
      accent: accent.value,
      font_scale: Number(fontScale.value),
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

  let updateInfo = null;

  function renderUpdate(info) {
    updateInfo = info || {};
    const rows = [
      ["Install", updateInfo.root || (updateInfo.frozen ? "packaged build" : "not a git checkout")],
      ["Version", `${updateInfo.version || status.version || "—"}${updateInfo.commit ? ` · ${updateInfo.commit}` : ""}`],
      ["Branch", updateInfo.branch || "—"],
      ["Remote", updateInfo.remote_url || "—"],
      ["Working tree", updateInfo.dirty == null ? "unknown"
        : updateInfo.dirty ? `modified · ${updateInfo.dirty_count} tracked file(s)`
        : updateInfo.untracked_count ? `clean · ${updateInfo.untracked_count} untracked file(s)`
        : "clean"],
    ];
    if (updateInfo.subject) rows.push(["Current commit", `${updateInfo.commit_date || ""} · ${updateInfo.subject}`]);
    if (updateInfo.latest?.commit) rows.push(["Latest", `${updateInfo.latest.commit_date || ""} · ${updateInfo.latest.subject || ""}`]);
    if (updateInfo.checked_at) rows.push(["Last check", fmt.date(updateInfo.checked_at)]);
    if (updateInfo.behind != null) rows.push(["Status", updateInfo.behind > 0 ? `${updateInfo.behind} update commit(s) available` : "up to date"]);
    updateBox.replaceChildren(kv(rows));
    if (updateInfo.dirty_files?.length) {
      const more = updateInfo.dirty_count > updateInfo.dirty_files.length ? " …" : "";
      updateBox.appendChild(h("div", { class: "tiny muted wrap-any", text: `Local changes: ${updateInfo.dirty_files.join(", ")}${more}` }));
    }
    if (updateInfo.untracked_count) {
      updateBox.appendChild(h("div", { class: "tiny muted", text: `${updateInfo.untracked_count} untracked file(s) will be kept; the update stops only if one would be overwritten.` }));
    }
    let text;
    if (updateInfo.error) text = updateInfo.error;
    else if (updateInfo.frozen) text = "Packaged builds cannot self-update. Install a newer release.";
    else if (updateInfo.repo === false) text = "This installation is not a git checkout, so self-update is unavailable.";
    else if (updateInfo.dirty) text = updateInfo.reason || "Local changes block updates.";
    else if (updateInfo.update_available) text = `Update available${updateInfo.latest?.commit ? `: ${updateInfo.latest.commit} · ${updateInfo.latest.subject}` : `: ${updateInfo.behind} commit(s) behind`}.`;
    else if (updateInfo.behind === 0) text = "Already up to date.";
    else text = "Press CHECK FOR UPDATES to contact the configured remote.";
    const problem = !!(updateInfo.error || updateInfo.frozen || updateInfo.repo === false || updateInfo.dirty || updateInfo.update_available);
    updateMsg.className = `callout small ${problem ? "amber" : ""}`.trim();
    updateMsg.textContent = text;
    updateApplyBtn.disabled = !updateInfo.can_update;
  }

  async function loadUpdateStatus() {
    try {
      renderUpdate(await api.get("/api/update/status"));
    } catch (err) {
      updateMsg.className = "callout small amber";
      updateMsg.textContent = err.message || "Update status unavailable";
    }
  }

  async function checkUpdates() {
    const label = updateCheckBtn.querySelector("span");
    const original = label?.textContent;
    updateCheckBtn.disabled = true;
    if (label) label.textContent = "CHECKING…";
    try {
      const info = await api.post("/api/update/check");
      renderUpdate(info);
      if (info.error) toast(info.error, "error");
      else if (info.update_available) toast(`${info.behind} update commit(s) available`, "warn");
      else toast("Already up to date");
    } catch (err) {
      toast(err.message || "Update check failed", "error");
    } finally {
      updateCheckBtn.disabled = false;
      if (label && original) label.textContent = original;
    }
  }

  async function applyUpdate() {
    const ok = await confirmDialog({
      title: "Update DarkSec",
      message: `Fetch and fast-forward program files from ${updateInfo?.remote_url || "origin"}? Operator data in fielddata/ is not changed. DarkSec must be restarted afterward.`,
      confirmText: "UPDATE",
      requireText: "UPDATE",
    });
    if (!ok) return;
    updateApplyBtn.disabled = true;
    updateCheckBtn.disabled = true;
    updateMsg.className = "callout small";
    updateMsg.textContent = "Updating from origin…";
    try {
      const info = await api.post("/api/update/apply");
      renderUpdate(info);
      if (!info.ok) toast(info.error || "Update failed", "error");
      else if (info.updated) toast("Updated. Restart DarkSec to run the new code.", "ok");
      else toast(info.message || "Already up to date");
    } catch (err) {
      toast(err.message || "Update failed", "error");
    } finally {
      updateCheckBtn.disabled = false;
      updateApplyBtn.disabled = !updateInfo?.can_update;
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

  async function browserGpsFix() {
    if (!navigator.geolocation) { toast("Browser geolocation not supported", "warn"); return; }
    toast("Requesting location permission…", "warn");
    navigator.geolocation.getCurrentPosition(async (pos) => {
      const c = pos.coords;
      lat.value = c.latitude;
      lon.value = c.longitude;
      if (Number.isFinite(c.altitude)) elevation.value = Math.round(c.altitude);
      const accuracy = Number.isFinite(c.accuracy) ? `±${fmt.num(c.accuracy, 1)} m` : "accuracy unavailable";
      const lanEnabled = !!(store.settings?.lan_access_enabled || lanInput?.checked);
      if (lanEnabled) {
        try {
          await store.saveSettings({
            lat: c.latitude,
            lon: c.longitude,
            elevation_m: Number.isFinite(c.altitude) ? Math.round(c.altitude) : store.settings?.elevation_m,
            position_source: "phone",
            position_updated_at: new Date().toISOString(),
            position_fix_at: new Date().toISOString(),
            position_accuracy_m: Number.isFinite(c.accuracy) ? c.accuracy : null,
            position_mode: 3,
          });
          await store.loadStatus({ force: true });
          const quality = positionConfidence(store.settings);
          positionStatus.className = `callout ${quality.tone} small`;
          positionStatus.textContent = `Phone/browser fix auto-saved over LAN (${accuracy}).`;
          toast("Phone location saved", "ok");
          await updateCoordPreview();
          return;
        } catch (err) {
          toast(err.message || "Could not save phone location", "error");
        }
      }
      positionStatus.className = "callout warn small";
      positionStatus.textContent = `Phone/browser fix captured (${accuracy}). Review the coordinate and SAVE POSITION to apply it; this is not live tracking.`;
      toast("Location captured — review and save", "ok");
      await updateCoordPreview();
    }, (err) => {
      toast(err.message || "Location permission denied", "error");
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
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
          ["PHONE POSITION", opsecActive(store.settings) ? "OP//MASKED" : fmt.coord(fix.lat, fix.lon, 6)],
          ["REPORTED ACCURACY", `±${fmt.num(fix.accuracy_m, 1)} m`],
          ["FIX TIME", fmt.date(fix.timestamp)],
          ["RECEIVED", `${fmt.ago(fix.received_at)} · ${fix.age_seconds}s old`],
          ["ALTITUDE", fix.alt_m != null ? `${fmt.num(fix.alt_m, 1)} m (not applied)` : "not reported"],
        ]));
        phoneAcceptButton.disabled = Number(fix.age_seconds) > 30;
        // Auto-accept when LAN/mobile access is enabled.
        const lanEnabled = !!(store.settings?.lan_access_enabled || lanInput?.checked);
        if (lanEnabled && Number(fix.age_seconds) <= 30 && fix.received_at && fix.received_at !== autoAcceptedReceivedAt) {
          autoAcceptedReceivedAt = fix.received_at;
          acceptPhoneGps().catch(() => {});
        }
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

  async function loadBundleStatus() {
    if (disposed) return;
    try {
      const st = await api.get("/api/library/bundle/status");
      const rows = [
        ["Local bundle files", fmt.num(st.local_files || 0, 0)],
        ["Local bundle size", fmt.bytes(st.local_bytes || 0)],
        ["Installed bundle", st.bundle_id || "—"],
        ["Installed", st.installed_at ? fmt.ago(st.installed_at) : "never"],
        ["Disk free", st.disk_free != null ? fmt.bytes(st.disk_free) : "—"],
      ];
      const box = h("div", { class: "col gap-4" }, kv(rows));
      if (st.error) box.appendChild(h("div", { class: "callout small amber", text: String(st.error).slice(0, 300) }));
      bundleStatus.replaceChildren(box);
      if (!bundleUrl.value && st.manifest_url) bundleUrl.value = st.manifest_url;
      renderBundleJob(st.job || {});
      if (st.job && st.job.running) pollBundle();
    } catch (err) {
      bundleStatus.textContent = err.message || "Bundle status unavailable";
    }
  }

  function renderBundleJob(job) {
    const running = !!job.running;
    bundleDownloadBtn.disabled = running;
    bundleCancelBtn.disabled = !running;
    const total = Number(job.total_bytes || 0);
    const done = Number(job.downloaded_bytes || 0);
    const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
    const fill = bundleProgress.querySelector("span");
    if (fill) fill.style.width = `${pct}%`;
    let text = "";
    if (running) {
      const part = job.part_count ? ` · part ${job.part_index}/${job.part_count}` : "";
      text = `${job.message || "Working"}${part}${done ? ` · ${fmt.bytes(done)}${total ? ` / ${fmt.bytes(total)}` : ""}` : ""}`;
    } else if (job.phase === "complete") {
      text = job.message || "Bundle installed";
    } else if (job.phase === "error") {
      text = job.error || job.message || "Bundle download failed";
    } else if (job.phase === "cancelled") {
      text = "Bundle download cancelled";
    }
    bundleJobText.textContent = text;
    if (job.phase === "complete" && lastBundlePhase !== "complete") {
      lastBundlePhase = "complete";
      toast("Document library bundle installed · metadata index started", "ok");
      refreshLibrary();
      loadBundleStatus();
    } else if (job.phase === "error" && lastBundlePhase !== "error") {
      lastBundlePhase = "error";
      toast(text || "Bundle download failed", "error");
    } else if (job.phase === "cancelled" && lastBundlePhase !== "cancelled") {
      lastBundlePhase = "cancelled";
      toast("Bundle download cancelled", "warn");
    }
  }

  async function pollBundle() {
    if (bundlePoll) clearTimeout(bundlePoll);
    if (disposed) return;
    try {
      const job = await api.get("/api/library/bundle/job");
      renderBundleJob(job);
      if (job.running) {
        bundlePoll = setTimeout(pollBundle, 1000);
      } else {
        loadBundleStatus();
      }
    } catch (err) {
      bundleJobText.textContent = err.message || "Could not read bundle progress";
      bundlePoll = setTimeout(pollBundle, 2000);
    }
  }

  async function downloadBundle() {
    const url = bundleUrl.value.trim();
    if (!url) {
      toast("Enter a document bundle manifest URL", "warn");
      bundleUrl.focus();
      return;
    }
    const ok = await confirmDialog({
      title: "Download document library",
      message: "Download and extract this document bundle? It can be several gigabytes. Only install a bundle you are allowed to redistribute. Existing files are kept.",
      confirmText: "DOWNLOAD",
    });
    if (!ok) return;
    try {
      await store.saveSettings({ document_bundle_manifest_url: url });
      const result = await api.post("/api/library/bundle/download", { url });
      if (!result.ok) {
        toast(result.error || "Could not start bundle download", "error");
        return;
      }
      lastBundlePhase = "";
      toast("Document library download started", "warn");
      pollBundle();
    } catch (err) {
      toast(err.message || "Could not start bundle download", "error");
    }
  }

  async function cancelBundle() {
    try {
      await api.post("/api/library/bundle/cancel");
      toast("Cancelling bundle download…", "warn");
      pollBundle();
    } catch (err) {
      toast(err.message || "Could not cancel bundle download", "error");
    }
  }

  function renderLibrary(target, stats) {
    target.replaceChildren();
    target.appendChild(kv([
      ["Documents", fmt.num(stats.documents || 0, 0)],
      ["Full text", fmt.num(stats.text_indexed || 0, 0)],
      ["Text extracted", fmt.bytes(stats.chars || 0)],
      ["Index size", fmt.bytes(stats.db_bytes || 0)],
    ]));
    const ex = stats.extractors || {};
    const tools = [
      ["pdftotext", !!ex.pdftotext],
      ["pypdf", !!ex.pypdf],
      ["OCR", !!ex.ocr],
      ["DjVu", !!ex.djvutxt],
    ];
    const missing = tools.filter(([, ok]) => !ok).map(([name]) => name);
    if (missing.length) {
      target.appendChild(h("div", { class: "callout small amber mt-10", text: `Missing text tools: ${missing.join(", ")}. Run ./install.sh to install poppler, tesseract OCR and DjVu support.` }));
    } else {
      target.appendChild(h("div", { class: "tiny muted mt-10", text: "Text tools: pdftotext · pypdf · OCR · DjVu" }));
    }
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
      target.replaceChildren();
      if (!list.length) { target.appendChild(h("div", { class: "small muted", text: "No audit events." })); return 0; }
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
      return list.length;
    } catch (err) {
      target.replaceChildren(h("div", { class: "small danger-text", text: err.message }));
      return 0;
    }
  }

  async function loadBackups(target) {
    try {
      const out = await api.get("/api/logistics/backups");
      target.replaceChildren();
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

  async function loadFeedHealth(box) {
    box.replaceChildren(loading("READING FEED STATUS"));
    let out;
    try {
      out = await api.get("/api/feeds");
    } catch (err) {
      box.replaceChildren(h("div", { class: "callout danger small", text: err.message || "Could not read feed status" }));
      return 0;
    }
    const feeds = (out.feeds || []).slice().sort((a, b) => String(a.name).localeCompare(String(b.name)));
    if (!feeds.length) {
      box.replaceChildren(emptyState("No feeds registered", "The built-in feed registry could not be read.", "globe"));
      return 0;
    }
    const list = h("div", { class: "list" });
    for (const f of feeds) {
      const tone = f.status === "OK" ? "green" : f.status === "STALE" ? "amber" : f.status === "ERROR" ? "red" : "muted";
      const row = h("div", { class: "list-item" });
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "row between items-center gap-8" },
        h("div", { class: "li-title", text: f.name }),
        badge(f.status || "NEVER", tone)));
      const bits = [String(f.kind || "").toUpperCase(), `${f.count ?? 0} item(s)`, `fetched ${fmt.ago(f.fetched_at)}`];
      if (f.stale_reason) bits.push(`stale: ${f.stale_reason}`);
      main.appendChild(h("div", { class: "li-sub", text: bits.filter(Boolean).join(" · ") }));
      if (f.error) main.appendChild(h("div", { class: "li-sub", style: { color: "var(--red)" }, text: String(f.error).slice(0, 200) }));
      row.appendChild(main);
      const tools = h("div", { class: "row gap-6 wrap" });
      tools.append(
        btn("TEST", { size: "sm", tone: "ghost", icon: "refresh", onClick: () => testFeed(f, box) }),
        btn("EDIT", { size: "sm", tone: "ghost", icon: "edit", onClick: () => editFeed(f, box) }));
      row.appendChild(tools);
      list.appendChild(row);
    }
    box.replaceChildren(list);
    return feeds.length;
  }

  async function testFeed(feed, box) {
    toast(`${feed.name}: testing source`, "warn");
    try {
      const out = await api.post("/api/feeds/refresh", { id: feed.id });
      const result = (out.results || [])[0] || {};
      if (result.status === "OK") toast(`${feed.name}: ${result.count ?? 0} items`);
      else toast(`${feed.name}: ${result.error || result.status || "failed"}`, "error");
    } catch (err) {
      toast(err.message || "Feed test failed", "error");
    }
    loadFeedHealth(box);
  }

  function editFeed(feed, box) {
    const name = input({ value: feed.name || "" });
    const url = input({ value: feed.url || "" });
    const kind = input({ value: feed.kind || "" });
    const m = modal({
      title: `Edit feed · ${feed.id}`,
      sub: "Local source configuration",
      body: h("div", { class: "col gap-10" },
        field("Display name", name),
        field("Source URL", url, "Changing the URL clears this feed's cached payload so old items are not presented under the new source."),
        field("Parser kind", kind, "Must match an implemented parser: rss, gdacs, eonet, usgs, celestrak, opensky, swpc_kp, weather, air_quality, who_news…"),
        h("div", { class: "callout small", text: `Feed ID ${feed.id} is fixed. Position-dependent feeds override the URL with the configured latitude/longitude at refresh time.` })),
      actions: [
        btn("CANCEL", { tone: "ghost", onClick: () => m.close() }),
        btn("SAVE", { tone: "primary", icon: "save", onClick: async () => {
          try {
            await api.put(`/api/feeds/${encodeURIComponent(feed.id)}`, {
              name: name.value.trim(), url: url.value.trim(), kind: kind.value.trim(),
            });
            toast("Feed updated");
            m.close();
            loadFeedHealth(box);
          } catch (err) {
            toast(err.message || "Could not update feed", "error");
          }
        } }),
      ],
    });
  }

  async function refreshFeeds() {
    toast("Refreshing public feeds — network required", "warn");
    try {
      const out = await api.post("/api/feeds/refresh", {});
      const ok = (out.results || []).filter((r) => r.status === "OK").length;
      toast(`Feeds refreshed: ${ok}/${(out.results || []).length} OK`);
    } catch (err) { toast(err.message || "Feed refresh failed", "error"); }
  }

  async function loadAlprStatus(box) {
    box.replaceChildren(loading("READING GLOBAL ALPR STATUS"));
    try {
      const st = await api.get("/api/map/alpr/status");
      const counts = st.counts || {};
      const total = Object.values(counts).reduce((sum, n) => sum + Number(n || 0), 0);
      const rows = [
        ["Dataset", st.available ? "AVAILABLE" : "NOT DOWNLOADED"],
        ["Features", total ? fmt.num(total, 0) : "—"],
        ["US / Canada", `${counts.us != null ? fmt.num(counts.us, 0) : "—"} / ${counts.ca != null ? fmt.num(counts.ca, 0) : "—"}`],
        ["Downloaded", st.fetched_at ? fmt.ago(st.fetched_at) : "never"],
        ["Size", fmt.bytes(st.bytes)],
        ["Source", "deflock.org / OpenStreetMap (ODbL)"],
      ];
      const out = h("div", { class: "col gap-6" }, kv(rows));
      if (st.error) out.appendChild(h("div", { class: "callout small amber", text: String(st.error).slice(0, 300) }));
      if (!st.available) out.appendChild(h("div", { class: "callout small amber", text: "No global ALPR dataset cached yet. Use REFRESH GLOBAL ALPR while online." }));
      box.replaceChildren(out);
    } catch (err) {
      box.replaceChildren(h("div", { class: "callout small amber", text: err.message || "Could not read global ALPR status" }));
    }
  }

  async function refreshGlobalAlpr(box) {
    const ok = await confirmDialog({
      title: "Refresh global ALPR dataset",
      message: "Download the US and Canada DeFlock ALPR GeoJSON (~39 MB) into fielddata/maps/alpr? This contacts data.dontgetflocked.com. Existing cached data remains if the download fails.",
      confirmText: "DOWNLOAD",
    });
    if (!ok) return;
    box.replaceChildren(h("div", { class: "callout small", text: "Downloading global ALPR dataset… this can take a minute." }));
    try {
      const result = await api.post("/api/map/alpr/refresh");
      const total = Object.values(result.counts || {}).reduce((sum, n) => sum + Number(n || 0), 0);
      if (result.ok) toast(`Global ALPR refreshed · ${fmt.num(total, 0)} features`, "ok");
      else toast(result.error || "Global ALPR refresh failed", "error");
    } catch (err) {
      toast(err.message || "Global ALPR refresh failed", "error");
    }
    await loadAlprStatus(box);
  }

  async function loadCctvCatalog(box) {
    box.replaceChildren(loading("READING CCTV CATALOG STATUS"));
    try {
      const [out, packsOut] = await Promise.all([
        api.get("/api/cctv/catalog/status").catch(() => ({})),
        api.get("/api/cctv/catalog/packs").catch(() => ({})),
      ]);
      const feed = out.feed || {};
      const tone = feed.status === "OK" ? "green" : feed.status === "ERROR" ? "red" : "muted";
      const rows = h("div", { class: "col gap-6" });
      rows.appendChild(h("div", { class: "row between items-center gap-8" },
        h("div", { class: "li-title", text: feed.name || "CCTV catalog" }),
        badge(feed.status || "NEVER", tone)));
      rows.appendChild(kv([
        ["Cached items", String(out.item_count ?? 0)],
        ["Fetched", fmt.ago(feed.fetched_at)],
      ]));
      const fc = out.frame_cache || {};
      if (fc.entries != null) rows.appendChild(kv([["Frame cache", `${fc.entries} entries · ${Object.entries(fc.status_counts || {}).map(([k,v])=>`${k}:${v}`).join(" · ") || "no fetches"}`]]));
      if (out.catalog_refresh_hours) rows.appendChild(kv([["Refresh policy", `controlled cadence hint: ${out.catalog_refresh_hours}h (operator-managed)`]]));
      if (Array.isArray(fc.recent_errors) && fc.recent_errors.length) {
        const errBox = h("div", { class: "callout warn small mt-4" });
        errBox.appendChild(h("div", { class: "tiny mono", text: `RECENT FRAME/STREAM ERRORS (${fc.recent_errors.length})` }));
        for (const e of fc.recent_errors.slice(0, 5)) errBox.appendChild(h("div", { class: "tiny dim mt-2", style: { overflowWrap: "anywhere" }, text: `${String(e.id || '').slice(0, 48)} · ${String(e.at || '').slice(0, 19)} · ${String(e.message || '').slice(0, 120)}` }));
        rows.appendChild(errBox);
      }
      if (out.source_note) rows.appendChild(h("div", { class: "tiny muted", text: String(out.source_note).slice(0, 240) }));
      if (feed.error) rows.appendChild(h("div", { class: "callout danger small", text: String(feed.error).slice(0, 240) }));

      const packs = Array.isArray(packsOut.packs) ? packsOut.packs : (Array.isArray(out.pack_stats) ? out.pack_stats : []);
      rows.appendChild(h("div", { class: "side-label mt-8", text: `CATALOG PACKS (${packs.length})` }));
      rows.appendChild(h("div", { class: "tiny dim", text: "Enable/disable import sources (by file). Save changes, then Import/Refresh Catalog. Frame snapshots use registered URLs only with throttled transient caching." }));
      if (packs.length) {
        const list = h("div", { class: "col gap-4 mt-4" });
        const inputs = [];
        for (const p of packs.slice(0, 30)) {
          const name = String(p.source || p.path || "pack");
          const enabled = p.enabled !== false;
          const inp = h("input", { type: "checkbox", checked: enabled });
          inputs.push({ inp, name, path: String(p.path || "") });
          const meta = [];
          if (p.count != null) meta.push(`${p.count} items`);
          if (p.error) meta.push(`error: ${String(p.error).slice(0, 80)}`);
          list.appendChild(h("label", { class: "row items-start gap-8", style: { cursor: "pointer" } },
            inp,
            h("div", { class: "col", style: { minWidth: 0 } },
              h("div", { class: "tiny mono", style: { overflowWrap: "anywhere" }, text: name }),
              meta.length ? h("div", { class: "tiny dim", text: meta.join(" · ") }) : null)));
        }
        if (packs.length > 30) list.appendChild(h("div", { class: "tiny dim", text: `…and ${packs.length - 30} more packs` }));
        rows.appendChild(list);
        rows.appendChild(h("div", { class: "row mt-8" }, btn("SAVE PACK SELECTION", { size: "sm", icon: "save", onClick: async () => {
          const disabled = inputs.filter(x => !x.inp.checked).map(x => x.name);
          try {
            await api.put("/api/cctv/catalog/packs", { disabled });
            toast(`Saved ${disabled.length} disabled pack(s). Run Import/Refresh Catalog to apply.`);
          } catch (err) { toast(err.message || "Could not save pack selection", "error"); }
          loadCctvCatalog(box);
        }})));
      } else {
        rows.appendChild(h("div", { class: "tiny dim mt-4", text: "No catalog packs detected yet. Add JSON catalogs under fielddata/maps/cctv_catalogs/ or place a local gods-eye-view checkout, then Import/Refresh Catalog." }));
      }
      box.replaceChildren(rows);
    } catch (err) {
      box.replaceChildren(h("div", { class: "callout danger small", text: err.message || "Could not read CCTV catalog status" }));
    }
  }

  async function importCctvCatalog(box) {
    toast("Importing local CCTV catalogs…", "warn");
    try {
      const out = await api.post("/api/cctv/catalog/import", {});
      const result = (out.results || [])[0] || {};
      if (result.status === "OK") toast(`CCTV catalog imported: ${result.count ?? 0} items`);
      else toast(`CCTV catalog: ${result.error || result.status || "failed"}`, "error");
    } catch (err) {
      toast(err.message || "CCTV catalog import failed", "error");
    }
    loadCctvCatalog(box);
    loadFeedHealth(feedHealthBox);
  }

  async function refreshOsmCctv(box) {
    toast("Refreshing OSM CCTV (Overpass, network required)…", "warn");
    try {
      const out = await api.post("/api/feeds/refresh", { id: "public_cctv" });
      const result = (out.results || [])[0] || {};
      if (result.status === "OK") toast(`OSM CCTV refreshed: ${result.count ?? 0} items`);
      else toast(`OSM CCTV: ${result.error || result.status || "failed"}`, "error");
    } catch (err) {
      toast(err.message || "OSM CCTV refresh failed", "error");
    }
    loadCctvCatalog(box);
    loadFeedHealth(feedHealthBox);
  }

  async function updateCoordPreview() {
    if (opsecActive(store.settings)) {
      coordPreview.replaceChildren(h("div", { class: "coord-line" },
        h("span", { text: "COORDINATES" }), h("b", { text: "OP//MASKED" })));
      return;
    }
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
    if (bundlePoll) clearTimeout(bundlePoll);
    for (const fn of cleanups) try { fn(); } catch {}
  };
}
