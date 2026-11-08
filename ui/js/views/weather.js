import { api } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, stat, badge, btn, pageHead, alertStrip, emptyState, loading, fmt, toast,
  modal, field, input, textarea, select,
} from "../components.js";
import { openRecordForm, deleteRecord, recordTable, rowActions } from "../records.js";
import { calculatorGrid } from "../calculators.js";
import { feedBadge, kv, referenceSection, severityBadge, positionConfidence } from "./shared.js";
import * as U from "../units.js";

const WEATHER_AUTO_MS = 120_000;
let weatherTimer = null;
let weatherBusy = false;
let weatherAutoAttempted = false;
let rerender = () => {};

const PINNED_WEATHER_FEEDS = [
  "open_meteo_weather", "open_meteo_air", "nws_alerts", "swpc_kp", "swpc_scales", "usgs_quakes", "gdacs", "eonet",
];

export async function render(root) {
  if (weatherTimer) { clearInterval(weatherTimer); weatherTimer = null; }
  root.replaceChildren(loading("READING ENVIRONMENT"));
  let feedsOut, dash, logs, almanac;
  try {
    [feedsOut, dash, logs] = await Promise.all([
      api.get("/api/feeds"),
      store.loadDashboard().catch(() => null),
      api.get("/api/records/weather_log?limit=40"),
    ]);
    almanac = dash?.almanac || null;
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }

  const feeds = feedsOut.feeds || [];
  const feedById = Object.fromEntries(feeds.map((f) => [f.id, f]));
  const weatherFeed = feedById.open_meteo_weather || null;
  const airFeed = feedById.open_meteo_air || null;
  const nwsFeed = feedById.nws_alerts || null;
  const quakeFeed = feedById.usgs_quakes || null;
  const kpFeed = feedById.swpc_kp || null;

  const needsInitialFetch = !weatherAutoAttempted && !store.settings?.opsec_mode
    && ((weatherFeed && weatherFeed.status === "NEVER") || (airFeed && airFeed.status === "NEVER"));
  if (needsInitialFetch) {
    weatherAutoAttempted = true;
    try {
      await Promise.allSettled([
        weatherFeed?.status === "NEVER" ? api.post("/api/feeds/refresh", { id: "open_meteo_weather" }) : null,
        airFeed?.status === "NEVER" ? api.post("/api/feeds/refresh", { id: "open_meteo_air" }) : null,
      ].filter(Boolean));
      feedsOut = await api.get("/api/feeds");
    } catch {}
  }

  const feeds2 = feedsOut.feeds || feeds;
  const byId = Object.fromEntries(feeds2.map((f) => [f.id, f]));
  const wx = byId.open_meteo_weather || weatherFeed;
  const aq = byId.open_meteo_air || airFeed;
  const nws = byId.nws_alerts || nwsFeed;
  const quakes = byId.usgs_quakes || quakeFeed;
  const kp = byId.swpc_kp || kpFeed;

  const positionInfo = positionConfidence(store.settings || {});
  const observer = fmt.coord(store.settings?.lat, store.settings?.lon, 4);
  const latest = (logs.items || [])[0] || null;
  const wxItem = (wx?.items || [])[0] || null;
  const aqItem = (aq?.items || [])[0] || null;
  const kpItem = [...(kp?.items || [])].reverse().find((i) => i.kp != null) || (kp?.items || []).slice(-1)[0] || null;
  const nwsItems = nws?.items || [];
  const quakeItems = quakes?.items || [];
  const severeNws = nwsItems.some((i) => ["CRITICAL", "SEVERE", "EXTREME", "ALERT"].includes(String(i.severity || "").toUpperCase()));

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // ENVIRONMENT",
    title: "Weather & Environment",
    desc: `Open-Meteo model conditions and air quality for ${observer} (${positionInfo.mapLabel}), plus cached official alerts/space weather and local instrument observations. Model feeds are not observations; verify before life-safety decisions.`,
    actions: [
      btn("REFRESH CONDITIONS", { icon: "refresh", tone: "primary", onClick: () => refreshWeatherFeeds(true) }),
      btn("REFRESH ALL FEEDS", { icon: "refresh", onClick: refreshAll }),
      btn("LOG OBSERVATION", { icon: "plus", onClick: () => addObs() }),
      btn("MANUAL", { icon: "book", onClick: () => { location.hash = "#/manual?doc=weather"; } }),
    ],
  }));

  root.appendChild(alertStrip("Open-Meteo provides model forecasts for the configured position, not local sensor readings. Weather feeds require network and may be stale or incomplete. A barometer, thermometer and your own eyes are primary instruments.", ""));

  if (["unverified", "manual", "geocoded"].includes(String(store.settings?.position_source || "unverified").toLowerCase()) && (wx || aq)) {
    root.appendChild(alertStrip(`Conditions use the configured position (${positionInfo.mapLabel}). ${positionInfo.text}`, "amber"));
  }
  if ((wx && ["NEVER", "ERROR", "STALE"].includes(wx.status)) || (aq && ["NEVER", "ERROR", "STALE"].includes(aq.status))) {
    root.appendChild(alertStrip("Current conditions or air quality are not freshly cached. Use REFRESH CONDITIONS while online; if position is missing/invalid, set latitude/longitude in settings.", "amber"));
  }

  const stats = h("div", { class: "grid grid-4 mt-14" });
  stats.appendChild(stat("Conditions", wxItem ? (wxItem.condition || "cached") : "—", wx ? `${wx.status} · ${fmt.ago(wx.fetched_at)}` : "no weather feed", wxItem && /thunder|freez|heavy|hail|violent/i.test(String(wxItem.condition || "")) ? "acc" : ""));
  stats.appendChild(stat("Temperature", wxItem?.temperature_c != null ? U.tempC(wxItem.temperature_c) : (latest?.temp_c != null ? U.tempC(latest.temp_c) : "—"), wxItem?.temperature_c != null ? "Open-Meteo model" : (latest ? `local obs ${fmt.ago(latest.ts)}` : "no data")));
  stats.appendChild(stat("Air Quality", aqItem?.aqi != null ? `AQI ${aqItem.aqi}` : "—", aqItem ? `${aqItem.category || "unknown"} · ${fmt.ago(aq?.fetched_at)}` : "no AQ snapshot", ["UNHEALTHY", "VERY UNHEALTHY", "HAZARDOUS", "CRITICAL", "SEVERE"].some((s) => String(aqItem?.category || aqItem?.severity || "").toUpperCase().includes(s)) ? "acc" : ""));
  stats.appendChild(stat("Alerts / Kp", String(nwsItems.length).padStart(2, "0"), `NWS alerts · Kp ${kpItem?.kp != null ? kpItem.kp : "—"} · ${quakeItems.length} quake(s)`, severeNws ? "acc" : ""));
  root.appendChild(stats);

  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);

  grid.appendChild(panel({
    title: "Current Conditions (Open-Meteo)",
    sub: `Model forecast for operator position · ${wx?.status || "NEVER"} · fetched ${fmt.ago(wx?.fetched_at)}`,
    icon: "cloud",
    cls: "span-2",
    tools: [btn("REFRESH", { size: "sm", tone: "ghost", icon: "refresh", onClick: () => refreshFeed(wx || { id: "open_meteo_weather", name: "Open-Meteo Local Weather" }) })],
    body: buildConditions(wx, wxItem, aq, aqItem, positionInfo),
  }));

  grid.appendChild(panel({
    title: "Local Instrument Observations",
    sub: "From your latest weather log entry",
    icon: "weather",
    body: buildLocalObs(latest),
  }));

  grid.appendChild(panel({
    title: "Pressure & Temperature Trend",
    sub: "Last 40 local observations",
    icon: "activity",
    body: buildTrend(logs.items || []),
  }));

  grid.appendChild(panel({
    title: "Almanac",
    sub: "Local rise / set",
    icon: "moon",
    tools: [btn("NAV", { size: "sm", onClick: () => { location.hash = "#/nav"; } })],
    body: buildAlmanac(almanac),
  }));

  grid.appendChild(panel({
    title: "Official Alerts & Space Weather",
    sub: "Cached NWS, USGS and NOAA SWPC items",
    icon: "warning",
    body: buildAlerts(nws, kp, quakes),
  }));

  const feedWrap = h("div", { class: "col gap-10" });
  const pinned = PINNED_WEATHER_FEEDS.map((id) => byId[id]).filter(Boolean);
  const rest = feeds2.filter((f) => !PINNED_WEATHER_FEEDS.includes(f.id));
  feedWrap.appendChild(h("div", { class: "side-label", text: "WEATHER & HAZARD FEEDS" }));
  for (const feed of pinned) feedWrap.appendChild(feedCard(feed, () => refreshFeed(feed)));
  feedWrap.appendChild(h("div", { class: "side-label mt-10", text: "ALL OTHER CACHED FEEDS" }));
  if (!rest.length) feedWrap.appendChild(h("div", { class: "small muted", text: "No other feeds." }));
  for (const feed of rest) feedWrap.appendChild(feedCard(feed, () => refreshFeed(feed)));
  grid.appendChild(panel({
    title: "Cached Feeds",
    sub: "Weather-relevant feeds first; verify at source",
    icon: "globe",
    cls: "span-2",
    tools: [btn("REFRESH ALL", { size: "sm", icon: "refresh", onClick: refreshAll })],
    body: feedWrap,
  }));

  root.appendChild(panel({
    title: "Observation Log",
    sub: `${logs.count || 0} records`,
    icon: "note",
    cls: "mt-14",
    tools: [btn("NEW", { size: "sm", icon: "plus", onClick: () => addObs() })],
    body: recordTable(logs.items || [], [
      { key: "ts", label: "Time", render: (r) => fmt.date(r.ts) },
      { key: "temp_c", label: "Temp", num: true, render: (r) => U.tempC(r.temp_c) },
      { key: "pressure_hpa", label: "Pressure", num: true, render: (r) => U.pressureHpa(r.pressure_hpa) },
      { key: "humidity", label: "RH%", num: true, render: (r) => r.humidity != null ? fmt.num(r.humidity, 0) : "—" },
      { key: "wind_kmh", label: "Wind", num: true, render: (r) => U.speedKmh(r.wind_kmh, 0) },
      { key: "cloud_type", label: "Cloud" },
      { key: "note", label: "Note", render: (r) => r.note || "—" },
      { key: "act", label: "", render: (r) => rowActions({
        onEdit: () => openRecordForm({ table: "weather_log", item: r, onSaved: () => render(root) }),
        onDelete: () => deleteRecord("weather_log", r.id, "weather observation", () => render(root)),
      }) },
    ], { empty: "No observations logged" }),
  }));

  root.appendChild(panel({
    title: "Meteorological Calculators",
    sub: "Estimates and standard formulas",
    icon: "flask",
    cls: "mt-14",
    body: await calculatorGrid("WEATHER"),
  }));

  const cards = await referenceSection("WEATHER");
  if (cards) root.appendChild(panel({ title: "Weather Field Card", icon: "book", cls: "mt-14", body: cards }));

  async function refreshWeatherFeeds(showToast) {
    if (showToast) toast("Refreshing Open-Meteo conditions and air quality", "warn");
    try {
      await Promise.allSettled([
        api.post("/api/feeds/refresh", { id: "open_meteo_weather" }),
        api.post("/api/feeds/refresh", { id: "open_meteo_air" }),
      ]);
      if (showToast) toast("Conditions refresh requested");
      render(root);
    } catch (err) { if (showToast) toast(err.message || "Refresh failed", "error"); }
  }

  async function refreshAll() {
    toast("Refreshing public feeds — network required", "warn");
    try {
      const out = await api.post("/api/feeds/refresh", {});
      const ok = (out.results || []).filter((r) => r.status === "OK").length;
      toast(`Feeds refreshed: ${ok}/${(out.results || []).length} OK`);
      render(root);
    } catch (err) { toast(err.message || "Feed refresh failed", "error"); }
  }

  async function refreshFeed(feed) {
    try {
      const out = await api.post("/api/feeds/refresh", { id: feed.id });
      const result = (out.results || [])[0] || {};
      if (result.status === "OK") toast(`${feed.name}: ${result.count ?? 0} items`);
      else toast(`${feed.name}: ${result.error || result.status || "failed"}`, "error");
      render(root);
    } catch (err) { toast(err.message || "Feed refresh failed", "error"); }
  }

  function addObs() {
    const s = store.settings || {};
    openRecordForm({ table: "weather_log", defaults: { lat: s.lat, lon: s.lon }, onSaved: () => render(root) });
  }

  weatherTimer = setInterval(async () => {
    if (weatherBusy || document.hidden) return;
    if (!location.hash.startsWith("#/weather")) return;
    if (document.querySelector(".modal.open, .modal-backdrop")) return;
    if (document.activeElement && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    weatherBusy = true;
    const y = window.scrollY;
    try { await render(root); window.scrollTo(0, y); } catch {}
    finally { weatherBusy = false; }
  }, WEATHER_AUTO_MS);

  return () => { if (weatherTimer) { clearInterval(weatherTimer); weatherTimer = null; } };
}

