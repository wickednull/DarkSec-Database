/* Offline map workspace.
   Vector basemap is bundled Natural Earth data; raster tiles come only from
   local files or an operator-supplied MBTiles archive. No tile server is
   contacted. Everything else is waypoints and tracks in the local database. */
import { api, download } from "../api.js";
import * as mapGl from "./map-gl.js?v=map-upgrade-8";
import { store } from "../store.js";
import {
  h, panel, pageHead, btn, iconBtn, icon, badge, toast, modal, field, input, select,
  emptyState, loading, fmt, alertStrip, checkbox, confirmDialog,
} from "../components.js";
import * as U from "../units.js";
import { openRecordForm, deleteRecord } from "../records.js";
import { downloadText, readTextFile, positionConfidence } from "./shared.js";

const R = 6371008.8;
const TILE = 256;
const TYPE_COLOR = {
  WAYPOINT: "#5dff9a", CAMP: "#ffb02e", WATER: "#48d7ff", CACHE: "#c78bff",
  LZ: "#ffffff", HAZARD: "#ff5c5c", RV: "#5dff9a", OBSERVATION: "#ffd166", IMPORTED: "#7f9b8b",
};

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function wrapLon(lon) { return ((lon + 180) % 360 + 360) % 360 - 180; }
function project(lat, lon) {
  const phi = clamp(Number(lat) || 0, -85.05112878, 85.05112878) * Math.PI / 180;
  return { x: (lon + 180) / 360, y: (1 - Math.log(Math.tan(phi) + 1 / Math.cos(phi)) / Math.PI) / 2 };
}
function unproject(x, y) {
  const lon = x * 360 - 180;
  const n = Math.PI - 2 * Math.PI * y;
  return { lat: Math.atan(0.5 * (Math.exp(n) - Math.exp(-n))) * 180 / Math.PI, lon };
}

/* Local geodesic helpers keep measurement responsive while dragging. */
function distanceM(a, b) {
  const p1 = a.lat * Math.PI / 180, p2 = b.lat * Math.PI / 180;
  const dp = p2 - p1, dl = (b.lon - a.lon) * Math.PI / 180;
  const s = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.atan2(Math.sqrt(s), Math.sqrt(Math.max(0, 1 - s)));
}
function bearingDeg(a, b) {
  const p1 = a.lat * Math.PI / 180, p2 = b.lat * Math.PI / 180;
  const dl = (b.lon - a.lon) * Math.PI / 180;
  const y = Math.sin(dl) * Math.cos(p2);
  const x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

function niceDistance(m) {
  if (m <= 0) return { m: U.isImperial() ? 3.048 : 10, label: U.distM(10) };
  const units = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000,
    50000, 100000, 200000, 500000, 1000000, 2000000, 5000000];
  for (const u of units) if (u >= m) return { m: u, label: U.distM(u) };
  return { m: units[units.length - 1], label: U.distM(units[units.length - 1]) };
}

