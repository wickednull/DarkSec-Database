/* Logistics: mission tasks, caches, movement planning and operator data. */
import { api, download } from "../api.js";
import {
  h, panel, stat, badge, btn, pageHead, alertStrip, emptyState, loading, fmt,
  toast, tabs, iconBtn, confirmDialog,
} from "../components.js";
import { openRecordForm, deleteRecord, recordTable, rowActions } from "../records.js";
import { store } from "../store.js";
import { kv, downloadText, readTextFile, printText } from "./shared.js";
import * as U from "../units.js";

let lastTab = "mission";
let currentRoot = null;
let currentParams = {};

export async function render(root, params = {}) {
  currentRoot = root;
  currentParams = params;
  if (["mission", "caches", "movement", "trips", "data"].includes(params.tab)) lastTab = params.tab;
  root.replaceChildren(loading("LOADING LOGISTICS"));
  let tasks, caches, waypoints, inventory, low, backups, trips;
  try {
    [tasks, caches, waypoints, inventory, low, backups, trips] = await Promise.all([
      api.get("/api/records/tasks?limit=500"),
      api.get("/api/records/caches?limit=500"),
      api.get("/api/records/waypoints?limit=500"),
      api.get("/api/records/inventory?limit=1000"),
      api.get("/api/logistics/low-stock"),
      api.get("/api/logistics/backups"),
      api.get("/api/trips"),
    ]);
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message || "Could not load logistics data." }));
    return;
  }

  const taskItems = tasks.items || [];
  const cacheItems = caches.items || [];
  const waypointItems = waypoints.items || [];
  const inv = inventory.items || [];
  const lowItems = low.items || [];
  const backupItems = backups.items || [];
  const tripItems = trips.items || [];
  const activeTrips = tripItems.filter((trip) => trip.status === "ACTIVE");
  const openTasks = taskItems.filter((t) => !t.done);
  const immediate = openTasks.filter((t) => t.priority === "IMMEDIATE");
  const activeCaches = cacheItems.filter((c) => ["ACTIVE", "SEALED"].includes(String(c.status || "").toUpperCase()));
  const lowCritical = lowItems.length;

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // SUSTAINMENT",
    title: "Logistics",
    desc: "Mission list, trip plans, local check-ins, caches and operator-data custody. Check-ins are not transmitted to contacts.",
    actions: [
      btn("NEW TASK", { icon: "plus", tone: "primary", onClick: () => addTask(root) }),
      btn("ADD CACHE", { icon: "pin", onClick: () => addCache(root) }),
      btn("BACKUP", { icon: "save", onClick: () => backupNow(root) }),
      btn("EXPORT", { icon: "download", tone: "ghost", onClick: () => exportData() }),
    ],
  }));

  root.appendChild(alertStrip(
    "Operator data is not the document library. Backups contain records, checklists and settings; encrypted vault notes stay encrypted and require the vault passphrase. "
    + "Keep at least one copy on removable media stored separately from the machine.",
    "",
  ));

  const stats = h("div", { class: "grid grid-4 mt-14" });
  stats.appendChild(stat("Open tasks", String(openTasks.length).padStart(2, "0"),
    immediate.length ? `${immediate.length} immediate` : "Mission list", immediate.length ? "acc" : ""));
  stats.appendChild(stat("Active caches", String(activeCaches.length).padStart(2, "0"),
    `${cacheItems.length} total cache site(s)`));
  stats.appendChild(stat("Low stock", String(lowCritical).padStart(2, "0"),
    `${inv.length} inventory line(s)`, lowCritical ? "warn" : ""));
  stats.appendChild(stat("Backups", String(backupItems.length).padStart(2, "0"),
    backupItems[0] ? `latest ${fmt.ago(backupItems[0].mtime)}` : "none written yet"));
  stats.appendChild(stat("Active trips", String(activeTrips.length).padStart(2, "0"),
    activeTrips.filter((trip) => trip.alert === "RETURN OVERDUE" || trip.alert === "CHECK-IN OVERDUE").length
      ? "check-in attention" : "local tracking only",
    activeTrips.some((trip) => trip.alert === "RETURN OVERDUE") ? "warn" : ""));
  root.appendChild(stats);

  root.appendChild(h("div", { class: "mt-14" }, tabs([
    { id: "mission", label: "MISSION", count: openTasks.length },
    { id: "caches", label: "CACHES", count: cacheItems.length },
    { id: "movement", label: "MOVEMENT" },
    { id: "trips", label: "TRIPS", count: activeTrips.length },
    { id: "data", label: "OPERATOR DATA", count: backupItems.length },
  ], lastTab, (id) => { lastTab = id; render(root, params); })));

  if (lastTab === "mission") missionSection(root, openTasks, taskItems);
  else if (lastTab === "caches") cachesSection(root, cacheItems);
  else if (lastTab === "movement") movementSection(root, waypointItems);
  else if (lastTab === "trips") tripSection(root, tripItems, waypointItems);
  else dataSection(root, backupItems, lowItems);
}