function feedMeta(feed) {
  if (!feed) return "feed unavailable";
  const parts = [String(feed.kind || "").toUpperCase(), feed.status || "NEVER", `fetched ${fmt.ago(feed.fetched_at)}`];
  if (feed.latest_item_time) parts.push(`data ${fmt.ago(feed.latest_item_time)}`);
  if (feed.stale_reason) parts.push(`stale:${feed.stale_reason}`);
  if (feed.error) parts.push(feed.error);
  return parts.join(" · ");
}

function buildConditions(wx, wxItem, aq, aqItem, positionInfo) {
  if (!wx && !aq) return emptyState("No condition feeds available", "The Open-Meteo feeds are not in the registry.", "cloud");
  const wrap = h("div", { class: "col gap-10" });
  if (!wxItem && !aqItem) {
    wrap.appendChild(emptyState("No current conditions cached", wx?.status === "NEVER" || aq?.status === "NEVER"
      ? "Refresh conditions while online. Position-dependent feeds require a valid latitude/longitude."
      : feedMeta(wx) || feedMeta(aq), "cloud"));
    if (wx?.error) wrap.appendChild(h("div", { class: "callout danger small", text: `Weather: ${wx.error}` }));
    if (aq?.error) wrap.appendChild(h("div", { class: "callout danger small", text: `Air quality: ${aq.error}` }));
    return wrap;
  }
  if (wxItem) {
    wrap.appendChild(h("div", { class: "row gap-14 wrap items-start" },
      h("div", { class: "col gap-4" },
        h("div", { class: "big-num", text: U.tempC(wxItem.temperature_c) }),
        h("div", { class: "tiny muted", text: String(wxItem.condition || "model conditions").toUpperCase() })),
      kv([
        ["Feels like", U.tempC(wxItem.apparent_c)],
        ["Humidity", wxItem.humidity_pct != null ? `${fmt.num(wxItem.humidity_pct, 0)} %` : "—"],
        ["Wind", U.speedKmh(wxItem.wind_kmh)],
        ["Gusts", U.speedKmh(wxItem.gust_kmh)],
        ["Precipitation", U.precipMm(wxItem.precip_mm)],
        ["Pressure (MSL)", U.pressureHpa(wxItem.pressure_hpa)],
        ["Source", feedMeta(wx)],
        ["Position", positionInfo.mapLabel],
      ])));
  }
  if (aqItem) {
    wrap.appendChild(h("div", { class: "panel", style: { padding: "10px 12px" } },
      h("div", { class: "row gap-10 wrap items-center" },
        badge(`US AQI ${aqItem.aqi ?? "—"}`, String(aqItem.severity || "INFO").toUpperCase() === "CRITICAL" ? "red" : String(aqItem.severity || "").toUpperCase() === "SEVERE" ? "amber" : "green"),
        h("div", { class: "small", text: String(aqItem.category || "UNKNOWN") }),
        h("div", { class: "tiny muted", text: aqItem.pm2_5 != null ? `PM2.5 ${fmt.num(aqItem.pm2_5, 1)}` : "" }),
        h("div", { class: "tiny muted", text: feedMeta(aq) })),
      h("div", { class: "tiny muted mt-6", text: aqItem.summary || "Air quality index for the operator position. Not for evacuation decisions alone." })));
  }
  return wrap;
}

