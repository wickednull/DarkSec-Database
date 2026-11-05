import { api } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, stat, badge, btn, pageHead, alertStrip, emptyState, loading, fmt, toast,
  modal, field, input, textarea, select,
} from "../components.js";
import { openRecordForm, deleteRecord, recordTable, rowActions, severityTone } from "../records.js";
import { calculatorGrid } from "../calculators.js";
import { feedBadge, kv, referenceSection, severityBadge, positionConfidence } from "./shared.js";

const FEED_ICON = {
  gdacs: "globe", swpc_kp: "sun", swpc_scales: "sun", nws_alerts: "weather",
  reliefweb: "globe", usgs_quakes: "activity", who_don: "medical",
  eonet: "globe", smithsonian_volcano: "activity", open_meteo_weather: "cloud",
  open_meteo_air: "cloud", safecast: "radiation",
};

const WEATHER_AUTO_MS = 120_000;
let weatherTimer = null;
let weatherBusy = false;

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
  const positionInfo = positionConfidence(store.settings || {});
  const observer = fmt.coord(store.settings?.lat, store.settings?.lon, 4);
  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // ENVIRONMENT",
    title: "Weather & Environment",
    desc: `Local observations and cached official feeds. Location-based forecasts use ${observer} · ${positionInfo.mapLabel}. These are nowcasts and warnings, not a forecast product.`,
    actions: [
      btn("REFRESH FEEDS", { icon: "refresh", tone: "primary", onClick: refreshAll }),
      btn("LOG OBSERVATION", { icon: "plus", onClick: () => addObs() }),
      btn("MANUAL", { icon: "book", onClick: () => { location.hash = "#/manual?doc=weather"; } }),
    ],
  }));

  root.appendChild(alertStrip("Weather feeds require network and may be stale or incomplete. A barometer, thermometer and your own eyes are the primary instruments. Never rely on a single model or feed for life-safety decisions.", ""));

  const latest = (logs.items || [])[0] || null;
  const summary = feedsOut.summary || {};
  const allItems = summary.latest || [];
  const nws = allItems.filter((i) => i.source === "NWS");
  const quakes = allItems.filter((i) => i.source === "USGS");
  const kpItem = allItems.find((i) => i.source === "NOAA SWPC" && i.kp != null);

  const stats = h("div", { class: "grid grid-4 mt-14" });
  stats.appendChild(stat("Temperature", latest?.temp_c != null ? `${fmt.num(latest.temp_c, 1)}°C` : "—", latest ? fmt.ago(latest.ts) : "no local reading"));
  stats.appendChild(stat("Pressure", latest?.pressure_hpa != null ? `${fmt.num(latest.pressure_hpa, 1)} hPa` : "—", "station pressure if entered"));
  stats.appendChild(stat("Kp index", kpItem?.kp != null ? String(kpItem.kp) : "—", kpItem ? `NOAA SWPC · ${fmt.ago(kpItem.time)}` : "no cached space weather"));
  stats.appendChild(stat("Alerts", String(nws.length).padStart(2, "0"), `${quakes.length} quake item(s) cached`, nws.some((i) => String(i.severity).toUpperCase() === "SEVERE" || String(i.severity).toUpperCase() === "EXTREME") ? "acc" : ""));
  root.appendChild(stats);

  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);

  /* ------------------------------------------------------------- nowcast */
  const nowcast = h("div", { class: "col" });
  if (!latest) {
    nowcast.appendChild(emptyState("No local weather observation", "Log temperature, pressure, wind and cloud cover to build a local record.", "weather"));
  } else {
    const rows = [
      ["Observed", fmt.date(latest.ts)],
      ["Temperature", `${fmt.num(latest.temp_c, 1)} °C`],
      ["Pressure", latest.pressure_hpa != null ? `${fmt.num(latest.pressure_hpa, 1)} hPa` : "—"],
      ["Humidity", latest.humidity != null ? `${fmt.num(latest.humidity, 0)} %` : "—"],
      ["Wind", latest.wind_kmh != null ? `${fmt.num(latest.wind_kmh, 0)} km/h @ ${fmt.num(latest.wind_dir, 0)}°` : "—"],
      ["Gusts", latest.gust_kmh != null ? `${fmt.num(latest.gust_kmh, 0)} km/h` : "—"],
      ["Precipitation", latest.precip_mm != null ? `${fmt.num(latest.precip_mm, 1)} mm` : "—"],
      ["Cloud", latest.cloud_cover != null ? `${fmt.num(latest.cloud_cover, 0)} oktas ${latest.cloud_type || ""}` : "—"],
      ["Visibility", latest.visibility_km != null ? `${fmt.num(latest.visibility_km, 1)} km` : "—"],
    ];
    nowcast.appendChild(kv(rows));
    if (latest.note) nowcast.appendChild(h("div", { class: "callout small", text: latest.note }));
    const derived = h("div", { class: "col gap-4 mt-10" });
    nowcast.appendChild(derived);
    computeDerived(latest, derived);
  }
  nowcast.appendChild(h("div", { class: "row wrap mt-10" },
    btn("ADD OBSERVATION", { icon: "plus", size: "sm", onClick: () => addObs() }),
    btn("EDIT LATEST", { icon: "edit", size: "sm", tone: "ghost", onClick: () => {
      if (latest) openRecordForm({ table: "weather_log", item: latest, onSaved: () => render(root) });
    } })));
  grid.appendChild(panel({
    title: "Local nowcast",
    sub: "From your latest observation",
    icon: "weather",
    body: nowcast,
  }));

  /* ---------------------------------------------------------- trend chart */
  const trendPanel = panel({
    title: "Pressure & temperature trend",
    sub: "Last 40 observations",
    icon: "activity",
    body: buildTrend(logs.items || []),
  });
  grid.appendChild(trendPanel);

  /* ------------------------------------------------------------- almanac */
  const alm = h("div", { class: "almanac" });
  if (almanac) {
    const rows = [
      ["SUNRISE", fmt.time(almanac.sun.next_rise)], ["SUNSET", fmt.time(almanac.sun.next_set)],
      ["CIVIL DAWN", fmt.time(almanac.sun.civil_dawn)], ["CIVIL DUSK", fmt.time(almanac.sun.civil_dusk)],
      ["MOONRISE", fmt.time(almanac.moon.next_rise)], ["MOONSET", fmt.time(almanac.moon.next_set)],
      ["ILLUMINATION", `${almanac.moon.illumination_pct}%`], ["PHASE", almanac.moon.phase_name],
      ["SUN ALT", `${almanac.sun.altitude_deg}°`], ["SUN AZ", `${almanac.sun.azimuth_deg}°`],
    ];
    for (const [k, v] of rows) {
      alm.appendChild(h("div", { class: "k", text: k }));
      alm.appendChild(h("div", { class: "v", text: v }));
    }
  } else {
    alm.appendChild(h("div", { class: "small muted", text: "Almanac unavailable. Check the configured position." }));
  }
  grid.appendChild(panel({
    title: "Almanac",
    sub: "Local rise / set",
    icon: "moon",
    tools: [btn("NAV", { size: "sm", onClick: () => { location.hash = "#/nav"; } })],
    body: alm,
  }));

  /* --------------------------------------------------------------- feeds */
  const feedWrap = h("div", { class: "col" });
  const feedList = feedsOut.feeds || [];
  if (!feedList.length) {
    feedWrap.appendChild(emptyState("No feed catalog", "The feed registry is empty.", "cloud"));
  }
  for (const feed of feedList) {
    feedWrap.appendChild(feedCard(feed, () => refreshOne(feed)));
  }
  grid.appendChild(panel({
    title: "Official feeds",
    sub: "Cached locally · verify at source",
    icon: "globe",
    cls: "span-2",
    tools: [btn("REFRESH ALL", { size: "sm", icon: "refresh", onClick: refreshAll })],
    body: feedWrap,
  }));

  /* ------------------------------------------------------ observations log */
  const logPanel = panel({
    title: "Observation log",
    sub: `${logs.count || 0} records`,
    icon: "note",
    cls: "span-2",
    tools: [btn("NEW", { size: "sm", icon: "plus", onClick: () => addObs() })],
    body: recordTable(logs.items || [], [
      { key: "ts", label: "Time", render: (r) => fmt.date(r.ts) },
      { key: "temp_c", label: "Temp", num: true, render: (r) => r.temp_c != null ? `${fmt.num(r.temp_c, 1)}°` : "—" },
      { key: "pressure_hpa", label: "Pressure", num: true, render: (r) => r.pressure_hpa != null ? fmt.num(r.pressure_hpa, 1) : "—" },
      { key: "humidity", label: "RH%", num: true, render: (r) => r.humidity != null ? fmt.num(r.humidity, 0) : "—" },
      { key: "wind_kmh", label: "Wind", num: true, render: (r) => r.wind_kmh != null ? `${fmt.num(r.wind_kmh, 0)} km/h` : "—" },
      { key: "cloud_type", label: "Cloud" },
      { key: "note", label: "Note", render: (r) => r.note || "—" },
      { key: "act", label: "", render: (r) => rowActions({
        onEdit: () => openRecordForm({ table: "weather_log", item: r, onSaved: () => render(root) }),
        onDelete: () => deleteRecord("weather_log", r.id, "weather observation", () => render(root)),
      }) },
    ], { empty: "No observations logged" }),
  });
  root.appendChild(logPanel);

  /* ----------------------------------------------------------- calculators */
  const calcPanel = panel({
    title: "Meteorological calculators",
    sub: "Estimates and standard formulas",
    icon: "flask",
    body: await calculatorGrid("WEATHER"),
  });
  root.appendChild(calcPanel);

  const cards = await referenceSection("WEATHER");
  if (cards) root.appendChild(panel({ title: "Weather field card", icon: "book", body: cards }));

  /* ---------------------------------------------------------------- actions */
  async function refreshAll() {
    toast("Refreshing public feeds — network required", "warn");
    try {
      const out = await api.post("/api/feeds/refresh", {});
      const ok = (out.results || []).filter((r) => r.status === "OK").length;
      toast(`Feeds refreshed: ${ok}/${(out.results || []).length} OK`);
      render(root);
    } catch (err) { toast(err.message || "Feed refresh failed", "error"); }
  }

  async function refreshOne(feed) {
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
    openRecordForm({
      table: "weather_log",
      defaults: { lat: s.lat, lon: s.lon },
      onSaved: () => render(root),
    });
  }

  weatherTimer = setInterval(async () => {
    if (weatherBusy || document.hidden) return;
    if (!location.hash.startsWith("#/weather")) return;
    if (document.querySelector(".modal.open, .modal")) return;
    if (document.activeElement && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    weatherBusy = true;
    const y = window.scrollY;
    try {
      await render(root);
      window.scrollTo(0, y);
    } catch {}
    finally { weatherBusy = false; }
  }, WEATHER_AUTO_MS);

  return () => {
    if (weatherTimer) { clearInterval(weatherTimer); weatherTimer = null; }
  };
}

