/* WebGL/3D prototype map (MapLibre GL) alongside the classic canvas map.
   Uses local/offline-capable raster tiles: /api/map/tiles/{z}/{x}/{y}.png
   Raster style (no external glyphs/sprites). True DEM terrain requires raster-dem tiles. */
import { api } from "../api.js";
import { store } from "../store.js";
import { h, pageHead, btn, alertStrip, loading, fmt, toast, panel, checkbox, select, field, badge } from "../components.js";
import { positionConfidence, opsecActive } from "./shared.js?v=opsec-1";
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

function rasterStyle() {
  const tile = `${location.origin}/api/map/tiles/{z}/{x}/{y}.png`;
  return {
    version: 8,
    name: "DarkSec Raster",
    sources: { local: { type: "raster", tiles: [tile], tileSize: 256, attribution: "Local/offline tiles · DarkSec" } },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#030806" } },
      { id: "local-raster", type: "raster", source: "local", paint: { "raster-opacity": 0.92, "raster-fade-duration": 120 } }
    ]
  };
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

export async function render(root) {
  root.replaceChildren(loading("INITIALISING GL MAP"));
  const settings = store.settings || {};
  const homeLat = Number.isFinite(Number(settings.lat)) ? Number(settings.lat) : 39.8283;
  const homeLon = Number.isFinite(Number(settings.lon)) ? Number(settings.lon) : -98.5795;
  const hidePosition = opsecActive(settings);
  const positionInfo = positionConfidence(settings);
  let intel, waypoints, tracks, demStatus, overlayStatus, overlayWind, overlayRain, overlayCloud;
  let statusBox = null, coordBox = null;
  try {
    [intel, waypoints, tracks, demStatus, overlayStatus, overlayWind, overlayRain, overlayCloud] = await Promise.all([
      api.get("/api/map/intel").catch(() => null),
      api.get("/api/records/waypoints?limit=2000").then(r=>r.items||[]).catch(() => []),
      api.get("/api/records/tracks?limit=500").then(r=>r.items||[]).catch(() => []),
      api.get("/api/map/dem/status").catch(() => ({ available:false })),
      api.get("/api/map/overlays/status").catch(() => ({ overlays:[] })),
      api.get("/api/map/overlays/wind").catch(() => null),
      api.get("/api/map/overlays/rain").catch(() => null),
      api.get("/api/map/overlays/cloud").catch(() => null),
    ]);
  } catch (err) { root.replaceChildren(h("div", { class: "callout danger", text: err.message })); return; }

  let maplibregl;
  try { maplibregl = await loadMapLibre(); } catch (err) { root.replaceChildren(h("div", { class: "callout danger", text: err.message })); return; }

  const state = { maxAgeHours: 0, show: { hazards:true, conflict:true, aircraft:true, sats:true, maritime:true, cables:true, cctv:true, cctvAlpr:true, wind:false, rain:false, cloud:false, terrain: !!demStatus?.available, waypoints:true, tracks:true, daynight:true, home:!hidePosition } };
  let map = null, popup = null, daynightTimer = null, glLiveTimer = null, inspector = null;

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "COMMAND // TERRAIN (GL PROTOTYPE)",
    title: "Map & Terrain — 3D/GL",
    desc: `WebGL situational map over local/offline raster tiles. Pitch/rotate for 3D perspective. Position: ${hidePosition ? "OP//MASKED" : positionInfo.mapLabel}. DEM terrain, wind/rain/cloud overlays, and maritime/cables/CCTV layers use local cache when available.`,
    actions: [
      btn("2D CANVAS", { icon: "map", onClick: () => { location.hash = "#/map?renderer=2d"; } }),
      btn("3D GL", { icon: "layers", tone: "primary", onClick: () => { location.hash = "#/map?renderer=gl"; } }),
      btn("RESET", { icon: "target", onClick: () => map?.easeTo({ center: hidePosition ? [0,20] : [homeLon,homeLat], zoom: hidePosition ? 1.6 : 4, pitch:0, bearing:0 }) }),
      btn("PITCH 45°", { icon: "layers", onClick: () => map?.easeTo({ pitch:45, duration:600 }) }),
      btn("GLOBE/FLAT", { icon: "globe", onClick: toggleProjection }),
      btn("REFRESH OVERLAYS", { icon: "refresh", tone:"ghost", onClick: refreshOverlays }),
    ],
  }));
  root.appendChild(alertStrip(`Local/offline basemap + cached intel. DEM ${demStatus?.available ? 'available (' + (demStatus.encoding||'terrarium') + ')' : 'not configured/cached'} · overlays cached via operator refresh (online required to fetch). Cables auto-populate from a cached public dataset on feed refresh (online first time; attribution required). CCTV can load nearby OSM surveillance locations via feed refresh when operator position is set. Maritime/AIS works with a configured GeoJSON/JSON source or local file (fielddata/maps/ais.geojson).`, demStatus?.available ? "cyan" : "amber"));

  const layout = h("div", { class: "map-layout mt-14" });
  const wrap = h("div", { class: "map-shell", style: { height: "min(72vh, 820px)", minHeight: "460px", position: "relative" } });
  const container = h("div", { style: { position: "absolute", inset: "0" } });
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
      position: "absolute", left: "10px", bottom: "10px", zIndex: "3",
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
  for (const [key,label] of [
    ["hazards","Hazard/disaster events"],["conflict","Conflict events"],
    ["aircraft","Aircraft"],["sats","Satellites"],["maritime","Maritime/AIS"],
    ["cables","Cables/landing points"],["cctv","CCTV/cameras (OSM)"],
    ["wind","Wind overlay (cached)"],["rain","Rain/precip overlay (cached)"],["cloud","Cloud overlay (cached)"],
    ["terrain","DEM terrain/hillshade (if available)"],
    ["waypoints","Waypoints"],["tracks","Tracks"],["daynight","Day/night terminator"],["home","Home/position"],
  ]) {
    layerBox.appendChild(checkbox(label, !!state.show[key], (on)=>{
      state.show[key]=on;
      if (["wind","rain","cloud"].includes(key)) refreshOverlaySources();
      if (["hazards","conflict","cctv"].includes(key)) refreshIntelSources();
      setVis(key,on);
    }));
  }
  layerBox.appendChild(h("div", { class:"tiny muted mt-6", text:"CCTV filters" }));
  layerBox.appendChild(checkbox("Show ALPR-tagged CCTV (OSM audited tags)", !!state.show.cctvAlpr, (on)=>{ state.show.cctvAlpr=on; refreshIntelSources(); }));
  const statsBox = h("div", { class: "tiny muted", text: "Loading..." });
  side.appendChild(panel({ title:"GL Layers", icon:"layers", body: h("div",{class:"col gap-8"},
    field("INTEL AGE", ageSel), layerBox,
    h("div", { class: "row wrap gap-6 mt-6" },
      btn("REFRESH DATA", { size:"sm", icon:"refresh", onClick: refreshData }),
      btn("FIT DATA", { size:"sm", tone:"ghost", onClick: fitData }))
  ) }));
  side.appendChild(panel({ title:"Situational Summary", icon:"globe", body: statsBox }));
  side.appendChild(panel({ title:"Inspector", icon:"info", body: inspector }));

  map = new maplibregl.Map({ container, style: rasterStyle(), center: hidePosition ? [0,20] : [homeLon,homeLat], zoom: hidePosition ? 1.6 : 3.5, pitch:0, bearing:0, attributionControl:true, maxPitch:85 });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch:true, showCompass:true, showZoom:true }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ maxWidth:120, unit: U.isImperial() ? "imperial" : "metric" }), "bottom-left");
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
      hazards:['hazards'], conflict:['conflict'], aircraft:['aircraft','aircraft-hdg'], sats:['sats'],
      maritime:['maritime-cluster','maritime-pt'], cables:['cables-cluster','cables-pt'], cctv:['cctv-cluster','cctv-pt'],
      wind:['wind-pt','wind-dir'], rain:['rain-pt'], cloud:['cloud-pt'],
      terrain:['hillshade'],
      waypoints:['waypoints'], tracks:['tracks'], daynight:['night-fill','term-line','sun-pt'], home:['home-halo','home-dot'],
    }[key] || [];
    for (const id of ids) try { if (map.getLayer(id)) map.setLayoutProperty(id,'visibility', on?'visible':'none'); } catch {}
    if (key === 'terrain') {
      try {
        if (on && demStatus?.available && map.getSource('dem')) map.setTerrain({ source: 'dem', exaggeration: 1.15 });
        else if (typeof map.setTerrain === 'function') map.setTerrain(null);
      } catch {}
    }
  }

  function intelFeaturesFiltered(kind){
    const features = (intel?.features||[]).filter(f=>f?.geometry?.type==='Point');
    const maxAge = state.maxAgeHours;
    const passAge = (p)=> maxAge<=0 || p?.age_hours==null || Number(p.age_hours)<=maxAge;
    if (kind==='hazards') return fc(features.filter(f=>{ const p=f.properties||{}; const c=p.kind==='conflict'||p.feed_id==='gdelt_conflict'||p.feed_id==='ucdp_candidate'; return !c && passAge(p); }));
    return fc(features.filter(f=>{ const p=f.properties||{}; const c=p.kind==='conflict'||p.feed_id==='gdelt_conflict'||p.feed_id==='ucdp_candidate'; return c && passAge(p); }));
  }
  function aircraftGeo(){
    return fc((intel?.aircraft||[]).slice(0,250).map(a=>pt(a.lon,a.lat,{ kind:'aircraft', title:a.callsign||'Aircraft', alt_m:a.alt_m, speed_kmh:a.speed_kmh, heading:a.heading_deg, source:a.source||'OpenSky', url:a.url, time:a.time })).filter(Boolean));
  }
  function aircraftHeadingGeo(){
    return fc((intel?.aircraft||[]).slice(0,250).map(a=>{
      const lo=num(a.lon),la=num(a.lat),hdg=num(a.heading_deg),spd=num(a.speed_kmh);
      if(lo==null||la==null||hdg==null) return null;
      const km = Math.max(8, Math.min(60, (spd||200)*0.08));
      const end = destination(lo,la,hdg,km);
      return { type:"Feature", properties:{}, geometry:{ type:"LineString", coordinates:[[lo,la], end] } };
    }).filter(Boolean));
  }
  function satsGeo(){
    return fc((intel?.satellites||[]).slice(0,150).map(s=>pt(s.lon,s.lat,{ kind:'sat', title:s.name||`NORAD ${s.norad||''}`, norad:s.norad, alt_km:s.alt_km, elevation_deg:s.elevation_deg, source:s.source||'CelesTrak/SGP4', time:s.time })).filter(Boolean));
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
    const items = (intel?.cctv||[]).filter(c => state.show.cctvAlpr !== false || !c.alpr);
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
    statsBox.textContent = `Hazards ${hz} · Conflict ${cf} · Aircraft ${(intel?.aircraft||[]).length} · Sats ${(intel?.satellites||[]).length} · Maritime ${(intel?.maritime||[]).length} · Cables ${(intel?.cables||[]).length} · CCTV ${(intel?.cctv||[]).length} · Wind ${ov(overlayWind)} · Rain ${ov(overlayRain)} · Cloud ${ov(overlayCloud)}${summary.newest_age_hours!=null ? ` · newest intel ${Math.round(summary.newest_age_hours)}h` : ''}`;
  }
  function refreshOverlaySources(){
    if (!map) return;
    const s = (id,data)=>{ try { if (map.getSource(id)) map.getSource(id).setData(data); } catch {} };
    s('wind', overlayGeo('wind')); s('wind-dir', windDirGeo()); s('rain', overlayGeo('rain')); s('cloud', overlayGeo('cloud'));
  }
  function refreshIntelSources(){
    if (!map) return;
    const s = (id,data)=>{ try { if (map.getSource(id)) map.getSource(id).setData(data); } catch {} };
    s('hazards', intelFeaturesFiltered('hazards')); s('conflict', intelFeaturesFiltered('conflict'));
    s('aircraft', aircraftGeo()); s('aircraft-hdg', aircraftHeadingGeo()); s('sats', satsGeo());
    s('maritime', maritimeGeo()); s('cables', cablesGeo()); s('cctv', cctvGeo());
    updateStats();
  }
  async function refreshData(){
    toast('Refreshing map data', 'warn');
    try {
      const [i,w,t,ds] = await Promise.all([
        api.get('/api/map/intel').catch(()=>null),
        api.get('/api/records/waypoints?limit=2000').then(r=>r.items||[]).catch(()=>[]),
        api.get('/api/records/tracks?limit=500').then(r=>r.items||[]).catch(()=>[]),
        api.get('/api/map/dem/status').catch(()=>null),
      ]);
      intel = i; waypoints = w; tracks = t; if (ds) demStatus = ds;
      if (map?.getSource('waypoints')) map.getSource('waypoints').setData(fc(waypoints.map(wp=>pt(wp.lon,wp.lat,{kind:'waypoint',title:wp.name,type:wp.type||'WAYPOINT',note:(wp.note||'').slice(0,180)})).filter(Boolean)));
      if (map?.getSource('tracks')) map.getSource('tracks').setData(tracksGeo());
      refreshIntelSources();
      toast('Map data refreshed');
    } catch (e) { toast(e.message || 'Refresh failed', 'error'); }
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
    for (const s of (intel?.satellites||[]).slice(0,150)) if(Number.isFinite(Number(s.lon))&&Number.isFinite(Number(s.lat))) coords.push([Number(s.lon),Number(s.lat)]);
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
    try { if (typeof map.setProjection === 'function') map.setProjection({ type:'globe' }); } catch (e) { console.warn('Initial globe projection failed', e); }
    statusBox.textContent = `GL map loaded · globe projection (toggle available) · local raster tiles /api/map/tiles · DEM ${demStatus?.available ? 'ready (' + (demStatus.encoding||'terrarium') + ')' : 'not configured/cached'}. Refresh feeds for cables/CCTV/maritime, then enable/inspect layers.`;
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
    map.addLayer({ id:'aircraft', type:'circle', source:'aircraft', paint:{ 'circle-radius':3.4,'circle-color':'#48d7ff','circle-stroke-color':'#061c24','circle-stroke-width':1 } });
    map.addSource('sats', { type:'geojson', data: satsGeo() });
    map.addLayer({ id:'sats', type:'circle', source:'sats', paint:{ 'circle-radius':3.2,'circle-color':'#c78bff','circle-stroke-color':'#1b1024','circle-stroke-width':1 } });

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

    const clickIds = ['hazards','conflict','aircraft','sats','maritime-pt','cables-pt','cctv-pt','wind-pt','rain-pt','cloud-pt','waypoints','home-dot'];
    for (const lid of clickIds) {
      map.on('mouseenter', lid, ()=>{ map.getCanvas().style.cursor='pointer'; });
      map.on('mouseleave', lid, ()=>{ map.getCanvas().style.cursor=''; });
      map.on('click', lid, (e)=>{ const f=e.features?.[0]; if(!f) return; showInspector(f.properties||{}, e.lngLat); });
    }
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
  });

  return () => { try { clearInterval(daynightTimer); clearInterval(glLiveTimer); popup?.remove(); map?.remove(); } catch {} };
}