function buildLocalObs(latest) {
  const wrap = h("div", { class: "col" });
  if (!latest) {
    wrap.appendChild(emptyState("No local weather observation", "Log temperature, pressure, wind and cloud cover to build a local record.", "weather"));
  } else {
    wrap.appendChild(kv([
      ["Observed", fmt.date(latest.ts)],
      ["Temperature", U.tempC(latest.temp_c)],
      ["Pressure", U.pressureHpa(latest.pressure_hpa)],
      ["Humidity", latest.humidity != null ? `${fmt.num(latest.humidity, 0)} %` : "—"],
      ["Wind", latest.wind_kmh != null ? `${U.speedKmh(latest.wind_kmh)} @ ${fmt.num(latest.wind_dir, 0)}°` : "—"],
      ["Gusts", U.speedKmh(latest.gust_kmh)],
      ["Precipitation", U.precipMm(latest.precip_mm)],
      ["Cloud", latest.cloud_cover != null ? `${fmt.num(latest.cloud_cover, 0)} oktas ${latest.cloud_type || ""}` : "—"],
      ["Visibility", U.distKm(latest.visibility_km, 1)],
    ]));
    if (latest.note) wrap.appendChild(h("div", { class: "callout small mt-8", text: latest.note }));
    const derived = h("div", { class: "col gap-4 mt-10" });
    wrap.appendChild(derived);
    computeDerived(latest, derived);
  }
  wrap.appendChild(h("div", { class: "row wrap mt-10" },
    btn("ADD OBSERVATION", { icon: "plus", size: "sm", onClick: () => openRecordForm({ table: "weather_log", defaults: { lat: store.settings?.lat, lon: store.settings?.lon }, onSaved: rerender }) }),
    latest ? btn("EDIT LATEST", { icon: "edit", size: "sm", tone: "ghost", onClick: () => openRecordForm({ table: "weather_log", item: latest, onSaved: rerender }) }) : null));
  return wrap;
}

