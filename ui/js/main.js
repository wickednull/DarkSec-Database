import { api, download } from "./api.js";
import { store } from "./store.js";
import {
  h, append, icon, hydrateIcons, toast, modal, btn, input, field, emptyState, fmt,
} from "./components.js";
import { openRecordForm } from "./records.js";
import { positionConfidence } from "./views/shared.js";

import * as dashboard from "./views/dashboard.js";
import * as mapView from "./views/map.js?v=map-upgrade-8";
import * as library from "./views/library.js";
import * as manual from "./views/manual.js";
import * as navView from "./views/nav.js";
import * as comms from "./views/comms.js";
import * as sdr from "./views/sdr.js";
import * as medical from "./views/medical.js";
import * as supply from "./views/supply.js?v=better-5";
import * as cbrn from "./views/cbrn.js";
import * as security from "./views/security.js";
import * as logistics from "./views/logistics.js";
import * as weather from "./views/weather.js";
import * as intel from "./views/intel.js?v=better-7";
import * as assistant from "./views/assistant.js?v=better-6";
import * as settings from "./views/settings.js?v=better-7";

const ROUTES = {
  dashboard: { title: "Situation Report", group: "COMMAND", icon: "dashboard", view: dashboard },
  map: { title: "Map & Terrain", group: "COMMAND", icon: "map", view: mapView },
  intel: { title: "Global Threat & Live Intel", group: "COMMAND", icon: "warning", view: intel },
  assistant: { title: "Delamain (Local AI)", group: "COMMAND", icon: "cpu", view: assistant },
  library: { title: "Document Library", group: "COMMAND", icon: "library", view: library },
  manual: { title: "Field Manual", group: "COMMAND", icon: "book", view: manual },
  nav: { title: "Navigation", group: "OPERATIONS", icon: "compass", view: navView },
  comms: { title: "Communications", group: "OPERATIONS", icon: "radio", view: comms },
  sdr: { title: "SDR / Spectrum", group: "OPERATIONS", icon: "antenna", view: sdr },
  medical: { title: "Field Medicine", group: "OPERATIONS", icon: "medical", view: medical },
  supply: { title: "Water / Food / Power", group: "OPERATIONS", icon: "drop", view: supply },
  cbrn: { title: "CBRN", group: "OPERATIONS", icon: "radiation", view: cbrn },
  security: { title: "Security & Vault", group: "OPERATIONS", icon: "shield", view: security },
  logistics: { title: "Logistics", group: "OPERATIONS", icon: "box", view: logistics },
  weather: { title: "Weather & Environment", group: "OPERATIONS", icon: "cloud", view: weather },
  settings: { title: "System Settings", group: "SYSTEM", icon: "settings", view: settings },
};

const NAV_ORDER = [
  ["COMMAND", ["dashboard", "map", "intel", "assistant", "library", "manual"]],
  ["OPERATIONS", ["nav", "comms", "sdr", "medical", "supply", "cbrn", "security", "logistics", "weather"]],
  ["SYSTEM", ["settings"]],
];

let cleanupCurrent = null;
let clockTimer = null;
let statusTimer = null;
let syncTimer = null;
let syncLastSuccess = null;
let paletteOpen = false;
let palettePreviousFocus = null;

function restorePaletteFocus() {
  const el = palettePreviousFocus;
  palettePreviousFocus = null;
  if (el && typeof el.focus === "function" && el.isConnected) {
    try { el.focus(); } catch {}
  }
}
let coordTimer = null;
let netDown = false;
let lastStatus = null;

/* ------------------------------------------------------------------ boot */