async function computeDerived(latest, target) {
  const values = [];
  async function calc(name, payload) {
    try {
      const out = await api.post(`/api/calc/${name}`, payload);
      return out.result;
    } catch { return null; }
  }
  if (latest.temp_c != null && latest.wind_kmh != null && latest.wind_kmh >= 4.8 && latest.temp_c <= 10) {
    const r = await calc("wind_chill_c", { temp_c: latest.temp_c, wind_kmh: latest.wind_kmh });
    if (r && r.applicable) values.push(["Wind chill", `${fmt.num(r.value_c, 1)} °C`]);
  }
  if (latest.temp_c != null && latest.humidity != null) {
    const r = await calc("heat_index_c", { temp_c: latest.temp_c, rh: latest.humidity });
    if (r && r.applicable) values.push(["Heat index", `${fmt.num(r.value_c, 1)} °C`]);
    const dew = await calc("dew_point_c", { temp_c: latest.temp_c, rh: latest.humidity });
    if (typeof dew === "number") values.push(["Dew point", `${fmt.num(dew, 1)} °C`]);
    const wet = await calc("wet_bulb_c", { temp_c: latest.temp_c, rh: latest.humidity });
    if (typeof wet === "number") values.push(["Wet bulb", `${fmt.num(wet, 1)} °C`]);
  }
  if (latest.pressure_hpa != null) {
    const pa = await calc("pressure_altitude_ft", { pressure_hpa: latest.pressure_hpa });
    if (typeof pa === "number") values.push(["Pressure altitude", `${fmt.num(pa, 0)} ft`]);
  }
  if (values.length) {
    target.appendChild(h("div", { class: "side-label", text: "DERIVED VALUES" }));
    target.appendChild(kv(values));
  }
}

