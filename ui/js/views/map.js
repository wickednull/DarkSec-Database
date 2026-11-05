/* Offline map workspace.
   Vector basemap is bundled Natural Earth data; raster tiles come only from
   local files or an operator-supplied MBTiles archive. No tile server is
   contacted. Everything else is waypoints and tracks in the local database. */
import { api, download } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, pageHead, btn, iconBtn, icon, badge, toast, modal, field, input, select,
  emptyState, loading, fmt, alertStrip, checkbox, confirmDialog,
} from "../components.js";
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
  if (m <= 0) return { m: 10, label: "10 m" };
  const units = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000,
    50000, 100000, 200000, 500000, 1000000, 2000000, 5000000];
  for (const u of units) if (u >= m) return { m: u, label: u >= 1000 ? `${u / 1000} km` : `${u} m` };
  return { m: units[units.length - 1], label: `${units[units.length - 1] / 1000} km` };
}

export async function render(root, params = {}) {
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
  let intelSummaryData = null;
  let sunInfo = null;
  let declInfo = null;
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
    intelEvents: true, intelConflict: true, intelAircraft: true, intelSats: true, intelDayNight: true,
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
    desc: `Bundled vector terrain, local raster tiles, waypoints/tracks and all cached points intelligence. Position marker: ${positionInfo.mapLabel}. Conflict diamonds are monthly UCDP verified or live media-derived GDELT reports; both can be imprecise. Works offline.`,
    actions: [
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
  cacheStatusEl = h("div", { class: "map-cache-status tiny muted", text: cacheStatusText() });
  const intelSummaryEl = h("div", { class: "map-cache-status tiny", text: "Intel not loaded" });
  side.appendChild(panel({
    title: "Layers", icon: "layers",
    body: h("div", { class: "col" },
      layerRows,
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
      ["Night side", "rgba(0,0,0,.55)", "square"],
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
    if (visible.intelDayNight) drawIntelDayNight(ctx, w, hh);
    drawPosition(ctx, w, hh);
    drawMeasure(ctx, w, hh);
    updateScale(w, hh);
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
      { value: "5", label: "5 km radius" }, { value: "10", label: "10 km radius" },
      { value: "25", label: "25 km radius" }, { value: "50", label: "50 km radius" },
      { value: "100", label: "100 km radius" }, { value: "viewport", label: "Current viewport only" },
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
    for (const f of intelFeatures) {
      const g = f.geometry;
      if (!g || g.type !== "Point") continue;
      const p = f.properties || {};
      if (p.kind === "conflict" || p.feed_id === "gdelt_conflict" || p.feed_id === "ucdp_candidate") continue;
      const lat = g.coordinates[1], lon = g.coordinates[0];
      if (lat == null || lon == null) continue;
      intelPoint(ctx, vw, vh, lat, lon, p.severity, p.mag);
    }
  }

  function drawIntelConflict(ctx, vw, vh) {
    for (const f of intelFeatures) {
      const g = f.geometry;
      if (!g || g.type !== "Point") continue;
      const p = f.properties || {};
      if (!(p.kind === "conflict" || p.feed_id === "gdelt_conflict" || p.feed_id === "ucdp_candidate")) continue;
      const lat = Number(g.coordinates[1]), lon = Number(g.coordinates[0]);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const s = latLonToScreen(lat, lon);
      if (s.x < -20 || s.y < -20 || s.x > vw + 20 || s.y > vh + 20) continue;
      const age = Number(p.age_hours);
      const recent = !Number.isFinite(age) || age <= 24;
      const fatalities = Number(p.fatalities) || 0;
      const size = p.verified && fatalities > 0 ? 2.6 + Math.min(4.5, Math.sqrt(fatalities)) : 2.6;
      const color = p.severity === "CRITICAL" ? "#ff5c5c" : (p.verified ? "#ff8a3d" : "#ffb02e");
      ctx.save();
      ctx.globalAlpha = recent ? 0.95 : 0.5;
      ctx.translate(s.x, s.y);
      ctx.rotate(Math.PI / 4);
      ctx.fillStyle = color;
      ctx.fillRect(-size, -size, size * 2, size * 2);
      ctx.restore();
      if (view.zoom >= 7 && (recent || p.severity === "CRITICAL")) {
        ctx.globalAlpha = recent ? 0.95 : 0.6;
        ctx.font = "8px JetBrains Mono, monospace";
        ctx.textBaseline = "top";
        ctx.fillStyle = color;
        ctx.fillText(`${p.verified ? "VERIFIED" : "LIVE"}${fatalities ? ` ${fatalities} KILLED` : ""}`, s.x + 7, s.y + 2);
        ctx.globalAlpha = 1;
      }
    }
  }

  function drawIntelAircraft(ctx, w, hh) {
    for (const a of (intelAircraft || []).slice(0, 60)) {
      const s = latLonToScreen(Number(a.lat), Number(a.lon));
      if (s.x < -20 || s.y < -20 || s.x > w + 20 || s.y > hh + 20) continue;
      ctx.strokeStyle = "#48d7ff";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(s.x, s.y, 2.5, 0, Math.PI * 2); ctx.stroke();
      ctx.fillStyle = "rgba(72,215,255,.9)";
      ctx.beginPath(); ctx.arc(s.x, s.y, 1.6, 0, Math.PI * 2); ctx.fill();
    }
  }

  function drawIntelSats(ctx, w, hh) {
    for (const s of (intelSats || []).slice(0, 60)) {
      const lat = Number(s.lat), lon = Number(s.lon);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
      const s2 = latLonToScreen(lat, lon);
      if (s2.x < -20 || s2.y < -20 || s2.x > w + 20 || s2.y > hh + 20) continue;
      ctx.strokeStyle = "#c78bff";
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(s2.x, s2.y, 2, 0, Math.PI * 2); ctx.stroke();
      ctx.fillStyle = "rgba(199,139,255,.9)";
      ctx.beginPath(); ctx.arc(s2.x, s2.y, 1.2, 0, Math.PI * 2); ctx.fill();
    }
  }

  function drawIntelDayNight(ctx, w, hh) {
    const sun = sunInfo;
    const dec = Number(declInfo?.declination_deg);
    const sunObj = sun?.sun;
    const utcRaw = sunObj?.utc || sun?.utc || new Date().toISOString();
    const utc = new Date(String(utcRaw).replace("Z", "+00:00"));
    const utcH = utc.getUTCHours() + utc.getUTCMinutes() / 60 + utc.getUTCSeconds() / 3600;
    const lambdaSolar = 15 * (12 - utcH);
    const delta = (view.lon - lambdaSolar) * Math.PI / 180;
    const decRad = (dec * Math.PI) / 180;
    const tanDec = Math.tan(decRad);
    let phi = 0;
    if (Number.isFinite(dec) && Math.abs(tanDec) > 1e-6) phi = Math.atan2(Math.cos(decRad) * Math.cos(delta), tanDec);
    const latTerm = (phi * 180) / Math.PI;
    if (!Number.isFinite(latTerm)) return;
    const s = latLonToScreen(Math.max(-85, Math.min(85, latTerm)), view.lon);
    ctx.strokeStyle = "rgba(148,197,255,0.55)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, s.y);
    ctx.lineTo(w, s.y);
    ctx.stroke();
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
        ctx.fillText(fmt.num(total / 1000, 2) + " km", s.x + 5, s.y - 5);
      }
    }
    if (measurePoints.length > 1) {
      const last = latLonToScreen(measurePoints.at(-1).lat, measurePoints.at(-1).lon);
      ctx.font = "10px JetBrains Mono, monospace";
      ctx.fillStyle = "#ffb02e";
      ctx.fillText(`TOTAL ${fmt.num(total / 1000, 2)} km`, last.x + 8, last.y + 12);
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
    const rows = [...intelFeatures].sort((a, b) => {
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
        const recenter = () => {
          const lon = Number(f.geometry?.coordinates?.[0]), lat = Number(f.geometry?.coordinates?.[1]);
          if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
          view.lat = lat; view.lon = lon; view.zoom = Math.max(view.zoom, 5); draw();
        };
        item.addEventListener("click", recenter);
        item.addEventListener("keydown", (e) => { if (e.key === "Enter") recenter(); });
        intelList.appendChild(item);
      }
      if (rows.length > 12) intelList.appendChild(h("div", { class: "tiny dim", text: `+${rows.length - 12} more mapped points` }));
    }
    const s = intelSummaryData || {};
    const newest = s.newest_age_hours;
    intelPanel.querySelector(".sub").textContent = `${rows.length} mapped · newest ${intelAge(newest)}`;
    intelSummaryEl.textContent = `MAP INTEL: ${s.hazards ?? 0} hazard · ${s.conflict ?? 0} conflict (${s.verified_conflict ?? 0} verified) · newest ${intelAge(newest)}`;
    intelSummaryEl.style.color = newest != null && newest > 6 ? "#ffb02e" : "";
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
    const ll = screenToLatLon(e.clientX - rect.left, e.clientY - rect.top);
    updateReadout(ll, { x: e.clientX - rect.left, y: e.clientY - rect.top });
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
    // Select nearest waypoint within 12 screen pixels.
    let best = null, bestD = 144;
    for (const wp of waypoints) {
      const s = latLonToScreen(wp.lat, wp.lon);
      const d = (s.x - (e.clientX - rect.left)) ** 2 + (s.y - (e.clientY - rect.top)) ** 2;
      if (d < bestD) { best = wp; bestD = d; }
    }
    if (best) selectWaypoint(best);
    else { selected = null; renderWaypoints(); draw(); }
    updateReadout(ll);
  });
  canvas.addEventListener("pointerleave", () => { if (!drag) hover = null; });
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