export async function render(root, params = {}) {
  const want = params.renderer || params.mode || localStorage.getItem("darksec.map.renderer") || "2d";
  if (want === "gl" || want === "3d") {
    try { localStorage.setItem("darksec.map.renderer", "gl"); } catch {}
    return mapGl.render(root, { ...params, renderer: "gl" });
  }
  try { localStorage.setItem("darksec.map.renderer", "2d"); } catch {}
  root.replaceChildren(loading("INITIALISING OFFLINE MAP"));
  let layers, waypoints, tracks, coordNow;
  const settings = store.settings || {};
  const configuredLat = Number(settings.lat), configuredLon = Number(settings.lon);
  const homeLat = Number.isFinite(configuredLat) ? configuredLat : 39.8283;
  const homeLon = Number.isFinite(configuredLon) ? configuredLon : -98.5795;
  const positionInfo = positionConfidence(settings);
  let intelFeatures = [];
  let intelAircraft = [];
  let intelSats = [];
  let intelMaritime = [];
  let intelCables = [];
  let intelCctv = [];
  let intelSummaryData = null;
  let sunInfo = null;
  let declInfo = null;
  let selectedIntel = null; // { kind: 'event'|'aircraft'|'sat'|'maritime'|'cable'|'cctv', data, feature? }
  let intelMaxAgeHours = 0; // 0 = all
  let livePreviewTimer = null;
  try {
    const [l, w, t, intel, cel, dec] = await Promise.all([
      api.get("/api/map/layers").catch(() => ({ vector: [], raster: [] })),
      api.get("/api/records/waypoints?limit=2000").catch(() => ({ items: [] })),
      api.get("/api/records/tracks?limit=500").catch(() => ({ items: [] })),
      api.get("/api/map/intel").catch(() => null),
      api.get(`/api/nav/celestial?lat=${homeLat}&lon=${homeLon}&elevation=${Number(settings.elevation_m) || 0}`).catch(() => null),
      api.get(`/api/nav/declination?lat=${homeLat}&lon=${homeLon}&date=${new Date().toISOString().slice(0, 10)}`).catch(() => null),
    ]);
    layers = l;
    waypoints = w.items || [];
    tracks = t.items || [];
    intelFeatures = (intel && intel.features) || [];
    intelAircraft = (intel && intel.aircraft) || [];
    intelSats = (intel && intel.satellites) || [];
    intelMaritime = (intel && intel.maritime) || [];
    intelCables = (intel && intel.cables) || [];
    intelCctv = (intel && intel.cctv) || [];
    intelSummaryData = (intel && intel.summary) || null;
    sunInfo = cel;
    declInfo = dec;
    try { coordNow = await api.get(`/api/map/coords?lat=${homeLat}&lon=${homeLon}`); } catch { coordNow = null; }
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }

  const startLat = homeLat;
  const startLon = homeLon;
  const view = {
    lat: Number.isFinite(startLat) ? startLat : 39.8283,
    lon: Number.isFinite(startLon) ? startLon : -98.5795,
    zoom: 5,
  };
  const visible = {
    countries: true, states: true, land: true, lakes: true, rivers: true,
    places: true, graticule: true, tracks: true, waypoints: true,
    basemap: true, labels: true,
    intelEvents: true, intelConflict: true, intelAircraft: true, intelSats: true, intelMaritime: true, intelCables: true, intelCctv: true, intelDayNight: true,
    clusterDense: true, cctvShowAlpr: true,
  };
  const raster = (layers.raster || []);
  let tileAvailable = !!(raster.length || settings.mbtiles_path);
  let disposed = false;
  let world = null;
  let selected = null;
  let hover = null;
  let measureMode = false;
  let placeMode = false;
  let measurePoints = [];
  let drag = null;
  let moved = false;
  let raf = 0;
  let resizeObs = null;
  let readoutTimer = null;
  let lastReadout = "";
  const tileCache = new Map();
  let downloading = false;
  let cacheInfo = {
    tile_count: Number(layers.cache?.tile_count) || 0,
    total_mb: Number(layers.cache?.total_mb) || 0,
  };
  let activeSource = layers.activeSource || "esri_imagery";
  let cacheStatusEl = null;

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "COMMAND // TERRAIN",
    title: "Map & Terrain",
    desc: `Bundled vector terrain, local raster tiles, waypoints/tracks and cached intelligence. Position: ${positionInfo.mapLabel}. Click intel/aircraft/satellites for details. Use intel age filter in Layers. Works offline.`,
    actions: [
      btn("2D CANVAS", { icon: "map", tone: "primary", onClick: () => { location.hash = "#/map?renderer=2d"; } }),
      btn("3D GL", { icon: "layers", onClick: () => { location.hash = "#/map?renderer=gl"; } }),
      btn("IMPORT GPX", { icon: "upload", onClick: importGpx }),
      btn("EXPORT GPX", { icon: "download", onClick: () => download("/api/map/gpx/export", "darksec-waypoints.gpx") }),
      btn("NEW WAYPOINT", { icon: "plus", tone: "primary", onClick: () => addWaypoint() }),
    ],
  }));

  if (!layers.vector?.length && !tileAvailable) {
    root.appendChild(alertStrip("No offline basemap is installed. Waypoints and tracks still work, but no terrain will render. Add maps/vector/world.geojson or point Settings at an MBTiles archive.", "red"));
  } else if (!tileAvailable) {
    root.appendChild(alertStrip("Vector basemap only. Detail is limited at close range; use a paper map or add an MBTiles archive for tactical-scale imagery.", ""));
  }

  const layout = h("div", { class: "map-layout mt-14" });
  const shell = h("div", { class: "map-shell" });
  const canvas = h("canvas");
  shell.appendChild(canvas);
  const hud = h("div", { class: "map-hud" });
  const hudZoom = h("div", { class: "hud-box" }, "ZOOM ", h("b", { text: String(view.zoom) }));
  const hudCenter = h("div", { class: "hud-box" });
  const hudMode = h("div", { class: "hud-box" });
  hud.append(hudZoom, hudCenter, hudMode);
  shell.appendChild(hud);

  const tools = h("div", { class: "map-tools" });
  const toolDefs = [
    ["plus", "Zoom in", () => zoomBy(1)],
    ["minus", "Zoom out", () => zoomBy(-1)],
    ["target", "Recentre on configured position", home],
    ["route", "Measure distance", () => toggleMeasure()],
    ["pin", "Place waypoint on next click", () => togglePlace()],
    ["layers", "Fit all waypoints", () => fitWaypoints()],
    ["maximize", "Fit world", () => { view.lat = 20; view.lon = 0; view.zoom = 1; draw(); }],
  ];
  for (const [ic, title, fn] of toolDefs) {
    const b = iconBtn(ic, title, fn);
    b.dataset.tool = ic;
    tools.appendChild(b);
  }
  
  // Offline map pack controls
  const downloadBtn = iconBtn("download", "Download this area for offline use", openDownloadDialog);
  downloadBtn.dataset.tool = "download";
  tools.appendChild(downloadBtn);
  shell.appendChild(tools);

  const attribution = h("div", { class: "map-attribution tiny" }, "");
  shell.appendChild(attribution);

  const scaleWrap = h("div", { class: "map-scale" });
  const scaleBar = h("div", { class: "bar" });
  const scaleLabel = h("span", { text: "" });
  scaleWrap.append(scaleBar, scaleLabel);
  shell.appendChild(scaleWrap);
  const readout = h("div", { class: "map-readout" });
  shell.appendChild(readout);
  const mapPopup = h("div", { class: "map-popup", hidden: true });
  shell.appendChild(mapPopup);
  layout.appendChild(shell);

  const side = h("div", { class: "map-side" });
  layout.appendChild(side);
  root.appendChild(layout);

  /* ------------------------------------------------------------ side panels */
  const search = h("input", { class: "input", type: "search", placeholder: "Filter waypoints…", autocomplete: "off" });
  const wpList = h("div", { class: "col gap-4" });
  const wpPanel = panel({
    title: "Waypoints",
    sub: `${waypoints.length} plotted`,
    icon: "pin",
    body: h("div", { class: "col" },
      h("div", { class: "search-box" }, icon("search"), search),
      wpList,
      h("div", { class: "row wrap mt-10" },
        btn("NEW", { size: "sm", icon: "plus", onClick: () => addWaypoint() }),
        btn("FIT", { size: "sm", tone: "ghost", onClick: fitWaypoints }))),
  });
  side.appendChild(wpPanel);

  const intelList = h("div", { class: "col gap-4" });
  const intelPanel = panel({
    title: "Live Intel",
    sub: `${intelFeatures.length} mapped · newest —`,
    icon: "globe",
    body: h("div", { class: "col" },
      h("div", { class: "tiny muted mb-6", text: "Newest cached points. Click to recentre. Age is shown because a cached event is not a live warning." }),
      intelList),
  });
  side.appendChild(intelPanel);

  const layerRows = h("div", { class: "col gap-4" });
  const layerDefs = [
    ["basemap", "Raster tiles"], ["countries", "Countries"], ["states", "States / regions"],
    ["land", "Land outline"], ["lakes", "Lakes"], ["rivers", "Rivers"],
    ["places", "Settlements"], ["graticule", "Grid graticule"], ["tracks", "Tracks"],
    ["waypoints", "Waypoints"], ["labels", "Labels"],
    ["intelEvents", "Hazard / disaster events"], ["intelConflict", "Conflict events"],
    ["intelAircraft", "Live aircraft"], ["intelSats", "Satellites"],
    ["intelMaritime", "Maritime / AIS"],
    ["intelCables", "Cables / landing points"],
    ["intelCctv", "CCTV / cameras (OSM)"],
    ["intelDayNight", "Day / night"],
  ];
  for (const [key, label] of layerDefs) {
    const cb = checkbox(label, visible[key], (on) => { visible[key] = on; draw(); });
    cb.dataset.layer = key;
    if (key === "basemap" && !tileAvailable) {
      cb.querySelector("input").disabled = true;
      cb.title = "No raster layer or MBTiles archive configured";
    }
    layerRows.appendChild(cb);
  }
  const displayOpts = h("div", { class: "col gap-4 mt-8", style: { borderTop: '1px solid var(--line)', paddingTop: '8px' } });
  displayOpts.appendChild(h("div", { class: "tiny muted", text: "DISPLAY / SCALE" }));
  displayOpts.appendChild(checkbox("Cluster dense markers (CCTV/cables/maritime)", visible.clusterDense, (on) => { visible.clusterDense = on; draw(); }));
  displayOpts.appendChild(checkbox("Show ALPR-tagged CCTV (OSM audited tags)", visible.cctvShowAlpr, (on) => { visible.cctvShowAlpr = on; draw(); renderIntelDetails(); }));
  layerRows.appendChild(displayOpts);
  cacheStatusEl = h("div", { class: "map-cache-status tiny muted", text: cacheStatusText() });
  const intelSummaryEl = h("div", { class: "map-cache-status tiny", text: "Intel not loaded" });
  const ageFilter = select([
    { value: "0", label: "Intel age: all cached" },
    { value: "6", label: "Intel age: last 6 hours" },
    { value: "24", label: "Intel age: last 24 hours" },
    { value: "72", label: "Intel age: last 3 days" },
    { value: "168", label: "Intel age: last 7 days" },
    { value: "720", label: "Intel age: last 30 days" },
  ], "0");
  ageFilter.addEventListener('change', () => { intelMaxAgeHours = Number(ageFilter.value)||0; draw(); renderIntel(); });
  side.appendChild(panel({
    title: "Layers", icon: "layers",
    body: h("div", { class: "col" },
      layerRows,
      h("div", { class: "mt-8" }, field("INTEL AGE FILTER", ageFilter)),
      intelSummaryEl,
      h("div", { class: "tiny dim mt-10", text: `${raster.length} raster source(s) · MBTiles ${settings.mbtiles_path ? "configured" : "not configured"}` }),
      cacheStatusEl,
      h("div", { class: "row gap-6 mt-8" },
        btn("MANAGE OFFLINE MAP", { size: "sm", icon: "download", tone: "primary", onClick: openDownloadDialog }),
        btn("REFRESH", { size: "sm", tone: "ghost", onClick: async () => { await refreshCache(); draw(); } }))),
  }));

  function legendItem(label, color, shape = "diamond") {
    const sw = h("i", { style: { background: color } });
    if (shape === "diamond") sw.style.transform = "rotate(45deg)";
    if (shape === "dot") sw.style.borderRadius = "50%";
    if (shape === "ring") {
      sw.style.background = "transparent";
      sw.style.border = `1px solid ${color}`;
      sw.style.borderRadius = "50%";
    }
    return h("span", {}, sw, document.createTextNode(label));
  }
  const legend = h("div", { class: "map-legend" });
  const legendGroups = [
    ["Waypoints", [
      ["Waypoint", TYPE_COLOR.WAYPOINT, "diamond"],
      ["Camp", TYPE_COLOR.CAMP, "diamond"],
      ["Water", TYPE_COLOR.WATER, "diamond"],
      ["Cache", TYPE_COLOR.CACHE, "diamond"],
      ["Hazard", TYPE_COLOR.HAZARD, "diamond"],
      ["Observation", TYPE_COLOR.OBSERVATION, "diamond"],
    ]],
    ["Live intel", [
      ["Critical event", "#ff5c5c", "dot"],
      ["Severe event", "#ffb02e", "dot"],
      ["Routine event", "#5dff9a", "dot"],
      ["Conflict (UCDP verified)", "#ff8a3d", "diamond"],
      ["Conflict (GDELT live)", "#ffb02e", "diamond"],
      ["Aircraft", "#48d7ff", "ring"],
      ["Satellite", "#c78bff", "ring"],
      ["Maritime / AIS", "#35c9ff", "dot"],
      ["Cables / landing points", "#ffd166", "dot"],
      ["CCTV / cameras (OSM)", "#b7ff6a", "dot"],
      ["Night/terminator", "rgba(148,197,255,.6)", "dot"],
    ]],
  ];
  for (const [group, items] of legendGroups) {
    legend.appendChild(h("div", { class: "legend-group" }, h("b", { text: group })));
    for (const [label, color, shape] of items) legend.appendChild(legendItem(label, color, shape));
  }
  side.appendChild(panel({
    title: "Legend", icon: "info",
    body: h("div", { class: "col gap-6" },
      legend,
      h("div", { class: "tiny dim", text: "Hazard dots scale with reported magnitude. Conflict diamonds are orange for monthly UCDP verified events and amber for live GDELT media reports; faded markers are older than 24 h. Satellite positions are propagated from cached TLEs, not observations." })),
    flush: false,
  }));

  /* --------------------------------------------------------------- render */
  function setCenterFromWorld(wx, wy) {
    const w = canvas.clientWidth || 800;
    const hh = canvas.clientHeight || 500;
    const scale = TILE * (2 ** view.zoom);
    const ll = unproject((wx + w / 2) / scale, (wy + hh / 2) / scale);
    view.lat = clamp(ll.lat, -85, 85);
    view.lon = wrapLon(ll.lon);
  }
  function centerWorld() {
    const p = project(view.lat, view.lon);
    const scale = TILE * (2 ** view.zoom);
    return { x: p.x * scale, y: p.y * scale, scale };
  }
  function screenToLatLon(px, py) {
    const c = centerWorld();
    const ll = unproject((c.x + px - canvas.clientWidth / 2) / c.scale,
      (c.y + py - canvas.clientHeight / 2) / c.scale);
    return { lat: ll.lat, lon: wrapLon(ll.lon) };
  }
  function latLonToScreen(lat, lon) {
    const c = centerWorld();
    const p = project(lat, lon);
    return { x: p.x * c.scale - c.x + canvas.clientWidth / 2,
      y: p.y * c.scale - c.y + canvas.clientHeight / 2 };
  }

  function draw() {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; paint(); });
  }

  function paint() {
    if (disposed) return;
    _clusterHitRegions = [];
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, canvas.clientWidth || 900);
    const hh = Math.max(1, canvas.clientHeight || 560);
    const pw = Math.round(w * dpr), ph = Math.round(hh * dpr);
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw; canvas.height = ph;
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, hh);
    ctx.fillStyle = "#030806";
    ctx.fillRect(0, 0, w, hh);
    hudZoom.lastChild.textContent = String(view.zoom);
    hudCenter.textContent = `${view.lat.toFixed(4)} ${view.lon.toFixed(4)}`;

    if (visible.basemap) drawTiles(ctx, w, hh);

    if (world) {
      const c = centerWorld();
      // Keep the vector layer translucent so a raster basemap can show through;
      // with no tiles the dark canvas behind still reads as a dark vector map.
      const hasRaster = !!visible.basemap && tileAvailable;
      if (visible.land) drawFeatures(ctx, world, "land", c, w, hh, { fill: hasRaster ? "rgba(7,19,13,.38)" : "#07130d", stroke: "rgba(93,255,154,.10)", width: .7 });
      if (visible.countries) drawFeatures(ctx, world, "countries", c, w, hh, { fill: hasRaster ? "rgba(10,31,20,.30)" : "rgba(10,31,20,.72)", stroke: "rgba(93,255,154,.32)", width: .9 });
      if (visible.states && view.zoom >= 3) drawFeatures(ctx, world, "states", c, w, hh, { fill: null, stroke: "rgba(93,255,154,.13)", width: .55 });
      if (visible.lakes) drawFeatures(ctx, world, "lakes", c, w, hh, { fill: hasRaster ? "rgba(9,33,44,.55)" : "rgba(9,33,44,.92)", stroke: "rgba(72,215,255,.35)", width: .8 });
      if (visible.rivers && view.zoom >= 3) drawFeatures(ctx, world, "rivers", c, w, hh, { stroke: "rgba(72,215,255,.35)", width: .7 });
      if (visible.places && view.zoom >= 3) drawPlaces(ctx, world, c, w, hh);
    }

    if (visible.graticule) drawGraticule(ctx, w, hh);
    if (visible.tracks) drawTracks(ctx, w, hh);
    if (visible.waypoints) drawWaypoints(ctx, w, hh);
    if (visible.intelEvents) drawIntel(ctx, w, hh);
    if (visible.intelConflict) drawIntelConflict(ctx, w, hh);
    if (visible.intelAircraft) drawIntelAircraft(ctx, w, hh);
    if (visible.intelSats) drawIntelSats(ctx, w, hh);
    if (visible.intelMaritime) drawIntelMaritime(ctx, w, hh);
    if (visible.intelCables) drawIntelCables(ctx, w, hh);
    if (visible.intelCctv) drawIntelCctv(ctx, w, hh);
    if (visible.intelDayNight) drawIntelDayNight(ctx, w, hh);
    drawPosition(ctx, w, hh);
    drawMeasure(ctx, w, hh);
    updateScale(w, hh);
    positionMapPopup();
  }

  /* ------------------------------------------------------- offline map packs */
  const sourceMeta = {};
  function updateAttribution() {
    const layer = (layers.raster || []).find((l) => l.id === activeSource);
    const attr = layer?.attribution || sourceMeta[activeSource]?.attribution || "";
    attribution.textContent = attr ? `Tiles: ${attr}` : "Vector basemap · no tile attribution";
  }
  api.get("/api/map/cache/sources").then((r) => {
    for (const s of r.sources || []) sourceMeta[s.id] = s;
    updateAttribution();
  }).catch(() => {});
  updateAttribution();

  function cacheStatusText() {
    if (!cacheInfo.tile_count) return "No offline tiles stored";
    return `${cacheInfo.tile_count.toLocaleString()} tiles · ${cacheInfo.total_mb} MB cached`;
  }

  async function refreshCache() {
    try {
      const st = await api.get("/api/map/cache/status");
      cacheInfo = { tile_count: st.tile_count || 0, total_mb: st.total_mb || 0 };
      if (cacheStatusEl) cacheStatusEl.textContent = cacheStatusText();
      const l = await api.get("/api/map/layers").catch(() => null);
      if (l) {
        layers = l;
        if (l.activeSource) activeSource = l.activeSource;
        tileAvailable = !!((l.raster || []).length || settings.mbtiles_path);
        updateAttribution();
      }
    } catch { /* offline: keep last known values */ }
  }

  function viewportBounds() {
    const w = canvas.clientWidth || 900;
    const hh = canvas.clientHeight || 560;
    const tl = screenToLatLon(0, 0);
    const br = screenToLatLon(w, hh);
    return {
      latMin: Math.min(tl.lat, br.lat), latMax: Math.max(tl.lat, br.lat),
      lonMin: Math.min(tl.lon, br.lon), lonMax: Math.max(tl.lon, br.lon),
    };
  }

  function radiusBounds(lat, lon, km) {
    const dLat = km / 110.574;
    const cos = Math.max(0.05, Math.abs(Math.cos(lat * Math.PI / 180)));
    const dLon = km / (111.320 * cos);
    return {
      latMin: clamp(lat - dLat, -85, 85), latMax: clamp(lat + dLat, -85, 85),
      lonMin: wrapLon(lon - dLon), lonMax: wrapLon(lon + dLon),
    };
  }

  function openDownloadDialog() {
    if (downloading) { toast("A download is already running", "warn"); return; }

    const sourceSel = select([], activeSource, { id: "dl-source" });
    const radiusSel = select([
      { value: "5", label: `${U.distKm(5)} radius` }, { value: "10", label: `${U.distKm(10)} radius` },
      { value: "25", label: `${U.distKm(25)} radius` }, { value: "50", label: `${U.distKm(50)} radius` },
      { value: "100", label: `${U.distKm(100)} radius` }, { value: "viewport", label: "Current viewport only" },
    ], "25");
    const zoomSel = select([
      { value: "12", label: "Zoom 8–12 (regional)" }, { value: "13", label: "Zoom 9–13" },
      { value: "14", label: "Zoom 10–14" }, { value: "15", label: "Zoom 11–15 (street detail)" },
      { value: "16", label: "Zoom 12–16 (building detail)" },
    ], "15");
    const centerLat = input({ type: "number", step: "0.0001", value: view.lat.toFixed(4) });
    const centerLon = input({ type: "number", step: "0.0001", value: view.lon.toFixed(4) });
    const estimateEl = h("div", { class: "callout", text: "Calculating…" });
    const progressWrap = h("div", { class: "col gap-6" });
    const progressBar = h("div", { class: "bar", style: { width: "0%" } });
    const progressTrack = h("div", { class: "progress-track" }, progressBar);
    progressTrack.style.display = "none";
    const progressText = h("div", { class: "tiny muted", text: "" });
    progressWrap.append(progressTrack, progressText);

    const body = h("div", { class: "col gap-12" },
      h("div", { class: "tiny muted", text:
        "Downloads real map tiles while you have a connection, then serves them from disk with no network. " +
        "Only the area you choose is stored. Nothing is uploaded." }),
      field("Tile source", sourceSel),
      field("Area", radiusSel),
      field("Detail (zoom range)", zoomSel),
      h("div", { class: "row gap-8" },
        field("Centre latitude", centerLat),
        field("Centre longitude", centerLon)),
      estimateEl,
      progressWrap);

    const dl = modal({
      title: "Download offline map pack",
      sub: "Operator-triggered · stored locally",
      body,
      wide: true,
      actions: [
        btn("CLEAR CACHE", { tone: "danger", size: "sm", onClick: async () => {
          const ok = await confirmDialog({
            title: "Clear offline tiles",
            message: "Delete every downloaded tile? You will need a connection to download them again.",
            confirmText: "DELETE", danger: true,
          });
          if (!ok) return;
          try {
            const r = await api.post("/api/map/cache/clear", {});
            toast(`Cleared ${r.removed} tiles (${r.freed_mb} MB)`, "success");
            await refreshCache();
            dl.close();
            draw();
          } catch (err) { toast(`Clear failed: ${err.message}`, "error"); }
        }}),
        btn("CANCEL", { tone: "ghost", size: "sm", onClick: () => dl.close() }),
        btn("DOWNLOAD", { tone: "primary", onClick: startDownload }),
      ],
    });

    function currentRequest() {
      const source = sourceSel.value || activeSource;
      const radius = radiusSel.value;
      const zMax = Number(zoomSel.value) || 15;
      let bbox;
      if (radius === "viewport") {
        bbox = viewportBounds();
      } else {
        bbox = radiusBounds(Number(centerLat.value) || 0, Number(centerLon.value) || 0, Number(radius));
      }
      const zMin = Math.max(6, zMax - 4);
      return { source, bbox, zMin, zMax };
    }

    let estimateSeq = 0;
    function updateEstimate() {
      const { source, bbox, zMin, zMax } = currentRequest();
      const seq = ++estimateSeq;
      api.post("/api/map/cache/estimate", {
        lat_min: bbox.latMin, lat_max: bbox.latMax,
        lon_min: bbox.lonMin, lon_max: bbox.lonMax,
        zoom_min: zMin, zoom_max: zMax, source,
      }).then((e) => {
        if (seq !== estimateSeq) return;
        const mb = (e.missing * 18 / 1024).toFixed(1);
        estimateEl.textContent = `Zoom ${zMin}–${zMax} · ${e.total.toLocaleString()} tiles · ` +
          `${e.missing.toLocaleString()} to fetch · ≈${mb} MB`;
        estimateEl.className = e.missing > 8000 ? "callout warn" : "callout";
      }).catch((err) => {
        if (seq !== estimateSeq) return;
        estimateEl.textContent = `Estimate unavailable: ${err.message}`;
        estimateEl.className = "callout danger";
      });
    }
    updateEstimate();
    [sourceSel, radiusSel, zoomSel, centerLat, centerLon].forEach((el) =>
      el.addEventListener("change", updateEstimate));

    api.get("/api/map/cache/sources").then((r) => {
      sourceSel.replaceChildren();
      for (const s of r.sources || []) {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = `${s.name}${s.tile_count ? ` · ${s.tile_count} cached` : ""}`;
        sourceSel.appendChild(opt);
      }
      sourceSel.value = activeSource;
      if (!sourceSel.value && r.sources?.length) sourceSel.value = r.sources[0].id;
      if (!r.online) {
        estimateEl.textContent = "No internet connection detected. You can review options, but downloads will fail.";
        estimateEl.className = "callout danger";
      }
      updateEstimate();
    }).catch(() => {});

    async function startDownload() {
      if (downloading) return;
      const { source, bbox, zMin, zMax } = currentRequest();
      downloading = true;
      progressTrack.style.display = "";
      try {
        const job = await api.post("/api/map/cache/download", {
          lat_min: bbox.latMin, lat_max: bbox.latMax,
          lon_min: bbox.lonMin, lon_max: bbox.lonMax,
          zoom_min: zMin, zoom_max: zMax, source,
        });
        activeSource = source;
        await pollJob(job.id);
      } catch (err) {
        toast(`Download failed: ${err.message}`, "error");
      } finally {
        downloading = false;
      }
    }

    function pollJob(jobId) {
      return new Promise((resolve) => {
        const tick = async () => {
          let job;
          try { job = await api.get(`/api/map/cache/job/${jobId}`); }
          catch { resolve(); return; }
          const pct = job.total ? Math.round(job.completed / job.total * 100) : 0;
          progressBar.style.width = `${pct}%`;
          progressText.textContent =
            `${pct}% · ${job.completed}/${job.total} · ${job.downloaded} new · ` +
            `${job.already_cached} cached · ${job.failed} failed`;
          if (job.state === "done") {
            toast(`Offline pack ready: ${job.downloaded} new tiles, ` +
              `${(job.bytes_added / 1048576).toFixed(1)} MB`, "success");
            await refreshCache(); draw(); resolve(); return;
          }
          if (job.state === "blocked") {
            toast(`Provider refused the download: ${job.blocked}`, "error");
            estimateEl.textContent = `Blocked by provider: ${job.blocked}. Try another tile source.`;
            estimateEl.className = "callout danger";
            resolve(); return;
          }
          if (job.state === "error") {
            toast(`Download error: ${job.error}`, "error");
            resolve(); return;
          }
          setTimeout(tick, 900);
        };
        tick();
      });
    }
  }

  function drawTiles(ctx, w, hh) {
    if (!tileAvailable) return;
    const z = Math.round(view.zoom);
    if (z > 19) return;
    const scale = TILE * (2 ** z);
    const c = { x: project(view.lat, view.lon).x * scale, y: project(view.lat, view.lon).y * scale };
    const left = c.x - w / 2, top = c.y - hh / 2;
    const x0 = Math.floor(left / TILE), x1 = Math.floor((left + w) / TILE);
    const y0 = Math.floor(top / TILE), y1 = Math.floor((top + hh) / TILE);
    const n = 2 ** z;
    for (let ty = y0; ty <= y1; ty++) {
      if (ty < 0 || ty >= n) continue;
      for (let tx = x0; tx <= x1; tx++) {
        const wx = ((tx % n) + n) % n;
        const sx = tx * TILE - left, sy = ty * TILE - top;
        const img = tileImage(z, wx, ty);
        if (img && img.complete && img.naturalWidth) {
          ctx.globalAlpha = 0.84;
          try { ctx.drawImage(img, sx, sy, TILE, TILE); } catch {}
          ctx.globalAlpha = 1;
        } else {
          ctx.fillStyle = "rgba(255,255,255,.01)";
          ctx.fillRect(sx, sy, TILE, TILE);
        }
      }
    }
  }

  function tileImage(z, x, y) {
    const key = `${z}/${x}/${y}`;
    if (tileCache.has(key)) return tileCache.get(key);
    const img = new Image();
    img.decoding = "async";
    img.onload = () => draw();
    img.onerror = () => { img.dataset.failed = "1"; };
    img.src = `/api/map/tiles/${z}/${x}/${y}.png?v=4`;
    tileCache.set(key, img);
    if (tileCache.size > 320) {
      const first = tileCache.keys().next().value;
      tileCache.delete(first);
    }
    return img;
  }

  function featurePaths(feature) {
    const g = feature.geometry;
    if (!g) return [];
    if (g.type === "Polygon") return [g.coordinates];
    if (g.type === "MultiPolygon") return g.coordinates;
    if (g.type === "LineString") return [[g.coordinates]];
    if (g.type === "MultiLineString") return [g.coordinates];
    return [];
  }

  function tracePath(ctx, coords, c, w, hh) {
    let started = false;
    for (const p of coords) {
      if (!p || p.length < 2) continue;
      const pr = project(p[1], p[0]);
      const x = pr.x * c.scale - c.x + w / 2;
      const y = pr.y * c.scale - c.y + hh / 2;
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    return started;
  }

  function drawFeatures(ctx, data, layer, c, w, hh, style) {
    ctx.beginPath();
    let found = false;
    for (const f of data.features) {
      if (f.properties?.layer !== layer) continue;
      for (const poly of featurePaths(f)) {
        if (f.geometry.type === "Polygon" || f.geometry.type === "MultiPolygon") {
          for (const ring of poly) if (tracePath(ctx, ring, c, w, hh)) { ctx.closePath(); found = true; }
        } else {
          for (const line of poly) if (tracePath(ctx, line, c, w, hh)) found = true;
        }
      }
    }
    if (!found) return;
    if (style.fill) { ctx.fillStyle = style.fill; ctx.fill("evenodd"); }
    if (style.stroke) { ctx.strokeStyle = style.stroke; ctx.lineWidth = style.width || 1; ctx.stroke(); }
  }

  function drawPlaces(ctx, data, c, w, hh) {
    ctx.font = "9px JetBrains Mono, monospace";
    ctx.textBaseline = "middle";
    for (const f of data.features) {
      if (f.properties?.layer !== "places") continue;
      const p = f.properties;
      if (p.latitude == null || p.longitude == null) continue;
      const s = latLonToScreen(p.latitude, p.longitude);
      if (s.x < -20 || s.y < -20 || s.x > w + 20 || s.y > hh + 20) continue;
      const pop = Number(p.pop_max) || 0;
      if (view.zoom < 5 && pop < 1_000_000) continue;
      ctx.fillStyle = "rgba(255,176,46,.9)";
      ctx.beginPath(); ctx.arc(s.x, s.y, pop > 2_000_000 ? 2.5 : 1.5, 0, Math.PI * 2); ctx.fill();
      if (visible.labels && (view.zoom >= 6 || pop > 2_000_000)) {
        ctx.fillStyle = "rgba(210,235,220,.78)";
        ctx.strokeStyle = "rgba(0,0,0,.7)";
        ctx.lineWidth = 2;
        const label = p.name.length > 22 ? p.name.slice(0, 21) + "…" : p.name;
        ctx.strokeText(label, s.x + 5, s.y - 1);
        ctx.fillText(label, s.x + 5, s.y - 1);
      }
    }
  }

  function drawGraticule(ctx, w, hh) {
    const spacing = view.zoom >= 10 ? 1 : view.zoom >= 6 ? 5 : view.zoom >= 3 ? 15 : 30;
    ctx.strokeStyle = "rgba(93,255,154,.07)";
    ctx.lineWidth = .7;
    ctx.font = "8px JetBrains Mono, monospace";
    ctx.fillStyle = "rgba(123,154,136,.45)";
    for (let lat = -80; lat <= 80; lat += spacing) {
      const y = latLonToScreen(lat, view.lon).y;
      if (y < 0 || y > hh) continue;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
      if (visible.labels) ctx.fillText(`${Math.abs(lat)}°${lat >= 0 ? "N" : "S"}`, 3, y - 2);
    }
    const step = spacing * 2;
    for (let lon = -180; lon < 180; lon += step) {
      const x = latLonToScreen(view.lat, lon).x;
      if (x < 0 || x > w) continue;
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, hh); ctx.stroke();
      if (visible.labels) ctx.fillText(`${Math.abs(lon)}°${lon >= 0 ? "E" : "W"}`, x + 3, hh - 22);
    }
  }

  function drawTracks(ctx, w, hh) {
    for (const t of tracks) {
      let gj;
      try { gj = JSON.parse(t.geojson); } catch { continue; }
      const lines = gj.type === "LineString" ? [gj.coordinates]
        : gj.type === "MultiLineString" ? gj.coordinates
        : gj.type === "Feature" && gj.geometry?.type === "LineString" ? [gj.geometry.coordinates] : [];
      for (const line of lines) {
        ctx.beginPath();
        for (const p of line || []) {
          if (!p || p.length < 2) continue;
          const s = latLonToScreen(p[1], p[0]);
          if (!Number.isFinite(s.x)) continue;
          if (s === line[0]) ctx.moveTo(s.x, s.y); else ctx.lineTo(s.x, s.y);
        }
        ctx.strokeStyle = t.color || "#48d7ff";
        ctx.globalAlpha = 0.8;
        ctx.lineWidth = 1.6;
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
    }
  }

  function drawWaypoints(ctx, w, hh) {
    for (const wp of waypoints) {
      const s = latLonToScreen(Number(wp.lat), Number(wp.lon));
      if (s.x < -30 || s.y < -30 || s.x > w + 30 || s.y > hh + 30) continue;
      const color = TYPE_COLOR[wp.type] || TYPE_COLOR.WAYPOINT;
      const isSel = selected?.id === wp.id;
      const size = isSel ? 7 : 5;
      ctx.save();
      ctx.translate(s.x, s.y);
      if (wp.type === "HAZARD") {
        ctx.fillStyle = color;
        ctx.beginPath(); ctx.moveTo(0, -size); ctx.lineTo(size, size); ctx.lineTo(-size, size); ctx.closePath(); ctx.fill();
      } else if (wp.type === "LZ") {
        ctx.strokeStyle = color; ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.arc(0, 0, size, 0, Math.PI * 2); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(-size, 0); ctx.lineTo(size, 0); ctx.stroke();
      } else {
        ctx.fillStyle = color;
        ctx.beginPath(); ctx.moveTo(0, -size); ctx.lineTo(size, 0); ctx.lineTo(0, size); ctx.lineTo(-size, 0); ctx.closePath(); ctx.fill();
      }
      if (isSel) {
        ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(0, 0, size + 5, 0, Math.PI * 2); ctx.stroke();
      }
      ctx.restore();
      if (visible.labels && (isSel || view.zoom >= 7)) {
        ctx.font = "9px JetBrains Mono, monospace";
        ctx.textBaseline = "middle";
        ctx.strokeStyle = "rgba(0,0,0,.82)"; ctx.lineWidth = 2.4;
        ctx.strokeText(wp.name, s.x + 8, s.y);
        ctx.fillStyle = isSel ? "#ffffff" : color;
        ctx.fillText(wp.name, s.x + 8, s.y);
      }
    }
  }

  function intelPoint(ctx, vw, vh, lat, lon, sev, mag) {
    const s = latLonToScreen(lat, lon);
    if (s.x < -20 || s.y < -20 || s.x > vw + 20 || s.y > vh + 20) return;
    const critical = ["CRITICAL", "SEVERE", "EXTREME"].includes(String(sev || "").toUpperCase());
    const color = critical ? "#ff5c5c" : ["ALERT", "SEVERE"].includes(String(sev || "").toUpperCase()) ? "#ffb02e" : "#5dff9a";
    const r = critical ? 4.5 : (mag != null ? 2.5 + Math.min(4, Number(mag) || 0) : 2.5);
    ctx.fillStyle = critical ? "#ff5c5c" : ["ALERT", "SEVERE"].includes(String(sev || "").toUpperCase()) ? "#ffb02e" : "#5dff9a";
    ctx.beginPath(); ctx.arc(s.x, s.y, r, 0, Math.PI * 2); ctx.fill();
    if (view.zoom >= 8) {
      ctx.font = "8px JetBrains Mono, monospace";
      ctx.textBaseline = "top";
      ctx.fillStyle = "#c8e6d4";
      ctx.fillText(`${sev || "EVENT"}${mag != null ? ` M${mag}` : ""}`, s.x + 7, s.y + 2);
    }
  }

  function drawIntel(ctx, vw, vh) {
    const maxAge = Number(intelMaxAgeHours)||0;
    for (const f of intelFeatures) {
      const g = f.geometry;
      if (!g || g.type !== "Point") continue;
      const p = f.properties || {};
      if (p.kind === "conflict" || p.feed_id === "gdelt_conflict" || p.feed_id === "ucdp_candidate") continue;
      if (maxAge > 0 && p.age_hours != null && Number(p.age_hours) > maxAge) continue;
      const lat = Number(g.coordinates[1]), lon = Number(g.coordinates[0]);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const selected = selectedIntel?.kind === 'event' && selectedIntel.feature === f;
      if (selected) {
        const sp = latLonToScreen(lat, lon);
        if (sp.x >= -24 && sp.y >= -24 && sp.x <= vw + 24 && sp.y <= vh + 24) {
          ctx.save();
          ctx.strokeStyle = '#ffffff';
          ctx.fillStyle = 'rgba(255,255,255,.12)';
          ctx.lineWidth = 1.5;
          ctx.beginPath(); ctx.arc(sp.x, sp.y, 8.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
          ctx.strokeStyle = 'rgba(93,255,154,.35)';
          ctx.beginPath(); ctx.arc(sp.x, sp.y, 13, 0, Math.PI * 2); ctx.stroke();
          ctx.restore();
        }
      }
      intelPoint(ctx, vw, vh, lat, lon, p.severity, p.mag);
      if (selected) {
        const sp = latLonToScreen(lat, lon);
        if (sp.x >= -24 && sp.y >= -24 && sp.x <= vw + 24 && sp.y <= vh + 24) {
          ctx.save();
          ctx.font = 'bold 9px JetBrains Mono, monospace';
          ctx.textBaseline = 'top';
          ctx.strokeStyle = 'rgba(0,0,0,.75)';
          ctx.lineWidth = 3;
          const label = String(p.title || 'Event').slice(0, 42);
          ctx.strokeText(label, sp.x + 11, sp.y - 8);
          ctx.fillStyle = '#ffffff';
          ctx.fillText(label, sp.x + 11, sp.y - 8);
          ctx.restore();
        }
      }
    }
  }

  function drawIntelConflict(ctx, vw, vh) {
    const maxAge = Number(intelMaxAgeHours)||0;
    for (const f of intelFeatures) {
      const g = f.geometry;
      if (!g || g.type !== "Point") continue;
      const p = f.properties || {};
      if (!(p.kind === "conflict" || p.feed_id === "gdelt_conflict" || p.feed_id === "ucdp_candidate")) continue;
      if (maxAge > 0 && p.age_hours != null && Number(p.age_hours) > maxAge) continue;
      const lat = Number(g.coordinates[1]), lon = Number(g.coordinates[0]);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const sp = latLonToScreen(lat, lon);
      if (sp.x < -20 || sp.y < -20 || sp.x > vw + 20 || sp.y > vh + 20) continue;
      const age = Number(p.age_hours);
      const recent = !Number.isFinite(age) || age <= 24;
      const fatalities = Number(p.fatalities) || 0;
      const size = p.verified && fatalities > 0 ? 2.6 + Math.min(4.5, Math.sqrt(fatalities)) : 2.6;
      const color = p.severity === "CRITICAL" ? "#ff5c5c" : (p.verified ? "#ff8a3d" : "#ffb02e");
      const selected = selectedIntel?.kind === 'event' && selectedIntel.feature === f;
      ctx.save();
      ctx.globalAlpha = recent ? 0.95 : 0.5;
      ctx.translate(sp.x, sp.y);
      ctx.rotate(Math.PI / 4);
      ctx.fillStyle = color;
      ctx.fillRect(-size, -size, size * 2, size * 2);
      if (selected) {
        ctx.strokeStyle = '#ffffff';
        ctx.lineWidth = 1.2;
        ctx.strokeRect(-size - 2, -size - 2, (size + 2) * 2, (size + 2) * 2);
      }
      ctx.restore();
      if (selected) {
        ctx.save();
        ctx.strokeStyle = 'rgba(255,255,255,.8)';
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(sp.x, sp.y, size + 9, 0, Math.PI * 2); ctx.stroke();
        ctx.restore();
      }
      if (view.zoom >= 7 && (recent || p.severity === "CRITICAL" || selected)) {
        ctx.globalAlpha = recent || selected ? 0.95 : 0.6;
        ctx.font = `${selected ? 'bold ' : ''}8px JetBrains Mono, monospace`;
        ctx.textBaseline = "top";
        ctx.fillStyle = selected ? '#fff' : color;
        const label = selected ? String(p.title || 'Conflict').slice(0, 44) : `${p.verified ? "VERIFIED" : "LIVE"}${fatalities ? ` ${fatalities} KILLED` : ""}`;
        if (selected) { ctx.strokeStyle = 'rgba(0,0,0,.75)'; ctx.lineWidth = 3; ctx.strokeText(label, sp.x + 9, sp.y + 2); }
        ctx.fillText(label, sp.x + 9, sp.y + 2);
        ctx.globalAlpha = 1;
      }
    }
  }

  function drawIntelAircraft(ctx, w, hh) {
    const showLabels = view.zoom >= 5;
    for (const a of (intelAircraft || []).slice(0, 120)) {
      const s = latLonToScreen(Number(a.lat), Number(a.lon));
      if (s.x < -24 || s.y < -24 || s.x > w + 24 || s.y > hh + 24) continue;
      const hdg = Number(a.heading_deg);
      const sel = selectedIntel?.kind === 'aircraft' && selectedIntel.data === a;
      ctx.save();
      ctx.translate(s.x, s.y);
      if (Number.isFinite(hdg)) ctx.rotate(hdg * Math.PI / 180);
      ctx.strokeStyle = sel ? '#fff' : '#48d7ff';
      ctx.fillStyle = sel ? 'rgba(255,255,255,.95)' : 'rgba(72,215,255,.9)';
      ctx.lineWidth = sel ? 1.6 : 1;
      ctx.beginPath();
      ctx.moveTo(0, -5); ctx.lineTo(3.5, 4); ctx.lineTo(0, 2); ctx.lineTo(-3.5, 4); ctx.closePath();
      ctx.fill(); ctx.stroke();
      ctx.restore();
      if (showLabels && (a.callsign || sel)) {
        ctx.font = `${sel ? 'bold ' : ''}8px JetBrains Mono, monospace`;
        ctx.fillStyle = sel ? '#fff' : 'rgba(72,215,255,.85)';
        ctx.textBaseline = 'top';
        const label = String(a.callsign || 'ACFT').trim();
        ctx.fillText(label, s.x + 7, s.y + 2);
      }
    }
  }

  function drawIntelSats(ctx, w, hh) {
    const showLabels = view.zoom >= 3;
    for (const sat of (intelSats || []).slice(0, 80)) {
      const lat = Number(sat.lat), lon = Number(sat.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const sp = latLonToScreen(lat, lon);
      if (sp.x < -24 || sp.y < -24 || sp.x > w + 24 || sp.y > hh + 24) continue;
      const sel = selectedIntel?.kind === 'sat' && selectedIntel.data === sat;
      ctx.strokeStyle = sel ? '#fff' : '#c78bff';
      ctx.fillStyle = sel ? 'rgba(255,255,255,.95)' : 'rgba(199,139,255,.9)';
      ctx.lineWidth = sel ? 1.5 : 1;
      ctx.beginPath(); ctx.arc(sp.x, sp.y, sel ? 3.2 : 2.2, 0, Math.PI * 2); ctx.stroke(); ctx.fill();
      ctx.strokeStyle = sel ? 'rgba(255,255,255,.35)' : 'rgba(199,139,255,.25)';
      ctx.beginPath(); ctx.arc(sp.x, sp.y, sel ? 8 : 5, 0, Math.PI * 2); ctx.stroke();
      if (showLabels && (sat.name || sel)) {
        ctx.font = `${sel ? 'bold ' : ''}8px JetBrains Mono, monospace`;
        ctx.fillStyle = sel ? '#fff' : 'rgba(199,139,255,.85)';
        ctx.textBaseline = 'top';
        ctx.fillText(String(sat.name || `NORAD ${sat.norad || '?'}`).slice(0, 18), sp.x + 8, sp.y + 2);
      }
    }
  }



  let _clusterHitRegions = []; // [{x,y,r,lat,lon,zoomTo,kind,count}] for 2D cluster click-to-zoom
  function drawSimpleIntelPoints(ctx, w, hh, items, opts = {}) {
    const { color = '#35c9ff', selKind = '', limit = 1500, labelKey = 'title', labelZoom = 9, cluster = true, getColor = null, getLabel = null } = opts;
    const arr = (items || []).slice(0, limit);
    const itemColor = (it) => (getColor ? getColor(it) : color);
    // Grid clustering for dense layers at low zoom (keeps "everything shows" usable).
    if (cluster && visible.clusterDense !== false && view.zoom <= 8 && arr.length > 250) {
      const cell = view.zoom <= 3 ? 52 : view.zoom <= 5 ? 40 : view.zoom <= 7 ? 30 : 22;
      const buckets = new Map();
      let selKey = null;
      for (const it of arr) {
        const lat = Number(it.lat), lon = Number(it.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        const sp = latLonToScreen(lat, lon);
        if (sp.x < -40 || sp.y < -40 || sp.x > w + 40 || sp.y > hh + 40) continue;
        const k = `${Math.floor(sp.x / cell)}:${Math.floor(sp.y / cell)}`;
        let b = buckets.get(k);
        if (!b) { b = { x: 0, y: 0, lat: 0, lon: 0, n: 0, items: [], hasAlpr: false }; buckets.set(k, b); }
        b.x += sp.x; b.y += sp.y; b.lat += lat; b.lon += lon; b.n += 1; if (b.items.length < 3) b.items.push(it);
        if (it.alpr) b.hasAlpr = true;
        if (selectedIntel?.kind === selKind && selectedIntel.data === it) selKey = k;
      }
      for (const [k, b] of buckets) {
        const cx = b.x / b.n, cy = b.y / b.n;
        if (b.n === 1) {
          const it = b.items[0];
          const sel = selectedIntel?.kind === selKind && selectedIntel.data === it;
          const ic = itemColor(it);
          const r = sel ? 5.2 : (view.zoom <= 5 ? 4.2 : 3.4);
          ctx.save();
          ctx.globalAlpha = 0.18; ctx.fillStyle = ic; ctx.beginPath(); ctx.arc(cx, cy, r + 3.5, 0, Math.PI*2); ctx.fill(); ctx.globalAlpha = 1;
          ctx.fillStyle = sel ? '#ffffff' : ic;
          ctx.strokeStyle = sel ? '#ffffff' : 'rgba(0,0,0,.7)'; ctx.lineWidth = sel ? 1.6 : 1.2;
          ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI*2); ctx.fill(); ctx.stroke();
          ctx.restore();
          continue;
        }
        const r = Math.max(9, Math.min(24, 8 + Math.sqrt(b.n) * 2.2));
        const ccol = b.hasAlpr && selKind === 'cctv' ? '#d98cff' : color;
        const clat = b.lat / b.n, clon = b.lon / b.n;
        _clusterHitRegions.push({ x: cx, y: cy, r: r + 4, lat: clat, lon: clon, zoomTo: Math.min(18, view.zoom + 2), kind: selKind, count: b.n });
        ctx.save();
        ctx.globalAlpha = k === selKey ? 0.95 : 0.82;
        ctx.fillStyle = ccol;
        ctx.strokeStyle = 'rgba(0,0,0,.55)';
        ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
        ctx.fillStyle = '#041008';
        ctx.font = `bold ${r >= 14 ? 10 : 9}px JetBrains Mono, monospace`;
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(String(b.n > 999 ? '999+' : b.n), cx, cy + 0.5);
        ctx.restore();
      }
      return;
    }
    const showLabels = view.zoom >= labelZoom;
    const baseR = view.zoom <= 3 ? 4.8 : view.zoom <= 6 ? 4.0 : 3.2;
    for (const it of arr) {
      const lat = Number(it.lat), lon = Number(it.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const sp = latLonToScreen(lat, lon);
      if (sp.x < -24 || sp.y < -24 || sp.x > w + 24 || sp.y > hh + 24) continue;
      const sel = selectedIntel?.kind === selKind && selectedIntel.data === it;
      const ic = itemColor(it);
      const r = sel ? Math.max(baseR + 1.4, 5) : baseR;
      ctx.save();
      ctx.globalAlpha = sel ? 0.35 : 0.18;
      ctx.fillStyle = ic;
      ctx.beginPath(); ctx.arc(sp.x, sp.y, r + 3.5, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1;
      ctx.fillStyle = sel ? '#ffffff' : ic;
      ctx.strokeStyle = sel ? '#ffffff' : 'rgba(0,0,0,.7)';
      ctx.lineWidth = sel ? 1.6 : 1.2;
      ctx.beginPath(); ctx.arc(sp.x, sp.y, r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      if (sel) {
        ctx.strokeStyle = ic;
        ctx.globalAlpha = 0.5;
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(sp.x, sp.y, r + 6, 0, Math.PI * 2); ctx.stroke();
        ctx.globalAlpha = 1;
      }
      ctx.restore();
      if ((showLabels || sel) && (it[labelKey] || it.name || sel)) {
        ctx.save();
        ctx.font = `${sel ? 'bold ' : ''}8px JetBrains Mono, monospace`;
        ctx.textBaseline = 'top';
        const label = getLabel ? getLabel(it) : String(it[labelKey] || it.name || it.vessel || it.callsign || it.camera_id || it.cable_id || 'ITEM').slice(0, 28);
        if (sel) { ctx.strokeStyle = 'rgba(0,0,0,.75)'; ctx.lineWidth = 3; ctx.strokeText(label, sp.x + 8, sp.y + 2); }
        ctx.fillStyle = sel ? '#fff' : ic;
        ctx.fillText(label, sp.x + 8, sp.y + 2);
        ctx.restore();
      }
    }
  }

  function drawIntelMaritime(ctx, w, hh) {
    drawSimpleIntelPoints(ctx, w, hh, intelMaritime, { color: '#35c9ff', selKind: 'maritime', labelKey: 'title', labelZoom: 7 });
  }
  function drawIntelCables(ctx, w, hh) {
    drawSimpleIntelPoints(ctx, w, hh, intelCables, { color: '#ffd166', selKind: 'cable', labelKey: 'title', labelZoom: 5 });
  }
  function drawIntelCctv(ctx, w, hh) {
    if (!visible.intelCctv) return;
    let items = intelCctv || [];
    if (visible.cctvShowAlpr === false) items = items.filter(it => !it.alpr);
    drawSimpleIntelPoints(ctx, w, hh, items, {
      color: '#b7ff6a', selKind: 'cctv', labelKey: 'title', labelZoom: 11,
      getColor: (it) => it.alpr ? '#ff66cc' : '#b7ff6a',
      getLabel: (it) => {
        const base = String(it.title || it.camera_id || (it.alpr ? 'ALPR' : 'Camera')).slice(0, 28);
        return it.alpr && !/ALPR|ANPR/i.test(base) ? `${base} [ALPR]` : base;
      },
    });
  }

  function solarPosition(date = new Date()) {
    const start = Date.UTC(date.getUTCFullYear(), 0, 0);
    const diff = date.getTime() - start;
    const dayOfYear = Math.floor(diff / 86400000);
    const decl = -23.44 * Math.cos((360 / 365) * (dayOfYear + 10) * Math.PI / 180);
    const utcH = date.getUTCHours() + date.getUTCMinutes() / 60 + date.getUTCSeconds() / 3600;
    let subLon = -15 * (utcH - 12);
    subLon = ((subLon + 180) % 360 + 360) % 360 - 180;
    return { decl, subLon };
  }

  function terminatorLat(lon, decl, subLon) {
    const d = decl * Math.PI / 180;
    const h = (lon - subLon) * Math.PI / 180;
    const tanD = Math.tan(d);
    if (Math.abs(tanD) < 1e-8) return 0;
    const lat = Math.atan(-Math.cos(h) / tanD) * 180 / Math.PI;
    return Math.max(-85, Math.min(85, lat));
  }

  function drawIntelDayNight(ctx, w, hh) {
    const now = new Date();
    const { decl, subLon } = solarPosition(now);
    const sunUpNorth = decl >= 0; // subsolar in north => north side of terminator tends to day? opposite: night is away from sun
    // Sample terminator across visible longitudes (with padding)
    const tl = screenToLatLon(0, 0), br = screenToLatLon(w, hh);
    let lonMin = Math.min(tl.lon, br.lon) - 30, lonMax = Math.max(tl.lon, br.lon) + 30;
    if (lonMax - lonMin > 360) { lonMin = -180; lonMax = 180; }
    const pts = [];
    const step = Math.max(2, (lonMax - lonMin) / 80);
    for (let lon = lonMin; lon <= lonMax; lon += step) pts.push({ lon, lat: terminatorLat(lon, decl, subLon) });
    // Build night polygon: terminator curve + bottom or top edge depending on which side is night
    // Determine if center of view is in daylight
    const center = { lat: view.lat, lon: view.lon };
    const cosH = Math.cos((center.lon - subLon) * Math.PI / 180);
    const sinAlt = Math.sin(center.lat * Math.PI / 180) * Math.sin(decl * Math.PI / 180) + Math.cos(center.lat * Math.PI / 180) * Math.cos(decl * Math.PI / 180) * cosH;
    const centerIsDay = sinAlt > 0;
    // Night side: if center is day, night is the opposite side of terminator; if center night, shade side containing center
    const nightToNorth = !sunUpNorth ? centerIsDay : !centerIsDay; // heuristic refined below
    // Better: for each point, the sunward side is where altitude > 0. Use subsolar hemisphere.
    // Build polygon covering night: go along terminator, then along map edge on night side.
    ctx.save();
    const path = () => {
      ctx.beginPath();
      const first = latLonToScreen(pts[0].lat, pts[0].lon);
      ctx.moveTo(first.x, first.y);
      for (let i = 1; i < pts.length; i++) { const p = latLonToScreen(pts[i].lat, pts[i].lon); ctx.lineTo(p.x, p.y); }
      // close to night edge: test a point slightly "away" from subsolar
      const midLon = pts[Math.floor(pts.length / 2)].lon;
      const midLat = pts[Math.floor(pts.length / 2)].lat;
      // move toward pole opposite subsolar? subsolar lat = decl. night pole side is opposite sign of decl influence; simpler: sample above/below terminator
      const testLat = Math.max(-85, Math.min(85, midLat + (decl > 0 ? -15 : 15)));
      const testSin = Math.sin(testLat * Math.PI / 180) * Math.sin(decl * Math.PI / 180) + Math.cos(testLat * Math.PI / 180) * Math.cos(decl * Math.PI / 180) * Math.cos((midLon - subLon) * Math.PI / 180);
      const nightIsBelow = testSin < 0; // lower on screen? lat increases upward in geo but screen y inverted
      const last = latLonToScreen(pts[pts.length - 1].lat, pts[pts.length - 1].lon);
      const firstP = latLonToScreen(pts[0].lat, pts[0].lon);
      if (nightIsBelow) {
        // in geo coords, "below" terminator in that test means more negative lat relative? but we need screen edge
        // if testLat < midLat gave night, shade toward south (higher screen y)
        const edgeY = testLat < midLat ? hh + 40 : -40;
        ctx.lineTo(last.x, edgeY); ctx.lineTo(firstP.x, edgeY);
      } else {
        const edgeY = testLat < midLat ? -40 : hh + 40;
        ctx.lineTo(last.x, edgeY); ctx.lineTo(firstP.x, edgeY);
      }
      ctx.closePath();
    };
    path();
    ctx.fillStyle = "rgba(5, 12, 30, 0.22)";
    ctx.fill();
    // terminator line
    ctx.beginPath();
    for (let i = 0; i < pts.length; i++) { const p = latLonToScreen(pts[i].lat, pts[i].lon); if (!i) ctx.moveTo(p.x, p.y); else ctx.lineTo(p.x, p.y); }
    ctx.strokeStyle = "rgba(148,197,255,0.45)";
    ctx.lineWidth = 1.2;
    ctx.stroke();
    // subsolar marker
    const ss = latLonToScreen(decl, subLon);
    if (ss.x > -20 && ss.x < w + 20 && ss.y > -20 && ss.y < hh + 20) {
      ctx.fillStyle = "rgba(255,220,120,0.85)";
      ctx.beginPath(); ctx.arc(ss.x, ss.y, 3.5, 0, Math.PI*2); ctx.fill();
      ctx.strokeStyle = "rgba(255,220,120,0.35)"; ctx.beginPath(); ctx.arc(ss.x, ss.y, 8, 0, Math.PI*2); ctx.stroke();
    }
    ctx.restore();
  }

  function drawPosition(ctx, w, hh) {
    const lat = Number(store.settings?.lat), lon = Number(store.settings?.lon);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
    const s = latLonToScreen(lat, lon);
    if (s.x < -20 || s.y < -20 || s.x > w + 20 || s.y > hh + 20) return;
    const quality = positionConfidence(store.settings || {});
    const color = quality.tone === "danger" ? "#ff5c5c" : quality.tone === "warn" ? "#ffb02e" : "#48d7ff";
    ctx.strokeStyle = color; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.arc(s.x, s.y, 7, 0, Math.PI * 2); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(s.x - 11, s.y); ctx.lineTo(s.x + 11, s.y); ctx.moveTo(s.x, s.y - 11); ctx.lineTo(s.x, s.y + 11); ctx.stroke();
    ctx.fillStyle = color; ctx.beginPath(); ctx.arc(s.x, s.y, 2, 0, Math.PI * 2); ctx.fill();
    ctx.font = "8px JetBrains Mono, monospace";
    ctx.fillStyle = color;
    ctx.fillText(quality.mapLabel, s.x + 12, s.y + 2);
  }

  function drawMeasure(ctx, w, hh) {
    if (!measurePoints.length) return;
    ctx.strokeStyle = "#ffb02e"; ctx.lineWidth = 1.4;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    measurePoints.forEach((p, i) => {
      const s = latLonToScreen(p.lat, p.lon);
      if (!i) ctx.moveTo(s.x, s.y); else ctx.lineTo(s.x, s.y);
    });
    ctx.stroke();
    ctx.setLineDash([]);
    let total = 0;
    for (let i = 0; i < measurePoints.length; i++) {
      const s = latLonToScreen(measurePoints[i].lat, measurePoints[i].lon);
      ctx.fillStyle = "#ffb02e";
      ctx.beginPath(); ctx.arc(s.x, s.y, 3, 0, Math.PI * 2); ctx.fill();
      if (i) {
        total += distanceM(measurePoints[i - 1], measurePoints[i]);
        ctx.font = "9px JetBrains Mono, monospace";
        ctx.fillStyle = "#ffd166";
        ctx.fillText(U.distM(total), s.x + 5, s.y - 5);
      }
    }
    if (measurePoints.length > 1) {
      const last = latLonToScreen(measurePoints.at(-1).lat, measurePoints.at(-1).lon);
      ctx.font = "10px JetBrains Mono, monospace";
      ctx.fillStyle = "#ffb02e";
      ctx.fillText(`TOTAL ${U.distM(total)}`, last.x + 8, last.y + 12);
    }
  }

  function updateScale(w, hh) {
    const c = centerWorld();
    const mpp = Math.cos(view.lat * Math.PI / 180) * 2 * Math.PI * R / c.scale;
    const target = 88 * mpp;
    const nice = niceDistance(target);
    const px = nice.m / mpp;
    scaleBar.style.width = `${Math.max(18, Math.min(180, px))}px`;
    scaleLabel.textContent = nice.label;
  }

  /* ---------------------------------------------------------- map actions */
  function zoomAt(newZoom, px = null, py = null) {
    newZoom = clamp(Math.round(newZoom), 1, 18);
    if (newZoom === view.zoom) return;
    if (px === null) { px = canvas.clientWidth / 2; py = canvas.clientHeight / 2; }
    const before = screenToLatLon(px, py);
    view.zoom = newZoom;
    const after = screenToLatLon(px, py);
    view.lat = clamp(view.lat + (before.lat - after.lat), -85, 85);
    view.lon = wrapLon(view.lon + (before.lon - after.lon));
    draw();
  }
  function zoomBy(delta) { zoomAt(view.zoom + delta); }
  function home() {
    view.lat = homeLat;
    view.lon = homeLon;
    view.zoom = 10;
    selected = null;
    draw();
  }
  function fitWaypoints() {
    if (!waypoints.length) { toast("No waypoints to fit", "warn"); return; }
    let minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
    for (const w of waypoints) {
      minLat = Math.min(minLat, w.lat); maxLat = Math.max(maxLat, w.lat);
      minLon = Math.min(minLon, w.lon); maxLon = Math.max(maxLon, w.lon);
    }
    view.lat = (minLat + maxLat) / 2; view.lon = (minLon + maxLon) / 2;
    const span = Math.max(maxLat - minLat, maxLon - minLon, 0.01);
    view.zoom = span > 100 ? 2 : span > 40 ? 3 : span > 12 ? 4 : span > 4 ? 5 : span > 1.5 ? 6 : span > .5 ? 7 : span > .15 ? 8 : 9;
    draw();
  }
  function toggleMeasure() {
    measureMode = !measureMode;
    placeMode = false;
    canvas.classList.toggle("picking", measureMode || placeMode);
    shell.querySelectorAll(".map-tools button").forEach((b) => b.classList.toggle("active", b.dataset.tool === "route" && measureMode));
    updateModeHud();
    if (!measureMode) measurePoints = [];
    draw();
  }
  function togglePlace() {
    placeMode = !placeMode;
    measureMode = false;
    canvas.classList.toggle("picking", placeMode);
    shell.querySelectorAll(".map-tools button").forEach((b) => b.classList.toggle("active", b.dataset.tool === "pin" && placeMode));
    updateModeHud();
  }
  function updateModeHud() {
    hudMode.replaceChildren();
    hudMode.appendChild(document.createTextNode(measureMode ? "MODE " : placeMode ? "MODE " : "MODE "));
    hudMode.appendChild(h("b", { text: measureMode ? "MEASURE" : placeMode ? "PLACE" : "PAN" }));
  }

  function selectWaypoint(wp) {
    selected = wp;
    renderWaypoints();
    if (wp) {
      const s = latLonToScreen(wp.lat, wp.lon);
      if (s.x < 40 || s.y < 40 || s.x > canvas.clientWidth - 40 || s.y > canvas.clientHeight - 40) {
        view.lat = Number(wp.lat); view.lon = Number(wp.lon);
        view.zoom = Math.max(view.zoom, 12);
      }
    }
    draw();
  }

  function addWaypoint(latlon = null) {
    const s = store.settings || {};
    const defaults = latlon || { lat: Number(s.lat), lon: Number(s.lon) };
    openRecordForm({
      table: "waypoints",
      defaults: { lat: defaults.lat, lon: defaults.lon, ele: s.elevation_m, type: "WAYPOINT" },
      onSaved: () => reloadWaypoints(),
    });
  }

  async function reloadWaypoints() {
    try {
      const out = await api.get("/api/records/waypoints?limit=2000");
      waypoints = out.items || [];
      wpPanel.querySelector(".sub").textContent = `${waypoints.length} plotted`;
      renderWaypoints();
      draw();
    } catch (err) { toast(err.message, "error"); }
  }

  function renderWaypoints() {
    const q = search.value.trim().toLowerCase();
    const list = q ? waypoints.filter((w) => `${w.name} ${w.type} ${w.note}`.toLowerCase().includes(q)) : waypoints;
    wpList.replaceChildren();
    if (!list.length) {
      wpList.appendChild(h("div", { class: "small muted", text: q ? "No matching waypoints." : "No waypoints. Click the map in PLACE mode or use NEW." }));
      return;
    }
    for (const wp of list.slice(0, 120)) {
      const item = h("div", { class: "wp-item", role: "button", tabindex: "0" });
      const dot = h("span", { class: "wp-dot", style: { borderColor: TYPE_COLOR[wp.type] || "#5dff9a" } });
      item.appendChild(dot);
      const main = h("div", { class: "grow" });
      main.appendChild(h("div", { class: "wp-name", text: wp.name }));
      main.appendChild(h("div", { class: "wp-coord", text: `${Number(wp.lat).toFixed(5)}, ${Number(wp.lon).toFixed(5)}${wp.type ? ` · ${wp.type}` : ""}` }));
      item.appendChild(main);
      const del = iconBtn("trash", "Delete waypoint", async (e) => {
        e.stopPropagation();
        const ok = await deleteRecord("waypoints", wp.id, wp.name, () => { if (selected?.id === wp.id) selected = null; reloadWaypoints(); });
        if (!ok) return;
      });
      del.classList.add("wp-del");
      item.appendChild(del);
      item.addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        selectWaypoint(wp);
      });
      item.addEventListener("dblclick", () => {
        openRecordForm({ table: "waypoints", item: wp, onSaved: () => reloadWaypoints() });
      });
      item.addEventListener("keydown", (e) => { if (e.key === "Enter") selectWaypoint(wp); });
      wpList.appendChild(item);
    }
    if (list.length > 120) wpList.appendChild(h("div", { class: "tiny dim", text: `+${list.length - 120} more — refine the filter` }));
  }

  function intelAge(hours) {
    if (hours === null || hours === undefined || Number.isNaN(Number(hours))) return "age unknown";
    const h = Number(hours);
    if (h < 1) return `${Math.max(1, Math.round(h * 60))}m ago`;
    if (h < 24) return `${Math.round(h)}h ago`;
    return `${Math.round(h / 24)}d ago`;
  }

  function renderIntel() {
    const maxAge = Number(intelMaxAgeHours)||0;
    const rows = [...intelFeatures].filter((f)=>{ if(maxAge<=0) return true; const a=f.properties?.age_hours; return a==null || Number(a)<=maxAge; }).sort((a, b) => {
      const aa = a.properties?.age_hours, bb = b.properties?.age_hours;
      if (aa == null && bb == null) return 0;
      if (aa == null) return 1;
      if (bb == null) return -1;
      return aa - bb;
    });
    intelList.replaceChildren();
    if (!rows.length) {
      intelList.appendChild(h("div", { class: "small muted", text: "No current points intel cached. Refresh feeds while online." }));
    } else {
      for (const f of rows.slice(0, 12)) {
        const p = f.properties || {};
        const conflict = p.kind === "conflict" || p.feed_id === "gdelt_conflict" || p.feed_id === "ucdp_candidate";
        const color = conflict ? (p.verified ? "#ff8a3d" : "#ffb02e") : p.severity === "CRITICAL" ? "#ff5c5c" : "#5dff9a";
        const item = h("div", { class: "wp-item", role: "button", tabindex: "0" });
        item.appendChild(h("span", { class: "wp-dot", style: { borderColor: color } }));
        const main = h("div", { class: "grow" });
        main.appendChild(h("div", { class: "wp-name", text: String(p.title || "Event").slice(0, 110) }));
        main.appendChild(h("div", { class: "wp-coord", text: `${p.source || "?"} · ${intelAge(p.age_hours)}${p.place ? ` · ${p.place}` : ""}${p.verified ? " · VERIFIED" : ""}` }));
        item.appendChild(main);
        const selectItem = () => {
          const lon = Number(f.geometry?.coordinates?.[0]), lat = Number(f.geometry?.coordinates?.[1]);
          if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
          selectedIntel = { kind: 'event', data: p, feature: f };
          selected = null;
          view.lat = lat; view.lon = lon; view.zoom = Math.max(view.zoom, 5);
          renderIntelDetails();
          draw();
        };
        item.addEventListener("click", selectItem);
        item.addEventListener("keydown", (e) => { if (e.key === "Enter") selectItem(); });
        intelList.appendChild(item);
      }
      if (rows.length > 12) intelList.appendChild(h("div", { class: "tiny dim", text: `+${rows.length - 12} more mapped points` }));
    }
    const s = intelSummaryData || {};
    const newest = s.newest_age_hours;
    intelPanel.querySelector(".sub").textContent = `${rows.length} mapped · newest ${intelAge(newest)}`;
    intelSummaryEl.textContent = `MAP INTEL: ${s.hazards ?? 0} hazard · ${s.conflict ?? 0} conflict (${s.verified_conflict ?? 0} verified) · maritime ${s.maritime ?? (intelMaritime||[]).length} · cables ${s.cables ?? (intelCables||[]).length} · cctv ${s.cctv ?? (intelCctv||[]).length} · newest ${intelAge(newest)}`;
    intelSummaryEl.style.color = newest != null && newest > 6 ? "#ffb02e" : "";
  }

  function pickIntel(px, py) {
    let best = null, bestD = 196; // 14px radius
    const test = (x, y, kind, data, feature) => {
      const d = (x-px)*(x-px)+(y-py)*(y-py);
      if (d < bestD) { bestD = d; best = { kind, data, feature }; }
    };
    if (visible.intelAircraft) for (const a of (intelAircraft||[])) { const sp = latLonToScreen(Number(a.lat),Number(a.lon)); test(sp.x,sp.y,'aircraft',a,null); }
    if (visible.intelSats) for (const sat of (intelSats||[])) { const sp = latLonToScreen(Number(sat.lat),Number(sat.lon)); test(sp.x,sp.y,'sat',sat,null); }
    if (visible.intelMaritime) for (const m of (intelMaritime||[])) { const sp = latLonToScreen(Number(m.lat),Number(m.lon)); test(sp.x,sp.y,'maritime',m,null); }
    if (visible.intelCables) for (const c of (intelCables||[])) { const sp = latLonToScreen(Number(c.lat),Number(c.lon)); test(sp.x,sp.y,'cable',c,null); }
    if (visible.intelCctv) for (const c of (intelCctv||[])) { if (visible.cctvShowAlpr === false && c && c.alpr) continue; const sp = latLonToScreen(Number(c.lat),Number(c.lon)); test(sp.x,sp.y,'cctv',c,null); }
    const maxAge = Number(intelMaxAgeHours) || 0;
    for (const f of (intelFeatures||[])) {
      const g=f.geometry; if(!g||g.type!=='Point') continue;
      const p=f.properties||{};
      const conflict = p.kind==='conflict'||p.feed_id==='gdelt_conflict'||p.feed_id==='ucdp_candidate';
      if (conflict && !visible.intelConflict) continue;
      if (!conflict && !visible.intelEvents) continue;
      if (maxAge > 0 && p.age_hours != null && Number(p.age_hours) > maxAge) continue;
      const sp = latLonToScreen(Number(g.coordinates[1]), Number(g.coordinates[0]));
      test(sp.x,sp.y,'event',p,f);
    }
    return best;
  }

  function pickCluster(px, py) {
    let best = null, bestD = Infinity;
    for (const c of (_clusterHitRegions || [])) {
      if (!c) continue;
      if (c.kind === 'cctv' && !visible.intelCctv) continue;
      if (c.kind === 'cable' && !visible.intelCables) continue;
      if (c.kind === 'maritime' && !visible.intelMaritime) continue;
      const d = (c.x - px) * (c.x - px) + (c.y - py) * (c.y - py);
      const rr = (c.r || 12) * (c.r || 12);
      if (d <= rr && d < bestD) { bestD = d; best = c; }
    }
    return best;
  }

  function selectedIntelLatLon(sel = selectedIntel) {
    if (!sel) return null;
    if (sel.kind === 'event') {
      const c = sel.feature?.geometry?.coordinates;
      if (Array.isArray(c) && Number.isFinite(Number(c[0])) && Number.isFinite(Number(c[1]))) return { lat: Number(c[1]), lon: Number(c[0]) };
      if (Number.isFinite(Number(sel.data?.lat)) && Number.isFinite(Number(sel.data?.lon))) return { lat: Number(sel.data.lat), lon: Number(sel.data.lon) };
      return null;
    }
    const lat = Number(sel.data?.lat), lon = Number(sel.data?.lon);
    return (Number.isFinite(lat) && Number.isFinite(lon)) ? { lat, lon } : null;
  }

  function hideMapPopup() {
    mapPopup.hidden = true;
    mapPopup.replaceChildren();
    mapPopup.style.left = ''; mapPopup.style.top = '';
  }

  function positionMapPopup() {
    if (!mapPopup || mapPopup.hidden || !selectedIntel) return;
    const ll = selectedIntelLatLon(selectedIntel);
    if (!ll) { hideMapPopup(); return; }
    const sp = latLonToScreen(ll.lat, ll.lon);
    const w = canvas.clientWidth || 800, hh = canvas.clientHeight || 500;
    if (sp.x < -40 || sp.y < -40 || sp.x > w + 40 || sp.y > hh + 40) { hideMapPopup(); return; }
    const pw = mapPopup.offsetWidth || 260, ph = mapPopup.offsetHeight || 120;
    let left = sp.x + 14, top = sp.y - 14;
    if (left + pw > w - 8) left = sp.x - pw - 14;
    if (top + ph > hh - 8) top = hh - ph - 8;
    if (left < 8) left = 8;
    if (top < 8) top = 8;
    mapPopup.style.left = `${Math.round(left)}px`;
    mapPopup.style.top = `${Math.round(top)}px`;
  }

  function showMapPopup(sel) {
    if (!sel || !mapPopup) { hideMapPopup(); return; }
    const ll = selectedIntelLatLon(sel);
    if (!ll) { hideMapPopup(); return; }
    const s = latLonToScreen(ll.lat, ll.lon);
    const w = canvas.clientWidth || 800, hh = canvas.clientHeight || 500;
    if (s.x < -40 || s.y < -40 || s.x > w + 40 || s.y > hh + 40) { hideMapPopup(); return; }
    const d = sel.data || {};
    const isConflict = d.kind === 'conflict' || d.feed_id === 'gdelt_conflict' || d.feed_id === 'ucdp_candidate';
    const kicker = sel.kind === 'aircraft' ? 'AIRCRAFT' : sel.kind === 'sat' ? 'SATELLITE' : sel.kind === 'maritime' ? 'MARITIME / AIS' : sel.kind === 'cable' ? 'CABLE / LANDING POINT' : sel.kind === 'cctv' ? 'CCTV / CAMERA (OSM)' : (isConflict ? (d.verified ? 'CONFLICT · VERIFIED' : 'CONFLICT · LIVE') : 'HAZARD / EVENT');
    const title = sel.kind === 'aircraft' ? (d.callsign || d.title || 'Aircraft')
      : sel.kind === 'sat' ? (d.name || `NORAD ${d.norad || '?'}`)
      : sel.kind === 'maritime' ? (d.title || d.vessel || d.mmsi || 'Vessel')
      : sel.kind === 'cable' ? (d.title || d.cable_id || 'Cable point')
      : sel.kind === 'cctv' ? (d.title || d.camera_id || 'Camera')
      : (d.title || 'Event');
    const meta = [];
    if (sel.kind === 'event') {
      if (d.source) meta.push(d.source);
      if (d.severity) meta.push(d.severity);
      if (d.time) meta.push(fmt.date(d.time));
      if (d.age_hours != null) meta.push(intelAge(d.age_hours));
      if (d.place) meta.push(d.place);
      if (d.mag != null) meta.push(`M${d.mag}`);
      if (d.fatalities != null) meta.push(`${d.fatalities} killed`);
      if (d.alt_m != null || d.altitude_m != null) meta.push(`${Math.round(d.alt_m ?? d.altitude_m)} m`);
    } else if (sel.kind === 'aircraft') {
      if (d.source) meta.push(d.source);
      if (d.alt_m != null) meta.push(`${Math.round(d.alt_m)} m`);
      if (d.speed_kmh != null) meta.push(`${Math.round(d.speed_kmh)} km/h`);
      if (d.heading_deg != null) meta.push(`${Math.round(d.heading_deg)}°`);
    } else if (sel.kind === 'sat') {
      if (d.norad) meta.push(`NORAD ${d.norad}`);
      if (d.alt_km != null) meta.push(`${Math.round(d.alt_km)} km`);
      if (d.elevation_deg != null) meta.push(`el ${Math.round(d.elevation_deg)}°`);
    } else if (sel.kind === 'maritime') {
      if (d.mmsi) meta.push(`MMSI ${d.mmsi}`);
      if (d.callsign) meta.push(d.callsign);
      if (d.speed_kn != null) meta.push(`${Number(d.speed_kn).toFixed(1)} kn`);
      if (d.course_deg != null) meta.push(`${Math.round(d.course_deg)}°`);
      if (d.source) meta.push(d.source);
    } else if (sel.kind === 'cable') {
      if (d.cable_id) meta.push(d.cable_id);
      if (d.operator) meta.push(d.operator);
      if (d.source) meta.push(d.source);
    } else if (sel.kind === 'cctv') {
      if (d.alpr) meta.push('ALPR/ANPR');
      if (d.flock_tagged) meta.push('Flock-tagged (OSM)');
      if (d.surveillance_type) meta.push(d.surveillance_type);
      if (d.brand) meta.push(d.brand);
      if (d.direction) meta.push(`${d.direction}° dir`);
      if (d.status) meta.push(d.status);
      if (d.camera_id) meta.push(`OSM ${d.camera_id}`);
      if (d.source) meta.push(d.source);
    }
    const closeBtn = h('button', { class: 'mp-close', type: 'button', title: 'Close', text: '×', onclick: (e) => { e.stopPropagation(); selectedIntel = null; renderIntelDetails(); draw(); } });
    const actions = h('div', { class: 'mp-actions' },
      btn('ZOOM', { size: 'sm', tone: 'ghost', onClick: (e) => { e?.stopPropagation?.(); if (ll) { view.lat = ll.lat; view.lon = ll.lon; view.zoom = Math.max(view.zoom, sel.kind === 'sat' ? 4 : 7); draw(); } } }),
      btn('COPY COORD', { size: 'sm', tone: 'ghost', onClick: async (e) => {
        e?.stopPropagation?.();
        const text = `${ll.lat.toFixed(6)}, ${ll.lon.toFixed(6)}`;
        try { await navigator.clipboard?.writeText(text); toast('Coordinates copied'); } catch { toast(text, 'warn'); }
      } }),
    );
    if (d.url) actions.appendChild(h('a', { class: 'badge cyan', href: d.url, target: '_blank', rel: 'noopener noreferrer', text: 'SOURCE', onclick: (e)=>e.stopPropagation() }));
    mapPopup.replaceChildren(
      closeBtn,
      h('div', { class: 'mp-kicker', text: kicker }),
      h('div', { class: 'mp-title', text: String(title).slice(0, 180) }),
      meta.length ? h('div', { class: 'mp-meta', text: meta.filter(Boolean).join(' · ').slice(0, 260) }) : null,
      d.summary && sel.kind === 'event' ? h('div', { class: 'mp-meta', style: { marginTop: '6px', color: 'var(--text-dim)' }, text: String(d.summary).slice(0, 180) }) : null,
      actions,
    );
    mapPopup.hidden = false;
    // Position after layout
    requestAnimationFrame(() => {
      if (mapPopup.hidden) return;
      const pw = mapPopup.offsetWidth || 260, ph = mapPopup.offsetHeight || 120;
      let left = s.x + 14, top = s.y - 14;
      if (left + pw > w - 8) left = s.x - pw - 14;
      if (top + ph > hh - 8) top = hh - ph - 8;
      if (left < 8) left = 8;
      if (top < 8) top = 8;
      mapPopup.style.left = `${Math.round(left)}px`;
      mapPopup.style.top = `${Math.round(top)}px`;
    });
  }

  function renderIntelDetails() {
    if (livePreviewTimer) { clearInterval(livePreviewTimer); livePreviewTimer = null; }
    let details = side.querySelector('#intel-details');
    if (!details) { details = h('div', { id: 'intel-details', class: 'mt-8' }); intelPanel.querySelector('.panel-body .col')?.appendChild(details); }
    if (!selectedIntel) { details.replaceChildren(); hideMapPopup(); return; }
    const d = selectedIntel.data || {};
    const rows = [];
    const push = (k,v) => { if(v!=null&&v!=='') rows.push([k,String(v)]); };
    if (selectedIntel.kind === 'event') {
      push('TITLE', d.title); push('SOURCE', d.source); push('SEVERITY', d.severity);
      push('TIME', d.time ? fmt.date(d.time) : ''); push('AGE', intelAge(d.age_hours)); push('PLACE', d.place);
      push('COUNTRY', d.country); push('PRECISION', d.precision); push('VERIFIED', d.verified ? 'YES (UCDP)' : 'NO');
      if (d.mag!=null) push('MAGNITUDE', d.mag); if (d.fatalities!=null) push('FATALITIES', d.fatalities);
      if (d.depth_km!=null) push('DEPTH', `${d.depth_km} km`); if (d.mentions!=null) push('MENTIONS', d.mentions);
      if (d.summary) push('SUMMARY', String(d.summary).slice(0,300));
      const ll = selectedIntelLatLon();
      if (ll) push('COORDINATES', `${ll.lat.toFixed(5)}, ${ll.lon.toFixed(5)}`);
    } else if (selectedIntel.kind === 'aircraft') {
      push('CALLSIGN', d.callsign || d.title); push('SOURCE', d.source || 'OpenSky');
      if (d.alt_m!=null) push('ALTITUDE', `${Math.round(d.alt_m)} m`); if (d.speed_kmh!=null) push('SPEED', `${Math.round(d.speed_kmh)} km/h`);
      if (d.heading_deg!=null) push('HEADING', `${Math.round(d.heading_deg)}°`); push('TIME', d.time ? fmt.date(d.time) : '');
      const ll = selectedIntelLatLon(); if (ll) push('COORDINATES', `${ll.lat.toFixed(5)}, ${ll.lon.toFixed(5)}`);
    } else if (selectedIntel.kind === 'sat') {
      push('NAME', d.name); push('NORAD', d.norad); if (d.alt_km!=null) push('ALTITUDE', `${Math.round(d.alt_km)} km`);
      if (d.elevation_deg!=null) push('ELEVATION', `${Math.round(d.elevation_deg)}°`); push('TIME', d.time ? fmt.date(d.time) : '');
      const ll = selectedIntelLatLon(); if (ll) push('COORDINATES', `${ll.lat.toFixed(5)}, ${ll.lon.toFixed(5)}`);
    } else if (selectedIntel.kind === 'maritime') {
      push('TITLE', d.title || d.vessel); push('MMSI', d.mmsi); push('CALLSIGN', d.callsign);
      if (d.speed_kn!=null) push('SPEED', `${Number(d.speed_kn).toFixed(1)} kn`);
      if (d.course_deg!=null) push('COURSE', `${Math.round(d.course_deg)}°`);
      if (d.heading_deg!=null) push('HEADING', `${Math.round(d.heading_deg)}°`);
      push('SOURCE', d.source); push('TIME', d.time ? fmt.date(d.time) : '');
      const ll = selectedIntelLatLon(); if (ll) push('COORDINATES', `${ll.lat.toFixed(5)}, ${ll.lon.toFixed(5)}`);
    } else if (selectedIntel.kind === 'cable') {
      push('TITLE', d.title || d.cable_id); push('CABLE ID', d.cable_id); push('OPERATOR', d.operator);
      if (d.capacity) push('CAPACITY', d.capacity);
      push('SOURCE', d.source); if (d.summary) push('SUMMARY', String(d.summary).slice(0,300));
      const ll = selectedIntelLatLon(); if (ll) push('COORDINATES', `${ll.lat.toFixed(5)}, ${ll.lon.toFixed(5)}`);
    } else if (selectedIntel.kind === 'cctv') {
      push('TITLE', d.title || d.camera_id);
      push('TYPE', d.feed_type || d.surveillance_type || d.kind);
      push('ALPR/ANPR', d.alpr ? 'YES (OSM-tagged ALPR/ANPR; locations from OSM, not live tracking)' : 'NO');
      if (d.flock_tagged) push('FLOCK TAG (OSM)', 'OSM tags mention Flock (community-mapped; verify on OpenStreetMap)');
      push('SURVEILLANCE TYPE', d.surveillance_type);
      push('CAMERA TYPE', d.camera_type);
      push('BRAND/MANUFACTURER', d.brand);
      push('OPERATOR', d.operator_name || d.operator);
      push('DIRECTION', d.direction != null && d.direction !== '' ? `${d.direction}°` : (d.heading_deg != null ? `${Math.round(Number(d.heading_deg))}°` : ''));
      push('ZONE', d.surveillance_zone);
      push('CAMERA ID', d.camera_id || d.id);
      push('STATUS', d.status);
      push('SOURCE', d.source || d.provider);
      push('PROVIDER', d.provider && d.provider !== d.source ? d.provider : '');
      push('CREDIT', d.credit);
      push('LICENSE', d.license);
      push('REGION', [d.city, d.region].filter(Boolean).join(', '));
      push('IMPORT', d.import_source);
      if (d.summary) push('TAGS/SUMMARY', String(d.summary).slice(0,400));
      push('TIME', d.time ? fmt.date(d.time) : '');
      const ll = selectedIntelLatLon(); if (ll) push('COORDINATES', `${ll.lat.toFixed(5)}, ${ll.lon.toFixed(5)}`);
    }
    const box = h('div', { class: 'panel', style: { padding: '8px 10px', borderColor: 'var(--line-strong)' } });
    box.appendChild(h('div', { class: 'row between items-center mb-6 gap-6' },
      h('div', { class: 'side-label', text: `SELECTED ${selectedIntel.kind.toUpperCase()}` }),
      h('div', { class: 'row gap-4' },
        btn('ZOOM', { size:'sm', tone:'ghost', onClick: ()=>{ const ll=selectedIntelLatLon(); if(ll){ view.lat=ll.lat; view.lon=ll.lon; view.zoom=Math.max(view.zoom, selectedIntel.kind==='sat'?4:7); draw(); } } }),
        btn('CLEAR', { size:'sm', tone:'ghost', onClick: ()=>{ selectedIntel=null; renderIntelDetails(); draw(); } }),
      )));
    if (selectedIntel.kind === 'cctv' && d.id) {
      const feedType = String(d.feed_type || d.feedType || '').toLowerCase();
      const rawRefresh = Number(d.frame_refresh_ms || d.frameRefreshMs || 0);
      const refreshSec = Math.max(10, Math.min(300, rawRefresh > 0 ? Math.round(rawRefresh / 1000) : 30));
      const hasStream = !!(d.stream_url || feedType === 'hls' || feedType === 'mp4' || feedType === 'm3u8' || feedType === 'video');
      const mediaWrap = h('div', { class: 'mt-6', style: { border: '1px solid var(--line)', borderRadius: '6px', overflow: 'hidden', background: 'rgba(0,0,0,.25)' } });
      const msg = h('div', { class: 'tiny dim', style: { padding: '6px 8px' }, text: '' });
      let live = false;
      let videoMode = false;
      const img = h('img', {
        alt: d.title || 'Camera preview',
        loading: 'eager',
        referrerPolicy: 'no-referrer',
        style: { display: 'block', width: '100%', maxHeight: '220px', objectFit: 'cover', background: '#08110c' },
      });
      const setImg = (force=false) => {
        if (videoMode) return;
        img.src = `/api/cctv/frame?id=${encodeURIComponent(String(d.id))}${force ? '&force=1' : ''}&t=${Date.now()}`;
      };
      const setLive = (on) => {
        live = !!on;
        if (livePreviewTimer) { clearInterval(livePreviewTimer); livePreviewTimer = null; }
        if (live) {
          livePreviewTimer = setInterval(() => setImg(false), refreshSec * 1000);
          liveBtn.text = `LIVE ON (${refreshSec}s)`;
          liveBtn.classList.add('cyan');
          msg.text = d.credit ? `Live preview (auto-refresh ${refreshSec}s, registered proxy) · ${String(d.credit).slice(0,90)}` : `Live preview (auto-refresh ${refreshSec}s, registered proxy).`;
        } else {
          liveBtn.text = 'LIVE PREVIEW';
          liveBtn.classList.remove('cyan');
          msg.text = d.credit ? `Snapshot: ${String(d.credit).slice(0,120)}` : 'Snapshot preview (registered source).';
        }
      };
      img.onerror = () => {
        if (videoMode) return;
        msg.text = hasStream ? `No proxied image preview. Use LOAD STREAM for registered ${feedType.toUpperCase() || 'video'} playback when supported.` : 'No snapshot preview available for this camera (or source blocked/unavailable).';
      };
      img.onload = () => { if (!live && !videoMode) msg.text = d.credit ? `Snapshot: ${String(d.credit).slice(0,120)}` : 'Snapshot preview (registered source).'; };
      setImg(false);

      async function loadStream() {
        if (videoMode) return;
        streamBtn.disabled = true; streamBtn.text = 'LOADING…';
        try {
          const info = await api.get(`/api/cctv/stream/info?id=${encodeURIComponent(String(d.id))}`);
          const proxied = info && info.proxied_url;
          const stype = String((info && info.feed_type) || feedType || '').toLowerCase();
          if (!proxied) { toast('No registered live stream for this camera; snapshot preview is available.', 'warn'); msg.text = 'No registered live stream URL. Use snapshot/live preview or open source link.'; return; }
          const video = h('video', { controls: true, playsInline: true, muted: true, autoplay: true, preload: 'metadata',
            style: { display: 'block', width: '100%', maxHeight: '260px', background: '#000' } });
          const nativeHls = video.canPlayType('application/vnd.apple.mpegurl');
          if (stype === 'hls' || stype === 'm3u8') {
            if (nativeHls) {
              video.src = proxied;
            } else {
              let Hls = window.Hls;
              if (!Hls || !Hls.isSupported || !Hls.isSupported()) {
                try {
                  if (!window.__darksecHlsLoading) {
                    window.__darksecHlsLoading = new Promise((resolve, reject) => {
                      const s = document.createElement('script');
                      s.src = '/ui/vendor/hls/hls.min.js?v=1';
                      s.async = true;
                      s.onload = () => resolve(window.Hls);
                      s.onerror = () => reject(new Error('Failed to load hls.js'));
                      document.head.appendChild(s);
                    });
                  }
                  Hls = await window.__darksecHlsLoading;
                } catch { Hls = window.Hls; } finally { window.__darksecHlsLoading = null; }
              }
              if (Hls && Hls.isSupported && Hls.isSupported()) {
                const hls = new Hls({ lowLatencyMode: false, backBufferLength: 30, maxBufferLength: 20 });
                hls.loadSource(proxied);
                hls.attachMedia(video);
                hls.on(Hls.Events.ERROR, (_, data) => { if (data && data.fatal) msg.text = `HLS error: ${data.type || 'fatal'}. Try snapshot preview or source link.`; });
              } else {
                video.src = proxied;
                msg.text = 'HLS stream requested via registered proxy. hls.js could not be loaded; try a browser with native HLS support.';
              }
            }
          } else {
            video.src = proxied;
          }
          video.onerror = () => { msg.text = `Stream playback failed in this browser. ${stype === 'hls' || stype === 'm3u8' ? 'HLS may require native support or hls.js.' : ''} Use snapshot preview or open source.`; };
          if (livePreviewTimer) { clearInterval(livePreviewTimer); livePreviewTimer = null; live = false; liveBtn.text = 'LIVE PREVIEW'; liveBtn.classList.remove('cyan'); }
          videoMode = true;
          img.replaceWith(video);
          msg.text = `Live stream (registered proxy${info.credit ? ` · ${String(info.credit).slice(0,70)}` : ''}).`;
          streamBtn.text = 'STREAM LOADED';
        } catch (err) {
          toast(err.message || 'Could not load stream', 'error');
          msg.text = `Stream load failed: ${String(err.message || err).slice(0,120)}`;
        } finally {
          if (!videoMode) { streamBtn.disabled = false; streamBtn.text = 'LOAD STREAM'; }
        }
      }

      const liveBtn = btn('LIVE PREVIEW', { size: 'sm', tone: 'ghost', onClick: () => setLive(!live) });
      const streamBtn = btn('LOAD STREAM', { size: 'sm', tone: 'ghost', onClick: loadStream, disabled: !hasStream });
      const controls = h('div', { class: 'row between items-center gap-6 wrap', style: { padding: '6px 8px', borderTop: '1px solid var(--line)' } },
        h('div', { class: 'tiny dim', text: feedType ? `Feed: ${feedType.toUpperCase()}` : 'Feed: snapshot/image' }),
        h('div', { class: 'row gap-4 wrap' }, liveBtn, hasStream ? streamBtn : null));
      mediaWrap.append(img, msg, controls);
      if (hasStream) {
        mediaWrap.appendChild(h('div', { class: 'tiny dim', style: { padding: '0 8px 8px' }, text: `Stream URLs are proxied only when registered in catalog/feed data (same-host subresources, bounded). HLS may require native browser support or vendored hls.js.` }));
      }
      box.appendChild(mediaWrap);
      if (d.license) box.appendChild(h('div', { class: 'tiny dim mt-4', text: `License/terms: ${String(d.license).slice(0,180)}` }));
    }
    const kv = h('div', { class: 'kv compact mt-6' });
    for (const [k,v] of rows) { kv.appendChild(h('span',{class:'k',text:k})); kv.appendChild(h('span',{class:'v',text:v})); }
    box.appendChild(kv);
    const actions = h('div', { class: 'row gap-6 mt-6 wrap' });
    if (d.url) actions.appendChild(h('a',{class:'badge cyan',href:d.url,target:'_blank',rel:'noopener noreferrer',text:'OPEN SOURCE'}));
    if (selectedIntel.kind === 'cctv' && d.viewer_url && d.viewer_url !== d.url) actions.appendChild(h('a',{class:'badge',href:d.viewer_url,target:'_blank',rel:'noopener noreferrer',text:'VIEWER'}));
    const ll = selectedIntelLatLon();
    if (ll) actions.appendChild(btn('COPY COORDS', { size:'sm', tone:'ghost', onClick: async () => { try { await navigator.clipboard?.writeText(`${ll.lat.toFixed(6)}, ${ll.lon.toFixed(6)}`); toast('Coordinates copied'); } catch { toast(`${ll.lat.toFixed(6)}, ${ll.lon.toFixed(6)}`, 'warn'); } } }));
    if (actions.childNodes.length) box.appendChild(actions);
    details.replaceChildren(box);
    showMapPopup(selectedIntel);
  }

  function updateReadout(latlon, screen = null) {
    if (!latlon) return;
    if (screen) hover = screen;
    const key = `${latlon.lat.toFixed(5)}|${latlon.lon.toFixed(5)}`;
    if (key === lastReadout) return;
    lastReadout = key;
    readout.replaceChildren();
    readout.appendChild(h("div", {}, "LAT ", h("b", { text: latlon.lat.toFixed(5) })));
    readout.appendChild(h("div", {}, "LON ", h("b", { text: latlon.lon.toFixed(5) })));
    const mgrs = h("div", { class: "mgrs", text: "MGRS …" });
    readout.appendChild(mgrs);
    clearTimeout(readoutTimer);
    readoutTimer = setTimeout(async () => {
      try {
        const c = await api.get(`/api/map/coords?lat=${latlon.lat}&lon=${latlon.lon}`);
        mgrs.textContent = c.mgrs;
        mgrs.title = c.utm;
      } catch { mgrs.textContent = "MGRS unavailable"; }
    }, 160);
    if (selected) {
      const d = distanceM(latlon, { lat: Number(selected.lat), lon: Number(selected.lon) });
      const brg = bearingDeg({ lat: Number(selected.lat), lon: Number(selected.lon) }, latlon);
      readout.appendChild(h("div", { class: "tiny", text: `${selected.name} · ${fmt.num(d / 1000, 2)} km · ${fmt.num(brg, 0)}°` }));
    }
  }

  /* --------------------------------------------------------------- events */
  canvas.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    canvas.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, y: e.clientY, lat: view.lat, lon: view.lon };
    moved = false;
    canvas.classList.add("dragging");
  });
  let lastHoverCheck = 0;
  canvas.addEventListener("pointermove", (e) => {
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
      if (!moved) return;
      const c = centerWorld();
      const scale = c.scale;
      const startP = project(drag.lat, drag.lon);
      const nx = startP.x * scale - dx;
      const ny = startP.y * scale - dy;
      const ll = unproject(nx / scale, ny / scale);
      view.lat = clamp(ll.lat, -85, 85);
      view.lon = wrapLon(ll.lon);
      draw();
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const ipx = e.clientX - rect.left, ipy = e.clientY - rect.top;
    const ll = screenToLatLon(ipx, ipy);
    updateReadout(ll, { x: ipx, y: ipy });
    if (!placeMode && !measureMode) {
      const now = performance.now ? performance.now() : Date.now();
      if (now - lastHoverCheck > 50) {
        lastHoverCheck = now;
        let hit = !!pickIntel(ipx, ipy) || !!pickCluster(ipx, ipy);
        if (!hit) {
          for (const wp of waypoints) {
            const sp = latLonToScreen(wp.lat, wp.lon);
            const d = (sp.x - ipx) ** 2 + (sp.y - ipy) ** 2;
            if (d < 144) { hit = true; break; }
          }
        }
        canvas.classList.toggle("hover-intel", hit);
      }
    } else {
      canvas.classList.remove("hover-intel");
    }
  });
  canvas.addEventListener("pointerup", (e) => {
    const wasMoved = moved;
    drag = null;
    canvas.classList.remove("dragging");
    if (wasMoved) return;
    const rect = canvas.getBoundingClientRect();
    const ll = screenToLatLon(e.clientX - rect.left, e.clientY - rect.top);
    if (placeMode) {
      placeMode = false;
      canvas.classList.remove("picking");
      shell.querySelectorAll(".map-tools button").forEach((b) => b.classList.toggle("active", false));
      updateModeHud();
      addWaypoint(ll);
      return;
    }
    if (measureMode) {
      measurePoints.push(ll);
      draw();
      updateReadout(ll);
      return;
    }
    // Select nearest intel item, cluster (zoom), then waypoint
    const ipx = e.clientX - rect.left, ipy = e.clientY - rect.top;
    const intelHit = pickIntel(ipx, ipy);
    const clusterHit = (!intelHit && visible.clusterDense !== false) ? pickCluster(ipx, ipy) : null;
    let best = null, bestD = 144;
    for (const wp of waypoints) {
      const sp = latLonToScreen(wp.lat, wp.lon);
      const d = (sp.x - ipx) ** 2 + (sp.y - ipy) ** 2;
      if (d < bestD) { best = wp; bestD = d; }
    }
    if (clusterHit && (!best || bestD > 100)) {
      view.lat = clamp(clusterHit.lat, -85, 85); view.lon = wrapLon(clusterHit.lon);
      view.zoom = Math.min(18, Math.max(view.zoom + 1.5, clusterHit.zoomTo || view.zoom + 2));
      draw();
    } else if (intelHit && (!best || bestD > 100)) {
      selectedIntel = intelHit;
      selected = null;
      renderIntelDetails();
      draw();
    } else if (best) {
      selectedIntel = null;
      renderIntelDetails();
      selectWaypoint(best);
    } else {
      selectedIntel = null; selected = null;
      renderIntelDetails(); renderWaypoints(); draw();
    }
    updateReadout(ll);
  });
  canvas.addEventListener("pointerleave", () => { if (!drag) hover = null; canvas.classList.remove("hover-intel"); });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    zoomAt(view.zoom + (e.deltaY < 0 ? 1 : -1), e.clientX - rect.left, e.clientY - rect.top);
  }, { passive: false });
  canvas.addEventListener("dblclick", (e) => {
    const rect = canvas.getBoundingClientRect();
    zoomAt(view.zoom + 1, e.clientX - rect.left, e.clientY - rect.top);
  });

  // Keyboard pan/zoom when the canvas has focus.
  canvas.tabIndex = 0;
  canvas.addEventListener("keydown", (e) => {
    const step = 0.15;
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) {
      e.preventDefault();
      const dx = e.key === "ArrowLeft" ? -100 : e.key === "ArrowRight" ? 100 : 0;
      const dy = e.key === "ArrowUp" ? -100 : e.key === "ArrowDown" ? 100 : 0;
      const c = centerWorld();
      const ll = unproject((project(view.lat, view.lon).x * c.scale - dx) / c.scale,
        (project(view.lat, view.lon).y * c.scale - dy) / c.scale);
      view.lat = clamp(ll.lat, -85, 85); view.lon = wrapLon(ll.lon); draw();
    } else if (e.key === "+" || e.key === "=") zoomBy(1);
    else if (e.key === "-") zoomBy(-1);
  });

  search.addEventListener("input", renderWaypoints);

  async function importGpx() {
    try {
      const file = await readTextFile(".gpx,.xml");
      if (!file) return;
      const out = await api.post("/api/map/gpx/import", { gpx: file.text });
      toast(`Imported ${out.waypoints || 0} waypoint(s), ${out.tracks || 0} track(s)`);
      const [w, t] = await Promise.all([
        api.get("/api/records/waypoints?limit=2000"),
        api.get("/api/records/tracks?limit=500"),
      ]);
      waypoints = w.items || []; tracks = t.items || [];
      wpPanel.querySelector(".sub").textContent = `${waypoints.length} plotted`;
      renderWaypoints(); fitWaypoints();
    } catch (err) { toast(err.message || "GPX import failed", "error"); }
  }

  /* ---------------------------------------------------------------- setup */
  try {
    const resp = await api.raw("/api/map/vector");
    world = await resp.json();
  } catch (err) {
    world = null;
    toast("Vector basemap unavailable — waypoints remain usable", "warn");
  }

  if (params.wp) {
    const wp = waypoints.find((w) => String(w.id) === String(params.wp));
    if (wp) { selected = wp; view.lat = Number(wp.lat); view.lon = Number(wp.lon); view.zoom = 12; }
  } else if (Number.isFinite(startLat) && Number.isFinite(startLon)) {
    view.lat = startLat; view.lon = startLon; view.zoom = 5;
  }
  if (params.lat && params.lon) {
    const la = Number(params.lat), lo = Number(params.lon);
    if (Number.isFinite(la) && Number.isFinite(lo)) { view.lat = la; view.lon = lo; view.zoom = Number(params.zoom) || 11; }
  }

  renderWaypoints();
  renderIntel();
  updateModeHud();
  hudCenter.textContent = `${view.lat.toFixed(4)} ${view.lon.toFixed(4)}`;
  if (coordNow) readout.appendChild(h("div", { class: "tiny", text: coordNow.mgrs || "" }));
  resizeObs = new ResizeObserver(() => draw());
  resizeObs.observe(shell);
  setTimeout(() => { draw(); updateReadout({ lat: view.lat, lon: view.lon }); }, 40);

  function onKey(e) {
    if (e.key === "Escape" && (measureMode || placeMode)) {
      measureMode = false; placeMode = false; measurePoints = [];
      canvas.classList.remove("picking"); updateModeHud(); draw();
    }
    if (e.key === "m" && !e.ctrlKey && !e.metaKey && document.activeElement?.tagName !== "INPUT") toggleMeasure();
  }
  document.addEventListener("keydown", onKey);

  return () => {
    disposed = true;
    if (raf) cancelAnimationFrame(raf);
    clearTimeout(readoutTimer);
    if (resizeObs) resizeObs.disconnect();
    document.removeEventListener("keydown", onKey);
    tileCache.clear();
  };
}

export const meta = { title: "Map", icon: "map" };