function buildTrend(items) {
  const wrap = h("div", { class: "col" });
  if (!items.length) return emptyState("No trend data", "Log observations to see environmental trends.", "activity");
  const ordered = [...items].reverse();
  const pressure = ordered.filter((x) => x.pressure_hpa != null).map((x) => Number(x.pressure_hpa));
  const temp = ordered.filter((x) => x.temp_c != null).map((x) => Number(x.temp_c));
  const c1 = h("canvas", { class: "vital-chart" });
  const c2 = h("canvas", { class: "vital-chart" });
  wrap.appendChild(h("div", { class: "tiny muted", text: "PRESSURE (hPa)" }));
  wrap.appendChild(c1);
  wrap.appendChild(h("div", { class: "tiny muted", text: "TEMPERATURE (°C)" }));
  wrap.appendChild(c2);
  setTimeout(() => {
    drawSeries(c1, pressure, "#5dff9a");
    drawSeries(c2, temp, "#ffb02e");
  }, 20);
  return wrap;
}

function drawSeries(canvas, data, color) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const w = rect.width, hh = rect.height;
  ctx.fillStyle = "#020604";
  ctx.fillRect(0, 0, w, hh);
  ctx.strokeStyle = "rgba(93,255,154,.09)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = (hh / 4) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  if (data.length < 2) {
    ctx.fillStyle = "rgba(123,154,136,.75)";
    ctx.font = "10px JetBrains Mono, monospace";
    ctx.fillText("INSUFFICIENT DATA", 8, hh / 2);
    return;
  }
  let min = Math.min(...data), max = Math.max(...data);
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * 0.15;
  min -= pad; max += pad;
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = 4 + (i / (data.length - 1)) * (w - 8);
    const y = hh - 5 - ((v - min) / (max - min)) * (hh - 12);
    if (!i) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.6;
  ctx.stroke();
  ctx.fillStyle = "rgba(123,154,136,.8)";
  ctx.font = "8px JetBrains Mono, monospace";
  ctx.fillText(fmt.num(max, 1), 3, 9);
  ctx.fillText(fmt.num(min, 1), 3, hh - 3);
}

