import { api, download } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, stat, gauge, badge, btn, iconBtn, icon, pageHead, alertStrip,
  emptyState, loading, fmt, toast, modal, textarea, confirmDialog,
} from "../components.js";
import { openRecordForm, deleteRecord, severityTone } from "../records.js";
import { positionConfidence, opsecActive } from "./shared.js?v=opsec-1";

const DASH_AUTO_MS = 60_000;
let dashTimer = null;
let dashBusy = false;

export async function render(root, params) {
  if (dashTimer) { clearInterval(dashTimer); dashTimer = null; }
  root.replaceChildren(loading("ASSEMBLING SITUATION REPORT"));
  let dash;
  try {
    dash = await store.loadDashboard();
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }
  let disposed = false;
  root.replaceChildren();

  const head = pageHead({
    kicker: "COMMAND // SITREP",
    title: "Situation Report",
    desc: "Local readiness, operational picture and cached intelligence. Estimates are not forecasts; verify before acting.",
    actions: [
      btn("REFRESH", { icon: "refresh", onClick: () => render(root, params) }),
      btn("SITREP", { icon: "print", onClick: () => showBrief() }),
      btn("NEW LOG", { icon: "plus", tone: "primary", onClick: () => openRecordForm({ table: "log_entries", onSaved: () => render(root, params) }) }),
    ],
  });
  root.appendChild(head);

  const alerts = h("div", { class: "col" });
  const r = dash.readiness || {};
  if (r.score === null || r.score === undefined) {
    alerts.appendChild(alertStrip("READINESS NOT COMPUTABLE — automated operational signals unavailable for this area.", "amber"));
  } else if (r.score < 60) {
    alerts.appendChild(alertStrip(`READINESS ${r.score}/100 — ${r.band}. Automated operational risk is elevated; review recommended actions and signals.`, r.score < 40 ? "red" : ""));
  }
  if ((r.nearby_critical_count || 0) > 0) {
    alerts.appendChild(alertStrip(`${r.nearby_critical_count} critical cached feed item(s) within 2000 km. Confirm with official sources before acting.`, "red"));
  }
  for (const trip of (dash.trips || []).filter((item) => item.status === "ACTIVE")) {
    const timing = trip.alert === "RETURN OVERDUE"
      ? `Expected return passed ${trip.return_minutes_overdue || 0} min ago`
      : trip.alert === "CHECK-IN OVERDUE"
        ? `Check-in overdue by ${trip.minutes_overdue || 0} min`
        : trip.next_checkin_at ? `Next check-in due ${fmt.date(trip.next_checkin_at)}` : "Check-in time unavailable";
    const tone = trip.alert === "RETURN OVERDUE" ? "red" : trip.alert === "CHECK-IN OVERDUE" ? "amber" : "";
    const strip = alertStrip(`ACTIVE TRIP · ${trip.name} · ${timing}. Check-in tracking is local only; no contact is notified.`, tone);
    strip.appendChild(btn("CHECK IN", { size: "sm", tone: "primary", onClick: async () => {
      try {
        await api.post(`/api/trips/${trip.id}/check-in`, {});
        toast("Check-in recorded locally; no contact was notified", "warn");
        render(root, params);
      } catch (err) { toast(err.message || "Could not record check-in", "error"); }
    } }));
    alerts.appendChild(strip);
  }
  const lib = dash.library || {};
  if (!lib.documents) {
    alerts.appendChild(alertStrip("Document library not indexed yet. Open LIBRARY and start an index to enable offline full-text search.", ""));
  }
  if (!dash.vault?.configured) {
    alerts.appendChild(alertStrip("Encrypted vault is not configured. Sensitive notes and plans should be protected before field use.", ""));
  }
  if (alerts.children.length) root.appendChild(alerts);

  const sync = dash.sync || {};
  const positionInfo = positionConfidence(dash.settings || store.settings || {});
  const syncOnline = dash.online !== false && sync.online !== false;
  const syncTone = sync.opsec ? "amber" : syncOnline ? "green" : "red";
  const syncText = sync.opsec
    ? "OPSEC · SYNC PAUSED"
    : syncOnline
      ? (sync.enabled === false ? "ONLINE · AUTO-SYNC OFF" : "LIVE · AUTO-SYNC ON")
      : "OFFLINE · CACHED DATA";
  const pz = (dash.readiness || {}).pizzint || {};
  const pzBadge = pz.doughcon != null
    ? badge(`PIZZINT DOUGHCON ${pz.doughcon}`, pz.doughcon >= 4 ? "green" : pz.doughcon === 3 ? "amber" : "red")
    : null;
  root.appendChild(h("div", { class: "row gap-8 wrap mt-10" },
    badge(syncText, syncTone),
    pzBadge,
    h("span", { class: "tiny muted", text: `Last refresh ${fmt.ago(sync.last_success)} · ${sync.feed_ok ?? "?"}/${sync.feed_total ?? "?"} feeds cached · dashboard refreshes every 60 s` })));

  /* ------------------------------------------------------------- hero */
  const hero = h("section", { class: "panel hero" });
  const gaugeWrap = h("div", { class: "hero-gauge" });
  const g = gauge(r.score ?? null, r.band || "NO DATA");
  const valueCircle = g.querySelector(".value");
  if (r.score !== null && r.score < 40) valueCircle?.classList.add("red");
  else if (r.score !== null && r.score < 60) valueCircle?.classList.add("amber");
  gaugeWrap.appendChild(g);
  gaugeWrap.appendChild(h("div", { class: "center mt-10" },
    h("span", { class: "badge " + (r.band === "NOMINAL" ? "green" : r.band === "CRITICAL" ? "red" : "amber"), text: r.band || "NO DATA" })));

  const copy = h("div", { class: "hero-copy" });
  copy.appendChild(h("div", { class: "bandline" },
    badge("FIELD CONSOLE", "muted"),
    badge(dash.settings?.callsign || "NIGHTWATCH", "cyan"),
    badge(dash.settings?.team || "UNASSIGNED", "muted")));
  const now = new Date();
  const headline = r.band === "NOMINAL" ? "HOLD THE LINE"
    : r.band === "CRITICAL" ? "IMMEDIATE ACTION"
    : r.band === "ELEVATED" ? "ELEVATED — WATCH"
    : r.band === "NO DATA" ? "UNASSESSED — ASSESS NOW"
    : "DEGRADED — ADAPT";
  copy.appendChild(h("h2", { text: headline }));
  copy.appendChild(h("p", {
    title: positionInfo.text,
    text: `${now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })} local · ${opsecActive(dash.settings) ? "OP//MASKED" : (dash.coords?.mgrs || "GRID UNKNOWN")} · ` +
      `${positionInfo.mapLabel} · ${(dash.tasks || []).length} open task(s), ${(dash.incidents || []).length} open incident(s), ${(dash.low_stock || []).length} low-stock item(s).`,
  }));

  const bars = h("div", { class: "cat-bars" });
  for (const c of (r.categories || []).slice(0, 8)) {
    const bar = h("div", { class: "cat-bar" });
    bar.appendChild(h("span", { text: c.category }));
    const track = h("div", { class: "bar" });
    const fill = h("span", { text: "" });
    const sc = c.score ?? null;
    fill.style.width = `${sc == null ? 0 : Math.max(0, Math.min(100, sc))}%`;
    if (sc == null) fill.classList.add("muted");
    else if (sc < 40) fill.classList.add("red");
    else if (sc < 60) fill.classList.add("amber");
    track.appendChild(fill);
    bar.appendChild(track);
    bar.appendChild(h("b", { text: `${sc == null ? "—" : Math.round(sc)}%` }));
    bars.appendChild(bar);
  }
  copy.appendChild(bars);

  const quick = h("div", { class: "quick-actions mt-14" });
  const actions = [
    ["note", "LOG", () => openRecordForm({ table: "log_entries", onSaved: () => render(root, params) })],
    ["check", "TASK", () => openRecordForm({ table: "tasks", onSaved: () => render(root, params) })],
    ["warning", "INCIDENT", () => openRecordForm({ table: "incidents", onSaved: () => render(root, params) })],
    ["pin", "WAYPOINT", () => openRecordForm({ table: "waypoints", defaults: position(), onSaved: () => render(root, params) })],
    ["book", "MANUAL", () => { location.hash = "#/manual"; }],
    ["antenna", "SDR", () => { location.hash = "#/sdr"; }],
    ["refresh", "FEEDS", () => refreshFeeds(root, params)],
    ["print", "BRIEF", showBrief],
    ["globe", "INTEL", () => { location.hash = "#/intel"; }],
    ["route", "TRIPS", () => { location.hash = "#/logistics?tab=trips"; }],
  ];
  for (const [ic, label, fn] of actions) {
    const b = h("button", { class: "quick-action", type: "button" });
    b.appendChild(icon(ic));
    b.appendChild(h("span", { text: label }));
    b.addEventListener("click", () => { try { fn(); } catch (err) { toast(err.message, "error"); } });
    quick.appendChild(b);
  }
  copy.appendChild(quick);
  hero.append(gaugeWrap, copy);
  root.appendChild(hero);

  /* --------------------------------------------------- recommended actions */
  const sit = dash.sitrep || {};
  const sitActions = sit.actions || [];
  if (sitActions.length) {
    const actList = h("div", { class: "col gap-6" });
    for (const a of sitActions.slice(0, 6)) {
      const row = h("div", { class: `list-item ${a.urgency === 0 ? "alert" : a.urgency <= 2 ? "warn" : ""}` });
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title", text: a.text }));
      row.appendChild(main);
      actList.appendChild(row);
    }
    root.appendChild(panel({
      title: "RECOMMENDED ACTIONS",
      sub: `SITREP SYNTHESIS · STANCE ${sit.stance || "UNKNOWN"}`,
      icon: "target",
      tools: [btn("SITREP", { icon: "print", onClick: () => showBrief() })],
      body: actList,
      cls: "mt-14",
    }));
  }

  /* ------------------------------------------------------------ stats */
  const stats = h("div", { class: "grid grid-4 mt-14" });
  stats.appendChild(stat("Open tasks", String((dash.tasks || []).length).padStart(2, "0"), "Mission list", (dash.tasks || []).some((t) => t.priority === "IMMEDIATE") ? "acc" : ""));
  stats.appendChild(stat("Incidents", String((dash.incidents || []).length).padStart(2, "0"), "Unresolved", (dash.incidents || []).some((i) => i.severity === "CRITICAL") ? "acc" : ""));
  stats.appendChild(stat("Low stock", String((dash.low_stock || []).length).padStart(2, "0"), "Below minimum"));
  stats.appendChild(stat("Documents", fmt.num(lib.documents || 0, 0), `${fmt.num(lib.text_indexed || 0, 0)} full-text indexed`));
  stats.appendChild(stat("Battery", dash.system?.battery_pct != null ? `${fmt.num(dash.system.battery_pct, 0)}%` : "—", dash.system?.battery_plugged ? "external power" : "on battery"));
  stats.appendChild(stat("Vault", dash.vault?.unlocked ? "OPEN" : dash.vault?.configured ? "LOCKED" : "NOT SET", dash.vault?.configured ? "encrypted notes" : "configure it in SECURITY", dash.vault?.unlocked ? "acc" : ""));
  stats.appendChild(stat("SDR", dash.hardware?.hackrf?.connected ? "CONNECTED" : dash.hardware?.hackrf?.present ? "NO RADIO" : "NO TOOLS", "HackRF / receive first"));
  stats.appendChild(stat("Intel cache", fmt.ago(sync.last_success), `${sync.feed_ok ?? 0}/${sync.feed_total ?? 0} feeds · ${sync.critical_count ?? 0} critical`));
  root.appendChild(stats);

  /* ------------------------------------------------------------- grid */
  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);

  /* tasks */
  const taskList = h("div", { class: "list" });
  if (!(dash.tasks || []).length) taskList.appendChild(emptyState("No open tasks", "Add the next action, not the whole plan.", "check"));
  for (const t of dash.tasks || []) {
    const item = h("div", { class: `list-item ${t.priority === "IMMEDIATE" ? "alert" : t.priority === "PRIORITY" ? "warn" : ""}` });
    const cb = h("input", { type: "checkbox", checked: !!t.done, title: "Mark complete" });
    cb.addEventListener("change", async () => {
      try {
        await api.patch(`/api/records/tasks/${t.id}`, { done: cb.checked });
        toast("Task updated");
        render(root, params);
      } catch (err) { toast(err.message, "error"); cb.checked = !cb.checked; }
    });
    item.appendChild(cb);
    const main = h("div", { class: "li-main" });
    main.appendChild(h("div", { class: "li-title", text: t.title }));
    main.appendChild(h("div", { class: "li-sub", text: `${t.priority || "ROUTINE"}${t.due ? ` · due ${t.due}` : ""}${t.category ? ` · ${t.category}` : ""}` }));
    item.appendChild(main);
    item.appendChild(iconBtn("edit", "Edit task", () => openRecordForm({ table: "tasks", item: t, onSaved: () => render(root, params) })));
    taskList.appendChild(item);
  }
  const taskPanel = panel({
    title: "Mission tasks", icon: "check", cls: "span-2",
    tools: [btn("ADD", { size: "sm", onClick: () => openRecordForm({ table: "tasks", onSaved: () => render(root, params) }) })],
    body: taskList,
  });
  grid.appendChild(taskPanel);

  /* incidents */
  const incList = h("div", { class: "list" });
  if (!(dash.incidents || []).length) incList.appendChild(emptyState("No open incidents", "Log hazards when they appear.", "shield"));
  for (const it of dash.incidents || []) {
    const item = h("div", { class: `list-item ${it.severity === "CRITICAL" ? "alert" : it.severity === "WARNING" ? "warn" : ""}` });
    item.appendChild(h("span", { class: `badge ${severityTone(it.severity)}`, text: it.severity || "INFO" }));
    const main = h("div", { class: "li-main" });
    main.appendChild(h("div", { class: "li-title", text: it.title }));
    main.appendChild(h("div", { class: "li-sub", text: `${fmt.date(it.ts)}${it.category ? ` · ${it.category}` : ""}` }));
    item.appendChild(main);
    item.appendChild(btn("RESOLVE", { size: "sm", tone: "ghost", onClick: async () => {
      try { await api.patch(`/api/records/incidents/${it.id}`, { resolved: 1 }); toast("Incident resolved"); render(root, params); }
      catch (err) { toast(err.message, "error"); }
    } }));
    incList.appendChild(item);
  }
  grid.appendChild(panel({
    title: "Open incidents", icon: "warning",
    tools: [btn("LOG", { size: "sm", onClick: () => openRecordForm({ table: "incidents", onSaved: () => render(root, params) }) })],
    body: incList,
  }));

  /* recent log */
  const logList = h("div", { class: "list" });
  if (!(dash.logs || []).length) logList.appendChild(emptyState("No log entries", "A field log is evidence and memory.", "note"));
  for (const l of dash.logs || []) {
    const item = h("div", { class: "list-item" });
    item.appendChild(h("span", { class: "badge muted", text: (l.category || "LOG").slice(0, 8) }));
    const main = h("div", { class: "li-main" });
    main.appendChild(h("div", { class: "li-title", text: l.title }));
    main.appendChild(h("div", { class: "li-sub truncate", text: l.body || "" }));
    item.appendChild(main);
    item.appendChild(h("span", { class: "li-time", text: fmt.time(l.ts) }));
    logList.appendChild(item);
  }
  grid.appendChild(panel({
    title: "Operations log", icon: "note",
    tools: [btn("NEW", { size: "sm", onClick: () => openRecordForm({ table: "log_entries", onSaved: () => render(root, params) }) })],
    body: logList,
  }));

  /* almanac */
  const alm = dash.almanac;
  const almanac = h("div", { class: "almanac" });
  if (alm) {
    const rows = [
      ["SUNRISE", fmt.time(alm.sun.next_rise)], ["SUNSET", fmt.time(alm.sun.next_set)],
      ["CIVIL DAWN", fmt.time(alm.sun.civil_dawn)], ["CIVIL DUSK", fmt.time(alm.sun.civil_dusk)],
      ["MOONRISE", fmt.time(alm.moon.next_rise)], ["MOONSET", fmt.time(alm.moon.next_set)],
      ["ILLUMINATION", `${alm.moon.illumination_pct}%`], ["PHASE", alm.moon.phase_name],
      ["SUN ALT", `${alm.sun.altitude_deg}°`], ["SUN AZ", `${alm.sun.azimuth_deg}°`],
    ];
    for (const [k, v] of rows) {
      almanac.appendChild(h("div", { class: "k", text: k }));
      almanac.appendChild(h("div", { class: "v", text: v }));
    }
  } else {
    almanac.appendChild(h("div", { class: "small muted", text: "Astronomical almanac unavailable." }));
  }
  grid.appendChild(panel({
    title: "Almanac", sub: "Local times", icon: "sun",
    tools: [btn("NAV", { size: "sm", onClick: () => { location.hash = "#/nav"; } })],
    body: almanac,
  }));

  /* feeds */
  const feedWrap = h("div", { class: "col" });
  const fsummary = dash.feeds || { feeds: [], latest: [] };
  if (!fsummary.feeds?.length) {
    feedWrap.appendChild(emptyState("No feed state", "Feeds are optional and require network.", "cloud"));
  } else {
    feedWrap.appendChild(h("div", { class: "pill-row" }, ...fsummary.feeds.map((f) =>
      h("span", { class: `badge ${f.status === "OK" ? "green" : f.status === "ERROR" ? "red" : "muted"}`, title: `${f.name} — ${f.error || f.status}`, text: `${f.id.toUpperCase()} ${f.status === "OK" ? f.count : f.status}` }))));
    const latest = (fsummary.latest || []).slice(0, 5);
    if (latest.length) {
      const list = h("div", { class: "list mt-10" });
      for (const it of latest) {
        const row = h("div", { class: `list-item ${it.severity === "CRITICAL" ? "alert" : it.severity === "ALERT" ? "warn" : ""}` });
        const main = h("div", { class: "li-main" });
        main.appendChild(h("div", { class: "li-title truncate", text: it.title || "Feed item" }));
        main.appendChild(h("div", { class: "li-sub truncate", text: `${it.source || ""}${it.place ? ` · ${it.place}` : ""}${it.time ? ` · ${fmt.date(it.time)}` : ""}` }));
        row.appendChild(main);
        if (it.url) {
          const a = h("a", { class: "badge cyan", href: it.url, target: "_blank", rel: "noopener noreferrer", text: "SOURCE" });
          row.appendChild(a);
        }
        list.appendChild(row);
      }
      feedWrap.appendChild(list);
    } else {
      feedWrap.appendChild(h("div", { class: "small muted mt-10", text: "No cached items. Use REFRESH when a network is available." }));
    }
  }
  grid.appendChild(panel({
    title: "Intelligence feeds", sub: "Cached · verify with official sources", icon: "globe",
    tools: [btn("REFRESH", { size: "sm", icon: "refresh", onClick: () => refreshFeeds(root, params) })],
    body: feedWrap,
  }));

  /* low stock + library + system */
  const lowList = h("div", { class: "list" });
  if (!(dash.low_stock || []).length) lowList.appendChild(h("div", { class: "small muted", text: "No inventory items below minimum." }));
  for (const item of dash.low_stock || []) {
    const row = h("div", { class: "list-item warn" });
    const main = h("div", { class: "li-main" });
    main.appendChild(h("div", { class: "li-title", text: item.name }));
    main.appendChild(h("div", { class: "li-sub", text: `${fmt.num(item.qty)} ${item.unit || ""} on hand · minimum ${fmt.num(item.min_qty)}` }));
    row.appendChild(main);
    lowList.appendChild(row);
  }
  grid.appendChild(panel({
    title: "Sustainment warnings", icon: "box",
    tools: [btn("LOGISTICS", { size: "sm", onClick: () => { location.hash = "#/logistics"; } })],
    body: lowList,
  }));

  const libRows = h("div", { class: "kv" });
  libRows.appendChild(h("span", { class: "k", text: "Documents" })); libRows.appendChild(h("span", { class: "v", text: fmt.num(lib.documents || 0, 0) }));
  libRows.appendChild(h("span", { class: "k", text: "Full text" })); libRows.appendChild(h("span", { class: "v", text: fmt.num(lib.text_indexed || 0, 0) }));
  libRows.appendChild(h("span", { class: "k", text: "Index size" })); libRows.appendChild(h("span", { class: "v", text: fmt.bytes(lib.db_bytes || 0) }));
  libRows.appendChild(h("span", { class: "k", text: "Job" })); libRows.appendChild(h("span", { class: "v", text: lib.job?.message || "idle" }));
  const libAction = h("div", { class: "mt-10 row" },
    btn("OPEN LIBRARY", { size: "sm", onClick: () => { location.hash = "#/library"; } }),
    btn("SEARCH", { size: "sm", tone: "ghost", onClick: () => { location.hash = "#/library"; } }));
  libRows.appendChild(h("div", { class: "wide" }, libAction));
  grid.appendChild(panel({ title: "Document library", icon: "library", body: libRows }));

  const sys = dash.system || {};
  const sysKv = h("div", { class: "kv" });
  for (const [k, v] of [
    ["Host", sys.hostname || "—"], ["CPU load", `${sys.load_1m ?? "—"} / ${sys.cpu_count ?? "—"} cores`],
    ["Memory", `${fmt.num(sys.memory_pct, 1)}% used`], ["Disk", `${fmt.num(sys.disk_pct, 1)}% used`],
    ["Disk free", fmt.bytes(sys.disk_free)], ["Uptime", fmt.duration(sys.uptime_s)],
    ["Data", sys.data_dir || "—"], ["DB", fmt.bytes(sys.db_bytes)],
  ]) {
    sysKv.appendChild(h("span", { class: "k", text: k }));
    sysKv.appendChild(h("span", { class: "v", text: String(v) }));
  }
  grid.appendChild(panel({ title: "Platform", icon: "cpu", body: sysKv }));

  // Delegated refresh helper
  function position() {
    const s = store.settings || {};
    return { lat: s.lat, lon: s.lon, ele: s.elevation_m };
  }

  updateBadgesFrom(dash);

  dashTimer = setInterval(async () => {
    if (disposed || dashBusy || document.hidden) return;
    if (!location.hash.startsWith("#/dashboard")) return;
    if (document.querySelector(".modal.open, .modal")) return;
    if (document.activeElement && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    dashBusy = true;
    const y = window.scrollY;
    try {
      await render(root, params);
      window.scrollTo(0, y);
    } catch {}
    finally { dashBusy = false; }
  }, DASH_AUTO_MS);

  return () => {
    disposed = true;
    if (dashTimer) { clearInterval(dashTimer); dashTimer = null; }
  };
}