function buildAlmanac(almanac) {
  const alm = h("div", { class: "almanac" });
  if (!almanac) return h("div", { class: "small muted", text: "Almanac unavailable. Check the configured position." });
  for (const [k, v] of [
    ["SUNRISE", fmt.time(almanac.sun?.next_rise)], ["SUNSET", fmt.time(almanac.sun?.next_set)],
    ["CIVIL DAWN", fmt.time(almanac.sun?.civil_dawn)], ["CIVIL DUSK", fmt.time(almanac.sun?.civil_dusk)],
    ["MOONRISE", fmt.time(almanac.moon?.next_rise)], ["MOONSET", fmt.time(almanac.moon?.next_set)],
    ["ILLUMINATION", almanac.moon ? `${almanac.moon.illumination_pct}%` : "—"], ["PHASE", almanac.moon?.phase_name || "—"],
    ["SUN ALT", almanac.sun ? `${almanac.sun.altitude_deg}°` : "—"], ["SUN AZ", almanac.sun ? `${almanac.sun.azimuth_deg}°` : "—"],
  ]) {
    alm.appendChild(h("div", { class: "k", text: k }));
    alm.appendChild(h("div", { class: "v", text: v || "—" }));
  }
  return alm;
}

function buildAlerts(nws, kp, quakes) {
  const wrap = h("div", { class: "col gap-10" });
  const sections = [];
  if (nws) sections.push(["NWS Alerts", nws, (nws.items || []).slice(0, 5).map((i) => ({ title: i.title, sub: `${i.severity || "INFO"} · ${i.area || ""} · ${fmt.ago(i.time)}`, sev: i.severity, url: i.url }))]);
  if (kp) sections.push(["Space Weather (Kp)", kp, (kp.items || []).slice(-5).reverse().map((i) => ({ title: i.title || `Kp ${i.kp}`, sub: `${fmt.ago(i.time)} · ${i.source || "NOAA SWPC"}`, sev: i.severity, url: i.url }))]);
  if (quakes) sections.push(["Recent Earthquakes", quakes, (quakes.items || []).slice(0, 5).map((i) => ({ title: i.title, sub: `${i.place || ""}${i.magnitude != null ? ` · M${i.magnitude}` : ""} · ${fmt.ago(i.time)}`, sev: i.severity, url: i.url }))]);
  for (const [label, feed, rows] of sections) {
    wrap.appendChild(h("div", { class: "row between items-center" }, h("div", { class: "side-label", text: label }), feedBadge(feed.status)));
    if (!rows.length) wrap.appendChild(h("div", { class: "small muted", text: feed.status === "NEVER" ? "Never fetched." : "No cached items." }));
    for (const row of rows) {
      const li = h("div", { class: `list-item ${["CRITICAL", "EXTREME"].includes(String(row.sev || "").toUpperCase()) ? "alert" : ["ALERT", "SEVERE"].includes(String(row.sev || "").toUpperCase()) ? "warn" : ""}` });
      li.appendChild(severityBadge(row.sev || "INFO"));
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title truncate", text: row.title || "Item" }));
      main.appendChild(h("div", { class: "li-sub truncate", text: row.sub || "" }));
      li.appendChild(main);
      if (row.url) li.appendChild(h("a", { class: "badge cyan", href: row.url, target: "_blank", rel: "noopener noreferrer", text: "OPEN" }));
      wrap.appendChild(li);
    }
  }
  return wrap;
}