/* ----------------------------------------------------------------- mission */

function missionSection(root, open, all) {
  const done = all.filter((t) => t.done).slice(0, 20);
  const list = h("div", { class: "list" });
  if (!open.length) {
    list.appendChild(emptyState("No open tasks", "Write the next action. Keep it small enough to start now.", "check"));
  }
  for (const t of open) {
    const item = h("div", { class: `list-item ${t.priority === "IMMEDIATE" ? "alert" : t.priority === "PRIORITY" ? "warn" : ""}` });
    const cb = h("input", { type: "checkbox", checked: !!t.done, title: "Mark complete" });
    cb.addEventListener("change", async () => {
      try {
        await api.patch(`/api/records/tasks/${t.id}`, { done: cb.checked });
        toast("Task updated");
        reRender();
      } catch (err) { toast(err.message || "Update failed", "error"); cb.checked = !cb.checked; }
    });
    item.appendChild(cb);
    const main = h("div", { class: "li-main" });
    main.appendChild(h("div", { class: "li-title", text: t.title }));
    const bits = [t.priority || "ROUTINE"];
    if (t.due) bits.push(`due ${t.due}`);
    if (t.category) bits.push(t.category);
    main.appendChild(h("div", { class: "li-sub", text: bits.join(" · ") }));
    if (t.detail) main.appendChild(h("div", { class: "small muted mt-4", text: t.detail }));
    item.appendChild(main);
    item.appendChild(iconBtn("edit", "Edit task", () => openRecordForm({ table: "tasks", item: t, onSaved: reRender })));
    list.appendChild(item);
  }

  root.appendChild(panel({
    title: "Mission tasks", icon: "check",
    sub: "Ordered by priority. Completing a task keeps it in the database for the record.",
    tools: [btn("ADD TASK", { size: "sm", icon: "plus", onClick: () => addTask(root) })],
    body: list,
  }));

  const doneList = h("div", { class: "list mt-10" });
  if (!done.length) doneList.appendChild(emptyState("Nothing completed yet", "Finished tasks collect here.", "check"));
  for (const t of done) {
    const item = h("div", { class: "list-item" });
    const cb = h("input", { type: "checkbox", checked: true, title: "Reopen task" });
    cb.addEventListener("change", async () => {
      try {
        await api.patch(`/api/records/tasks/${t.id}`, { done: cb.checked });
        toast("Task reopened");
        reRender();
      } catch (err) { toast(err.message || "Update failed", "error"); cb.checked = !cb.checked; }
    });
    item.appendChild(cb);
    const main = h("div", { class: "li-main" });
    main.appendChild(h("div", { class: "li-title muted", text: t.title }));
    main.appendChild(h("div", { class: "li-sub", text: `closed · ${fmt.date(t.ts)}` }));
    item.appendChild(main);
    item.appendChild(iconBtn("edit", "Edit task", () => openRecordForm({ table: "tasks", item: t, onSaved: reRender })));
    doneList.appendChild(item);
  }
  root.appendChild(panel({
    title: "Completed", icon: "history", cls: "mt-14",
    sub: "Last 20 completed items.",
    body: doneList,
  }));
}

function addTask(root) {
  openRecordForm({ table: "tasks", onSaved: () => reRender() });
}

/* -------------------------------------------------------------- field trips */