async function boot() {
  const bootEl = document.getElementById("boot");
  try {
    await store.loadStatus();
    lastStatus = store.state.status;
  } catch (err) {
    showBootError(err);
    return;
  }
  hydrateIcons(document);
  buildNavigation();
  bindChrome();
  applyAppearance();
  updateStatusUI(lastStatus);
  document.getElementById("app").hidden = false;
  try {
    if (localStorage.getItem("darksec.nav.collapsed") === "1") {
      document.getElementById("app")?.classList.add("nav-collapsed");
      const m = document.getElementById("menuBtn");
      if (m) { m.setAttribute("aria-expanded", "false"); m.setAttribute("aria-label", "Expand navigation"); }
    }
  } catch {}
  bootEl.classList.add("done");
  setTimeout(() => bootEl.remove(), 600);
  window.addEventListener("hashchange", route);
  route();
  startClock();
  refreshGrid();
  pollSync();
  syncTimer = setInterval(() => {
    if (!document.hidden) pollSync();
  }, 15_000);
  statusTimer = setInterval(async () => {
    if (document.hidden) return;
    try {
      const status = await store.loadStatus();
      lastStatus = status;
      netDown = false;
      updateStatusUI(status);
      updateBadges(status);
      touchNet();
    } catch (err) {
      netDown = true;
      touchNet();
      if (err.status === 401) authLost();
    }
  }, 30_000);
  store.subscribe((state, type) => {
    if (type === "settings") {
      applyAppearance();
      refreshGrid();
    }
    if (type === "status" && state.status) {
      lastStatus = state.status;
      updateStatusUI(state.status);
      refreshGrid();
    }
  });
}

function showBootError(err) {
  const bootEl = document.getElementById("boot");
  if (!bootEl) return;
  bootEl.replaceChildren();
  bootEl.classList.add("boot-error-wrap");
  const box = h("div", { class: "boot-error" });
  box.appendChild(h("div", { class: "boot-mark", text: "DS" }));
  box.appendChild(h("div", { class: "boot-text", text: "DARKSEC NIGHTWATCH" }));
  const msg = err.status === 401
    ? "AUTHENTICATION REQUIRED — launch the console from the DarkSec application so a local session token is issued."
    : (err.message || "The local field console failed to start.");
  box.appendChild(h("div", { class: "boot-error", text: msg }));
  if (err.status !== 401) {
    box.appendChild(h("div", { class: "tiny muted mt-10", text: "Start the backend with: python3 -m darksec --port 8765" }));
  }
  box.appendChild(h("div", { class: "tiny muted mt-10", text: "If this is a fresh session, relaunch DarkSec and try again." }));
  bootEl.appendChild(box);
}

/* --------------------------------------------------------------- chrome */

function buildNavigation() {
  for (const [group, ids] of NAV_ORDER) {
    const target = group === "COMMAND" ? document.getElementById("navPrimary")
      : group === "OPERATIONS" ? document.getElementById("navOps")
      : document.getElementById("navSystem");
    if (!target) continue;
    for (const id of ids) {
      const route = ROUTES[id];
      const link = h("a", { href: `#/${id}`, dataset: { nav: id }, title: route.title });
      link.appendChild(icon(route.icon));
      link.appendChild(h("span", { class: "nav-text", text: route.title }));
      link.appendChild(h("span", { class: "nav-badge", dataset: { badge: id }, text: "" }));
      target.appendChild(link);
    }
  }
}

function updateBadges(status = lastStatus) {
  if (!status) return;
  const counts = status.counts || {};
  const set = (id, value) => {
    const el = document.querySelector(`[data-badge="${id}"]`);
    if (!el) return;
    el.textContent = value ? String(value) : "";
    el.style.display = value ? "inline-block" : "none";
  };
  set("logistics", counts.tasks_open);
  set("medical", counts.patients);
  set("security", counts.incidents_open);
  set("sdr", counts.recordings);
  set("dashboard", status.feeds?.critical_count);
}