function feedCard(feed, onRefresh) {
  const card = h("div", { class: "device-card" });
  const head = h("div", { class: "row between" });
  const title = h("div", { class: "row gap-6" });
  title.appendChild(h("span", { class: "dev-name", text: feed.name }));
  title.appendChild(feedBadge(feed.status));
  head.appendChild(title);
  const tools = h("div", { class: "row gap-4" });
  if (feed.url) {
    tools.appendChild(h("a", { class: "btn btn-sm btn-ghost", href: feed.url, target: "_blank", rel: "noopener noreferrer", text: "SOURCE" }));
  }
  tools.appendChild(btn("REFRESH", { size: "sm", tone: "ghost", onClick: onRefresh }));
  head.appendChild(tools);
  card.appendChild(head);
  card.appendChild(h("div", { class: "tiny muted mt-6", text: `${feed.kind.toUpperCase()} · fetched ${fmt.ago(feed.fetched_at)} · ${feed.count} cached item(s)${feed.error ? ` · ${feed.error}` : ""}` }));
  const items = feed.items || [];
  if (items.length) {
    const list = h("div", { class: "list mt-6" });
    for (const it of items.slice(0, 6)) {
      const row = h("div", { class: `list-item ${it.severity === "CRITICAL" || it.severity === "EXTREME" ? "alert" : it.severity === "ALERT" || it.severity === "SEVERE" ? "warn" : ""}` });
      row.appendChild(severityBadge(it.severity || "INFO"));
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title truncate", text: it.title || "Feed item" }));
      const sub = [it.place || it.area, it.magnitude != null ? `M${it.magnitude}` : "", it.kp != null ? `Kp ${it.kp}` : "", it.time ? fmt.date(it.time) : "", it.source].filter(Boolean).join(" · ");
      main.appendChild(h("div", { class: "li-sub truncate", text: sub }));
      row.appendChild(main);
      if (it.url) row.appendChild(h("a", { class: "badge cyan", href: it.url, target: "_blank", rel: "noopener noreferrer", text: "OPEN" }));
      list.appendChild(row);
    }
    card.appendChild(list);
    if (items.length > 6) card.appendChild(h("div", { class: "tiny dim mt-6", text: `+${items.length - 6} more cached items` }));
  } else {
    card.appendChild(h("div", { class: "small muted mt-6", text: feed.status === "NEVER" ? "Never fetched. A network connection is required." : "No cached items." }));
  }
  return card;
}