function tripSection(root, trips, waypoints) {
  const waypointOptions = [{ value: "", label: "— not linked —" }].concat(
    waypoints.filter((w) => w.lat != null && w.lon != null).map((w) => ({
      value: String(w.id),
      label: `${w.name} · ${fmt.coord(w.lat, w.lon, 4)}`,
    })),
  );
  const intervalOptions = [15, 30, 60, 120, 240, 480, 720, 1440]
    .map((minutes) => ({ value: String(minutes), label: minutes < 60 ? `${minutes} minutes` : `${minutes / 60} hour(s)` }));
  const fields = [
    { name: "name", label: "Trip / route name", required: true, wide: true },
    { name: "start_waypoint_id", label: "Start waypoint", type: "select", options: waypointOptions },
    { name: "end_waypoint_id", label: "Destination waypoint", type: "select", options: waypointOptions },
    { name: "route", label: "Route and checkpoints", type: "textarea", rows: 3, wide: true,
      hint: "Write the intended route, alternates, bail-out points and terrain notes. This is not turn-by-turn navigation." },
    { name: "departure_at", label: "Planned departure (device local time)", type: "datetime-local", required: true },
    { name: "return_at", label: "Expected return (device local time)", type: "datetime-local", required: true },
    { name: "checkin_interval_min", label: "Check-in interval", type: "select", options: intervalOptions, default: "60" },
    { name: "party_size", label: "Party size", type: "number", min: 1, max: 100, default: 1 },
    { name: "emergency_contact", label: "Off-site contact (name + phone)", wide: true,
      hint: "Share the printed plan and overdue procedure with this person yourself; DarkSec does not contact them." },
    { name: "notes", label: "Plan / overdue action notes", type: "textarea", rows: 3, wide: true },
  ];

  root.appendChild(alertStrip(
    "Field accountability only: start the trip, check in manually, and close it when safely back. Deadlines use this device's clock. The app sends no alert or location to your contact, and cannot notify anyone while closed or without power. Trip details/contact are in the local database and backups, not the encrypted vault. Give a trusted person the printed plan and a clear overdue action before departure.",
    "amber",
  ));
  const wrap = h("div", { class: "col gap-10 mt-14" });
  if (!trips.length) {
    wrap.appendChild(emptyState("No trip plans", "Create a plan with a departure, expected return and off-site contact before setting out.", "route"));
  }

  for (const trip of trips) {
    const stateTone = trip.alert_level === "red" ? "red" : trip.alert_level === "amber" ? "amber"
      : trip.status === "ACTIVE" ? "green" : trip.status === "COMPLETED" ? "muted" : "cyan";
    const startWp = waypoints.find((w) => Number(w.id) === Number(trip.start_waypoint_id));
    const endWp = waypoints.find((w) => Number(w.id) === Number(trip.end_waypoint_id));
    const deadline = trip.status === "ACTIVE"
      ? trip.alert === "RETURN OVERDUE"
        ? `RETURN OVERDUE · ${trip.return_minutes_overdue || 0} min`
        : trip.alert === "CHECK-IN OVERDUE"
          ? `CHECK-IN OVERDUE · ${trip.minutes_overdue || 0} min`
          : `Next check-in ${trip.next_checkin_at ? fmt.date(trip.next_checkin_at) : "unknown"}`
      : trip.alert === "DEPARTURE PASSED" ? "Departure time passed — start or revise plan" : trip.alert;
    const details = h("div", { class: "col gap-6" });
    details.appendChild(h("div", { class: "row wrap gap-6" }, badge(trip.status, stateTone), badge(deadline, stateTone)));
    details.appendChild(kv([
      ["Start", startWp ? `${startWp.name} · ${fmt.coord(startWp.lat, startWp.lon, 5)}` : "not linked to a waypoint"],
      ["Destination", endWp ? `${endWp.name} · ${fmt.coord(endWp.lat, endWp.lon, 5)}` : "not linked to a waypoint"],
      ["Departure", fmt.date(trip.departure_at)],
      ["Expected return", fmt.date(trip.return_at)],
      ["Check-in interval", `${trip.checkin_interval_min || 60} min`],
      ["Last local check-in", trip.last_checkin_at ? fmt.date(trip.last_checkin_at) : "not started"],
      ["Off-site contact", trip.emergency_contact || "NOT SET — share a plan with someone"],
      ["Party size", String(trip.party_size || 1)],
      ["Route / checkpoints", trip.route || "not recorded"],
      ["Notes", trip.notes || "—"],
    ]));

    const actions = h("div", { class: "row wrap gap-6 mt-10" });
    if (trip.status === "PLANNED") {
      actions.appendChild(btn("START TRIP", { tone: "primary", icon: "route", onClick: () => tripAction(trip.id, "start", "Trip started") }));
      actions.appendChild(btn("EDIT PLAN", { tone: "ghost", icon: "edit", onClick: () => editTrip(trip) }));
      actions.appendChild(btn("CANCEL", { tone: "ghost", onClick: () => cancelTrip(trip) }));
    } else if (trip.status === "ACTIVE") {
      actions.appendChild(btn("CHECK IN NOW", { tone: "primary", icon: "check", onClick: () => tripAction(trip.id, "check-in", "Check-in recorded locally") }));
      actions.appendChild(btn("FINISH TRIP", { tone: "ghost", icon: "check", onClick: () => tripAction(trip.id, "finish", "Trip marked complete") }));
      actions.appendChild(btn("CANCEL", { tone: "ghost", onClick: () => cancelTrip(trip) }));
      actions.appendChild(btn("PRINT PLAN", { tone: "ghost", icon: "print", onClick: () => printTrip(trip, startWp, endWp) }));
    } else {
      actions.appendChild(btn("EDIT PLAN", { tone: "ghost", icon: "edit", onClick: () => editTrip(trip) }));
      actions.appendChild(btn("PRINT PLAN", { tone: "ghost", icon: "print", onClick: () => printTrip(trip, startWp, endWp) }));
      actions.appendChild(btn("DELETE", { tone: "ghost", icon: "trash", onClick: () => deleteRecord("trip_plans", trip.id, "trip plan", reRender) }));
    }
    if (startWp) actions.appendChild(btn("MAP START", { tone: "ghost", icon: "map", onClick: () => { location.hash = `#/map?wp=${startWp.id}`; } }));
    if (endWp) actions.appendChild(btn("MAP DEST", { tone: "ghost", icon: "map", onClick: () => { location.hash = `#/map?wp=${endWp.id}`; } }));
    details.appendChild(actions);
    wrap.appendChild(panel({ title: trip.name, sub: `PLAN #${trip.id} · saved ${fmt.ago(trip.updated || trip.created)}`, icon: "route", body: details }));
  }

  root.appendChild(panel({
    title: "Trip plans & check-ins", icon: "route",
    sub: "Local accountability ledger · no live tracking or outbound check-in",
    tools: [btn("NEW TRIP PLAN", { size: "sm", icon: "plus", tone: "primary", onClick: () => editTrip(null) })],
    body: wrap,
  }));

  function editTrip(item) {
    openRecordForm({
      table: "trip_plans", item, fields,
      defaults: { checkin_interval_min: 60, party_size: 1 },
      title: item ? "Edit field trip plan" : "New field trip plan",
      intro: h("div", { class: "callout warn small", text: "Times use this device's local timezone and are stored as UTC. Keep a paper copy; this local app does not transmit alerts." }),
      onSaved: () => reRender(),
    });
  }

  async function tripAction(id, action, message) {
    try {
      const out = await api.post(`/api/trips/${id}/${action}`, {});
      toast(message, action === "check-in" ? "" : "warn");
      if (action === "check-in" && out.note) toast(out.note, "warn");
      reRender();
    } catch (err) { toast(err.message || "Trip update failed", "error"); }
  }

  async function cancelTrip(trip) {
    if (!(await confirmDialog({
      title: "Cancel trip monitoring",
      message: `Cancel ${trip.name}? This stops overdue check-in warnings in DarkSec. Ensure your off-site contact knows the plan changed.`,
      confirmText: "CANCEL TRIP", danger: true,
    }))) return;
    await tripAction(trip.id, "cancel", "Trip monitoring cancelled");
  }
}