function updateBadgesFrom(dash) {
  /* main.js owns nav badge updates; nothing to do for dashboard beyond local stats. */
}

async function refreshFeeds(root, params) {
  toast("Refreshing public feeds — network required", "warn");
  try {
    const out = await api.post("/api/feeds/refresh", {});
    const ok = (out.results || []).filter((x) => x.status === "OK").length;
    toast(`Feeds refreshed: ${ok}/${(out.results || []).length} OK`);
    render(root, params);
  } catch (err) { toast(err.message || "Feed refresh failed", "error"); }
}

async function showBrief() {
  let text = "";
  try { text = await api.text("/api/brief"); }
  catch (err) { toast(err.message || "Brief unavailable", "error"); return; }
  const pre = h("pre", { class: "brief", text });
  const m = modal({
    title: "Situation report",
    sub: "UNCLASSIFIED // FIELD OPERATIONS // GENERATED LOCALLY",
    wide: true, body: pre,
    actions: [
      btn("COPY", { onClick: () => { navigator.clipboard?.writeText(text).then(() => toast("Brief copied")).catch(() => toast("Copy failed", "error")); } }),
      btn("SAVE TO LOG", { icon: "save", tone: "ghost", onClick: async () => {
        const ok = await confirmDialog({
          title: "Save SITREP",
          message: "Save this server-generated report to the mission log? It will be tagged SITREP.",
          confirmText: "SAVE",
        });
        if (!ok) return;
        try {
          const res = await api.post("/api/brief/save", {});
          if (res && res.ok === false) throw new Error(res.error || "Could not save SITREP");
          toast("SITREP saved to mission log");
        } catch (err) { toast(err.message || "Could not save SITREP", "error"); }
      } }),
      btn("MARKDOWN", { icon: "download", onClick: () => {
        location.href = "/api/brief?format=md&download=true";
      } }),
      btn("DOWNLOAD", { icon: "download", onClick: () => {
        const blob = new Blob([text], { type: "text/plain" });
        const a = h("a", { href: URL.createObjectURL(blob), download: `darksec-sitrep-${new Date().toISOString().slice(0, 10)}.txt` });
        document.body.appendChild(a); a.click(); a.remove();
      } }),
      btn("PRINT", { icon: "print", tone: "primary", onClick: () => {
        const win = window.open("", "_blank", "width=820,height=900");
        if (!win) { toast("Popup blocked — use download", "warn"); return; }
        const safe = text.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
        win.document.write(`<!doctype html><title>DARKSEC SITREP</title><style>body{font:12px/1.55 "Courier New",monospace;color:#111;background:#fff;padding:24px;white-space:pre-wrap}h1{font:600 16px/1.3 monospace;letter-spacing:.14em;border-bottom:2px solid #111;padding-bottom:8px}</style><h1>DARKSEC // SITUATION REPORT</h1><pre>${safe}</pre>`);
        win.document.close();
        setTimeout(() => { win.focus(); win.print(); }, 250);
      } }),
    ],
  });
}