async function computeDerived(latest, target) {
  const values = [];
  async function calc(name, payload) {
    try { return (await api.post(`/api/calc/${name}`, payload)).result; } catch { return null; }
  }
  if (latest.temp_c != null && latest.wind_kmh != null && latest.wind_kmh >= 4.8 && latest.temp_c <= 10) {
    const r = await calc("wind_chill_c", { temp_c: latest.temp_c, wind_kmh: latest.wind_kmh });
    if (r && r.applicable) values.push(["Wind chill", U.tempC(r.value_c)]);
  }
  if (latest.temp_c != null && latest.humidity != null) {
    const r = await calc("heat_index_c", { temp_c: latest.temp_c, rh: latest.humidity });
    if (r && r.applicable) values.push(["Heat index", U.tempC(r.value_c)]);
    const dew = await calc("dew_point_c", { temp_c: latest.temp_c, rh: latest.humidity });
    if (typeof dew === "number") values.push(["Dew point", U.tempC(dew)]);
    const wet = await calc("wet_bulb_c", { temp_c: latest.temp_c, rh: latest.humidity });
    if (typeof wet === "number") values.push(["Wet bulb", U.tempC(wet)]);
  }
  if (latest.pressure_hpa != null) {
    const pa = await calc("pressure_altitude_ft", { pressure_hpa: latest.pressure_hpa });
    if (typeof pa === "number") values.push(["Pressure altitude", `${fmt.num(pa, 0)} ft`]);
  }
  if (values.length && target.isConnected) {
    target.appendChild(h("div", { class: "side-label", text: "DERIVED VALUES" }));
    target.appendChild(kv(values));
  }
}