function printTrip(trip, start, end) {
  const lines = [
    `STATUS: ${trip.status} / ${trip.alert}`,
    `PLANNED DEPARTURE: ${fmt.date(trip.departure_at)}`,
    `EXPECTED RETURN: ${fmt.date(trip.return_at)}`,
    `CHECK-IN EVERY: ${trip.checkin_interval_min || 60} minutes`,
    `LAST CHECK-IN: ${trip.last_checkin_at ? fmt.date(trip.last_checkin_at) : "not started"}`,
    `PARTY SIZE: ${trip.party_size || 1}`,
    `OFF-SITE CONTACT: ${trip.emergency_contact || "NOT SET"}`,
    `START: ${start ? `${start.name} (${fmt.coord(start.lat, start.lon, 6)})` : "not linked"}`,
    `DESTINATION: ${end ? `${end.name} (${fmt.coord(end.lat, end.lon, 6)})` : "not linked"}`,
    "", "ROUTE / CHECKPOINTS", trip.route || "(not recorded)",
    "", "NOTES / OVERDUE ACTION", trip.notes || "(not recorded)",
    "", "This is a locally stored plan. DarkSec does not send check-ins, track this party, or notify the contact.",
    "Give the contact this itinerary, route, expected return, check-in schedule and agreed overdue action before departure.",
    "Carry a paper map, compass, and a copy of this plan.",
  ];
  printText(`FIELD TRIP PLAN // ${trip.name}`, lines.join("\n"));
}

