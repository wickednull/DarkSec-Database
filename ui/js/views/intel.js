import { api } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, stat, gauge, badge, btn, pageHead, alertStrip, emptyState, loading, fmt, toast,
} from "../components.js";
import { kv, feedBadge, severityBadge, positionConfidence } from "./shared.js";

const AUTO_MS = 90_000;
let autoTimer = null;
let autoBusy = false;

export async function render(root) {
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  root.replaceChildren(loading("READING GLOBAL INTELLIGENCE"));
  let data;
  try {
    data = await api.get("/api/intel/overview");
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }
  root.replaceChildren();
  const threat = data.threat || {};
  const events = data.events || [];
  const aircraft = data.aircraft || [];
  const satellites = data.satellites || [];
  const air = data.air_quality || null;
  const sun = data.sun || null;
  const criticalList = (data.feeds || []).flatMap((f) => (f.items || [])).filter(
    (i) => ["CRITICAL", "SEVERE", "EXTREME"].includes(String(i.severity || "INFO").toUpperCase()),
  );
  const war = data.war || [];
  const conflictLive = data.conflict_live || [];
  const conflictVerified = data.conflict_verified || [];
  const warSummary = data.war_summary || {};
  const neoSummary = data.neo_summary || null;
  const neoObjects = data.neo_objects || [];
  const conflictNews = data.conflict_news || [];
  const disasterNews = data.disaster_news || [];
  const covidStats = data.covid_stats || null;
  const diseaseSummary = data.disease_summary || null;
  const threatBand = String(threat.band || "NO DATA");
  const positionInfo = positionConfidence(store.settings || {});
  const observer = fmt.coord(store.settings?.lat, store.settings?.lon, 4);

  root.appendChild(pageHead({
    kicker: "OPERATIONS // GLOBAL INTELLIGENCE",
    title: "Global Threat & Live Intel",
    desc: "Live intelligence while online: geocoded GDELT conflict reports, monthly verified UCDP conflict events, OpenSky aircraft, CelesTrak satellites, NASA asteroids and EONET hazards, GDACS disasters, USGS earthquakes, NOAA space weather, NWS alerts and world news. Every item carries its age; cached data stays readable offline and is never presented as a live warning.",
    actions: [
      btn("REFRESH INTEL", { icon: "refresh", tone: "primary", onClick: refreshIntel }),
      btn("OPEN MAP", { icon: "map", onClick: () => { location.hash = "#/map"; } }),
      btn("FEEDS", { icon: "globe", onClick: () => { location.hash = "#/weather"; } }),
    ],
  }));

  const sync = data.sync || store.state.sync || {};
  const syncTone = sync.opsec ? "amber" : sync.online ? "green" : "red";
  const syncLabel = sync.opsec
    ? "LINK PAUSED · OPSEC"
    : sync.online
      ? (sync.enabled === false ? "ONLINE · AUTO-SYNC OFF" : "LIVE · AUTO-SYNC ON")
      : "OFFLINE · CACHED DATA";
  root.appendChild(h("div", { class: "row gap-8 wrap mt-8" },
    badge(syncLabel, syncTone),
    h("span", { class: "tiny muted", text: `Last successful refresh ${fmt.ago(sync.last_success)} · ${sync.feed_ok ?? "?"}/${sync.feed_total ?? "?"} feeds cached · view auto-refreshes every 90 s` })));

  if (criticalList.length || threatBand === "SEVERE" || threatBand === "ELEVATED") {
    root.appendChild(alertStrip(
      `${criticalList.length} critical cached intel item(s) · local threat index ${threat.score ?? 0}/100 (${threatBand}). Confirm with official sources before acting.`,
      criticalList.length ? "red" : "amber",
    ));
  }
  if (!warSummary.newest_age_hours || warSummary.newest_age_hours > 6) {
    root.appendChild(alertStrip(
      warSummary.newest_age_hours
        ? `Newest conflict report is ${fmt.ago(warSummary.newest_time)} old. Treat the conflict picture as cached history, not a live warning; refresh feeds before relying on it.`
        : "No dated conflict reports are cached. Refresh while online; do not treat an empty panel as a quiet picture.",
      "amber",
    ));
  }

  const stats = h("div", { class: "grid grid-4 mt-14" });
  stats.appendChild(stat("THREAT INDEX", String(threat.score ?? 0), threatBand, (threatBand === "SEVERE" || threatBand === "ELEVATED") ? "acc" : ""));
  stats.appendChild(stat("AIRCRAFT TRACKED", String(aircraft.length).padStart(3, "0"), "OpenSky public positions"));
  stats.appendChild(stat("SATS OVER OBSERVER", String(satellites.length).padStart(3, "0"), "CelesTrak TLE · SGP4"));
  stats.appendChild(stat("CRITICAL CACHED", String(criticalList.length).padStart(3, "0"), "across all feeds"));
  stats.appendChild(stat("CONFLICT EVENTS", String(conflictLive.length + conflictVerified.length).padStart(3, "0"), `${conflictVerified.length} verified · ${conflictLive.length} live media`));
  if (neoSummary) {
    stats.appendChild(stat("NEO OBJECTS", String(neoSummary.total || 0), `${neoSummary.hazardous || 0} hazardous`, neoSummary.threat_level === "HIGH" || neoSummary.threat_level === "CRITICAL" ? "acc" : ""));
  }
  if (diseaseSummary) {
    stats.appendChild(stat("DISEASE TRACKING", String(diseaseSummary.active_outbreaks || 0), `${covidStats?.active ? (covidStats.active / 1000000).toFixed(1) + "M" : "0"} active COVID`, covidStats?.severity === "CRITICAL" ? "acc" : ""));
  }
  root.appendChild(stats);

  const threatPanel = panel({
    title: "Global Threat Index",
    sub: "Local aggregation over cached live events",
    icon: "warning",
    body: h("div", { class: "col gap-10" },
      h("div", { class: "row gap-14" },
        gauge(threat.score ?? 0, threatBand),
        h("div", { class: "col gap-4" },
          badge("METHOD", "LOCAL_WEIGHTED"),
          kv([
            ["CRITICAL ITEMS", String(criticalList.length)],
            ["BAND", threatBand],
            ["TOP SOURCE", Object.entries(threat.by_source || {}).sort((a, b) => b[1] - a[1])[0]?.[0] || "—"],
            ["HIGHEST LOAD", Object.entries(threat.by_source || {}).sort((a, b) => b[1] - a[1])[0]?.[1] ?? 0],
            ["EVENTS IN SCOPE", String(threat.events_scoped ?? true)],
          ]),
        ),
      ),
      h("div", { class: "tiny muted", text: `Weights apply to sourced live events (earthquakes, disasters, public safety warnings) within the local weighting window from the stored reference position (${threat.near_count ?? 0} events inside 500 km). Computed only from cached events. Not a forecast product.` }),
      h("div", { class: "tiny muted", text: sun ? `SOLAR STATE ${Number(sun?.sun?.altitude_deg) > 0 ? "DAY" : "NIGHT"} · SUN ALT ${sun?.sun?.altitude_deg ?? "—"} deg · NEXT RISE ${fmt.date(sun?.sun?.next_rise)} · NEXT SET ${fmt.date(sun?.sun?.next_set)}` : "SOLAR STATE UNKNOWN" }),
    ),
    cls: "span-2",
  });

  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);
  grid.appendChild(threatPanel);

  grid.appendChild(panel({
    title: "Live Aircraft (OpenSky public)",
    sub: `${aircraft.length} cached position(s)`,
    icon: "satellite",
    body: buildAircraft(aircraft),
  }));

  grid.appendChild(panel({
    title: "Satellite Watch (CelesTrak TLE)",
    sub: `SGP4 prediction for observer ${observer} · ${positionInfo.mapLabel}`,
    icon: "satellite",
    body: buildSats(satellites),
  }));

  const airBody = air ? h("div", { class: "col gap-4" },
    h("div", { class: "big-num", text: String(air.aqi ?? "—") }),
    h("div", { class: "tiny muted", text: `AQI ${air.aqi ?? "—"} · ${air.category || "UNKNOWN"} · ${air.source || ""} · fetched ${fmt.ago(air.time)}` }),
    emptyState("Air quality layer refreshes while online; cached snapshot remains readable offline.", "", "wind"),
  ) : h("div", { class: "small muted", text: "No air quality snapshot cached. Refresh while online." });
  grid.appendChild(panel({
    title: "Air Quality (Open-Meteo)",
    sub: "US AQI snapshot",
    icon: "wind",
    body: airBody,
  }));

  if (neoSummary) {
    grid.appendChild(panel({
      title: "Near-Earth Objects (NASA)",
      sub: `${neoSummary.total} objects · ${neoSummary.hazardous} hazardous · max threat ${neoSummary.max_threat_score ?? 0}/100 (${neoSummary.threat_level || "UNKNOWN"})`,
      icon: "alert",
      body: buildNEO(neoObjects),
    }));
  }

  if (covidStats && Object.keys(covidStats).length > 0) {
    grid.appendChild(panel({
      title: "Disease Tracking (COVID-19)",
      sub: `Active outbreaks: ${diseaseSummary?.active_outbreaks || 0} · Severity: ${covidStats.severity || "UNKNOWN"}`,
      icon: "biohazard",
      body: buildDisease(covidStats),
    }));
  }

  grid.appendChild(panel({
    title: "Global Live Events",
    sub: `${events.length} cached event(s) with coordinates`,
    icon: "globe",
    body: buildEvents(events),
    cls: "span-2",
  }));

  grid.appendChild(panel({
    title: "Conflict & War Intelligence",
    sub: `${warSummary.live ?? conflictLive.length} live media · ${warSummary.verified ?? conflictVerified.length} verified · ${warSummary.headlines ?? war.length} headlines · newest ${warSummary.newest_time ? fmt.ago(warSummary.newest_time) : "unavailable"}`,
    icon: "alert",
    body: buildWar(war),
    cls: "span-2",
  }));

  if (conflictNews.length > 0) {
    grid.appendChild(panel({
      title: "Conflict News (NewsAPI)",
      sub: `${conflictNews.length} real-time conflict stories`,
      icon: "alert",
      body: buildNews(conflictNews),
      cls: "span-2",
    }));
  }

  if (disasterNews.length > 0) {
    grid.appendChild(panel({
      title: "Disaster News (NewsAPI)",
      sub: `${disasterNews.length} real-time disaster stories`,
      icon: "alert",
      body: buildNews(disasterNews),
      cls: "span-2",
    }));
  }

  grid.appendChild(panel({
    title: "Live News & Public Safety Feeds",
    sub: "Cached official and wire sources",
    icon: "book",
    body: h("div", { class: "col gap-10" },
      h("div", { class: "grid grid-2" },
        ...(data.feeds || []).filter((f) => ["gdelt_conflict", "ucdp_candidate", "bbc_world", "aljazeera_world", "gdacs", "reliefweb", "nws_alerts", "swpc_kp", "swpc_scales", "usgs_quakes", "open_meteo_air", "opensky_states", "celestrak_tle"].includes(f.id)).slice(0, 6).map((f) => newsCard(f)),
      ),
    ),
  }));

  root.appendChild(h("div", { class: "tiny dim mt-14", text: "INTEL SOURCES: GDELT 2.0 (automated media coding, 15-minute slices), UCDP Candidate Events (monthly verified, lags weeks), OpenSky Network (public positions), CelesTrak (TLE/SGP4), USGS (earthquakes), BBC/Al Jazeera/Guardian/France24/DW (world news), International Crisis Group, Open-Meteo (air quality), NOAA SWPC (space weather), NWS (alerts), ReliefWeb/GDACS/WHO (disasters). Every item shows its age; a cached snapshot is never a live warning." }));

  return () => {};

  async function refreshIntel() {
    toast("Refreshing public intel feeds", "warn");
    try {
      const out = await api.post("/api/intel/refresh", {});
      const ok = (out.results || []).filter((r) => r.status === "OK").length;
      toast(`Intel refresh: ${ok}/${(out.results || []).length} source(s) OK`);
      render(root);
    } catch (err) { toast(err.message || "Intel refresh failed", "error"); }
  }

  function buildAircraft(items) {
    const wrap = h("div", { class: "col" });
    if (!items.length) {
      wrap.appendChild(emptyState("No aircraft positions cached", "Refresh while online to pull public OpenSky positions.", "satellite"));
      return wrap;
    }
    const list = h("div", { class: "list" });
    for (const a of items.slice(0, 12)) {
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title", text: a.callsign || a.title || "?" }));
      main.appendChild(h("div", { class: "li-sub", text: `ALT ${a.alt_m != null ? Math.round(a.alt_m) + " m" : "—"} · SPD ${a.speed_kmh != null ? Math.round(a.speed_kmh) + " km/h" : "—"} · HDG ${a.heading_deg != null ? Math.round(a.heading_deg) + "\u00b0" : "—"}` }));
      const row = h("div", { class: "list-item" });
      row.appendChild(main);
      list.appendChild(row);
    }
    wrap.appendChild(list);
    return wrap;
  }

  function buildSats(items) {
    const wrap = h("div", { class: "col" });
    if (!items.length) {
      wrap.appendChild(emptyState("No satellite overhead passes cached", "Requires CelesTrak TLE refresh and network. Propagation is SGP4.", "satellite"));
      return wrap;
    }
    const list = h("div", { class: "list" });
    for (const s of items.slice(0, 8)) {
      const row = h("div", { class: "list-item" });
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title", text: `${s.name} (NORAD ${s.norad})` }));
      main.appendChild(h("div", { class: "li-sub", text: `ALT ${Math.round(s.alt_km)} km · ELEV ${Math.round(s.elevation_deg)} deg over observer` }));
      row.appendChild(main);
      list.appendChild(row);
    }
    wrap.appendChild(list);
    return wrap;
  }

  function buildWar(items) {
    const wrap = h("div", { class: "col gap-14" });
    const live = conflictLive || [];
    const verified = conflictVerified || [];
    if (!items.length && !live.length && !verified.length) {
      wrap.appendChild(emptyState("No current conflict reporting cached", "Refresh intel feeds while online. An empty panel is not evidence that a conflict is quiet.", "globe"));
      return wrap;
    }

    function section(title, note, rows, kind) {
      const box = h("div", { class: "col gap-4" });
      box.appendChild(h("div", { class: "side-label", text: title }));
      box.appendChild(h("div", { class: "tiny muted", text: note }));
      const list = h("div", { class: "list" });
      for (const it of rows) {
        const ageH = Number(it.age_hours);
        const ageTone = !Number.isFinite(ageH) ? "amber" : ageH > 72 ? "amber" : "green";
        const row = h("div", { class: `list-item ${it.severity === "CRITICAL" ? "alert" : ""}` });
        const main = h("div", { class: "li-main" });
        main.appendChild(h("div", { class: "li-title", text: it.title || "Item" }));
        if (kind === "verified") {
          const deaths = Number(it.fatalities) || 0;
          main.appendChild(h("div", { class: "li-sub", text: `${it.country || it.place || "location unstated"} · ${fmt.date(it.time, false)} · ${deaths ? `${deaths} reported killed` : "no confirmed deaths recorded"} · ${it.event_type || "armed violence"}` }));
          if (it.source_headline) main.appendChild(h("div", { class: "li-sub", text: it.source_headline }));
        } else if (kind === "live") {
          main.appendChild(h("div", { class: "li-sub", text: `${it.source || "GDELT"} · ${it.place || "location approximate"} · ${it.precision || "reported location"}${it.mentions ? ` · ${it.mentions} mentions` : ""}` }));
        } else {
          main.appendChild(h("div", { class: "li-sub", text: `${it.source || ""}${it.age_hours != null ? ` · ${fmt.ago(it.time)}` : " · age unknown"}` }));
        }
        row.appendChild(main);
        const tools = h("div", { class: "row gap-6 mt-6 wrap" });
        tools.appendChild(badge(it.age_hours != null ? fmt.ago(it.time) : "AGE UNKNOWN", ageTone));
        if (kind === "verified") tools.appendChild(badge("UCDP VERIFIED", "cyan"));
        if (kind === "live") tools.appendChild(badge("LIVE MEDIA", "amber"));
        if (it.severity === "CRITICAL") tools.appendChild(severityBadge("CRITICAL"));
        if (it.url) tools.appendChild(h("a", { class: "badge cyan", href: it.url, target: "_blank", rel: "noopener noreferrer", text: "SOURCE" }));
        row.appendChild(tools);
        list.appendChild(row);
      }
      box.appendChild(list);
      return box;
    }

    if (live.length) {
      wrap.appendChild(section("LIVE MEDIA REPORTS // GDELT", "Automated geocoded coding of current news. Locations and actor/event labels can be wrong.", live.slice(0, 10), "live"));
    }
    if (verified.length) {
      wrap.appendChild(section("VERIFIED EVENTS // UCDP CANDIDATE", "Monthly verified release with a lag of weeks, not a live warning.", verified.slice(0, 10), "verified"));
    }
    if (items.length) {
      wrap.appendChild(section("CURRENT CONFLICT HEADLINES", "Wire and humanitarian reporting from the last 7 days. Confirm with the linked source.", items.slice(0, 12), "headline"));
    }
    wrap.appendChild(h("div", { class: "tiny dim", text: "UCDP is a monthly verified dataset; GDELT is automated media coding. Neither is a confirmed targeting picture. Use the linked source and official warnings before acting." }));
    return wrap;
  }

  function buildNEO(items) {
    const wrap = h("div", { class: "col" });
    if (!items.length) {
      wrap.appendChild(emptyState("No near-earth objects detected", "NASA NEO data refreshes automatically.", "globe"));
      return wrap;
    }
    const list = h("div", { class: "list" });
    for (const obj of items.slice(0, 10)) {
      const isHazardous = obj.is_hazardous;
      const row = h("div", { class: `list-item ${isHazardous ? "warn" : ""}` });
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title", text: obj.name || "Unknown Object" }));
      main.appendChild(h("div", { class: "li-sub", text: `${obj.diameter_meters.toFixed(0)}m · ${obj.miss_distance_km.toLocaleString()} km (${obj.miss_distance_ld.toFixed(2)} LD) · ${obj.velocity_kmh.toLocaleString()} km/h` }));
      main.appendChild(h("div", { class: "li-sub", text: `${obj.threat_level} threat (${obj.threat_score}/100)${isHazardous ? " · HAZARDOUS" : ""}` }));
      row.appendChild(main);
      const tools = h("div", { class: "row gap-6 mt-6" });
      if (obj.url) tools.appendChild(h("a", { class: "badge cyan", href: obj.url, target: "_blank", rel: "noopener noreferrer", text: "NASA JPL" }));
      row.appendChild(tools);
      list.appendChild(row);
    }
    wrap.appendChild(list);
    return wrap;
  }

  function buildNews(items) {
    const wrap = h("div", { class: "col" });
    if (!items.length) {
      wrap.appendChild(emptyState("No news articles available", "NewsAPI requires an API key. Configure in settings.", "globe"));
      return wrap;
    }
    const list = h("div", { class: "list" });
    for (const article of items.slice(0, 10)) {
      const row = h("div", { class: `list-item ${article.severity === "CRITICAL" ? "alert" : ""}` });
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title", text: article.title || "Untitled" }));
      main.appendChild(h("div", { class: "li-sub", text: `${article.source || "Unknown"} · ${article.severity || "INFO"} · Relevance: ${article.relevance_score || 0}/100` }));
      if (article.description) {
        main.appendChild(h("div", { class: "li-sub", text: article.description.slice(0, 120) + (article.description.length > 120 ? "..." : "") }));
      }
      if (article.published_at) {
        main.appendChild(h("div", { class: "li-sub text-xs", text: `Published: ${new Date(article.published_at).toLocaleString()}` }));
      }
      row.appendChild(main);
      const tools = h("div", { class: "row gap-6 mt-6" });
      if (article.url) tools.appendChild(h("a", { class: "badge cyan", href: article.url, target: "_blank", rel: "noopener noreferrer", text: "READ" }));
      row.appendChild(tools);
      list.appendChild(row);
    }
    wrap.appendChild(list);
    return wrap;
  }

  function buildDisease(stats) {
    const wrap = h("div", { class: "col" });
    if (!stats || Object.keys(stats).length === 0) {
      wrap.appendChild(emptyState("No disease tracking data available", "Disease data refreshes automatically.", "globe"));
      return wrap;
    }
    
    const content = h("div", { class: "col gap-6" });
    
    // Header with severity badge
    const header = h("div", { class: "row gap-6 items-center" });
    header.appendChild(h("div", { class: "li-title", text: stats.disease || "Unknown Disease" }));
    header.appendChild(severityBadge(stats.severity || "INFO"));
    content.appendChild(header);
    
    // Key metrics
    const metrics = h("div", { class: "grid grid-2 gap-4 mt-4" });
    
    metrics.appendChild(h("div", { class: "stat-box" },
      h("div", { class: "stat-label", text: "Total Cases" }),
      h("div", { class: "stat-value", text: (stats.cases || 0).toLocaleString() })
    ));
    
    metrics.appendChild(h("div", { class: "stat-box" },
      h("div", { class: "stat-label", text: "Active Cases" }),
      h("div", { class: "stat-value", text: (stats.active || 0).toLocaleString() })
    ));
    
    metrics.appendChild(h("div", { class: "stat-box" },
      h("div", { class: "stat-label", text: "Deaths" }),
      h("div", { class: "stat-value text-red", text: (stats.deaths || 0).toLocaleString() })
    ));
    
    metrics.appendChild(h("div", { class: "stat-box" },
      h("div", { class: "stat-label", text: "Critical" }),
      h("div", { class: "stat-value text-orange", text: (stats.critical || 0).toLocaleString() })
    ));
    
    metrics.appendChild(h("div", { class: "stat-box" },
      h("div", { class: "stat-label", text: "Recovered" }),
      h("div", { class: "stat-value text-green", text: (stats.recovered || 0).toLocaleString() })
    ));
    
    metrics.appendChild(h("div", { class: "stat-box" },
      h("div", { class: "stat-label", text: "Tests" }),
      h("div", { class: "stat-value", text: (stats.tests || 0).toLocaleString() })
    ));
    
    content.appendChild(metrics);
    
    // Additional metrics
    if (stats.cases_per_million || stats.deaths_per_million) {
      const additional = h("div", { class: "col gap-2 mt-4" });
      additional.appendChild(h("div", { class: "li-sub", text: `Cases per million: ${stats.cases_per_million || 0}` }));
      additional.appendChild(h("div", { class: "li-sub", text: `Deaths per million: ${stats.deaths_per_million || 0}` }));
      additional.appendChild(h("div", { class: "li-sub", text: `Relevance score: ${stats.relevance_score || 0}/100` }));
      content.appendChild(additional);
    }
    
    // Last updated
    if (stats.updated) {
      content.appendChild(h("div", { class: "li-sub text-xs mt-4", text: `Last updated: ${new Date(stats.updated).toLocaleString()}` }));
    }
    
    // Source link
    const tools = h("div", { class: "row gap-6 mt-6" });
    tools.appendChild(h("a", { class: "badge cyan", href: "https://disease.sh/", target: "_blank", rel: "noopener noreferrer", text: "SOURCE: Disease.sh" }));
    content.appendChild(tools);
    
    wrap.appendChild(content);
    return wrap;
  }

  function distFromOperator(ev) {
    const s = store.settings || {};
    const la1 = Number(s.lat), lo1 = Number(s.lon);
    const la2 = Number(ev.lat), lo2 = Number(ev.lon);
    if (![la1, lo1, la2, lo2].every(Number.isFinite)) return null;
    const R = 6371;
    const dLat = (la2 - la1) * Math.PI / 180;
    const dLon = (lo2 - lo1) * Math.PI / 180;
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(la1 * Math.PI / 180) * Math.cos(la2 * Math.PI / 180) * Math.sin(dLon / 2) ** 2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  function buildEvents(items) {
    const wrap = h("div", { class: "col" });
    if (!items.length) {
      wrap.appendChild(emptyState("No live events cached with coordinates", "Refresh feeds while online. Operator-entered events live in MISSION LOG.", "globe"));
      return wrap;
    }
    const list = h("div", { class: "list" });
    for (const ev of items.slice(0, 20)) {
      const isCrit = ["CRITICAL", "SEVERE", "EXTREME"].includes(String(ev.severity || "INFO").toUpperCase());
      const dist = distFromOperator(ev);
      const row = h("div", { class: `list-item ${isCrit ? "alert" : ""}` });
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title", text: ev.title || "Event" }));
      main.appendChild(h("div", { class: "li-sub", text: `${ev.source || ""} · ${ev.severity || ""}${ev.mag != null ? ` · M${ev.mag}` : ""}${ev.place ? ` · ${ev.place}` : ""}${ev.depth_km != null ? ` · ${ev.depth_km} km depth` : ""}${ev.time ? ` · ${fmt.date(ev.time)}` : ""}${dist != null ? ` · ${Math.round(dist)} km from operator` : ""}` }));
      row.appendChild(main);
      const tools = h("div", { class: "row gap-6 mt-6" });
      if (ev.lat != null && ev.lon != null) tools.appendChild(btn("LOG", { size: "sm", tone: "ghost", icon: "plus", onClick: () => logIncident(ev) }));
      if (ev.url) tools.appendChild(h("a", { class: "badge cyan", href: ev.url, target: "_blank", rel: "noopener noreferrer", text: "SOURCE" }));
      row.appendChild(tools);
      list.appendChild(row);
    }
    wrap.appendChild(list);
    return wrap;
  }

  async function logIncident(ev) {
    try {
      await api.post("/api/records/incidents", {
        title: String(ev.title || "Global intel event").slice(0, 200),
        severity: String(ev.severity || "INFO").toUpperCase(),
        category: "GLOBAL",
        detail: `Live intel: ${ev.source || "unknown"}${ev.mag != null ? ` M${ev.mag}` : ""} · ${ev.summary || ""}`.slice(0, 1000),
        lat: ev.lat, lon: ev.lon, resolved: 0,
      });
      toast("Incident logged to mission log");
      render(root);
    } catch (err) { toast(err.message || "Could not log incident", "error"); }
  }

  function newsCard(f) {
    const body = h("div", { class: "col gap-6" });
    body.appendChild(h("div", { class: "tiny muted", text: `${f.kind.toUpperCase()} · fetched ${fmt.ago(f.fetched_at)} · ${f.count} cached item(s)${f.error ? ` · ${f.error}` : ""}` }));
    const items = f.items || [];
    for (const it of items.slice(0, 3)) {
      body.appendChild(h("div", { class: "small", text: it.title || "Feed item" }));
    }
    const card = h("div", { class: "panel", style: { padding: 0 } });
    const head = h("div", { class: "panel-head" });
    head.appendChild(h("div", { class: "row gap-6" }));
    head.firstChild.appendChild(h("div", { class: "dev-name", text: f.name }));
    head.appendChild(feedBadge(f.status));
    const tools = h("div", { class: "panel-tools row gap-4" });
    if (f.url) tools.appendChild(h("a", { class: "btn btn-sm btn-ghost", href: f.url, target: "_blank", rel: "noopener noreferrer", text: "SOURCE" }));
    tools.appendChild(btn("REFRESH", { size: "sm", tone: "ghost", onClick: () => refreshFeed(f) }));
    head.appendChild(tools);
    card.appendChild(head);
    card.appendChild(h("div", { class: "panel-body flush" }, body));
    return card;
  }

  function refreshFeed(f) {
    api.post("/api/feeds/refresh", { id: f.id }).then((out) => {
      const result = (out.results || [])[0] || {};
      if (result.status === "OK") toast(`${f.name}: ${result.count ?? 0} items`);
      else toast(`${f.name}: ${result.error || result.status || "failed"}`, "error");
      render(root);
    }).catch(() => {});
  }

  autoTimer = setInterval(async () => {
    if (autoBusy || document.hidden) return;
    if (!location.hash.startsWith("#/intel")) return;
    if (document.querySelector(".modal.open, .modal")) return;
    autoBusy = true;
    const y = window.scrollY;
    try {
      await render(root);
      window.scrollTo(0, y);
    } catch {}
    finally { autoBusy = false; }
  }, AUTO_MS);

  return () => {
    if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
  };
}