function bindChrome() {
  const menu = document.getElementById("menuBtn");
  const sidebar = document.getElementById("sidebar");
  const scrim = document.getElementById("sidebarScrim");
  if (menu) menu.addEventListener("click", () => {
    const mobile = window.matchMedia("(max-width: 860px)").matches;
    if (mobile) {
      sidebar.classList.toggle("open");
      scrim.classList.toggle("open", sidebar.classList.contains("open"));
      menu.setAttribute("aria-expanded", sidebar.classList.contains("open") ? "true" : "false");
      return;
    }
    const app = document.getElementById("app");
    const collapsed = app.classList.toggle("nav-collapsed");
    try { localStorage.setItem("darksec.nav.collapsed", collapsed ? "1" : "0"); } catch {}
    menu.setAttribute("aria-expanded", collapsed ? "false" : "true");
    menu.setAttribute("aria-label", collapsed ? "Expand navigation" : "Collapse navigation");
  });
  if (scrim) scrim.addEventListener("click", () => {
    sidebar.classList.remove("open");
    scrim.classList.remove("open");
  });
  document.querySelectorAll(".nav a").forEach((a) => a.addEventListener("click", () => {
    sidebar.classList.remove("open");
    scrim.classList.remove("open");
  }));

  const assistantBtn = document.getElementById("assistantBtn");
  if (assistantBtn) assistantBtn.addEventListener("click", () => { location.hash = "#/assistant"; closeSidebar(); });
  const searchBtn = document.getElementById("searchBtn");
  if (searchBtn) searchBtn.addEventListener("click", openPalette);

  const opsecToggle = document.getElementById("opsecToggle");
  if (opsecToggle) opsecToggle.addEventListener("click", () => toggleOpsec(!store.state.opsec));

  const vaultPill = document.getElementById("vaultPill");
  if (vaultPill) vaultPill.addEventListener("click", () => { location.hash = "#/security?tab=vault"; });

  const netPill = document.getElementById("netPill");
  if (netPill) netPill.addEventListener("click", async () => {
    try {
      await api.post("/api/sync/now", {});
      await pollSync();
      toast("Sync cycle requested — public feeds refresh in the background");
    } catch { toast("Backend unreachable", "error"); }
  });

  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "k") {
      e.preventDefault();
      openPalette();
    } else if (e.key === "Escape") {
      closeSidebar();
      closePalette();
    } else if (e.key === "/" && !isTyping(e.target)) {
      e.preventDefault();
      openPalette();
    }
  });
  window.addEventListener("online", () => { pollSync(); toast("Network online — automatic sync resumes"); });
  window.addEventListener("offline", () => { netDown = false; touchNet(); toast("Network offline — cached data remains available", "warn"); });
  window.addEventListener("darksec:auth", authLost);
  window.addEventListener("beforeunload", () => { if (cleanupCurrent) try { cleanupCurrent(); } catch {} });
}

function isTyping(el) {
  if (!el) return false;
  return ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) || el.isContentEditable;
}

function closeSidebar() {
  const sidebar = document.getElementById("sidebar");
  const scrim = document.getElementById("sidebarScrim");
  const menu = document.getElementById("menuBtn");
  sidebar?.classList.remove("open");
  scrim?.classList.remove("open");
  menu?.setAttribute("aria-expanded", "false");
}

function applyAppearance() {
  const s = store.settings || {};
  document.documentElement.dataset.accent = s.accent || "green";
  document.body.classList.toggle("no-scan", !s.scanlines);
  document.body.classList.toggle("opsec", !!s.opsec_mode);
  const opsecToggle = document.getElementById("opsecToggle");
  if (opsecToggle) opsecToggle.setAttribute("aria-checked", s.opsec_mode ? "true" : "false");
  const scan = document.getElementById("scanlines");
  if (scan) scan.style.display = s.scanlines ? "" : "none";
}

async function toggleOpsec(value) {
  try {
    await store.saveSettings({ opsec_mode: !!value });
    toasts();
    toast(value ? "OPSEC blackout engaged — sensitive fields blurred" : "OPSEC blackout off", value ? "warn" : "");
    updateStatusUI(store.state.status || lastStatus);
  } catch (err) {
    toast(err.message || "Could not change OPSEC mode", "error");
  }
}

function toasts() {}