/* ------------------------------------------------------------------ caches */

function cachesSection(root, items) {
  const wrap = h("div");
  const columns = [
    { label: "CACHE", key: "name", render: (r) => h("div", {},
      h("div", { class: "strong", text: r.name }),
      h("div", { class: "tiny dim", text: r.contents || "" })) },
    { label: "STATUS", key: "status", render: (r) => badge(r.status || "UNKNOWN", cacheTone(r.status)) },
    { label: "COORDINATES", key: "lat", render: (r) => h("span", { class: "mono small", text: r.lat != null && r.lon != null ? fmt.coord(r.lat, r.lon, 5) : "—" }) },
    { label: "NOTE", key: "note", render: (r) => h("span", { class: "small muted", text: r.note || "—" }) },
    { label: "UPDATED", key: "updated", render: (r) => h("span", { class: "tiny dim", text: fmt.date(r.updated || r.created) }) },
    { label: "", key: "actions", render: (r) => rowActions({
      extra: [
        btn("MAP", { size: "sm", tone: "ghost", onClick: () => {
          if (r.lat == null || r.lon == null) { toast("Cache has no coordinates", "warn"); return; }
          location.hash = `#/map?lat=${encodeURIComponent(r.lat)}&lon=${encodeURIComponent(r.lon)}&zoom=13`;
        } }),
      ],
      onEdit: () => openRecordForm({ table: "caches", item: r, onSaved: () => reRender() }),
      onDelete: () => deleteRecord("caches", r.id, "cache", reRender),
    }) },
  ];
  wrap.appendChild(h("div", { class: "scroll-x" }, recordTable(items, columns, {
    empty: "No caches registered. A cache is only useful if you can find it and it is still there.",
    emptyIcon: "pin",
  })));
  root.appendChild(panel({
    title: "Cache register", icon: "pin",
    sub: "Do not store the only copy of essential supplies at a cache you cannot service. Verify contents and condition on a schedule.",
    tools: [btn("ADD CACHE", { size: "sm", icon: "plus", onClick: () => addCache(root) })],
    body: wrap,
  }));
}

function addCache(root) {
  openRecordForm({ table: "caches", defaults: currentPosition(), onSaved: () => reRender() });
}

function cacheTone(status) {
  const s = String(status || "").toUpperCase();
  if (s === "COMPROMISED" || s === "EMPTY") return "red";
  if (s === "PARTIAL") return "amber";
  if (s === "ACTIVE") return "green";
  return "muted";
}

/* ---------------------------------------------------------------- movement */

