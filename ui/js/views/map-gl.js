/* WebGL/3D prototype map (MapLibre GL) alongside the classic canvas map.
   Uses local/offline-capable raster tiles or OpenMapTiles-schema vector MBTiles.
   Raster style (no external glyphs/sprites). True DEM terrain requires raster-dem tiles. */
import { api, download } from "../api.js";
import { store } from "../store.js";
import { h, pageHead, btn, alertStrip, loading, fmt, toast, panel, checkbox, select, field, badge } from "../components.js";
import { positionConfidence, opsecActive, readTextFile } from "./shared.js?v=opsec-1";
import * as U from "../units.js";

let maplibrePromise = null;

function loadMapLibre() {
  if (window.maplibregl) return Promise.resolve(window.maplibregl);
  if (maplibrePromise) return maplibrePromise;
  maplibrePromise = new Promise((resolve, reject) => {
    if (!document.querySelector('link[data-maplibre]')) {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = "/ui/vendor/maplibre/maplibre-gl.css?v=5.6.0";
      link.dataset.maplibre = "1";
      document.head.appendChild(link);
    }
    const script = document.createElement("script");
    script.src = "/ui/vendor/maplibre/maplibre-gl.js?v=5.6.0";
    script.async = true;
    script.onload = () => resolve(window.maplibregl);
    script.onerror = () => reject(new Error("Failed to load MapLibre GL"));
    document.head.appendChild(script);
  });
  return maplibrePromise;
}

function rasterStyle(revision = "", source = "", generation = 0, labelsOverlay = false, mbtilesRaster = false, mbtilesAttribution = "") {
  const params = new URLSearchParams();
  if (revision) params.set("r", revision);
  if (source) params.set("s", source);
  if (generation) params.set("b", String(generation));
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const tile = `${location.origin}/api/map/tiles/{z}/{x}/{y}.png${suffix}`;
  const attribution = mbtilesAttribution || "Local/offline tiles · DarkSec";
  const sources = {
    local: { type: "raster", tiles: [tile], tileSize: 256, attribution },
  };
  const layers = [
    { id: "bg", type: "background", paint: { "background-color": "#030806" } },
    { id: "local-raster", type: "raster", source: "local", paint: { "raster-opacity": 0.92, "raster-fade-duration": 120 } },
  ];
  const showLabels = labelsOverlay && (
    ["esri_imagery", "usgs_imagery"].includes(source) || mbtilesRaster
  );
  if (showLabels) {
    const labelTiles = (id) =>
      [`${location.origin}/api/map/cache/tiles/${id}/{z}/{x}/{y}.png?v=5&s=${id}&b=${generation || 0}`];
    sources.labelsPlaces = {
      type: "raster", tiles: labelTiles("esri_labels_places"), tileSize: 256,
      attribution: "Esri, HERE, Garmin, OpenStreetMap contributors",
    };
    sources.labelsRoads = {
      type: "raster", tiles: labelTiles("esri_labels_roads"), tileSize: 256,
      attribution: "Esri, HERE, Garmin, OpenStreetMap contributors",
    };
    layers.push({ id: "labels-places", type: "raster", source: "labelsPlaces",
                  paint: { "raster-opacity": 0.96, "raster-fade-duration": 0 } });
    layers.push({ id: "labels-roads", type: "raster", source: "labelsRoads",
                  paint: { "raster-opacity": 0.96, "raster-fade-duration": 0 } });
  }
  return { version: 8, name: "DarkSec Raster", sources, layers };
}

async function vectorStyle(revision = "") {
  const style = await api.get("/api/map/vector/style");
  const source = style?.sources?.omt;
  if (source) {
    const rev = revision ? `/${encodeURIComponent(revision)}` : "";
    source.tiles = [`${location.origin}/api/map/vector/tiles${rev}/{z}/{x}/{y}.pbf`];
  }
  return style;
}

// A missing glyph file must never blank a vector map. Probe the first glyph
// range before MapLibre loads the style; if it is unavailable, drop text
// layers and the glyphs URL so land, water, roads and boundaries still paint.
async function ensureVectorGlyphs(style) {
  const template = style?.glyphs;
  if (!template) return style;
  const probe = template
    .replace("{fontstack}", encodeURIComponent("Open Sans Regular"))
    .replace("{range}", "0-255");
  try {
    const response = await fetch(probe, { credentials: "same-origin", cache: "no-store" });
    const type = response.headers.get("content-type") || "";
    if (!response.ok || /text\/html/i.test(type)) {
      throw new Error(`glyph pack returned ${response.status}`);
    }
    const body = new Uint8Array(await response.arrayBuffer());
    if (body.length < 100 || body[0] !== 0x0a) {
      throw new Error("glyph pack is not a valid PBF");
    }
    return style;
  } catch (err) {
    console.warn("Vector labels disabled — glyph pack unavailable:", err?.message || err);
    const plain = {
      ...style,
      metadata: { ...(style.metadata || {}), "darksec:labels": false },
      layers: (style.layers || []).filter((layer) => layer.type !== "symbol"),
    };
    delete plain.glyphs;
    return plain;
  }
}

const WORLD_RECT = {
  type: "Feature",
  properties: {},
  geometry: {
    type: "Polygon",
    coordinates: [[
      [-180, -89.999], [180, -89.999], [180, 89.999],
      [-180, 89.999], [-180, -89.999],
    ]],
  },
};

function applySpaceBackground(map, lightTheme) {
  // The starfield is CSS behind a transparent WebGL canvas. A world-sized
  // polygon keeps Earth's surface opaque so stars only appear beyond the limb.
  const before = (map.getStyle().layers || []).find((layer) => layer.id !== "background");
  try {
    if (!map.getSource("darksec-globe-base")) {
      map.addSource("darksec-globe-base", { type: "geojson", data: WORLD_RECT });
    }
    map.addLayer({
      id: "darksec-globe-base",
      type: "fill",
      source: "darksec-globe-base",
      paint: { "fill-color": lightTheme ? "#e9e5d9" : "#13241a" },
    }, before?.id);
  } catch (err) {
    console.warn("Could not add globe surface base", err);
  }
  try {
    map.setPaintProperty("background", "background-color", "rgba(0,0,0,0)");
    map.setPaintProperty("background", "background-pattern", null);
  } catch (err) {
    console.warn("Could not make space background transparent", err);
  }
}

function escapeHtml(s){ return String(s ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c])); }
function num(v){ const n=Number(v); return Number.isFinite(n)?n:null; }
function fc(features){ return { type:"FeatureCollection", features: features || [] }; }
function pt(lon,lat,props){ const lo=num(lon),la=num(lat); if(lo==null||la==null) return null; return { type:"Feature", geometry:{type:"Point",coordinates:[lo,la]}, properties: props||{} }; }

function solarPosition(date = new Date()) {
  const start = Date.UTC(date.getUTCFullYear(), 0, 0);
  const dayOfYear = Math.floor((date.getTime() - start) / 86400000);
  const decl = -23.44 * Math.cos((360 / 365) * (dayOfYear + 10) * Math.PI / 180);
  const utcH = date.getUTCHours() + date.getUTCMinutes() / 60 + date.getUTCSeconds() / 3600;
  let subLon = -15 * (utcH - 12);
  subLon = ((subLon + 180) % 360 + 360) % 360 - 180;
  return { decl, subLon };
}
function terminatorLat(lon, decl, subLon) {
  const d = decl * Math.PI / 180, h = (lon - subLon) * Math.PI / 180, tanD = Math.tan(d);
  if (Math.abs(tanD) < 1e-8) return 0;
  return Math.max(-85, Math.min(85, Math.atan(-Math.cos(h) / tanD) * 180 / Math.PI));
}
function dayNightGeoJSON() {
  const { decl, subLon } = solarPosition(new Date());
  const coords = [];
  for (let lon = -180; lon <= 180; lon += 2) coords.push([lon, terminatorLat(lon, decl, subLon)]);
  // determine night edge by sampling away from subsolar
  const mid = coords[Math.floor(coords.length/2)];
  const testLat = Math.max(-85, Math.min(85, mid[1] + (decl > 0 ? -20 : 20)));
  const testSin = Math.sin(testLat*Math.PI/180)*Math.sin(decl*Math.PI/180) + Math.cos(testLat*Math.PI/180)*Math.cos(decl*Math.PI/180)*Math.cos((mid[0]-subLon)*Math.PI/180);
  const edgeLat = testSin < 0 ? (testLat < mid[1] ? -85 : 85) : (testLat < mid[1] ? 85 : -85);
  const poly = [coords.concat([[180, edgeLat], [-180, edgeLat], coords[0]])];
  return {
    night: { type:"FeatureCollection", features:[{ type:"Feature", properties:{}, geometry:{ type:"Polygon", coordinates: poly } }] },
    term: { type:"FeatureCollection", features:[{ type:"Feature", properties:{}, geometry:{ type:"LineString", coordinates: coords } }] },
    sun: { type:"FeatureCollection", features:[pt(subLon, decl, { title:"Subsolar point" })].filter(Boolean) }
  };
}

function destination(lon, lat, bearingDeg, distKm) {
  const R=6371, br=bearingDeg*Math.PI/180, d=distKm/R;
  const lat1=lat*Math.PI/180, lon1=lon*Math.PI/180;
  const lat2=Math.asin(Math.sin(lat1)*Math.cos(d)+Math.cos(lat1)*Math.sin(d)*Math.cos(br));
  const lon2=lon1+Math.atan2(Math.sin(br)*Math.sin(d)*Math.cos(lat1), Math.cos(d)-Math.sin(lat1)*Math.sin(lat2));
  return [(lon2*180/Math.PI+540)%360-180, lat2*180/Math.PI];
}

function mapLoadingPage({ kicker = "COMMAND // TERRAIN", title = "Preparing map", desc = "", status = "Loading map data" } = {}) {
  return h("div", { class: "map-loading-page" },
    h("div", { class: "map-loading-card" },
      h("div", { class: "map-loading-kicker", text: kicker }),
      h("h2", { class: "map-loading-title", text: title }),
      h("p", { class: "map-loading-desc", text: desc }),
      h("div", { class: "map-loading-status" }, h("span", { class: "spinner" }), h("span", { text: status })),
      h("div", { class: "map-loading-meta" },
        h("div", {}, "Large offline tile caches ", h("span", { text: "can take a minute or two to index and warm up on first load." })),
        h("div", {}, "USB or slow storage ", h("span", { text: "may add a little extra time — the map will appear as soon as it’s ready." })),
      ),
    ),
  );
}