function startClock() {
  const local = document.getElementById("clockLocal");
  const utc = document.getElementById("clockUtc");
  const tick = () => {
    const now = new Date();
    if (local) local.textContent = now.toLocaleTimeString(undefined, { hour12: false });
    if (utc) utc.textContent = `UTC ${String(now.getUTCHours()).padStart(2, "0")}:${String(now.getUTCMinutes()).padStart(2, "0")}`;
    const epoch = document.getElementById("sbCoords");
    if (epoch) {
      const e = store.settings?.epoch;
      if (e) {
        const target = new Date(e);
        if (!Number.isNaN(target.getTime())) {
          const delta = Math.floor((Date.now() - target.getTime()) / 1000);
          const sign = delta >= 0 ? "H+" : "H-";
          const abs = Math.abs(delta);
          const d = Math.floor(abs / 86400), hh = Math.floor((abs % 86400) / 3600), mm = Math.floor((abs % 3600) / 60);
          epoch.textContent = `${sign}${d}d ${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
          return;
        }
      }
      if (lastStatus) epoch.textContent = `${Number(store.settings.lat).toFixed(4)}, ${Number(store.settings.lon).toFixed(4)}`;
    }
  };
  tick();
  clockTimer = setInterval(tick, 1000);
}

function updateStatusUI(status) {
  if (!status) return;
  const s = status.settings || store.state.settings || {};
  const system = status.system || {};
  const readiness = status.readiness || {};

  const led = document.getElementById("sbLed");
  const sbStatus = document.getElementById("sbStatus");
  const activeTrips = status.trip_alerts || [];
  const tripAlert = activeTrips.find((trip) => trip.alert === "RETURN OVERDUE")
    || activeTrips.find((trip) => trip.alert === "CHECK-IN OVERDUE");
  const tripLed = tripAlert?.alert === "RETURN OVERDUE" ? "bad" : "warn";
  if (sbStatus) {
    sbStatus.textContent = tripAlert
      ? (tripAlert.alert === "RETURN OVERDUE" ? "TRIP RETURN OVERDUE" : "TRIP CHECK-IN OVERDUE")
      : readiness.band ? `SYSTEM ${readiness.band}` : "SYSTEM NOMINAL";
    sbStatus.title = tripAlert
      ? `${tripAlert.name} · ${tripAlert.alert} · off-site contacts are not notified by this app`
      : activeTrips.length ? `Active trip: ${activeTrips[0].name} · ${activeTrips[0].alert}` : "";
  }
  if (led) led.className = `led ${tripAlert ? tripLed : readiness.score === null || readiness.score >= 60 ? "ok" : readiness.score >= 40 ? "warn" : "bad"}`;

  const cpu = document.getElementById("sbCpu"); if (cpu) cpu.textContent = system.load_1m ?? "—";
  const mem = document.getElementById("sbMem"); if (mem) mem.textContent = system.memory_pct ?? "—";
  const disk = document.getElementById("sbDisk"); if (disk) disk.textContent = system.disk_pct ?? "—";
  const ready = document.getElementById("sbReadiness");
  if (ready) ready.textContent = `READINESS ${readiness.score ?? "—"} // ${readiness.band || "NO DATA"}`;

  const vault = status.vault || {};
  const vaultText = document.getElementById("vaultText");
  const vaultPill = document.getElementById("vaultPill");
  if (vaultText) vaultText.textContent = vault.unlocked ? "VAULT OPEN" : vault.configured ? "VAULT LOCKED" : "NO VAULT";
  if (vaultPill) vaultPill.classList.toggle("unlocked", !!vault.unlocked);

  const grid = document.getElementById("gridValue");
  if (grid) grid.textContent = s.opsec_mode ? "OP//MASKED" : (grid.dataset.value || "----");

  if (status.sync) {
    store.state.sync = { ...(store.state.sync || {}), ...status.sync };
    store.state.online = status.online !== false;
  }

  applyAppearance();
  touchNet();
}

function touchNet() {
  const sync = store.state.sync || lastStatus?.sync;
  if (sync) {
    updateSyncPill(sync);
    return;
  }
  const pill = document.getElementById("netPill");
  const led = document.getElementById("netLed");
  const text = document.getElementById("netText");
  const online = navigator.onLine && !netDown;
  if (pill) pill.classList.toggle("online", online);
  if (led) led.className = `led ${online ? "ok" : "bad"}`;
  if (text) text.textContent = netDown ? "LINK DOWN" : online ? "ONLINE" : "OFFLINE";
}

/* ----------------------------------------------------------- live sync */

async function pollSync() {
  try {
    const sync = await api.get("/api/sync/status");
    netDown = false;
    store.state.sync = sync;
    store.state.online = !!sync.online;
    updateSyncPill(sync);
    if (sync.last_success) {
      if (syncLastSuccess && sync.last_success !== syncLastSuccess) {
        window.dispatchEvent(new CustomEvent("darksec:sync", { detail: sync }));
      }
      syncLastSuccess = sync.last_success;
    }
  } catch (err) {
    netDown = true;
    updateSyncPill(store.state.sync || null);
    if (err.status === 401) authLost();
  }
}

function updateSyncPill(sync) {
  const pill = document.getElementById("netPill");
  const led = document.getElementById("netLed");
  const text = document.getElementById("netText");
  if (!text) return;
  const s = sync || {};
  const online = netDown ? false : s.online !== false && navigator.onLine;
  let label, tone;
  if (netDown) {
    label = "LINK DOWN"; tone = "bad";
  } else if (s.opsec) {
    label = "LINK PAUSED"; tone = "warn";
  } else if (!online) {
    label = "OFFLINE"; tone = "bad";
  } else if (s.running) {
    label = "SYNCING"; tone = "warn";
  } else if (s.enabled === false) {
    label = "SYNC OFF"; tone = "warn";
  } else if (s.last_success) {
    label = `LIVE ${fmt.ago(s.last_success).replace(" ago", "")}`; tone = "ok";
  } else {
    label = "ONLINE"; tone = "ok";
  }
  if (pill) {
    pill.classList.toggle("online", tone === "ok");
    pill.classList.toggle("syncing", !!s.running);
  }
  if (led) led.className = `led ${tone}`;
  text.textContent = label;
  if (pill) {
    const feedBit = s.feed_total ? ` · feeds ${s.feed_ok ?? "?"}/${s.feed_total}` : "";
    const reason = s.opsec ? "OPSEC pause" : s.enabled === false ? "auto-sync off" : (s.reason || (online ? "idle" : "offline"));
    pill.title = `Link ${online ? "online" : "offline"} · ${reason}${feedBit} · last refresh ${fmt.ago(s.last_success)} · click to sync now`;
  }
}

function updateGpsStatus(settings = {}) {
  const chip = document.getElementById("sbGps");
  if (!chip) return;
  const source = String(settings.position_source || "unverified").toLowerCase();
  const info = positionConfidence(settings);
  const accuracy = Number(settings.position_accuracy_m);
  chip.classList.remove("gps-warn", "gps-danger");
  if (source === "phone" || source === "gpsd") {
    const label = source === "phone" ? "PHONE" : "RCVR";
    chip.textContent = `GPS ${label}${Number.isFinite(accuracy) ? ` ±${fmt.num(accuracy, 1)}m` : ""} · ${fmt.ago(settings.position_fix_at || settings.position_updated_at)}`;
  } else if (source === "manual") {
    chip.textContent = "GPS MANUAL REF";
  } else if (source === "geocoded") {
    chip.textContent = "GPS GEOCODED";
  } else {
    chip.textContent = "GPS UNVERIFIED";
  }
  if (info.tone === "warn") chip.classList.add("gps-warn");
  else if (info.tone === "danger") chip.classList.add("gps-danger");
  chip.title = info.text;
}

async function refreshGrid() {
  clearTimeout(coordTimer);
  const s = store.settings || {};
  updateGpsStatus(s);
  const lat = Number(s.lat), lon = Number(s.lon);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
  coordTimer = setTimeout(async () => {
    try {
      const c = await api.get(`/api/nav/coords?lat=${lat}&lon=${lon}`);
      const grid = document.getElementById("gridValue");
      if (grid) {
        grid.dataset.value = s.grid_format === "utm" ? c.utm : s.grid_format === "dd" ? c.dd : c.mgrs;
        grid.textContent = s.opsec_mode ? "OP//MASKED" : grid.dataset.value;
        grid.parentElement.title = `${c.dd} // ${c.utm} // ${c.mgrs} · ${positionConfidence(s).text}`;
      }
      const sb = document.getElementById("sbCoords");
      if (sb && !s.epoch) sb.textContent = `${lat.toFixed(4)}, ${lon.toFixed(4)}`;
    } catch {}
  }, 200);
}

function authLost() {
  const m = modal({
    title: "Session authentication lost",
    narrow: true,
    body: h("div", { class: "col" },
      h("p", { class: "small", text: "The local console token is no longer valid. Relaunch DarkSec NIGHTWATCH to issue a new localhost session." }),
      h("div", { class: "callout danger small", text: "No data has been lost. The backend binds to 127.0.0.1 and requires a per-launch token." })),
    actions: [btn("RELOAD", { tone: "primary", onClick: () => location.reload() })],
  });
  return m;
}

/* ---------------------------------------------------------------- router */

function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, "") || "dashboard";
  const [id0, qs] = raw.split("?");
  let id = id0;
  const params = {};
  if (qs) for (const [k, v] of new URLSearchParams(qs)) params[k] = v;
  if (id === "mapgl") { id = "map"; if (!params.renderer) params.renderer = "gl"; }
  return { id: ROUTES[id] ? id : "dashboard", params };
}

async function route() {
  const { id, params } = parseRoute();
  const routeDef = ROUTES[id];
  const view = document.getElementById("view");
  if (!view) return;
  if (cleanupCurrent) {
    try { cleanupCurrent(); } catch (err) { console.error(err); }
    cleanupCurrent = null;
  }
  view.replaceChildren();
  store.setView(id);
  document.title = `DARKSEC // ${routeDef.title}`;
  document.querySelectorAll(".nav a").forEach((a) => {
    const isActive = a.dataset.nav === id;
    a.classList.toggle("active", isActive);
    if (isActive) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  closeSidebar();
  const shell = h("div", { class: "view" });
  view.appendChild(shell);
  try {
    const result = await routeDef.view.render(shell, params);
    if (typeof result === "function") cleanupCurrent = result;
    else if (result && typeof result.cleanup === "function") cleanupCurrent = result.cleanup;
  } catch (err) {
    console.error(err);
    shell.replaceChildren(
      h("div", { class: "view-head" }, h("div", {}, h("h1", { text: routeDef.title }))),
      h("div", { class: "panel" }, h("div", { class: "panel-body" },
        h("div", { class: "callout danger", text: err.message || "This module failed to load." }),
        h("div", { class: "row mt-10" }, btn("RETRY", { tone: "primary", onClick: () => route() })))),
    );
  }
  requestAnimationFrame(() => {
    const main = document.getElementById("view");
    if (main) try { main.focus({ preventScroll: true }); } catch {}
  });
  view.focus({ preventScroll: true });
  view.scrollTop = 0;
  window.scrollTo(0, 0);
}

/* --------------------------------------------------------------- palette */

function commandList() {
  const commands = [];
  for (const [id, r] of Object.entries(ROUTES)) {
    commands.push({ type: "VIEW", title: r.title, subtitle: r.group, icon: r.icon, run: () => { location.hash = `#/${id}`; } });
  }
  commands.push(
    { type: "ACTION", title: "Open Delamain", subtitle: "Local AI assistant (Ollama)", icon: "cpu", run: () => { location.hash = "#/assistant"; } },
    { type: "ACTION", title: "New log entry", subtitle: "Record an observation", icon: "note", run: () => openRecordForm({ table: "log_entries" }) },
    { type: "ACTION", title: "New task", subtitle: "Add to mission list", icon: "check", run: () => openRecordForm({ table: "tasks" }) },
    { type: "ACTION", title: "New waypoint", subtitle: "Drop a coordinate", icon: "pin", run: () => openRecordForm({ table: "waypoints", defaults: currentPosition() }) },
    { type: "ACTION", title: "Print situation report", subtitle: "SITREP to paper", icon: "print", run: printBrief },
    { type: "ACTION", title: "Backup operator data", subtitle: "Local JSON snapshot", icon: "save", run: backupData },
    { type: "ACTION", title: "Refresh intelligence feeds", subtitle: "Requires network", icon: "refresh", run: refreshFeeds },
    { type: "ACTION", title: "Lock encrypted vault", subtitle: "Clear keys from memory", icon: "lock", run: lockVault },
    { type: "ACTION", title: "Toggle OPSEC blackout", subtitle: "Mask sensitive fields", icon: "eye", run: () => toggleOpsec(!store.state.opsec) },
    { type: "ACTION", title: "Toggle scanlines", subtitle: "Display effect", icon: "terminal", run: toggleScanlines },
  );
  return commands;
}

function currentPosition() {
  const s = store.settings || {};
  return { lat: s.lat, lon: s.lon, ele: s.elevation_m };
}

async function backupData() {
  try {
    const out = await api.post("/api/logistics/backup", {});
    toast(`Backup written: ${out.file}`);
  } catch (err) { toast(err.message || "Backup failed", "error"); }
}

async function refreshFeeds() {
  toast("Refreshing public feeds — this may take a few seconds", "warn");
  try {
    const out = await api.post("/api/feeds/refresh", {});
    const ok = (out.results || []).filter((r) => r.status === "OK").length;
    toast(`Feeds refreshed: ${ok}/${(out.results || []).length} OK`);
    await pollSync();
    if (["dashboard", "weather", "intel", "cbrn"].includes(store.state.view)) route();
  } catch (err) { toast(err.message || "Feed refresh failed", "error"); }
}

async function lockVault() {
  try {
    await api.post("/api/vault/lock", {});
    toast("Vault locked");
    if (lastStatus?.vault) lastStatus.vault.unlocked = false;
    updateStatusUI(lastStatus);
    if (store.state.view === "security") route();
  } catch (err) { toast(err.message || "Could not lock vault", "error"); }
}

async function toggleScanlines() {
  try {
    await store.saveSettings({ scanlines: !store.settings.scanlines });
    toast(`Scanlines ${store.settings.scanlines ? "on" : "off"}`);
  } catch (err) { toast(err.message, "error"); }
}

async function printBrief() {
  try {
    const text = await api.text("/api/brief");
    printText(text, "DARKSEC SITUATION REPORT");
  } catch (err) { toast(err.message || "Brief unavailable", "error"); }
}

export function printText(text, title = "DARKSEC") {
  const win = window.open("", "_blank", "width=820,height=900");
  if (!win) { toast("Popup blocked — use download instead", "warn"); return; }
  const safe = String(text).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  win.document.write(`<!doctype html><title>${title}</title><style>
    body{font:12px/1.55 "Courier New",monospace;color:#111;background:#fff;padding:24px;white-space:pre-wrap}
    h1{font:600 16px/1.3 monospace;letter-spacing:.14em;border-bottom:2px solid #111;padding-bottom:8px}
    @media print{body{padding:0}}
  </style><h1>${title}</h1><pre>${safe}</pre>`);
  win.document.close();
  setTimeout(() => { try { win.focus(); win.print(); } catch {} }, 250);
}

function openPalette() {
  if (paletteOpen) return;
  paletteOpen = true;
  palettePreviousFocus = document.activeElement;
  const root = document.getElementById("paletteRoot");
  const backdrop = h("div", { class: "palette-backdrop" });
  const palette = h("div", { class: "palette", role: "dialog", "aria-modal": "true", "aria-label": "Command palette" });
  const inputRow = h("div", { class: "palette-input" });
  inputRow.appendChild(icon("search"));
  const search = h("input", { type: "text", placeholder: "Search commands, documents, references, waypoints…", autocomplete: "off", spellcheck: false });
  inputRow.appendChild(search);
  inputRow.appendChild(h("span", { class: "tiny dim", text: "ESC" }));
  const results = h("div", { class: "palette-results" });
  const hint = h("div", { class: "palette-hint" },
    h("span", {}, h("kbd", { text: "↑↓" }), " navigate"),
    h("span", {}, h("kbd", { text: "↵" }), " run"),
    h("span", {}, h("kbd", { text: "ctrl+k" }), " toggle"));
  palette.append(inputRow, results, hint);
  backdrop.appendChild(palette);
  root.appendChild(backdrop);

  let items = [];
  let active = 0;
  let seq = 0;
  const commands = commandList();

  function close() {
    if (!paletteOpen) return;
    paletteOpen = false;
    backdrop.remove();
    document.removeEventListener("keydown", keyHandler, true);
    restorePaletteFocus();
  }
  function setActive(i) {
    active = Math.max(0, Math.min(items.length - 1, i));
    [...results.children].forEach((n, idx) => n.classList.toggle("active", idx === active));
    const el = results.children[active];
    if (el) el.scrollIntoView({ block: "nearest" });
  }
  function render() {
    results.replaceChildren();
    if (!items.length) {
      results.appendChild(h("div", { class: "palette-empty", text: "No matches. Try a callsign, document name, frequency or manual topic." }));
      return;
    }
    items.forEach((item, idx) => {
      const b = h("button", { class: `palette-item ${idx === active ? "active" : ""}`, type: "button" });
      b.appendChild(icon(item.icon || "chevron-right"));
      const main = h("div", { class: "p-main" });
      main.appendChild(h("div", { class: "p-title", text: item.title }));
      if (item.subtitle) main.appendChild(h("div", { class: "p-sub", text: item.subtitle }));
      b.appendChild(main);
      b.appendChild(h("span", { class: "p-type", text: item.type }));
      b.addEventListener("click", () => activate(idx));
      b.addEventListener("mousemove", () => { if (active !== idx) { active = idx; [...results.children].forEach((n, i) => n.classList.toggle("active", i === idx)); } });
      results.appendChild(b);
    });
  }
  function activate(idx) {
    const item = items[idx];
    if (!item) return;
    close();
    setTimeout(() => { try { item.run(); } catch (err) { toast(err.message || "Command failed", "error"); } }, 20);
  }
  function searchLocal(q) {
    const ql = q.toLowerCase();
    return commands.filter((c) => (c.title + " " + (c.subtitle || "")).toLowerCase().includes(ql)).slice(0, 9);
  }
  async function doSearch(q) {
    const local = searchLocal(q);
    if (!q.trim()) {
      items = local;
      active = 0;
      render();
      return;
    }
    const my = ++seq;
    items = local;
    active = 0;
    render();
    try {
      const res = await api.get(`/api/search?q=${encodeURIComponent(q)}&limit=12`);
      if (my !== seq) return;
      const remote = (res.results || []).map((r) => ({
        type: r.type, title: r.title, subtitle: r.subtitle,
        icon: r.type === "DOCUMENT" ? "file" : r.type === "MANUAL" ? "book" : r.type === "WAYPOINT" ? "pin" : "info",
        run: () => openSearchResult(r),
      }));
      // De-duplicate command entries already shown.
      const seen = new Set(items.map((i) => i.title + "|" + i.subtitle));
      items = [...items, ...remote.filter((r) => !seen.has(r.title + "|" + r.subtitle))];
      render();
    } catch {}
  }
  let debounce;
  search.addEventListener("input", () => {
    clearTimeout(debounce);
    const q = search.value;
    debounce = setTimeout(() => doSearch(q), 180);
  });
  function keyHandler(e) {
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key === "Tab") {
      const focusables = [search, ...results.querySelectorAll("button")].filter((el) => el.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      return;
    }
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(active - 1); }
    else if (e.key === "Enter") { e.preventDefault(); activate(active); }
  }
  document.addEventListener("keydown", keyHandler, true);
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
  setTimeout(() => search.focus(), 30);
  items = commands.slice(0, 9);
  render();
}

function closePalette() {
  const root = document.getElementById("paletteRoot");
  if (root && root.firstChild) {
    root.replaceChildren();
    paletteOpen = false;
    restorePaletteFocus();
  }
}

function openSearchResult(r) {
  if (r.view === "library") {
    location.hash = `#/library?doc=${encodeURIComponent(String(r.payload).replace(/^doc:/, ""))}`;
  } else if (r.view === "manual") {
    const payload = String(r.payload || "");
    if (payload.startsWith("card:")) location.hash = `#/manual?card=${encodeURIComponent(payload.slice(5))}`;
    else location.hash = `#/manual?doc=${encodeURIComponent(payload.replace(/^doc:/, ""))}`;
  } else if (r.view === "map") {
    location.hash = `#/map?wp=${encodeURIComponent(String(r.payload).replace(/^wp:/, ""))}`;
  } else {
    location.hash = `#/${r.view}`;
  }
}

window.addEventListener("DOMContentLoaded", boot);