function movementSection(root, waypoints) {
  const state = { from: "", to: "" };
  const coord = (w) => w && w.lat != null && w.lon != null;
  const options = [{ value: "", label: "— manual coordinates —" }]
    .concat(waypoints.filter(coord).map((w) => ({ value: String(w.id), label: `${w.name}${w.type ? ` (${w.type})` : ""}` })));
  const fromSel = h("select", { class: "select" });
  const toSel = h("select", { class: "select" });
  for (const o of options) {
    fromSel.appendChild(h("option", { value: o.value, text: o.label }));
    toSel.appendChild(h("option", { value: o.value, text: o.label }));
  }
  const fLat = h("input", { class: "input", type: "number", step: "0.000001", placeholder: "from lat" });
  const fLon = h("input", { class: "input", type: "number", step: "0.000001", placeholder: "from lon" });
  const tLat = h("input", { class: "input", type: "number", step: "0.000001", placeholder: "to lat" });
  const tLon = h("input", { class: "input", type: "number", step: "0.000001", placeholder: "to lon" });
  const speed = h("input", { class: "input", type: "number", step: "0.1", value: "4", min: "0.1" });
  const rest = h("input", { class: "input", type: "number", step: "1", value: "10", min: "0" });
  const terrain = h("input", { class: "input", type: "number", step: "0.05", value: "1.25", min: "1" });
  const depart = h("input", { class: "input", type: "time", value: new Date().toTimeString().slice(0, 5) });
  const out = h("div", { class: "mt-10" });

  function syncManual(sel, latEl, lonEl) {
    const wp = waypoints.find((w) => String(w.id) === sel.value);
    if (wp) {
      latEl.value = wp.lat;
      lonEl.value = wp.lon;
      latEl.disabled = true;
      lonEl.disabled = true;
    } else {
      latEl.disabled = false;
      lonEl.disabled = false;
    }
  }
  fromSel.addEventListener("change", () => syncManual(fromSel, fLat, fLon));
  toSel.addEventListener("change", () => syncManual(toSel, tLat, tLon));

  async function plan() {
    const aLat = Number(fLat.value), aLon = Number(fLon.value);
    const bLat = Number(tLat.value), bLon = Number(tLon.value);
    if (![aLat, aLon, bLat, bLon].every(Number.isFinite)) {
      out.replaceChildren(h("div", { class: "callout warn small", text: "Select two waypoints or enter start and destination coordinates." }));
      return;
    }
    out.replaceChildren(loading("COMPUTING ROUTE"));
    try {
      const leg = (await api.post("/api/calc/path_heading", { lat1: aLat, lon1: aLon, lat2: bLat, lon2: bLon })).result || {};
      const km = Number(leg.distance_km) || 0;
      const kmh = Math.max(0.1, Number(speed.value) || 4);
      const factor = Math.max(1, Number(terrain.value) || 1);
      const movingMin = (km * factor / kmh) * 60;
      const restMinPerHour = Math.max(0, Number(rest.value) || 0);
      const restMin = movingMin / 60 * restMinPerHour;
      const totalMin = movingMin + restMin;
      const [dh, dm] = String(depart.value || "00:00").split(":").map(Number);
      const start = new Date();
      start.setHours(dh || 0, dm || 0, 0, 0);
      const eta = new Date(start.getTime() + totalMin * 60000);
      const back = (Number(leg.bearing_deg) + 180) % 360;
      out.replaceChildren(
        kv([
          ["DISTANCE", U.distKm(km, 2)],
          ["BEARING", `${fmt.num(leg.bearing_deg, 1)}° · back ${fmt.num(back, 1)}°`],
          ["PLANNED PACE", `${U.speedKmh(kmh, 1)} · terrain ×${fmt.num(factor, 2)}`],
          ["MOVING TIME", fmt.duration(movingMin * 60)],
          ["REST", `${fmt.num(restMin, 0)} min`],
          ["TOTAL", fmt.duration(totalMin * 60)],
          ["ETA", eta.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" })],
        ]),
        h("div", { class: "callout small mt-10", text: "Great-circle planning estimate only. Terrain, weather, load, navigation error, water and security pauses can dominate. Compare against a current map and add a reserve." }),
      );
    } catch (err) { out.replaceChildren(h("div", { class: "callout danger small", text: err.message })); }
  }

  root.appendChild(panel({
    title: "Movement plan", icon: "compass",
    sub: "Straight-line distance and time budget between two known points. This is not a route and it does not account for obstacles.",
    body: h("div", { class: "col" },
      h("div", { class: "form-grid cols-2" },
        h("div", {}, h("label", { class: "hint", text: "START" }), fromSel),
        h("div", {}, h("label", { class: "hint", text: "DESTINATION" }), toSel)),
      h("div", { class: "form-grid cols-4 mt-10" },
        h("div", {}, h("label", { class: "hint", text: "FROM LAT" }), fLat),
        h("div", {}, h("label", { class: "hint", text: "FROM LON" }), fLon),
        h("div", {}, h("label", { class: "hint", text: "TO LAT" }), tLat),
        h("div", {}, h("label", { class: "hint", text: "TO LON" }), tLon)),
      h("div", { class: "form-grid cols-4 mt-10" },
        h("div", {}, h("label", { class: "hint", text: "PACE (KM/H)" }), speed),
        h("div", {}, h("label", { class: "hint", text: "TERRAIN FACTOR" }), terrain),
        h("div", {}, h("label", { class: "hint", text: "REST MIN / HOUR" }), rest),
        h("div", {}, h("label", { class: "hint", text: "DEPART" }), depart)),
      h("div", { class: "row mt-10" },
        btn("COMPUTE PLAN", { tone: "primary", icon: "compass", onClick: plan }),
        btn("ADD WAYPOINT", { tone: "ghost", icon: "pin", onClick: () => openRecordForm({ table: "waypoints", defaults: currentPosition(), onSaved: () => reRender() }) })),
      out,
    ),
  }));

  const wcols = [
    { label: "NAME", key: "name" },
    { label: "TYPE", key: "type", render: (r) => badge(r.type || "WAYPOINT", "muted") },
    { label: "COORDINATES", key: "lat", render: (r) => h("span", { class: "mono small", text: fmt.coord(r.lat, r.lon, 5) }) },
    { label: "ELEV", key: "ele", render: (r) => h("span", { class: "mono small", text: r.ele != null ? `${fmt.num(r.ele, 0)} m` : "—" }) },
    { label: "NOTE", key: "note", render: (r) => h("span", { class: "small muted", text: r.note || "—" }) },
    { label: "", key: "actions", render: (r) => rowActions({
      extra: [btn("MAP", { size: "sm", tone: "ghost", onClick: () => { location.hash = `#/map?wp=${encodeURIComponent(r.id)}`; } })],
      onEdit: () => openRecordForm({ table: "waypoints", item: r, onSaved: () => reRender() }),
      onDelete: () => deleteRecord("waypoints", r.id, "waypoint", reRender),
    }) },
  ];
  root.appendChild(panel({
    title: "Waypoints", icon: "pin", cls: "mt-14",
    sub: "Shared with the offline map. Coordinates are only as good as the source.",
    tools: [btn("ADD", { size: "sm", icon: "plus", onClick: () => openRecordForm({ table: "waypoints", defaults: currentPosition(), onSaved: () => reRender() }) })],
    body: h("div", { class: "scroll-x" }, recordTable(waypoints, wcols, { empty: "No waypoints saved", emptyIcon: "pin" })),
  }));
}

/* ------------------------------------------------------------ operator data */

function dataSection(root, backups, lowItems) {
  const body = h("div", { class: "col" });
  body.appendChild(h("div", { class: "callout small", text: "Backups are written to the local backup directory. Download a copy to removable media; a backup on the same failing disk is not a backup." }));

  const name = h("input", { class: "input", placeholder: "optional backup name" });
  body.appendChild(h("div", { class: "row mt-10" },
    name,
    btn("CREATE BACKUP", { tone: "primary", icon: "save", onClick: () => backupNow(null, name.value.trim()) }),
    btn("EXPORT JSON", { tone: "ghost", icon: "download", onClick: () => exportData("json") }),
    btn("EXPORT CSV", { tone: "ghost", icon: "download", onClick: () => exportData("csv") })));
  body.appendChild(h("div", { class: "row mt-10" },
    btn("RESTORE FROM FILE", { tone: "warn", icon: "upload", onClick: () => restoreFromFile() }),
    btn("PRINT BACKUP MANIFEST", { tone: "ghost", icon: "print", onClick: () => printManifest(backups) })));

  const list = h("div", { class: "list mt-14" });
  if (!backups.length) list.appendChild(emptyState("No backups yet", "Create one, then copy it off this machine.", "save"));
  for (const b of backups) {
    const item = h("div", { class: "list-item" });
    const main = h("div", { class: "li-main" });
    main.appendChild(h("div", { class: "li-title mono", text: b.name }));
    main.appendChild(h("div", { class: "li-sub", text: `${fmt.bytes(b.bytes)} · ${fmt.date(b.mtime)} · ${fmt.ago(b.mtime)}` }));
    item.appendChild(main);
    item.appendChild(btn("DOWNLOAD", { size: "sm", tone: "ghost", icon: "download", onClick: () => download(`/api/logistics/backups/${encodeURIComponent(b.name)}`) }));
    list.appendChild(item);
  }
  body.appendChild(list);
  root.appendChild(panel({ title: "Operator data custody", icon: "save", sub: "Records, checklists and non-secret settings.", body }));

  const lowList = h("div", { class: "list" });
  if (!lowItems.length) lowList.appendChild(emptyState("No low-stock lines", "Inventory is at or above configured minimums.", "check"));
  for (const r of lowItems) {
    const item = h("div", { class: "list-item warn" });
    const main = h("div", { class: "li-main" });
    main.appendChild(h("div", { class: "li-title", text: r.name }));
    main.appendChild(h("div", { class: "li-sub", text: `${fmt.num(r.qty, 2)} ${r.unit || "each"} on hand · minimum ${fmt.num(r.min_qty, 2)}${r.location ? ` · ${r.location}` : ""}` }));
    item.appendChild(main);
    item.appendChild(btn("SUPPLY", { size: "sm", tone: "ghost", onClick: () => { location.hash = "#/supply"; } }));
    lowList.appendChild(item);
  }
  root.appendChild(panel({
    title: "Low stock", icon: "warning", cls: "mt-14",
    sub: "Configured minimums from the inventory register.",
    tools: [btn("INVENTORY", { size: "sm", onClick: () => { location.hash = "#/supply"; } })],
    body: lowList,
  }));
}

async function backupNow(root, name = "") {
  try {
    const out = await api.post("/api/logistics/backup", name ? { name } : {});
    toast(`Backup written: ${out.file} (${fmt.bytes(out.bytes)})`);
    if (root) reRender();
  } catch (err) { toast(err.message || "Backup failed", "error"); }
}

function exportData(format = "json") {
  download(`/api/logistics/export?fmt=${format === "csv" ? "csv" : "json"}`);
  toast(`Export started (${String(format).toUpperCase()})`);
}

async function restoreFromFile() {
  const picked = await readTextFile(".json,application/json");
  if (!picked) return;
  let data;
  try { data = JSON.parse(picked.text); } catch { toast("File is not valid JSON", "error"); return; }
  if (!data || typeof data !== "object") { toast("Backup file has no data object", "error"); return; }
  const ok = await confirmDialog({
    title: "Restore operator data",
    message: "This replaces all current records and checklists with the contents of the selected file. Encrypted vault notes are not included and are not touched. Export a fresh backup first if you are unsure.",
    confirmText: "RESTORE",
    danger: true,
  });
  if (!ok) return;
  try {
    const out = await api.post("/api/logistics/restore", { data, mode: "replace", confirm: "RESTORE" });
    toast(`Restored ${out.records || 0} record(s)`);
    reRender();
  } catch (err) { toast(err.message || "Restore failed", "error"); }
}

function printManifest(backups) {
  const lines = ["DARKSEC // OPERATOR BACKUP MANIFEST", `Generated ${new Date().toISOString()}`, ""];
  if (!backups.length) lines.push("No backups present.");
  for (const b of backups) lines.push(`${b.mtime}  ${String(b.bytes).padStart(10)}  ${b.name}`);
  const text = lines.join("\n");
  const win = window.open("", "_blank", "width=820,height=900");
  if (!win) { toast("Popup blocked — download instead", "warn"); downloadText("darksec-backup-manifest.txt", text); return; }
  const safe = text.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  win.document.write(`<!doctype html><title>DarkSec backup manifest</title><style>body{font:12px/1.55 monospace;white-space:pre-wrap;padding:24px}</style><pre>${safe}</pre>`);
  win.document.close();
  setTimeout(() => { try { win.focus(); win.print(); } catch {} }, 250);
}

/* ------------------------------------------------------------------- utils */

function currentPosition() {
  const s = store.settings || {};
  return { lat: s.lat, lon: s.lon, ele: s.elevation_m };
}

function reRender() {
  if (currentRoot) render(currentRoot, currentParams);
}