export async function render(root, params = {}) {
  root.replaceChildren(mapLoadingPage({
    kicker: "COMMAND // TERRAIN (GL)",
    title: "Preparing your 3D map",
    desc: "Loading the local basemap, terrain, cached intelligence, and map layers. Everything stays local on this device.",
    status: "Initialising 3D map",
  }));
  const settings = store.settings || {};
  const homeLat = Number.isFinite(Number(settings.lat)) ? Number(settings.lat) : 39.8283;
  const homeLon = Number.isFinite(Number(settings.lon)) ? Number(settings.lon) : -98.5795;
  const hidePosition = opsecActive(settings);
  const positionInfo = positionConfidence(settings);
  const spaceBackground = settings.map_space_background !== false;
  const labelsOverlay = settings.map_labels_overlay !== false;
  const lightTheme = String(settings.theme_mode || "").toLowerCase() === "light"
    || document.documentElement.dataset.scheme === "light";
  const linkLat = Number(params.lat), linkLon = Number(params.lon);
  const linkZoom = Number(params.z ?? params.zoom);
  const hasLink = !hidePosition && Number.isFinite(linkLat) && Number.isFinite(linkLon)
    && Math.abs(linkLat) <= 85 && Math.abs(linkLon) <= 180;
  const hasLinkZoom = Number.isFinite(linkZoom);
  const glCenter = hasLink ? [linkLon, linkLat] : [homeLon, homeLat];
  const glZoom = hasLinkZoom ? Math.max(-2, Math.min(18, linkZoom)) : 3.5;
  let intel, waypoints, tracks, demStatus, overlayStatus, overlayWind, overlayRain, overlayCloud, mapLayers, cacheStatus;
  let statusBox = null, coordBox = null;
  try {
    [intel, waypoints, tracks, demStatus, overlayStatus, overlayWind, overlayRain, overlayCloud, mapLayers, cacheStatus] = await Promise.all([
      api.get("/api/map/intel").catch(() => null),
      api.get("/api/records/waypoints?limit=2000").then(r=>r.items||[]).catch(() => []),
      api.get("/api/records/tracks?limit=500").then(r=>r.items||[]).catch(() => []),
      api.get("/api/map/dem/status").catch(() => ({ available:false })),
      api.get("/api/map/overlays/status").catch(() => ({ overlays:[] })),
      api.get("/api/map/overlays/wind").catch(() => null),
      api.get("/api/map/overlays/rain").catch(() => null),
      api.get("/api/map/overlays/cloud").catch(() => null),
      api.get("/api/map/layers").catch(() => null),
      api.get("/api/map/cache/status").catch(() => null),
    ]);
  } catch (err) { root.replaceChildren(h("div", { class: "callout danger", text: err.message })); return; }

  let maplibregl;
  try { maplibregl = await loadMapLibre(); } catch (err) { root.replaceChildren(h("div", { class: "callout danger", text: err.message })); return; }

  const state = { maxAgeHours: 0, show: { hazards:true, conflict:true, aircraft:true, sats:true, satsOverheadOnly:false, maritime:true, cables:true, cctv:true, cctvAlpr:true, wind:false, rain:false, cloud:false, terrain: !!demStatus?.available, waypoints:true, tracks:true, daynight:true, home:!hidePosition, scale:true }, live: true, lastRefresh: Date.now() };
  let map = null, popup = null, scaleControl = null, daynightTimer = null, glLiveTimer = null, liveTimer = null, freshTimer = null, inspector = null;
  let movingFetchedAt = Date.now();
  let movingTimer = null, movingPollTimer = null;
  let mapStyle = null;
  const mbInfo = mapLayers?.mbtiles || {};
  const mbUsable = !mbInfo.configured || mbInfo.usable;
  if (mbInfo.vector && mbUsable) {
    try {
      mapStyle = await vectorStyle(mbInfo.revision || "");
    } catch (err) {
      toast(`Vector basemap style unavailable: ${err.message || err}`, "warn", 7000);
    }
  }
  const usingVector = mbInfo.vector && mbUsable;
  if (!mapStyle) {
    const mbNames = (mbInfo.archives || []).filter((a) => a.usable).map((a) => a.name || a.file).filter(Boolean).slice(0, 3).join(", ");
    const mbAttr = (mbInfo.raster && mbInfo.usable) ? `MBTiles · ${mbNames || "operator archive"}` : "";
    mapStyle = rasterStyle(mbInfo.revision || "", mapLayers?.activeSource || "", mapLayers?.cacheGeneration || 0, labelsOverlay, !!(mbInfo.raster && mbInfo.usable), mbAttr);
  }
  mapStyle = await ensureVectorGlyphs(mapStyle);
  const rasterLabelOverlays = !usingVector && labelsOverlay && (
    ["esri_imagery", "usgs_imagery"].includes(mapLayers?.activeSource || "") || !!(mbInfo.raster && mbInfo.usable)
  );
  const labelCacheCount = (id) => {
    const src = (cacheStatus?.sources || []).find((s) => s.source === id || s.id === id);
    return Number(src?.tile_count || 0);
  };
  const missingLabelOverlays = rasterLabelOverlays
    && labelCacheCount("esri_labels_places") <= 0
    && labelCacheCount("esri_labels_roads") <= 0;
  let intelAlpr = { mode: "points", points: [], clusters: [], in_view: 0, dataset_count: 0 };
  let alprFetchTimer = null, alprQueryKey = "";

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "COMMAND // TERRAIN (GL)",
    title: "Map & Terrain — 3D/GL",
    desc: `WebGL situational map over local/offline raster or vector MBTiles. Pitch/rotate for 3D perspective. Position: ${hidePosition ? "OP//MASKED" : positionInfo.mapLabel}. DEM terrain, wind/rain/cloud overlays, and maritime/cables/CCTV layers use local cache when available.`,
    actions: [
      btn("2D CANVAS", { icon: "map", onClick: () => { location.hash = "#/map?renderer=2d"; } }),
      btn("3D GL", { icon: "layers", tone: "primary", onClick: () => { location.hash = "#/map?renderer=gl"; } }),
      btn("IMPORT GPX", { icon: "upload", onClick: importGpxGl }),
      btn("EXPORT GPX", { icon: "download", onClick: () => download("/api/map/gpx/export", "darksec-waypoints.gpx") }),
      btn("RESET", { icon: "target", onClick: () => map?.easeTo({ center: hidePosition ? [0,20] : [homeLon,homeLat], zoom: hidePosition ? 1.6 : 4, pitch:0, bearing:0 }) }),
      btn("PITCH 45°", { icon: "layers", onClick: () => map?.easeTo({ pitch:45, duration:600 }) }),
      btn("GLOBE/FLAT", { icon: "globe", onClick: toggleProjection }),
      btn("REFRESH OVERLAYS", { icon: "refresh", tone:"ghost", onClick: refreshOverlays }),
    ],
  }));
  const vectorSchema = mapStyle?.metadata?.["darksec:schema"] || "openmaptiles";
  const vectorLabels = mapStyle?.metadata?.["darksec:labels"] !== false;
  const vectorNote = mbInfo.vector && mbUsable
    ? `Offline vector basemap: ${(mbInfo.archives || []).map((a) => a.name).filter(Boolean).slice(0, 2).join(", ") || "configured archive"} · ` +
      (vectorSchema === "generic"
        ? `generic schema (layers: ${(mbInfo.layers || []).join(", ") || "unknown"}) · `
        : "OpenMapTiles geometry · ") +
      (vectorLabels ? "bundled Open Sans labels. " : "labels unavailable (glyph pack missing). ")
    : "";
  const mapErrorBox = h("div", { class: "callout danger small mt-8", style: { display: "none" } });
  function showMapError(message) {
    mapErrorBox.style.display = "";
    mapErrorBox.textContent = message;
  }
  root.appendChild(alertStrip(`${vectorNote}Local/offline basemap + cached intel. DEM ${demStatus?.available ? 'available (' + (demStatus.encoding||'terrarium') + ')' : 'not configured/cached'} · overlays cached via operator refresh (online required to fetch). Cables auto-populate from a cached public dataset on feed refresh (online first time; attribution required). CCTV can load nearby OSM surveillance locations via feed refresh when operator position is set. Maritime/AIS works with a configured GeoJSON/JSON source or local file (fielddata/maps/ais.geojson).`, demStatus?.available ? "cyan" : "amber"));
  if (missingLabelOverlays) {
    root.appendChild(alertStrip(
      "Road/place label overlays are not cached. External raster/imagery packs without baked labels will look unlabeled offline until you download an offline map pack with labels enabled (or auto-cache labels while online).",
      "amber",
    ));
  }

  if (mbInfo.configured && !mbInfo.usable) {
    const why = mbInfo.error
      || (mbInfo.archives || []).map((a) => a.error).filter(Boolean).join("; ")
      || "archive cannot be opened";
    const found = (mbInfo.suggestions || []).find((a) => a.path);
    const hint = found
      ? ` Found “${found.file || found.name}” at ${found.path} — use FIND ON USB in Settings → Offline maps.`
      : "";
    root.appendChild(alertStrip(`Configured MBTiles source is not usable — ${why}.${hint}`, "red"));
  } else if (mbInfo.configured && mbInfo.resolved && mbInfo.resolved_path) {
    root.appendChild(alertStrip(`MBTiles drive moved; using ${mbInfo.resolved_path}. Save the new path in Settings → Offline maps.`, "amber"));
  }


  root.appendChild(mapErrorBox);
  const layout = h("div", { class: "map-layout mt-14" });
  const wrap = h("div", { class: `map-shell ${spaceBackground ? "map-space" : ""}`.trim(), style: { height: "min(72vh, 820px)", minHeight: "460px", position: "relative" } });
  if (spaceBackground) wrap.appendChild(h("div", { class: "map-starfield" }));
  const container = h("div", { style: { position: "absolute", inset: "0", zIndex: "1" } });
  wrap.appendChild(container);
  layout.appendChild(wrap);
  const side = h("div", { class: "map-side" });
  layout.appendChild(side);
  root.appendChild(layout);

  inspector = h("div", { class: "small muted", text: "Click a feature for details." });
  statusBox = h("div", { class: "tiny muted", text: "Initializing GL map..." });
  coordBox = h("div", { class: "tiny mono muted", text: "" });
  const readout = h("div", {
    class: "col gap-2",
    style: {
      position: "absolute", left: "10px", bottom: "44px", zIndex: "3",
      background: "rgba(3,8,6,0.78)", border: "1px solid rgba(93,255,154,0.14)",
      padding: "6px 8px", maxWidth: "min(560px, calc(100% - 20px))", pointerEvents: "none",
    },
  }, statusBox, coordBox);
  wrap.appendChild(readout);
  const ageSel = select([
    { value:"0", label:"All cached" }, { value:"6", label:"Last 6 hours" }, { value:"24", label:"Last 24 hours" },
    { value:"72", label:"Last 3 days" }, { value:"168", label:"Last 7 days" }, { value:"720", label:"Last 30 days" }
  ], "0");
  ageSel.addEventListener('change', () => { state.maxAgeHours = Number(ageSel.value)||0; refreshIntelSources(); });
  const layerBox = h("div", { class: "col gap-4" });
  function toggleScale(on) {
    if (!map || !scaleControl) return;
    try { map.removeControl(scaleControl); } catch {}
    if (on) try { map.addControl(scaleControl, "bottom-left"); } catch {}
  }
  for (const [key,label] of [
    ["hazards","Hazard/disaster events"],["conflict","Conflict events"],
    ["aircraft","Aircraft"],["sats","Satellites"],["maritime","Maritime/AIS"],
    ["cables","Cables/landing points"],["cctv","CCTV/cameras (OSM)"],
    ["wind","Wind overlay (cached)"],["rain","Rain/precip overlay (cached)"],["cloud","Cloud overlay (cached)"],
    ["terrain", `DEM terrain/hillshade (${demStatus?.available ? "available" : "not available"})`],
    ["waypoints","Waypoints"],["tracks","Tracks"],["daynight","Day/night terminator"],["home","Home/position"],
    ["scale","Scale bar"],
  ]) {
    const cb = checkbox(label, !!state.show[key], (on)=>{
      state.show[key]=on;
      if (key === "scale") { toggleScale(on); return; }
      if (["wind","rain","cloud"].includes(key)) refreshOverlaySources();
      if (["hazards","conflict","cctv"].includes(key)) refreshIntelSources();
      setVis(key,on);
    });
    if (key === "terrain" && !demStatus?.available) {
      const inp = cb.querySelector("input");
      if (inp) inp.disabled = true;
      cb.classList.add("dim");
      cb.title = "Configure a DEM MBTiles source in Settings → Offline maps";
    }
    layerBox.appendChild(cb);
  }
  layerBox.appendChild(h("div", { class:"tiny muted mt-6", text:"CCTV / satellite filters" }));
  layerBox.appendChild(checkbox("Show global ALPR cameras (DeFlock/OSM)", !!state.show.cctvAlpr, (on)=>{ state.show.cctvAlpr=on; refreshIntelSources(); setAlprVis(); }));
  layerBox.appendChild(checkbox("Satellites: only above horizon", !!state.show.satsOverheadOnly, (on)=>{ state.show.satsOverheadOnly=on; refreshIntelSources(); }));
  const liveCheck = checkbox("Auto-refresh map intel (60s)", !!state.live, (on)=>{ state.live=on; scheduleLive(); });
  const freshText = h("div", { class: "tiny muted", text: "Intel loaded" });
  const statsBox = h("div", { class: "tiny muted", text: "Loading..." });
  const mbArchive = (mbInfo.archives || []).find((a) => a.usable) || {};
  const basemapNote = mbInfo.vector
    ? h("div", { class: "tiny muted wrap-any", text: `Basemap: vector · ${(mbInfo.layers || []).join(", ") || "layers unknown"} · z${mbArchive.minzoom ?? "?"}–${mbArchive.maxzoom ?? "?"}${mbArchive.indexed ? "" : " · NO TILE INDEX (slow)"}` })
    : h("div", { class: "tiny muted", text: "Basemap: raster tiles / bundled overview" });
  side.appendChild(panel({ title:"GL Layers", icon:"layers", body: h("div",{class:"col gap-8"},
    field("INTEL AGE", ageSel), layerBox, basemapNote, liveCheck, freshText,
    h("div", { class: "row wrap gap-6 mt-6" },
      btn("REFRESH DATA", { size:"sm", icon:"refresh", onClick: refreshData }),
      btn("FIT DATA", { size:"sm", tone:"ghost", onClick: fitData }))
  ) }));
  side.appendChild(panel({ title:"Situational Summary", icon:"globe", body: statsBox }));
  const glLegend = h("div", { class: "map-legend" });
  const legendItem = (label, color, shape = "dot") => {
    const sw = h("i", { style: { background: color } });
    if (shape === "diamond") sw.style.transform = "rotate(45deg)";
    if (shape === "ring") { sw.style.background = "transparent"; sw.style.border = `1px solid ${color}`; sw.style.borderRadius = "50%"; }
    if (shape === "dot") sw.style.borderRadius = "50%";
    return h("span", {}, sw, document.createTextNode(label));
  };
  for (const [label, color, shape] of [
    ["Hazard/disaster", "#5dff9a", "dot"], ["Critical event", "#ff5c5c", "dot"],
    ["Conflict verified", "#ff8a3d", "diamond"], ["Conflict media", "#ffb02e", "diamond"],
    ["Aircraft", "#48d7ff", "ring"], ["Satellite overhead", "#c78bff", "ring"],
    ["Satellite below horizon", "#7a5a96", "ring"], ["Maritime/AIS", "#35c9ff", "dot"],
    ["Cables", "#ffd166", "dot"], ["CCTV", "#b7ff6a", "dot"], ["ALPR (DeFlock/OSM)", "#ff66cc", "dot"],
    ["Waypoint", "#5dff9a", "diamond"], ["Home", "#48d7ff", "ring"],
  ]) glLegend.appendChild(legendItem(label, color, shape));
  side.appendChild(panel({ title:"Legend", icon:"info", body: glLegend }));
  side.appendChild(panel({ title:"Inspector", icon:"info", body: inspector }));

  const mbMinZoom = Number.isFinite(Number(mbArchive.minzoom)) ? Number(mbArchive.minzoom) : 0;
  const mbMaxZoom = Number.isFinite(Number(mbArchive.maxzoom)) ? Number(mbArchive.maxzoom) : 22;
  const initialZoom = hidePosition ? 1.6 : Math.min(mbMaxZoom, Math.max(3.5, mbMinZoom));
  const baseInitialZoom = (hasLink || hasLinkZoom) ? glZoom : initialZoom;
  map = new maplibregl.Map({
    container, style: mapStyle, center: hidePosition ? [0,20] : glCenter,
    zoom: baseInitialZoom, pitch:0, bearing:0, attributionControl:true, maxPitch:85,
    canvasContextAttributes: { alpha: true, antialias: true },
  });
  if ((mbInfo.vector || mbInfo.raster) && !hidePosition && !hasLink && !hasLinkZoom) {
    const b = mbArchive.bounds;
    if (Array.isArray(b) && b.length === 4 && b.every((v) => Number.isFinite(Number(v)))) {
      const span = Math.max(Number(b[2]) - Number(b[0]), Number(b[3]) - Number(b[1]));
      if (span > 0 && span < 350) {
        // Fit the pack, but never below its own minzoom: a regional z5+ pack
        // fitted at z4 would request tiles that do not exist and render black.
        try {
          const cam = map.cameraForBounds(
            [[Number(b[0]), Number(b[1])], [Number(b[2]), Number(b[3])]],
            { padding: 40 },
          );
          if (cam && Number.isFinite(cam.zoom)) {
            map.jumpTo({
              center: cam.center,
              zoom: Math.max(mbMinZoom, Math.min(mbMaxZoom, cam.zoom)),
            });
          }
        } catch {}
      }
    }
  }
  let vectorTileSeen = false;
  let watchdogShown = false;
  let labelsStripped = false;
  function dropVectorLabels(reason) {
    if (labelsStripped || !map) return;
    labelsStripped = true;
    try {
      const current = map.getStyle?.();
      for (const layer of (current?.layers || [])) {
        if (layer.type === "symbol") {
          try { map.removeLayer(layer.id); } catch {}
        }
      }
      map.triggerRepaint();
    } catch {}
    console.warn("Vector labels removed:", reason || "style error");
  }
  map.on("error", (event) => {
    const message = String(event?.error?.message || event?.error || event?.message || "");
    if (/glyph|font/i.test(message)) dropVectorLabels(message);
  });
  map.on("sourcedata", (e) => {
    if (e.sourceId === "omt" && e.tile) {
      vectorTileSeen = true;
      if (watchdogShown) {
        watchdogShown = false;
        mapErrorBox.style.display = "none";
      }
      // Ensure vector tiles are painted even when the first style load raced
      // the container layout (a known blank-map failure in embedded views).
      try { map.triggerRepaint(); } catch {}
    }
  });
  if (mbInfo.vector) {
    setTimeout(() => {
      if (!map) return;
      let rendered = 0;
      try {
        const baseLayers = (map.getStyle()?.layers || [])
          .filter((layer) => ["fill", "line", "raster", "fill-extrusion"].includes(layer.type))
          .map((layer) => layer.id);
        rendered = baseLayers.length ? map.queryRenderedFeatures({ layers: baseLayers }).length : 0;
      } catch {}
      if (rendered > 0) return;
      // A glyph/style failure can leave the source loaded but nothing painted.
      // Remove text layers and give geometry a chance before reporting an error.
      dropVectorLabels("no vector features painted");
      watchdogShown = true;
      showMapError(
        `No vector map features painted for this view (z${map.getZoom().toFixed(1)}). Archive layers: ` +
        `${(mbInfo.layers || []).join(", ") || "unknown"} · z${mbArchive.minzoom ?? "?"}–${mbArchive.maxzoom ?? "?"}` +
        `${mbArchive.indexed ? "" : " · no tiles index (queries may be extremely slow)"}. ` +
        `Open Settings → Offline maps → DIAGNOSE if this persists.`
      );
    }, 9000);
  }
  const glResize = new ResizeObserver(() => {
    try { map.resize(); map.triggerRepaint(); } catch {}
  });
  glResize.observe(container);
  // Some embedded/headless Chromium builds paint the style background but not
  // the first vector tiles until the map is resized/repainted after layout.
  // Nudge it for the first ~12 seconds, then stop.
  let glRepaintTicks = 0;
  const glRepaintTimer = setInterval(() => {
    glRepaintTicks += 1;
    try { map.resize(); map.triggerRepaint(); } catch {}
    if (glRepaintTicks >= 6) clearInterval(glRepaintTimer);
  }, 2000);
  map.addControl(new maplibregl.NavigationControl({ visualizePitch:true, showCompass:true, showZoom:true }), "top-right");
  scaleControl = new maplibregl.ScaleControl({ maxWidth:120, unit: U.isImperial() ? "imperial" : "metric" });
  if (state.show.scale) map.addControl(scaleControl, "bottom-left");
  try { if (typeof map.setProjection === 'function') map.setProjection({ type:'globe' }); } catch (e) { console.warn('Initial globe projection failed', e); }
  popup = new maplibregl.Popup({ closeButton:true, closeOnClick:true, maxWidth:'340px' });

  function toggleProjection(){
    try {
      const p = map.getProjection?.();
      const type = p?.type || p?.name || p;
      if (type==='globe') map.setProjection({type:'mercator'});
      else map.setProjection({type:'globe'});
      setTimeout(() => { try { const now = map.getProjection?.(); const t = now?.type || now?.name || now; if (t) statusBox.textContent = `Projection: ${String(t).toUpperCase()} · local raster tiles · refresh feeds for cables/CCTV/maritime, then inspect layers.`; } catch {} }, 0);
    } catch(e){ console.warn('Projection toggle failed', e); toast(`Globe projection unavailable: ${e?.message || e}`); statusBox.textContent = `Globe projection unavailable in this runtime: ${e?.message || e}`; }
  }

  function setVis(key,on){
    const ids = {
      hazards:['hazards'], conflict:['conflict'], aircraft:['aircraft','aircraft-hdg'], sats:['sats','sat-footprint','sat-footprint-fill','sat-track'],
      maritime:['maritime-cluster','maritime-pt'], cables:['cables-cluster','cables-pt'], cctv:['cctv-cluster','cctv-pt'],
      wind:['wind-pt','wind-dir'], rain:['rain-pt'], cloud:['cloud-pt'],
      terrain:['hillshade'],
      waypoints:['waypoints'], tracks:['tracks'], daynight:['night-fill','term-line','sun-pt'], home:['home-halo','home-dot'],
    }[key] || [];
    for (const id of ids) try { if (map.getLayer(id)) map.setLayoutProperty(id,'visibility', on?'visible':'none'); } catch {}
    if (key === 'cctv') setAlprVis();
    if (key === 'terrain') {
      try {
        if (on && demStatus?.available && map.getSource('dem')) map.setTerrain({ source: 'dem', exaggeration: 1.15 });
        else if (typeof map.setTerrain === 'function') map.setTerrain(null);
      } catch {}
    }
  }

  function setAlprVis(){
    const on = !!(state.show.cctv && state.show.cctvAlpr);
    for (const id of ['alpr-points', 'alpr-clusters']) {
      try { if (map.getLayer(id)) map.setLayoutProperty(id,'visibility', on ? 'visible' : 'none'); } catch {}
    }
  }

  function intelFeaturesFiltered(kind){
    const features = (intel?.features||[]).filter(f=>f?.geometry?.type==='Point');
    const maxAge = state.maxAgeHours;
    const passAge = (p)=> maxAge<=0 || p?.age_hours==null || Number(p.age_hours)<=maxAge;
    if (kind==='hazards') return fc(features.filter(f=>{ const p=f.properties||{}; const c=p.kind==='conflict'||p.feed_id==='gdelt_conflict'||p.feed_id==='ucdp_candidate'; return !c && passAge(p); }));
    return fc(features.filter(f=>{ const p=f.properties||{}; const c=p.kind==='conflict'||p.feed_id==='gdelt_conflict'||p.feed_id==='ucdp_candidate'; return c && passAge(p); }));
  }
  function animatedTarget(item, kind){
    const speed = num(item.speed_kmh), heading = num(item.heading_deg);
    if (speed == null || heading == null || speed <= 0) return item;
    const parsed = Date.parse(item.time || "");
    const base = Number.isFinite(parsed) ? parsed : movingFetchedAt;
    const age = Math.max(0, (Date.now() - base) / 1000);
    const maxAge = kind === "sat" ? 300 : 900;
    if (age > maxAge) return item;
    const d = speed * age / 3600;
    const br = heading * Math.PI / 180, ang = d / 6371;
    const p1 = Number(item.lat) * Math.PI / 180, l1 = Number(item.lon) * Math.PI / 180;
    const p2 = Math.asin(Math.sin(p1) * Math.cos(ang) + Math.cos(p1) * Math.sin(ang) * Math.cos(br));
    const l2 = l1 + Math.atan2(Math.sin(br) * Math.sin(ang) * Math.cos(p1),
      Math.cos(ang) - Math.sin(p1) * Math.sin(p2));
    return { ...item,
      lat: Math.max(-85, Math.min(85, p2 * 180 / Math.PI)),
      lon: ((l2 * 180 / Math.PI + 540) % 360) - 180 };
  }
  function currentAircraft(){ return (intel?.aircraft || []).map(a => animatedTarget(a, "aircraft")); }
  function currentSatellites(){ return (intel?.satellites || []).map(s => animatedTarget(s, "sat")); }

  async function refreshMoving(){
    if (!map || document.hidden || store.settings?.opsec_mode || store.state.online === false) return;
    try {
      const moving = await api.get('/api/map/moving');
      if (!moving) return;
      if (Array.isArray(moving.aircraft)) intel = { ...(intel || {}), aircraft: moving.aircraft };
      if (Array.isArray(moving.satellites)) intel = { ...(intel || {}), satellites: moving.satellites };
      movingFetchedAt = Date.now();
      refreshIntelSources();
    } catch { /* keep dead-reckoning the last snapshot */ }
  }

  function aircraftGeo(){
    return fc(currentAircraft().slice(0,250).map(a=>pt(a.lon,a.lat,{ kind:'aircraft', title:a.callsign||'Aircraft', alt_m:a.alt_m, speed_kmh:a.speed_kmh, heading:a.heading_deg, source:a.source||'OpenSky', url:a.url, time:a.time })).filter(Boolean));
  }
  function aircraftHeadingGeo(){
    return fc(currentAircraft().slice(0,250).map(a=>{
      const lo=num(a.lon),la=num(a.lat),hdg=num(a.heading_deg),spd=num(a.speed_kmh);
      if(lo==null||la==null||hdg==null) return null;
      const km = Math.max(8, Math.min(60, (spd||200)*0.08));
      const end = destination(lo,la,hdg,km);
      return { type:"Feature", properties:{}, geometry:{ type:"LineString", coordinates:[[lo,la], end] } };
    }).filter(Boolean));
  }
  function satsGeo(){
    let sats = currentSatellites();
    if (state.show.satsOverheadOnly) sats = sats.filter(s => s && s.visible !== false);
    return fc(sats.slice(0,250).map(s=>pt(s.lon,s.lat,{ kind:'sat', title:s.name||`NORAD ${s.norad||''}`, norad:s.norad, alt_km:s.alt_km, elevation_deg:s.elevation_deg, azimuth_deg:s.azimuth_deg, visible:s.visible, source:s.source||'CelesTrak/SGP4', time:s.time })).filter(Boolean));
  }
  function tracksGeo(){
    const feats=[];
    for (const t of (tracks||[])) {
      try { const g = typeof t.geojson==='string'?JSON.parse(t.geojson):t.geojson; if (!g) continue; if (g.type==='FeatureCollection') feats.push(...(g.features||[])); else if (g.type==='Feature') feats.push(g); else feats.push({type:'Feature',properties:{name:t.name},geometry:g}); } catch {}
    }
    return fc(feats);
  }
  function maritimeGeo(){
    return fc((intel?.maritime||[]).map(m=>pt(m.lon,m.lat,{ kind:'maritime', title:m.title||m.vessel||m.mmsi||'Vessel', mmsi:m.mmsi, callsign:m.callsign, speed_kn:m.speed_kn, course_deg:m.course_deg, heading_deg:m.heading_deg, source:m.source, time:m.time, age_hours:m.age_hours, url:m.url, status:m.status })).filter(Boolean));
  }
  function cablesGeo(){
    return fc((intel?.cables||[]).map(c=>pt(c.lon,c.lat,{ kind:'cable', title:c.title||c.cable_id||'Cable point', cable_id:c.cable_id, operator:c.operator, capacity:c.capacity, source:c.source, time:c.time, url:c.url })).filter(Boolean));
  }
  function cctvGeo(){
    const globalAlpr = (intelAlpr.dataset_count || 0) > 0;
    const items = (intel?.cctv||[]).filter(c => !(globalAlpr && c.alpr) && (state.show.cctvAlpr !== false || !c.alpr));
    return fc(items.map(c=>pt(c.lon,c.lat,{
      kind: c.alpr ? 'alpr_camera' : 'cctv',
      title: c.title || c.camera_id || 'Camera',
      camera_id: c.camera_id, status: c.status, feed_url: c.feed_url || c.url, source: c.source, time: c.time, url: c.url,
      alpr: !!c.alpr, flock_tagged: !!c.flock_tagged,
      surveillance_type: c.surveillance_type, camera_type: c.camera_type, brand: c.brand,
      operator_name: c.operator_name, direction: c.direction, surveillance_zone: c.surveillance_zone,
      summary: c.summary, stream_url: c.stream_url || '', feed_type: c.feed_type || c.feedType || '', frame_refresh_ms: c.frame_refresh_ms || c.frameRefreshMs || 0, credit: c.credit || '', license: c.license || '', id: c.id || c.camera_id || '',
    })).filter(Boolean));
  }
  function alprPointsGeo(){
    return fc((intelAlpr.points || []).map(p => pt(p.lon, p.lat, {
      kind: 'alpr_camera',
      title: p.title || 'ALPR camera',
      camera_id: p.camera_id || p.osm_id || '',
      alpr: true, flock_tagged: !!p.flock_tagged,
      brand: p.brand || '', operator_name: p.operator_name || p.operator || '',
      surveillance_type: p.surveillance_type || 'ALPR',
      camera_type: p.camera_type || p.mount || '',
      direction: p.direction, surveillance_zone: p.surveillance_zone || p.zone || '',
      summary: p.summary || '', source: p.source || 'DeFlock / OpenStreetMap (ODbL)',
      url: p.url || '', osm_timestamp: p.osm_timestamp || '',
    })).filter(Boolean));
  }
  function alprClustersGeo(){
    return fc((intelAlpr.clusters || []).map(c => pt(c.lon, c.lat, {
      kind: 'alpr_cluster',
      title: c.title || `${c.count || 0} ALPR cameras`,
      count: c.count || 0,
      brand: c.brand || '',
      alpr: true,
      source: 'DeFlock / OpenStreetMap (ODbL)',
    })).filter(Boolean));
  }
  function satelliteFootprint(p, lngLat){
    if (!map || !lngLat || String(p?.kind || '').toLowerCase() !== 'sat') { clearSatelliteFootprint(); return; }
    const alt = Math.max(80, Number(p.alt_km) || 500);
    const rhoDeg = Math.acos(Math.min(1, 6371 / (6371 + alt))) * 180 / Math.PI;
    const ring = [];
    for (let b = 0; b < 360; b += 5) ring.push(destination(lngLat.lng, lngLat.lat, b, rhoDeg * 111.32));
    ring.push(ring[0]);
    try {
      map.getSource('sat-footprint')?.setData(fc([{
        type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [ring] },
      }]));
    } catch {}
  }
  function clearSatelliteFootprint(){ try { map?.getSource('sat-footprint')?.setData(fc([])); } catch {} }
  let satTrackToken = 0;
  async function loadSatelliteTrack(norad){
    const token = ++satTrackToken;
    if (!norad) { clearSatelliteTrack(); return; }
    try {
      const data = await api.get(`/api/intel/satellite-track?norad=${encodeURIComponent(String(norad))}&minutes=90`);
      if (token !== satTrackToken) return;
      const coords = [];
      let prevLon = null;
      for (const pt of (data?.points || [])) {
        let lon = Number(pt.lon);
        if (prevLon != null) {
          while (lon - prevLon > 180) lon -= 360;
          while (lon - prevLon < -180) lon += 360;
        }
        prevLon = lon;
        coords.push([lon, Number(pt.lat)]);
      }
      map?.getSource('sat-track')?.setData(fc(coords.length > 1 ? [{
        type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: coords },
      }] : []));
    } catch { if (token === satTrackToken) clearSatelliteTrack(); }
  }
  function clearSatelliteTrack(){ satTrackToken++; try { map?.getSource('sat-track')?.setData(fc([])); } catch {} }
  function overlayGeo(kind){
    const d = kind==='wind'?overlayWind : kind==='rain'?overlayRain : overlayCloud;
    return d?.data && d.data.type === 'FeatureCollection' ? d.data : fc([]);
  }
  function windDirGeo(){
    const base = overlayGeo('wind');
    const feats = [];
    for (const f of (base.features||[])) {
      const c = f.geometry?.coordinates; const p = f.properties||{};
      const lo=num(c?.[0]), la=num(c?.[1]), dir=num(p.wind_dir), sp=num(p.wind_kmh);
      if (lo==null||la==null||dir==null) continue;
      const km = Math.max(8, Math.min(45, (sp||10)*0.25));
      feats.push({ type:'Feature', properties:{}, geometry:{ type:'LineString', coordinates:[[lo,la], destination(lo,la,dir,km)] } });
    }
    return fc(feats);
  }

  function updateStats(){
    const feats = intel?.features||[];
    const hz = feats.filter(f=>{ const p=f.properties||{}; return !(p.kind==='conflict'||p.feed_id==='gdelt_conflict'||p.feed_id==='ucdp_candidate'); }).length;
    const cf = feats.length - hz;
    const summary = intel?.summary || {};
    const ov = (d)=> d?.data?.features?.length || 0;
    const localAlpr = (intel?.cctv||[]).filter(c=>c.alpr).length;
    const alprText = intelAlpr.dataset_count
      ? `global ${intelAlpr.in_view ?? 0}/${intelAlpr.dataset_count}`
      : `local ${localAlpr}`;
    statsBox.textContent = `Hazards ${hz} · Conflict ${cf} · Aircraft ${(intel?.aircraft||[]).length} · Sats ${(intel?.satellites||[]).length} · Maritime ${(intel?.maritime||[]).length} · Cables ${(intel?.cables||[]).length} · CCTV ${(intel?.cctv||[]).length} (ALPR ${alprText}) · Wind ${ov(overlayWind)} · Rain ${ov(overlayRain)} · Cloud ${ov(overlayCloud)}${summary.newest_age_hours!=null ? ` · newest intel ${Math.round(summary.newest_age_hours)}h` : ''}`;
  }
  function refreshOverlaySources(){
    if (!map) return;
    const s = (id,data)=>{ try { if (map.getSource(id)) map.getSource(id).setData(data); } catch {} };
    s('wind', overlayGeo('wind')); s('wind-dir', windDirGeo()); s('rain', overlayGeo('rain')); s('cloud', overlayGeo('cloud'));
  }
  function refreshAlprSources(){
    if (!map) return;
    const s = (id,data)=>{ try { if (map.getSource(id)) map.getSource(id).setData(data); } catch {} };
    s('alpr-points', alprPointsGeo()); s('alpr-clusters', alprClustersGeo());
  }
  function refreshIntelSources(){
    if (!map) return;
    const s = (id,data)=>{ try { if (map.getSource(id)) map.getSource(id).setData(data); } catch {} };
    s('hazards', intelFeaturesFiltered('hazards')); s('conflict', intelFeaturesFiltered('conflict'));
    s('aircraft', aircraftGeo()); s('aircraft-hdg', aircraftHeadingGeo()); s('sats', satsGeo());
    s('maritime', maritimeGeo()); s('cables', cablesGeo()); s('cctv', cctvGeo());
    refreshAlprSources();
    updateStats();
  }
  async function refreshAlpr(force = false){
    if (!map) return;
    const b = map.getBounds();
    const z = Math.round(map.getZoom());
    const key = `${b.getWest().toFixed(3)},${b.getSouth().toFixed(3)},${b.getEast().toFixed(3)},${b.getNorth().toFixed(3)},${z}`;
    if (!force && key === alprQueryKey) return;
    alprQueryKey = key;
    try {
      const data = await api.get(`/api/map/alpr?bbox=${b.getWest().toFixed(5)},${b.getSouth().toFixed(5)},${b.getEast().toFixed(5)},${b.getNorth().toFixed(5)}&zoom=${z}`);
      intelAlpr = data || { mode: "points", points: [], clusters: [], in_view: 0, dataset_count: 0 };
      refreshAlprSources();
      updateStats();
    } catch { /* the map remains usable without the optional global ALPR layer */ }
  }
  function scheduleAlprFetch(){
    if (alprFetchTimer) clearTimeout(alprFetchTimer);
    alprFetchTimer = setTimeout(() => refreshAlpr(), 450);
  }
  async function refreshData({ silent = false } = {}){
    if (!silent) toast('Refreshing map data', 'warn');
    try {
      const [i, w, t, ds] = await Promise.all([
        api.get('/api/map/intel').catch(()=>null),
        api.get('/api/records/waypoints?limit=2000').then(r=>r.items||[]).catch(()=>[]),
        api.get('/api/records/tracks?limit=500').then(r=>r.items||[]).catch(()=>[]),
        silent ? Promise.resolve(null) : api.get('/api/map/dem/status').catch(()=>null),
      ]);
      if (i) intel = i;
      if (Array.isArray(w)) waypoints = w;
      if (Array.isArray(t)) tracks = t;
      if (ds) demStatus = ds;
      if (map?.getSource('waypoints')) map.getSource('waypoints').setData(fc(waypoints.map(wp=>pt(wp.lon,wp.lat,{kind:'waypoint',title:wp.name,type:wp.type||'WAYPOINT',note:(wp.note||'').slice(0,180)})).filter(Boolean)));
      if (map?.getSource('tracks')) map.getSource('tracks').setData(tracksGeo());
      refreshIntelSources();
      await refreshAlpr(true);
      state.lastRefresh = Date.now();
      updateFreshness();
      if (!silent) toast('Map data refreshed');
    } catch (e) { if (!silent) toast(e.message || 'Refresh failed', 'error'); }
  }

  function updateFreshness(){
    if (!freshText) return;
    const opsec = !!store.settings?.opsec_mode;
    const offline = store.state.online === false;
    const age = state.lastRefresh ? Math.max(0, Math.round((Date.now() - state.lastRefresh) / 1000)) : null;
    const ageText = age == null ? "intel —" : age < 60 ? `intel ${age}s ago` : `intel ${Math.round(age / 60)}m ago`;
    freshText.textContent = `${ageText} · ${opsec ? "OPSEC" : offline ? "OFFLINE" : state.live ? "live 60s" : "manual"}`;
  }

  function scheduleLive(){
    if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
    if (freshTimer) { clearInterval(freshTimer); freshTimer = null; }
    updateFreshness();
    freshTimer = setInterval(updateFreshness, 10_000);
    if (!state.live) return;
    liveTimer = setInterval(async () => {
      if (document.hidden) return;
      if (store.settings?.opsec_mode) return;
      if (store.state.online === false) return;
      await refreshData({ silent: true });
    }, 60_000);
  }
  async function importGpxGl(){
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
      if (map?.getSource('waypoints')) map.getSource('waypoints').setData(fc(waypoints.map(wp=>pt(wp.lon,wp.lat,{kind:'waypoint',title:wp.name,type:wp.type||'WAYPOINT',note:(wp.note||'').slice(0,180)})).filter(Boolean)));
      if (map?.getSource('tracks')) map.getSource('tracks').setData(tracksGeo());
      fitData();
    } catch (err) { toast(err.message || "GPX import failed", "error"); }
  }

  async function refreshOverlays(kind='all'){
    toast('Refreshing/caching overlays (requires online access)…', 'warn');
    try {
      const r = await api.post('/api/map/overlays/refresh', { kind, lat: homeLat, lon: homeLon });
      const [w,ra,c] = await Promise.all([
        api.get('/api/map/overlays/wind').catch(()=>null),
        api.get('/api/map/overlays/rain').catch(()=>null),
        api.get('/api/map/overlays/cloud').catch(()=>null),
      ]);
      overlayWind = w; overlayRain = ra; overlayCloud = c;
      refreshOverlaySources();
      updateStats();
      const errs = (r?.results||[]).filter(x=>x.status==='ERROR');
      toast(errs.length ? `Overlays cached with ${errs.length} error(s)` : 'Overlays cached', errs.length ? 'warn' : 'ok');
    } catch (e) { toast(e.message || 'Overlay refresh failed', 'error'); }
  }
  function fitData(){
    if (!map) return;
    const coords = [];
    for (const f of (intel?.features||[])) { const c=f.geometry?.coordinates; if(c&&Number.isFinite(c[0])&&Number.isFinite(c[1])) coords.push(c); }
    for (const a of (intel?.aircraft||[]).slice(0,200)) if(Number.isFinite(Number(a.lon))&&Number.isFinite(Number(a.lat))) coords.push([Number(a.lon),Number(a.lat)]);
    for (const s of (intel?.satellites||[]).slice(0,250)) if(Number.isFinite(Number(s.lon))&&Number.isFinite(Number(s.lat))) coords.push([Number(s.lon),Number(s.lat)]);
    for (const w of waypoints) if(Number.isFinite(Number(w.lon))&&Number.isFinite(Number(w.lat))) coords.push([Number(w.lon),Number(w.lat)]);
    if (coords.length < 1) { map.easeTo({ center: hidePosition ? [0,20] : [homeLon,homeLat], zoom: hidePosition ? 1.6 : 4 }); return; }
    if (coords.length === 1) { map.easeTo({ center: coords[0], zoom: 8 }); return; }
    let minX=180,minY=85,maxX=-180,maxY=-85;
    for (const [x,y] of coords) { if(x<minX)minX=x; if(y<minY)minY=y; if(x>maxX)maxX=x; if(y>maxY)maxY=y; }
    try { map.fitBounds([[minX,minY],[maxX,maxY]], { padding: 40, maxZoom: 10, duration: 700 }); } catch { map.easeTo({ center: hidePosition ? [0,20] : [homeLon,homeLat], zoom: hidePosition ? 1.6 : 4 }); }
  }

  function fmtProp(k,v){
    if (v==null||v==='') return '';
    if (k==='alt_m') return U.altM(v);
    if (k==='alt_km') return U.altKm(v);
    if (k==='speed_kmh') return U.speedKmh(v);
    if (k==='heading' || k==='heading_deg' || k==='course_deg' || k==='direction') {
      const n = Number(v); return Number.isFinite(n) ? `${Math.round(n)}°` : String(v).slice(0,40);
    }
    if (k==='elevation_deg') return `${Math.round(Number(v))}°`;
    if (k==='age_hours') { const n=Number(v); return Number.isFinite(n) ? (n < 1 ? `${Math.max(1, Math.round(n*60))}m ago` : `${Math.round(n)}h ago`) : String(v); }
    if ((k==='time' || k==='updated' || k==='observed') && String(v).match(/^\d{4}-/)) { try { return fmt.date(v); } catch {} }
    if (k==='alpr') return (v===true||v==='true') ? 'YES (OSM-tagged ALPR/ANPR)' : 'NO';
    if (k==='flock_tagged') return (v===true||v==='true') ? 'YES (OSM tag mention; verify)' : 'NO';
    if (typeof v === 'boolean') return v ? 'YES' : 'NO';
    return String(v).slice(0,220);
  }

  function popupMeta(p){
    const parts = [];
    const push = (v) => { if (v!=null && v!=='') parts.push(String(v)); };
    if (p.source) push(p.source);
    if (p.provider && p.provider !== p.source) push(p.provider);
    if (p.severity) push(p.severity);
    if (p.verified === true || p.verified === 'true') push('VERIFIED');
    if (p.alpr === true || p.alpr === 'true') push('ALPR/ANPR');
    if (p.flock_tagged === true || p.flock_tagged === 'true') push('Flock-tagged (OSM)');
    if (p.surveillance_type && p.surveillance_type !== p.kind) push(p.surveillance_type);
    if (p.feed_type && p.feed_type !== p.surveillance_type) push(p.feed_type);
    if (p.brand) push(p.brand);
    if (p.operator_name) push(p.operator_name);
    if (p.direction != null && p.direction !== '') push(`${fmtProp('direction', p.direction)} dir`);
    if (p.mag != null && p.mag !== '') push(`M${p.mag}`);
    if (p.fatalities != null && p.fatalities !== '') push(`${p.fatalities} killed`);
    if (p.depth_km != null && p.depth_km !== '') push(`${p.depth_km} km depth`);
    if (p.place) push(p.place);
    if (p.region || p.city) push([p.city, p.region].filter(Boolean).join(', '));
    if (p.callsign && p.callsign !== p.title) push(p.callsign);
    if (p.alt_m != null) push(U.altM(p.alt_m));
    if (p.alt_km != null) push(U.altKm(p.alt_km));
    if (p.speed_kmh != null) push(U.speedKmh(p.speed_kmh));
    if (p.speed_kn != null) push(`${Math.round(Number(p.speed_kn))} kn`);
    if (p.heading_deg != null) push(`${Math.round(Number(p.heading_deg))}° hdg`);
    if (p.age_hours != null) push(fmtProp('age_hours', p.age_hours));
    if (p.credit) push(String(p.credit).slice(0, 48));
    return parts.filter(Boolean).slice(0, 9);
  }

  function showInspector(p, lngLat){
    if (glLiveTimer) { clearInterval(glLiveTimer); glLiveTimer = null; }
    p = p || {};
    if (String(p.kind || '').toLowerCase() === 'sat') {
      satelliteFootprint(p, lngLat);
      loadSatelliteTrack(p.norad);
    } else {
      clearSatelliteFootprint();
      clearSatelliteTrack();
    }
    const rows = Object.entries(p).filter(([k,v]) => v != null && v !== '' && !['geometry'].includes(k)).slice(0, 18);
    const isCamera = ['cctv', 'alpr_camera', 'camera'].includes(String(p.kind || '').toLowerCase()) || p.camera_id;
    const children = [
      h('div', { class: 'side-label', text: String(p.kind || 'FEATURE').toUpperCase() }),
      h('div', { class: 'dev-name', text: p.title || p.name || p.callsign || 'Selected feature' }),
    ];
    if (isCamera && p.id) {
      const feedType = String(p.feed_type || p.feedType || '').toLowerCase();
      const rawRefresh = Number(p.frame_refresh_ms || p.frameRefreshMs || 0);
      const refreshSec = Math.max(10, Math.min(300, rawRefresh > 0 ? Math.round(rawRefresh / 1000) : 30));
      const hasStream = !!(p.stream_url || feedType === 'hls' || feedType === 'mp4' || feedType === 'm3u8' || feedType === 'video');
      let videoMode = false;
      const wrap = h('div', { style: { border: '1px solid var(--line)', borderRadius: '6px', overflow: 'hidden', background: 'rgba(0,0,0,.25)' } });
      const img = h('img', {
        alt: p.title || 'Camera preview',
        loading: 'eager',
        referrerPolicy: 'no-referrer',
        style: { display: 'block', width: '100%', maxHeight: '220px', objectFit: 'cover' },
      });
      const cap = h('div', { class: 'tiny dim', style: { padding: '6px 8px' }, text: '' });
      let live = false;
      const setImg = (force=false) => { if (videoMode) return; img.src = `/api/cctv/frame?id=${encodeURIComponent(String(p.id))}${force ? '&force=1' : ''}&t=${Date.now()}`; };
      const liveBtn = btn('LIVE PREVIEW', { size: 'sm', tone: 'ghost', onClick: () => {
        live = !live;
        if (glLiveTimer) { clearInterval(glLiveTimer); glLiveTimer = null; }
        if (live) {
          glLiveTimer = setInterval(() => setImg(false), refreshSec * 1000);
          liveBtn.text = `LIVE ON (${refreshSec}s)`;
          cap.text = `Live preview (auto-refresh ${refreshSec}s, registered proxy).`;
        } else {
          liveBtn.text = 'LIVE PREVIEW';
          cap.text = p.credit ? `Snapshot: ${String(p.credit).slice(0,120)}` : 'Snapshot preview (registered source).';
        }
      }});
      const streamBtn = btn('LOAD STREAM', { size: 'sm', tone: 'ghost', disabled: !hasStream, onClick: async () => {
        if (videoMode) return;
        streamBtn.disabled = true; streamBtn.text = 'LOADING…';
        try {
          const info = await api.get(`/api/cctv/stream/info?id=${encodeURIComponent(String(p.id))}`);
          const proxied = info && info.proxied_url;
          const stype = String((info && info.feed_type) || feedType || '').toLowerCase();
          if (!proxied) { toast('No registered live stream for this camera; snapshot preview available.', 'warn'); cap.text = 'No registered live stream URL.'; return; }
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
                hls.loadSource(proxied); hls.attachMedia(video);
                hls.on(Hls.Events.ERROR, (_, data) => { if (data && data.fatal) cap.text = `HLS error: ${data.type || 'fatal'}. Try snapshot preview or source link.`; });
              } else {
                video.src = proxied;
                cap.text = 'HLS stream requested via registered proxy. hls.js could not be loaded; try native HLS support.';
              }
            }
          } else {
            video.src = proxied;
          }
          video.onerror = () => { cap.text = 'Stream playback failed. Use snapshot preview or open source link.'; };
          if (glLiveTimer) { clearInterval(glLiveTimer); glLiveTimer = null; live = false; liveBtn.text = 'LIVE PREVIEW'; }
          videoMode = true; img.replaceWith(video);
          cap.text = `Live stream (registered proxy${info.credit ? ` · ${String(info.credit).slice(0,70)}` : ''}).`;
          streamBtn.text = 'STREAM LOADED';
        } catch (err) {
          toast(err.message || 'Could not load stream', 'error');
          cap.text = `Stream load failed: ${String(err.message || err).slice(0,120)}`;
        } finally { if (!videoMode) { streamBtn.disabled = false; streamBtn.text = 'LOAD STREAM'; } }
      }});
      img.onerror = () => { if (!videoMode) cap.text = hasStream ? `No proxied image preview. Use LOAD STREAM for registered ${String(feedType||'video').toUpperCase()} playback when supported.` : 'No snapshot preview available from registered source.'; };
      img.onload = () => { if (!live && !videoMode) cap.text = p.credit ? `Snapshot: ${String(p.credit).slice(0,120)}` : 'Snapshot preview (registered source).'; };
      setImg(false);
      wrap.append(img, cap, h('div', { class: 'row between items-center gap-6 wrap', style: { padding: '6px 8px', borderTop: '1px solid var(--line)' } },
        h('div', { class: 'tiny dim', text: feedType ? `Feed: ${feedType.toUpperCase()}` : 'Feed: snapshot/image' }),
        h('div', { class: 'row gap-4 wrap' }, liveBtn, hasStream ? streamBtn : null)));
      if (hasStream) wrap.appendChild(h('div', { class: 'tiny dim', style: { padding: '0 8px 8px' }, text: 'Registered stream proxy (same-host subresources, bounded). HLS may require native browser support or vendored hls.js.' }));
      children.push(wrap);
      if (p.license) children.push(h('div', { class: 'tiny dim', text: `License/terms: ${String(p.license).slice(0, 180)}` }));
    }
    for (const [k, v] of rows) {
      children.push(h('div', { class: 'row between gap-8' },
        h('span', { class: 'tiny muted', text: k }),
        h('span', { class: 'tiny', style: { textAlign: 'right', overflowWrap: 'anywhere' }, text: fmtProp(k, v) })
      ));
    }
    if (lngLat && !store.settings.opsec_mode) children.push(h('div', { class: 'tiny muted mt-4', text: `${lngLat.lat.toFixed(5)}, ${lngLat.lng.toFixed(5)}` }));
    const actions = h('div', { class: 'row gap-6 mt-6 wrap' });
    if (p.url) actions.appendChild(h('a', { class: 'badge cyan', href: p.url, target: '_blank', rel: 'noopener noreferrer', text: 'OPEN SOURCE' }));
    if (lngLat && !store.settings.opsec_mode) actions.appendChild(btn('COPY COORDS', { size:'sm', tone:'ghost', onClick: async () => {
      const text = `${lngLat.lat.toFixed(6)}, ${lngLat.lng.toFixed(6)}`;
      try { await navigator.clipboard?.writeText(text); toast('Coordinates copied'); } catch { toast(text, 'warn'); }
    }}));
    if (actions.childNodes.length) children.push(actions);
    inspector.replaceChildren(h('div', { class: 'col gap-4' }, ...children));
    try {
      popup?.remove();
      if (map && lngLat) {
        const meta = popupMeta(p);
        const title = escapeHtml(p.title || p.name || p.callsign || 'Selected feature');
        const kicker = escapeHtml(String(p.kind || 'feature').toUpperCase());
        const metaHtml = meta.length ? `<div style="opacity:.78;margin-top:5px;line-height:1.45">${meta.map(escapeHtml).join(' · ')}</div>` : '';
        const summary = p.summary ? `<div style="opacity:.72;margin-top:6px;line-height:1.4">${escapeHtml(String(p.summary).slice(0, 180))}</div>` : '';
        const credit = p.credit ? `<div style="opacity:.62;margin-top:4px">Credit: ${escapeHtml(String(p.credit).slice(0, 120))}</div>` : '';
        const time = p.time && !meta.some(m=>String(m).includes(String(p.time).slice(0,10))) ? `<div style="opacity:.6;margin-top:4px">${escapeHtml(fmtProp('time', p.time))}</div>` : '';
        popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, maxWidth: '320px', className: 'ds-gl-popup' })
          .setLngLat(lngLat)
          .setHTML(`<div style="font-size:11px;line-height:1.35"><div style="opacity:.65;text-transform:uppercase;letter-spacing:.05em;font-size:9px">${kicker}</div><div style="font-weight:600;margin-top:2px;color:#eafff2;word-break:break-word">${title}</div>${metaHtml}${summary}${credit}${time}</div>`)
          .addTo(map);
      }
    } catch {}
  }

  map.on('error', (e) => {
    console.warn('map-gl error', e?.error || e);
    const msg = e?.error?.message || e?.message || 'tile/source error';
    statusBox.textContent = `Map issue: ${String(msg).slice(0, 120)}`;
    if (mbInfo.vector && !/Style is not done loading/i.test(String(msg))) {
      showMapError(`Vector basemap error: ${String(msg).slice(0, 220)}. Archive layers: ${(mbInfo.layers || []).join(", ") || "unknown"}.`);
    }
  });
  map.on('mousemove', (e) => {
    if (store.settings.opsec_mode) { coordBox.textContent = 'COORDS MASKED (OPSEC)'; return; }
    coordBox.textContent = `${e.lngLat.lat.toFixed(5)}, ${e.lngLat.lng.toFixed(5)} · z${map.getZoom().toFixed(1)} · pitch ${Math.round(map.getPitch())}°`;
  });
  map.on('zoomend', () => {
    if (!store.settings.opsec_mode) coordBox.textContent = `${map.getCenter().lat.toFixed(5)}, ${map.getCenter().lng.toFixed(5)} · z${map.getZoom().toFixed(1)} · pitch ${Math.round(map.getPitch())}°`;
  });
  updateStats();
  map.on('load', () => {
    if (spaceBackground) applySpaceBackground(map, lightTheme);
    try { if (typeof map.setProjection === 'function') map.setProjection({ type:'globe' }); } catch (e) { console.warn('Initial globe projection failed', e); }
    // The layout can settle after the style's first frame; resize/repaint so
    // vector geometry is not left unpainted (black map) until user movement.
    try { map.resize(); map.triggerRepaint(); } catch {}
    setTimeout(() => { try { map.resize(); map.triggerRepaint(); } catch {} }, 600);
    statusBox.textContent = `GL map ready · Globe projection · Local/offline basemap · DEM ${demStatus?.available ? `ready (${demStatus.encoding || "terrarium"})` : "not configured"}. Refresh data to load cached intel layers.`;
    // DEM terrain/hillshade hooks (offline local raster-dem tiles only)
    if (demStatus?.available && demStatus.tile_url) {
      try {
        const demTiles = `${location.origin}${demStatus.tile_url}`;
        map.addSource('dem', { type: 'raster-dem', tiles: [demTiles], tileSize: 256, encoding: demStatus.encoding || 'terrarium', maxzoom: 14 });
        map.addLayer({ id: 'hillshade', type: 'hillshade', source: 'dem', paint: { 'hillshade-exaggeration': 0.28, 'hillshade-shadow-color': 'rgba(0,0,0,0.22)' } });
        if (state.show.terrain && typeof map.setTerrain === 'function') map.setTerrain({ source: 'dem', exaggeration: 1.15 });
      } catch (e) { console.warn('DEM setup failed', e); }
    }
    const dn = dayNightGeoJSON();
    map.addSource('night', { type:'geojson', data: dn.night });
    map.addLayer({ id:'night-fill', type:'fill', source:'night', paint:{ 'fill-color':'rgba(5,12,30,0.22)' } });
    map.addSource('term', { type:'geojson', data: dn.term });
    map.addLayer({ id:'term-line', type:'line', source:'term', paint:{ 'line-color':'rgba(148,197,255,0.45)', 'line-width':1.2 } });
    map.addSource('sunpt', { type:'geojson', data: dn.sun });
    map.addLayer({ id:'sun-pt', type:'circle', source:'sunpt', paint:{ 'circle-radius':4, 'circle-color':'rgba(255,220,120,0.85)', 'circle-stroke-color':'rgba(255,220,120,0.35)', 'circle-stroke-width':5 } });

    map.addSource('home', { type:'geojson', data: fc([pt(homeLon,homeLat,{kind:'home',title:'Configured position',source:positionInfo.mapLabel})].filter(Boolean)) });
    map.addLayer({ id:'home-halo', type:'circle', source:'home', paint:{ 'circle-radius':10,'circle-color':'rgba(72,215,255,0.14)','circle-stroke-color':'#48d7ff','circle-stroke-width':1.5 } });
    map.addLayer({ id:'home-dot', type:'circle', source:'home', paint:{ 'circle-radius':3.5,'circle-color':'#48d7ff' } });

    map.addSource('waypoints', { type:'geojson', data: fc(waypoints.map(w=>pt(w.lon,w.lat,{kind:'waypoint',title:w.name,type:w.type||'WAYPOINT',note:(w.note||'').slice(0,180)})).filter(Boolean)) });
    map.addLayer({ id:'waypoints', type:'circle', source:'waypoints', paint:{
      'circle-radius':['interpolate',['linear'],['zoom'],2,2.5,8,4.5,12,6],
      'circle-color':['match',['get','type'],'HAZARD','#ff5c5c','WATER','#48d7ff','CAMP','#ffb02e','CACHE','#c78bff','#5dff9a'],
      'circle-stroke-color':'#03110a','circle-stroke-width':1 } });

    map.addSource('tracks', { type:'geojson', data: tracksGeo() });
    map.addLayer({ id:'tracks', type:'line', source:'tracks', paint:{ 'line-color':'rgba(93,255,154,0.55)','line-width':2 } });

    map.addSource('hazards', { type:'geojson', data: intelFeaturesFiltered('hazards') });
    map.addLayer({ id:'hazards', type:'circle', source:'hazards', paint:{
      'circle-radius':['case',['in',['get','severity'],['literal',['CRITICAL','SEVERE','EXTREME']]],5.2,3.4],
      'circle-color':['case',['in',['get','severity'],['literal',['CRITICAL','SEVERE','EXTREME']]],'#ff5c5c',['==',['get','severity'],'ALERT'],'#ffb02e','#5dff9a'],
      'circle-opacity':0.88,'circle-stroke-color':'rgba(0,0,0,.35)','circle-stroke-width':1 } });

    map.addSource('conflict', { type:'geojson', data: intelFeaturesFiltered('conflict') });
    map.addLayer({ id:'conflict', type:'circle', source:'conflict', paint:{
      'circle-radius':['case',['==',['get','verified'],true],4.8,4],
      'circle-color':['case',['==',['get','severity'],'CRITICAL'],'#ff5c5c',['==',['get','verified'],true],'#ff8a3d','#ffb02e'],
      'circle-opacity':0.86,'circle-stroke-color':'rgba(0,0,0,.35)','circle-stroke-width':1 } });

    map.addSource('aircraft-hdg', { type:'geojson', data: aircraftHeadingGeo() });
    map.addLayer({ id:'aircraft-hdg', type:'line', source:'aircraft-hdg', paint:{ 'line-color':'rgba(72,215,255,0.45)','line-width':1.4 } });
    map.addSource('aircraft', { type:'geojson', data: aircraftGeo() });
    map.addLayer({ id:'aircraft', type:'circle', source:'aircraft', paint:{
      'circle-radius':['interpolate',['linear'],['coalesce',['get','alt_m'],0],0,3.0,3000,3.6,10000,4.4,13000,4.8],
      'circle-color':'#48d7ff','circle-stroke-color':'#061c24','circle-stroke-width':1,
    } });
    map.addSource('sats', { type:'geojson', data: satsGeo() });
    map.addLayer({ id:'sats', type:'circle', source:'sats', paint:{
      'circle-radius':['interpolate',['linear'],['coalesce',['get','alt_km'],500],400,2.8,2000,3.2,20000,4.2,36000,4.8],
      'circle-color':['case',['==',['get','visible'],true],'#c78bff','#7a5a96'],
      'circle-opacity':['case',['==',['get','visible'],true],0.95,0.5],
      'circle-stroke-color':'#1b1024',
      'circle-stroke-width':1,
    } });
    map.addSource('sat-footprint', { type:'geojson', data: fc([]) });
    map.addLayer({ id:'sat-footprint-fill', type:'fill', source:'sat-footprint', paint:{ 'fill-color':'#c78bff','fill-opacity':0.035 } });
    map.addLayer({ id:'sat-footprint', type:'line', source:'sat-footprint', paint:{ 'line-color':'#c78bff','line-width':1.2,'line-dasharray':[3,2],'line-opacity':0.55 } });
    map.addSource('sat-track', { type:'geojson', data: fc([]) });
    map.addLayer({ id:'sat-track', type:'line', source:'sat-track', paint:{ 'line-color':'#c78bff','line-width':1.5,'line-dasharray':[3,2],'line-opacity':0.65 } });

    function addClustered(name, data, opts) {
      const { color, stroke, clusterMaxZoom = 13, clusterRadius = 45, pointPaint = {} } = opts;
      map.addSource(name, { type: 'geojson', data, cluster: true, clusterMaxZoom, clusterRadius });
      map.addLayer({ id: `${name}-cluster`, type: 'circle', source: name, filter: ['has', 'point_count'], paint: {
        'circle-color': color,
        'circle-radius': ['step', ['get', 'point_count'], 10, 20, 14, 100, 18, 500, 24],
        'circle-stroke-color': stroke || 'rgba(0,0,0,.45)',
        'circle-stroke-width': 1.5,
        'circle-opacity': 0.78,
      }});
      map.addLayer({ id: `${name}-pt`, type: 'circle', source: name, filter: ['!', ['has', 'point_count']], paint: Object.assign({
        'circle-radius': ['interpolate',['linear'],['zoom'],1,3.8,6,3.4,10,4.4,14,5.2],
        'circle-color': color,
        'circle-stroke-color': stroke || 'rgba(0,0,0,.45)',
        'circle-stroke-width': 1,
        'circle-opacity': 0.95,
      }, pointPaint) });
    }
    addClustered('maritime', maritimeGeo(), { color: '#35c9ff', stroke: '#06222b' });
    addClustered('cables', cablesGeo(), { color: '#ffd166', stroke: '#2b1f00' });
    addClustered('cctv', cctvGeo(), {
      color: '#b7ff6a', stroke: '#173000',
      pointPaint: {
        'circle-color': ['case', ['==', ['get','alpr'], true], '#ff66cc', '#b7ff6a'],
        'circle-stroke-color': ['case', ['==', ['get','alpr'], true], '#33001f', '#102100'],
        'circle-radius':['interpolate',['linear'],['zoom'],1,3.8,4,3.2,8,['case',['==',['get','alpr'],true],4.8,4.0],12,['case',['==',['get','alpr'],true],5.6,4.8],14,5.8],
      }
    });
    // Global ALPR from DeFlock. Points are shown when zoomed in; clusters are
    // shown at low zoom so the full national dataset stays navigateable.
    map.addSource('alpr-points', { type:'geojson', data: alprPointsGeo() });
    map.addLayer({ id:'alpr-points', type:'circle', source:'alpr-points', paint:{
      'circle-radius':['interpolate',['linear'],['zoom'],1,4.2,5,5.0,9,6.2,14,7.5],
      'circle-color':'#ff66cc',
      'circle-stroke-color':'#33001f',
      'circle-stroke-width':1.4,
      'circle-opacity':0.96,
    } });
    map.addSource('alpr-clusters', { type:'geojson', data: alprClustersGeo() });
    map.addLayer({ id:'alpr-clusters', type:'circle', source:'alpr-clusters', paint:{
      'circle-radius':['interpolate',['linear'],['get','count'],1,8,100,10,1000,14,10000,18],
      'circle-color':'#ff66cc',
      'circle-opacity':0.62,
      'circle-blur':0.2,
      'circle-stroke-color':'#33001f',
      'circle-stroke-width':1.2,
    } });

    map.addSource('rain', { type:'geojson', data: overlayGeo('rain') });
    map.addLayer({ id:'rain-pt', type:'circle', source:'rain', paint:{
      'circle-radius':['interpolate',['linear'],['zoom'],2,3,8,7,12,11],
      'circle-color':'rgba(86,170,255,0.55)',
      'circle-stroke-color':'rgba(86,170,255,0.25)','circle-stroke-width':1,
      'circle-opacity':['case',['>', ['coalesce',['get','precip_mm'],['get','rain_mm'],0], 0.5],0.6,0.28] } });
    map.addSource('cloud', { type:'geojson', data: overlayGeo('cloud') });
    map.addLayer({ id:'cloud-pt', type:'circle', source:'cloud', paint:{
      'circle-radius':['interpolate',['linear'],['zoom'],2,4,8,9,12,14],
      'circle-color':'rgba(220,230,245,0.22)',
      'circle-stroke-color':'rgba(220,230,245,0.12)','circle-stroke-width':1,
      'circle-opacity':['interpolate',['linear'],['coalesce',['get','cloud_pct'],0],0,0.08,50,0.22,100,0.38] } });
    map.addSource('wind', { type:'geojson', data: overlayGeo('wind') });
    map.addLayer({ id:'wind-pt', type:'circle', source:'wind', paint:{ 'circle-radius':2.8,'circle-color':'rgba(180,255,220,0.75)','circle-stroke-color':'rgba(180,255,220,0.25)','circle-stroke-width':4 } });
    map.addSource('wind-dir', { type:'geojson', data: windDirGeo() });
    map.addLayer({ id:'wind-dir', type:'line', source:'wind-dir', paint:{ 'line-color':'rgba(180,255,220,0.55)','line-width':1.6 } });

    for (const [key,on] of Object.entries(state.show)) setVis(key,on);

    const clickIds = ['hazards','conflict','aircraft','sats','maritime-pt','cables-pt','cctv-pt','alpr-points','wind-pt','rain-pt','cloud-pt','waypoints','home-dot'];
    for (const lid of clickIds) {
      map.on('mouseenter', lid, ()=>{ map.getCanvas().style.cursor='pointer'; });
      map.on('mouseleave', lid, ()=>{ map.getCanvas().style.cursor=''; });
      map.on('click', lid, (e)=>{ const f=e.features?.[0]; if(!f) return; showInspector(f.properties||{}, e.lngLat); });
    }
    map.on('click', (e) => { try { if (!map.queryRenderedFeatures(e.point).length) { clearSatelliteFootprint(); clearSatelliteTrack(); } } catch {} });
    map.on('mouseenter', 'alpr-clusters', () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', 'alpr-clusters', () => { map.getCanvas().style.cursor = ''; });
    map.on('click', 'alpr-clusters', (e) => {
      const f = e.features?.[0];
      if (!f) return;
      map.easeTo({ center: f.geometry.coordinates, zoom: Math.min(18, map.getZoom() + 2), duration: 500 });
    });
    map.on('moveend', scheduleAlprFetch);
    for (const cid of ['maritime-cluster','cables-cluster','cctv-cluster']) {
      map.on('mouseenter', cid, ()=>{ map.getCanvas().style.cursor='pointer'; });
      map.on('mouseleave', cid, ()=>{ map.getCanvas().style.cursor=''; });
      map.on('click', cid, (e) => {
        const features = map.queryRenderedFeatures(e.point, { layers: [cid] });
        const clusterId = features[0]?.properties?.cluster_id;
        const src = features[0]?.source;
        if (clusterId == null || !src) return;
        const source = map.getSource(src);
        const go = (err, zoom) => { if (!err && zoom != null) map.easeTo({ center: features[0].geometry.coordinates, zoom: Math.min(zoom + 0.5, 18) }); };
        try {
          const p = source.getClusterExpansionZoom(clusterId, go);
          if (p && typeof p.then === 'function') p.then(z => go(null, z)).catch(() => {});
        } catch {}
      });
    }

    daynightTimer = setInterval(()=>{ try { const dn=dayNightGeoJSON(); map.getSource('night')?.setData(dn.night); map.getSource('term')?.setData(dn.term); map.getSource('sunpt')?.setData(dn.sun); } catch {} }, 60000);
    // Move aircraft/satellites every second between API snapshots; poll the
    // lightweight moving endpoint without re-fetching the whole intel picture.
    const animateMoving = () => {
      if (!map || document.hidden) return;
      const put = (id, data) => { try { map.getSource(id)?.setData(data); } catch {} };
      if (state.show.aircraft) { put('aircraft', aircraftGeo()); put('aircraft-hdg', aircraftHeadingGeo()); }
      if (state.show.sats) put('sats', satsGeo());
    };
    movingTimer = setInterval(animateMoving, 1000);
    movingPollTimer = setInterval(refreshMoving, 20000);
    scheduleLive();
    refreshAlpr(true);
  });

  return () => { try { clearInterval(daynightTimer); clearInterval(glLiveTimer); clearInterval(liveTimer); clearInterval(freshTimer); clearInterval(movingTimer); clearInterval(movingPollTimer); clearInterval(glRepaintTimer); clearTimeout(alprFetchTimer); glResize?.disconnect(); popup?.remove(); map?.remove(); } catch {} };
}