function buildTrend(items) {
  const wrap = h("div", { class: "col" });
  if (!items.length) return emptyState("No trend data", "Log observations to see environmental trends.", "activity");
  const ordered = [...items].reverse();
  const pressure = ordered.filter((x) => x.pressure_hpa != null).map((x) => U.isImperial() ? Number(x.pressure_hpa) * 0.0295299830714 : Number(x.pressure_hpa));
  const temp = ordered.filter((x) => x.temp_c != null).map((x) => U.isImperial() ? Number(x.temp_c) * 9 / 5 + 32 : Number(x.temp_c));
  const c1 = h("canvas", { class: "vital-chart" });
  const c2 = h("canvas", { class: "vital-chart" });
  wrap.appendChild(h("div", { class: "tiny muted", text: `PRESSURE (${U.pressureUnit()})` }));
  wrap.appendChild(c1);
  wrap.appendChild(h("div", { class: "tiny muted", text: `TEMPERATURE (${U.tempUnit()})` }));
  wrap.appendChild(c2);
  setTimeout(() => { drawSeries(c1, pressure, "#5dff9a"); drawSeries(c2, temp, "#ffb02e"); }, 30);
  return wrap;
}

function drawSeries(canvas, data, color) {
  if (!canvas.isConnected) return;
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, rect.width || 300);
  const hh = Math.max(1, rect.height || 120);
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(hh * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.fillStyle = "#020604";
  ctx.fillRect(0, 0, w, hh);
  ctx.strokeStyle = "rgba(93,255,154,.09)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) { const y = (hh / 4) * i; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  if (data.length < 2) {
    ctx.fillStyle = "rgba(123,154,136,.75)";
    ctx.font = "10px JetBrains Mono, monospace";
    ctx.fillText("INSUFFICIENT DATA", 8, hh / 2);
    return;
  }
  let min = Math.min(...data), max = Math.max(...data);
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * 0.15; min -= pad; max += pad;
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = 4 + (i / (data.length - 1)) * (w - 8);
    const y = hh - 5 - ((v - min) / (max - min)) * (hh - 12);
    if (!i) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.stroke();
  ctx.fillStyle = "rgba(123,154,136,.8)"; ctx.font = "8px JetBrains Mono, monospace";
  ctx.fillText(fmt.num(max, 1), 3, 9); ctx.fillText(fmt.num(min, 1), 3, hh - 3);
}

