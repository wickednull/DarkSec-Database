import { api } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, stat, gauge, badge, btn, pageHead, alertStrip, emptyState, loading, fmt, toast, tabs, sectionTitle,
} from "../components.js";
import { feedBadge, severityBadge, positionConfidence } from "./shared.js";
import * as U from "../units.js";

const AUTO_MS = 90_000;
let autoTimer = null;
let autoBusy = false;
let activeTab = "overview";

const TAB_ITEMS = [
  { id: "overview", label: "OVERVIEW" },
  { id: "conflict", label: "CONFLICT" },
  { id: "cyber", label: "CYBER & NEWS" },
  { id: "events", label: "EVENTS & HAZARDS" },
  { id: "sensors", label: "AIR & SPACE" },
];

export async function render(root, params = {}) {
  if (params.tab && TAB_ITEMS.some((t) => t.id === params.tab)) activeTab = params.tab;
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
  ).filter((i) => {
    const _s = store.settings || {};
    const _lat = Number(_s.lat), _lon = Number(_s.lon);
    if (!Number.isFinite(_lat) || !Number.isFinite(_lon) || i.lat == null || i.lon == null) return true;
    const R = 6371;
    const toRad = (x) => x * Math.PI / 180;
    const dlat = toRad(i.lat - _lat), dlon = toRad(i.lon - _lon);
    const a = Math.sin(dlat / 2) ** 2 + Math.cos(toRad(_lat)) * Math.cos(toRad(i.lat)) * Math.sin(dlon / 2) ** 2;
    const d = 2 * R * Math.asin(Math.sqrt(a));
    return d <= 2000;
  });
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
  const feeds = data.feeds || [];
  const threatBand = String(threat.band || "NO DATA");
  let pizzint = null;
  try { pizzint = await api.get("/api/pizzint"); } catch (err) { pizzint = { error: err.message }; }
  const positionInfo = positionConfidence(store.settings || {});
  const observer = fmt.coord(store.settings?.lat, store.settings?.lon, 4);
  const feedById = Object.fromEntries(feeds.map((f) => [f.id, f]));

  root.appendChild(pageHead({
    kicker: "OPERATIONS // GLOBAL INTELLIGENCE",
    title: "Global Threat & Live Intel",
    desc: "Cached public-source intelligence organized by mission area. Live conflict media (GDELT), verified conflict (UCDP), cybersecurity news (The Hacker News), world news, hazards, aircraft, satellites and space weather are age-labeled and readable offline. Cached data is never presented as a live warning.",
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
  root.appendChild(h("div", { class: "row gap-8 wrap mt-8 items-center" },
    badge(syncLabel, syncTone),
    h("span", { class: "tiny muted", text: `Last successful refresh ${fmt.ago(sync.last_success)} · ${sync.feed_ok ?? "?"}/${sync.feed_total ?? "?"} feeds cached · view auto-refreshes every 90 s` })));

  if (criticalList.length || threatBand === "SEVERE" || threatBand === "ELEVATED") {
    root.appendChild(alertStrip(
      `${criticalList.length} critical cached intel item(s) · local threat index ${threat.score ?? 0}/100 (${threatBand}). Confirm with official sources before acting.`,
      criticalList.length ? "red" : "amber",
    ));
  }
  if (activeTab === "conflict" || activeTab === "overview") {
    if (!warSummary.newest_age_hours || warSummary.newest_age_hours > 6) {
      root.appendChild(alertStrip(
        warSummary.newest_age_hours
          ? `Newest conflict report is ${fmt.ago(warSummary.newest_time)} old. Treat the conflict picture as cached history, not a live warning; refresh feeds before relying on it.`
          : "No dated conflict reports are cached. Refresh while online; do not treat an empty panel as a quiet picture.",
        "amber",
      ));
    }
  }

  const tabBar = tabs(TAB_ITEMS, activeTab, (id) => {
    activeTab = id;
    try { history.replaceState(null, "", `#/intel?tab=${id}`); } catch {}
    render(root);
  });

  // Pizza intelligence (PizzINT) panel
  if (pizzint) {
    const locs = pizzint.locations || [];
    const doughcon = pizzint.doughcon != null ? `DOUGHCON ${pizzint.doughcon}` : "DOUGHCON ?";
    const status = pizzint.status || (pizzint.error ? "UNAVAILABLE" : "");
    const body = h("div", { class: "col gap-6" });
    body.appendChild(h("div", { class: "row gap-8 items-center" },
      badge(doughcon, pizzint.doughcon >= 4 ? "green" : pizzint.doughcon === 3 ? "amber" : "red"),
      h("span", { class: "small", text: status }),
      pizzint.error ? h("span", { class: "tiny muted", text: `(${pizzint.error})` }) : null));
    if (locs.length) {
      const list = h("div", { class: "row wrap gap-6" });
      for (const l of locs) {
        list.appendChild(h("span", { class: "badge muted", text: `${l.name}: ${l.status}${l.distance ? " · " + l.distance : ""}` }));
      }
      body.appendChild(list);
    }
    body.appendChild(h("div", { class: "tiny muted", text: "Advisory OSINT only. Pizza activity is not a verified military indicator. Source: pizzint.watch" }));
    root.appendChild(panel({
      title: "Pizza Intelligence (PizzINT)", icon: "flame",
      sub: "Open-source readiness signal from public pizza activity near sensitive sites",
      tools: [btn("REFRESH", { size: "sm", icon: "refresh", onClick: async () => {
        try { pizzint = await api.post("/api/pizzint/refresh"); render(root); }
        catch (err) { toast(err.message || "PizzINT refresh failed", "error"); }
      } })],
      body,
    }));
  }

  root.appendChild(h("div", { class: "mt-14" }, tabBar));

  const body = h("div", { class: "mt-14" });
  root.appendChild(body);

  if (activeTab === "overview") renderOverview(body);
  else if (activeTab === "conflict") renderConflict(body);
  else if (activeTab === "cyber") renderCyber(body);
  else if (activeTab === "events") renderEvents(body);
  else if (activeTab === "sensors") renderSensors(body);

  root.appendChild(h("div", { class: "tiny dim mt-14", text: "INTEL SOURCES: The Hacker News (cybersecurity RSS), GDELT 2.0 (automated media coding), UCDP Candidate Events (monthly verified), OpenSky Network, CelesTrak (TLE/SGP4), NASA NEO/EONET, USGS, BBC/Al Jazeera/Guardian/France24/DW, International Crisis Group, Open-Meteo, NOAA SWPC, NWS, ReliefWeb, GDACS, WHO, Safecast. Every item shows its age; cached snapshots are never live warnings." }));

  return () => {};

  function renderOverview(host) {
    const stats = h("div", { class: "grid grid-4" });
    stats.appendChild(stat("THREAT INDEX", String(threat.score ?? 0), threatBand, (threatBand === "SEVERE" || threatBand === "ELEVATED") ? "acc" : ""));
    stats.appendChild(stat("CRITICAL CACHED", String(criticalList.length).padStart(3, "0"), "across all feeds"));
    stats.appendChild(stat("CONFLICT EVENTS", String(conflictLive.length + conflictVerified.length).padStart(3, "0"), `${conflictVerified.length} verified · ${conflictLive.length} live media`));
    stats.appendChild(stat("CYBER HEADLINES", String((feedById.thehackernews?.items || []).length).padStart(3, "0"), "The Hacker News cached"));
    stats.appendChild(stat("WORLD EVENTS", String(events.length).padStart(3, "0"), "geocoded cached events"));
    stats.appendChild(stat("AIRCRAFT", String(aircraft.length).padStart(3, "0"), "OpenSky public positions"));
    stats.appendChild(stat("SATELLITES", String(satellites.length).padStart(3, "0"), "over observer (SGP4)"));
    if (neoSummary) stats.appendChild(stat("NEO OBJECTS", String(neoSummary.total || 0), `${neoSummary.hazardous || 0} hazardous`, neoSummary.threat_level === "HIGH" || neoSummary.threat_level === "CRITICAL" ? "acc" : ""));
    host.appendChild(stats);

    const grid = h("div", { class: "panel-grid grid-2 mt-14" });
    host.appendChild(grid);

    grid.appendChild(panel({
      title: "Global Threat Index",
      sub: "Local aggregation over cached live events",
      icon: "warning",
      cls: "span-2",
      body: h("div", { class: "col gap-10" },
        h("div", { class: "row gap-14 wrap" },
          gauge(threat.score ?? 0, threatBand),
          h("div", { class: "col gap-4" },
            badge("METHOD", "LOCAL_WEIGHTED"),
            h("div", { class: "kv compact" },
              ...[
                ["CRITICAL ITEMS", String(criticalList.length)],
                ["BAND", threatBand],
                ["TOP SOURCE", Object.entries(threat.by_source || {}).sort((a, b) => b[1] - a[1])[0]?.[0] || "—"],
                ["EVENTS NEAR OPERATOR", String(threat.near_count ?? 0)],
              ].flatMap(([k, v]) => [h("span", { class: "k", text: k }), h("span", { class: "v", text: v })]),
            ),
          ),
        ),
        h("div", { class: "tiny muted", text: `${threat.events_scoped ? `Location-weighted from ${observer} (${positionInfo.mapLabel})` : "Global cached view (set a valid position for local weighting)"}: severity, distance, recency and source credibility are applied. ${threat.near_count ?? 0} weighted event(s) within the near-operator window. Cached data only — verify before acting.` }),
        h("div", { class: "tiny muted", text: sun ? `SOLAR STATE ${Number(sun?.sun?.altitude_deg) > 0 ? "DAY" : "NIGHT"} · SUN ALT ${sun?.sun?.altitude_deg ?? "—"}° · NEXT RISE ${fmt.date(sun?.sun?.next_rise)} · NEXT SET ${fmt.date(sun?.sun?.next_set)}` : "SOLAR STATE UNKNOWN" }),
      ),
    }));

    const thn = feedById.thehackernews;
    if (thn) {
      grid.appendChild(panel({
        title: "Cybersecurity (The Hacker News)",
        sub: `${thn.count ?? 0} cached item(s) · ${feedBadge(thn.status).textContent || thn.status} · fetched ${fmt.ago(thn.fetched_at)}`,
        icon: "book",
        body: buildFeedItemList(thn, 5),
        tools: [btn("REFRESH", { size: "sm", tone: "ghost", onClick: () => refreshFeed(thn) })],
      }));
    }

    grid.appendChild(panel({
      title: "Conflict Snapshot",
      sub: `${warSummary.live ?? conflictLive.length} live media · ${warSummary.verified ?? conflictVerified.length} verified · newest ${warSummary.newest_time ? fmt.ago(warSummary.newest_time) : "unavailable"}`,
      icon: "alert",
      body: buildConflictSnapshot(),
      tools: [btn("OPEN TAB", { size: "sm", tone: "ghost", onClick: () => { activeTab = "conflict"; render(root); } })],
    }));
  }

  function buildConflictSnapshot() {
    const rows = [...(conflictLive || []).slice(0, 2).map((x) => ({ ...x, _kind: "live" })),
      ...(conflictVerified || []).slice(0, 2).map((x) => ({ ...x, _kind: "verified" })),
      ...(war || []).slice(0, 2).map((x) => ({ ...x, _kind: "headline" }))];
    if (!rows.length) return emptyState("No conflict reporting cached", "Refresh intel feeds while online.", "globe");
    const list = h("div", { class: "list" });
    for (const it of rows) {
      const row = h("div", { class: `list-item ${it.severity === "CRITICAL" ? "alert" : ""}` });
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title", text: it.title || "Item" }));
      main.appendChild(h("div", { class: "li-sub", text: `${it._kind === "verified" ? "UCDP" : it._kind === "live" ? "GDELT" : (it.source || "headline")} · ${fmt.ago(it.time)}` }));
      row.appendChild(main);
      if (it.url) row.appendChild(h("a", { class: "badge cyan", href: it.url, target: "_blank", rel: "noopener noreferrer", text: "SOURCE" }));
      list.appendChild(row);
    }
    return list;
  }

  function renderConflict(host) {
    host.appendChild(sectionTitle("Conflict & War Intelligence", "GDELT live media reports, UCDP verified events, and filtered wire headlines"));
    const grid = h("div", { class: "panel-grid grid-1 mt-10" });
    host.appendChild(grid);
    grid.appendChild(panel({
      title: "Conflict Reporting",
      sub: `${warSummary.live ?? conflictLive.length} live media · ${warSummary.verified ?? conflictVerified.length} verified · ${warSummary.headlines ?? war.length} headlines · newest ${warSummary.newest_time ? fmt.ago(warSummary.newest_time) : "unavailable"}`,
      icon: "alert",
      body: buildWar(war),
    }));
  }

  function renderCyber(host) {
    host.appendChild(sectionTitle("Cybersecurity & News", "The Hacker News RSS, world/news feeds, and NewsAPI stories"));
    const grid = h("div", { class: "panel-grid grid-2 mt-10" });
    host.appendChild(grid);

    const thn = feedById.thehackernews;
    grid.appendChild(panel({
      title: "The Hacker News",
      sub: thn ? `Cybersecurity RSS · ${thn.count ?? 0} cached · fetched ${fmt.ago(thn.fetched_at)} · ${thn.status || "?"}` : "Feed not yet cached",
      icon: "book",
      cls: "span-2",
      tools: thn ? [btn("REFRESH", { size: "sm", tone: "ghost", onClick: () => refreshFeed(thn) })] : [],
      body: thn ? buildFeedItemList(thn, 12, { showSummary: true }) : emptyState("The Hacker News feed is not cached yet", "Refresh intel while online to ingest https://feeds.feedburner.com/TheHackersNews.", "book"),
    }));

    if (conflictNews.length > 0) {
      grid.appendChild(panel({
        title: "Conflict News (NewsAPI)",
        sub: `${conflictNews.length} real-time conflict stories`,
        icon: "alert",
        body: buildNews(conflictNews),
      }));
    }
    if (disasterNews.length > 0) {
      grid.appendChild(panel({
        title: "Disaster News (NewsAPI)",
        sub: `${disasterNews.length} real-time disaster stories`,
        icon: "alert",
        body: buildNews(disasterNews),
      }));
    }

    const warFeedIds = ["bbc_middle_east", "twz_defense", "breaking_defense", "defense_one", "long_war_journal", "war_on_the_rocks"];
    const warFeeds = warFeedIds.map((id) => feedById[id]).filter(Boolean);
    if (warFeeds.length) {
      grid.appendChild(panel({
        title: "Defense & War Reporting Feeds",
        sub: "Specialized conflict/defense sources (cached). Headlines are filtered for conflict relevance in the Conflict tab.",
        icon: "alert",
        cls: "span-2",
        body: h("div", { class: "grid grid-2 gap-10" }, ...warFeeds.map((f) => newsCard(f, 4))),
      }));
    }

    const newsFeedIds = ["thehackernews", "bbc_world", "guardian_world", "aljazeera_world", "france24_world", "dw_news", "reliefweb", "crisisgroup", "gdacs", "rsoe_edis", "nws_alerts", "usgs_quakes"];
    const newsFeeds = newsFeedIds.map((id) => feedById[id]).filter(Boolean);
    grid.appendChild(panel({
      title: "News & Public Safety Feeds",
      sub: "Cached official and wire sources, grouped separately from tactical overlays",
      icon: "globe",
      cls: "span-2",
      body: h("div", { class: "grid grid-2 gap-10" }, ...newsFeeds.map((f) => newsCard(f, 3))),
    }));
  }

  function renderEvents(host) {
    host.appendChild(sectionTitle("Global Events & Hazards", "Geocoded cached events, near-earth objects, and disease tracking"));
    const grid = h("div", { class: "panel-grid grid-2 mt-10" });
    host.appendChild(grid);

    grid.appendChild(panel({
      title: "Global Live Events",
      sub: `${events.length} cached event(s) with coordinates`,
      icon: "globe",
      cls: "span-2",
      body: buildEvents(events),
    }));

    const rsoe = feedById.rsoe_edis;
    if (rsoe) {
      grid.appendChild(panel({
        title: "RSOE EDIS Disaster Events",
        sub: `Global hazard monitor · ${rsoe.count ?? 0} cached · fetched ${fmt.ago(rsoe.fetched_at)} · ${rsoe.status || "?"}`,
        icon: "alert",
        cls: "span-2",
        tools: [btn("REFRESH", { size: "sm", tone: "ghost", onClick: () => refreshFeed(rsoe) })],
        body: rsoe.count
          ? buildFeedItemList(rsoe, 12, { showSummary: true })
          : emptyState("RSOE EDIS not cached yet", "Refresh intel while online to ingest https://rsoe-edis.org/eventList.", "alert"),
      }));
    }

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
  }

  function renderSensors(host) {
    host.appendChild(sectionTitle("Air, Space & Environment", "Public aircraft positions, satellite passes, and air quality"));
    const grid = h("div", { class: "panel-grid grid-2 mt-10" });
    host.appendChild(grid);

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
      h("div", { class: "small muted", text: "Air quality layer refreshes while online; cached snapshot remains readable offline." }),
    ) : h("div", { class: "small muted", text: "No air quality snapshot cached. Refresh while online." });
    grid.appendChild(panel({
      title: "Air Quality (Open-Meteo)",
      sub: "US AQI snapshot",
      icon: "wind",
      cls: "span-2",
      body: airBody,
    }));
  }

  async function refreshIntel() {
    toast("Refreshing public intel feeds", "warn");
    try {
      const out = await api.post("/api/intel/refresh", {});
      const ok = (out.results || []).filter((r) => r.status === "OK").length;
      toast(`Intel refresh: ${ok}/${(out.results || []).length} source(s) OK`);
      render(root);
    } catch (err) { toast(err.message || "Intel refresh failed", "error"); }
  }

  function buildFeedItemList(f, limit = 8, opts = {}) {
    const wrap = h("div", { class: "col gap-6" });
    const items = f.items || [];
    if (!items.length) {
      wrap.appendChild(emptyState("No cached items", f.error ? String(f.error) : "Refresh while online to fetch items.", "book"));
      return wrap;
    }
    const list = h("div", { class: "list" });
    for (const it of items.slice(0, limit)) {
      const row = h("div", { class: "list-item" });
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title", text: it.title || "Feed item" }));
      const meta = [f.name, fmt.ago(it.time)].filter(Boolean).join(" · ");
      main.appendChild(h("div", { class: "li-sub", text: meta }));
      if (opts.showSummary && it.summary) {
        main.appendChild(h("div", { class: "li-sub", text: String(it.summary).slice(0, 180) + (String(it.summary).length > 180 ? "…" : "") }));
      }
      row.appendChild(main);
      const tools = h("div", { class: "row gap-6 mt-6 wrap" });
      if (it.time) tools.appendChild(badge(fmt.ago(it.time), "muted"));
      if (it.url) tools.appendChild(h("a", { class: "badge cyan", href: it.url, target: "_blank", rel: "noopener noreferrer", text: "READ" }));
      row.appendChild(tools);
      list.appendChild(row);
    }
    wrap.appendChild(list);
    return wrap;
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
      main.appendChild(h("div", { class: "li-sub", text: `ALT ${U.altM(a.alt_m)} · SPD ${U.speedKmh(a.speed_kmh)} · HDG ${a.heading_deg != null ? Math.round(a.heading_deg) + "\u00b0" : "—"}` }));
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
      main.appendChild(h("div", { class: "li-sub", text: `ALT ${U.altKm(s.alt_km)} · ELEV ${Math.round(s.elevation_deg)} deg over observer` }));
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
      wrap.appendChild(section("LIVE MEDIA REPORTS // GDELT", "Automated geocoded coding of current news. Locations and actor/event labels can be wrong.", live.slice(0, 12), "live"));
    }
    if (verified.length) {
      wrap.appendChild(section("VERIFIED EVENTS // UCDP CANDIDATE", "Monthly verified release with a lag of weeks, not a live warning.", verified.slice(0, 12), "verified"));
    }
    if (items.length) {
      wrap.appendChild(section("CURRENT CONFLICT HEADLINES", "Wire and humanitarian reporting from the last 7 days. Confirm with the linked source.", items.slice(0, 15), "headline"));
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
      main.appendChild(h("div", { class: "li-sub", text: `${U.lengthM(obj.diameter_meters)} · ${U.distKm(obj.miss_distance_km)} (${obj.miss_distance_ld.toFixed(2)} LD) · ${U.speedKmh(obj.velocity_kmh)}` }));
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
        main.appendChild(h("div", { class: "li-sub", text: article.description.slice(0, 140) + (article.description.length > 140 ? "…" : "") }));
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
    const header = h("div", { class: "row gap-6 items-center" });
    header.appendChild(h("div", { class: "li-title", text: stats.disease || "Unknown Disease" }));
    header.appendChild(severityBadge(stats.severity || "INFO"));
    content.appendChild(header);
    const metrics = h("div", { class: "grid grid-2 gap-4 mt-4" });
    for (const [label, value, cls] of [
      ["Total Cases", (stats.cases || 0).toLocaleString(), ""],
      ["Active Cases", (stats.active || 0).toLocaleString(), ""],
      ["Deaths", (stats.deaths || 0).toLocaleString(), "text-red"],
      ["Critical", (stats.critical || 0).toLocaleString(), "text-orange"],
      ["Recovered", (stats.recovered || 0).toLocaleString(), "text-green"],
      ["Tests", (stats.tests || 0).toLocaleString(), ""],
    ]) {
      metrics.appendChild(h("div", { class: "stat-box" },
        h("div", { class: "stat-label", text: label }),
        h("div", { class: `stat-value ${cls}`.trim(), text: value })));
    }
    content.appendChild(metrics);
    if (stats.updated) content.appendChild(h("div", { class: "li-sub text-xs mt-4", text: `Last updated: ${new Date(stats.updated).toLocaleString()}` }));
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
    for (const ev of items.slice(0, 25)) {
      const isCrit = ["CRITICAL", "SEVERE", "EXTREME"].includes(String(ev.severity || "INFO").toUpperCase());
      const dist = distFromOperator(ev);
      const row = h("div", { class: `list-item ${isCrit ? "alert" : ""}` });
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title", text: ev.title || "Event" }));
      main.appendChild(h("div", { class: "li-sub", text: `${ev.source || ""} · ${ev.severity || ""}${ev.mag != null ? ` · M${ev.mag}` : ""}${ev.place ? ` · ${ev.place}` : ""}${ev.depth_km != null ? ` · ${U.distKm(ev.depth_km)} depth` : ""}${ev.time ? ` · ${fmt.date(ev.time)}` : ""}${dist != null ? ` · ${U.distKm(dist)} from operator` : ""}` }));
      row.appendChild(main);
      const tools = h("div", { class: "row gap-6 mt-6 wrap" });
      tools.appendChild(badge(fmt.ago(ev.time), "muted"));
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

  function newsCard(f, limit = 3) {
    const body = h("div", { class: "col gap-6" });
    body.appendChild(h("div", { class: "tiny muted", text: `${String(f.kind || "").toUpperCase()} · fetched ${fmt.ago(f.fetched_at)} · ${f.count ?? 0} cached${f.error ? ` · ${f.error}` : ""}` }));
    const items = f.items || [];
    for (const it of items.slice(0, limit)) {
      const line = h("div", { class: "small" });
      if (it.url) {
        line.appendChild(h("a", { href: it.url, target: "_blank", rel: "noopener noreferrer", text: it.title || "Feed item", style: { color: "inherit", textDecoration: "none" } }));
      } else {
        line.textContent = it.title || "Feed item";
      }
      body.appendChild(line);
    }
    if (!items.length) body.appendChild(h("div", { class: "small muted", text: "No items cached yet." }));
    const card = h("div", { class: "panel", style: { padding: 0 } });
    const head = h("div", { class: "panel-head" });
    head.appendChild(h("div", { class: "dev-name", text: f.name }));
    head.appendChild(feedBadge(f.status));
    const tools = h("div", { class: "panel-tools row gap-4" });
    if (f.url) tools.appendChild(h("a", { class: "btn btn-sm btn-ghost", href: f.url, target: "_blank", rel: "noopener noreferrer", text: "SOURCE" }));
    tools.appendChild(btn("REFRESH", { size: "sm", tone: "ghost", onClick: () => refreshFeed(f) }));
    head.appendChild(tools);
    card.appendChild(head);
    card.appendChild(h("div", { class: "panel-body" }, body));
    return card;
  }

  function refreshFeed(f) {
    api.post("/api/feeds/refresh", { id: f.id }).then((out) => {
      const result = (out.results || [])[0] || {};
      if (result.status === "OK") toast(`${f.name}: ${result.count ?? 0} items`);
      else toast(`${f.name}: ${result.error || result.status || "failed"}`, "error");
      render(root);
    }).catch((err) => toast(err.message || "Refresh failed", "error"));
  }

  autoTimer = setInterval(async () => {
    if (autoBusy || document.hidden) return;
    if (!location.hash.startsWith("#/intel")) return;
    if (document.querySelector(".modal.open, .modal-backdrop")) return;
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