function feedCard(feed, onRefresh) {
  const card = h("div", { class: "device-card" });
  const head = h("div", { class: "row between wrap gap-6" });
  const title = h("div", { class: "row gap-6 wrap items-center" });
  title.appendChild(h("span", { class: "dev-name", text: feed.name }));
  title.appendChild(feedBadge(feed.status));
  if (feed.stale_reason) title.appendChild(badge(`STALE: ${feed.stale_reason.toUpperCase()}`, "amber"));
  head.appendChild(title);
  const tools = h("div", { class: "row gap-4" });
  if (feed.url) tools.appendChild(h("a", { class: "btn btn-sm btn-ghost", href: feed.url, target: "_blank", rel: "noopener noreferrer", text: "SOURCE" }));
  tools.appendChild(btn("REFRESH", { size: "sm", tone: "ghost", onClick: onRefresh }));
  head.appendChild(tools);
  card.appendChild(head);
  card.appendChild(h("div", { class: "tiny muted mt-6", text: feedMeta(feed) }));
  const items = feed.items || [];
  if (items.length) {
    const list = h("div", { class: "list mt-6" });
    for (const it of items.slice(0, 5)) {
      const row = h("div", { class: `list-item ${["CRITICAL", "EXTREME"].includes(String(it.severity || "").toUpperCase()) ? "alert" : ["ALERT", "SEVERE"].includes(String(it.severity || "").toUpperCase()) ? "warn" : ""}` });
      row.appendChild(severityBadge(it.severity || "INFO"));
      const main = h("div", { class: "li-main" });
      const titleText = it.title || "Feed item";
      if (it.url) main.appendChild(h("div", { class: "li-title truncate" }, h("a", { href: it.url, target: "_blank", rel: "noopener noreferrer", text: titleText, style: { color: "inherit" } })));
      else main.appendChild(h("div", { class: "li-title truncate", text: titleText }));
      const sub = [it.place || it.area || it.country, it.magnitude != null ? `M${it.magnitude}` : "", it.kp != null ? `Kp ${it.kp}` : "", it.aqi != null ? `AQI ${it.aqi}` : "", it.condition || "", it.temperature_c != null ? U.tempC(it.temperature_c) : "", it.time ? fmt.date(it.time) : ""].filter(Boolean).join(" · ");
      if (sub) main.appendChild(h("div", { class: "li-sub truncate", text: sub }));
      row.appendChild(main);
      list.appendChild(row);
    }
    card.appendChild(list);
    if (items.length > 5) card.appendChild(h("div", { class: "tiny dim mt-6", text: `+${items.length - 5} more cached items` }));
  } else {
    card.appendChild(h("div", { class: "small muted mt-6", text: feed.status === "NEVER" ? "Never fetched. A network connection is required, and position-dependent feeds need valid coordinates." : (feed.error ? `No items: ${feed.error}` : "No cached items.") }));
  }
  return card;
}