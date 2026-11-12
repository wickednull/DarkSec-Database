/* Navigation: coordinate systems, compass work, celestial almanac and fixes. */
import { api } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, pageHead, btn, input, field, select, loading, toast, fmt, badge,
} from "../components.js";
import { calculatorGrid } from "../calculators.js";
import {
  kv, errorBox, referenceSection, positionConfidence, manualPositionMetadata, gpsPositionMetadata,
  phoneGpsMetadata, opsecActive,
} from "./shared.js?v=opsec-1";
import * as U from "../units.js";

const COMPASS_SVG = (declination = 0, sunAz = null, moonAz = null) => {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 240 240");
  svg.setAttribute("class", "compass");
  const add = (tag, attrs = {}) => {
    const el = document.createElementNS(ns, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    svg.appendChild(el);
    return el;
  };
  add("circle", { cx: 120, cy: 120, r: 112, fill: "none", stroke: "rgba(93,255,154,.16)", "stroke-width": 1 });
  add("circle", { cx: 120, cy: 120, r: 96, fill: "none", stroke: "rgba(93,255,154,.09)", "stroke-width": 1 });
  for (let i = 0; i < 72; i++) {
    const a = (i * 5 - 90) * Math.PI / 180;
    const major = i % 9 === 0;
    const mid = i % 3 === 0;
    const r1 = major ? 104 : mid ? 108 : 110;
    add("line", {
      x1: 120 + Math.cos(a) * r1, y1: 120 + Math.sin(a) * r1,
      x2: 120 + Math.cos(a) * 114, y2: 120 + Math.sin(a) * 114,
      stroke: major ? "rgba(93,255,154,.85)" : "rgba(123,154,136,.45)",
      "stroke-width": major ? 1.6 : mid ? 1 : .6,
    });
  }
  for (const [label, deg] of [["N", 0], ["E", 90], ["S", 180], ["W", 270]]) {
    const a = (deg - 90) * Math.PI / 180;
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", 120 + Math.cos(a) * 84);
    t.setAttribute("y", 120 + Math.sin(a) * 84 + 4);
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("fill", label === "N" ? "#5dff9a" : "#7b9a88");
    t.setAttribute("font-size", "11");
    t.setAttribute("font-family", "JetBrains Mono, monospace");
    t.textContent = label;
    svg.appendChild(t);
  }
  const needle = (az, color, width, label) => {
    if (az === null || az === undefined || Number.isNaN(Number(az))) return;
    const a = (Number(az) - 90) * Math.PI / 180;
    const x = 120 + Math.cos(a) * 70, y = 120 + Math.sin(a) * 70;
    add("line", { x1: 120, y1: 120, x2: x, y2: y, stroke: color, "stroke-width": width, "stroke-linecap": "round" });
    const dot = add("circle", { cx: x, cy: y, r: 4, fill: color });
    dot.setAttribute("opacity", ".9");
    const t = document.createElementNS(ns, "text");
    t.setAttribute("x", x); t.setAttribute("y", y - 8);
    t.setAttribute("text-anchor", "middle"); t.setAttribute("fill", color);
    t.setAttribute("font-size", "8"); t.setAttribute("font-family", "JetBrains Mono, monospace");
    t.textContent = label;
    svg.appendChild(t);
  };
  needle(0, "rgba(123,154,136,.34)", 1, "");
  needle(sunAz, "#ffd166", 2, "SUN");
  needle(moonAz, "#48d7ff", 2, "MOON");
  add("circle", { cx: 120, cy: 120, r: 3, fill: "#5dff9a" });
  const t = document.createElementNS(ns, "text");
  t.setAttribute("x", 120); t.setAttribute("y", 151);
  t.setAttribute("text-anchor", "middle"); t.setAttribute("fill", "#7b9a88");
  t.setAttribute("font-size", "7.5"); t.setAttribute("font-family", "JetBrains Mono, monospace");
  t.textContent = `DECL ${Number(declination).toFixed(1)}°`;
  svg.appendChild(t);
  return svg;
};

function timeOrDash(v) {
  if (!v) return "—";
  const d = new Date(v);
  return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

export async function render(root, params) {
  root.replaceChildren(loading("COMPUTING NAVIGATION PICTURE"));
  const s = store.settings || {};
  const positionInfo = positionConfidence(s);
  const configuredLat = Number(s.lat), configuredLon = Number(s.lon);
  const lat = Number.isFinite(configuredLat) ? configuredLat : 39.8283;
  const lon = Number.isFinite(configuredLon) ? configuredLon : -98.5795;
  const elevation = Number(s.elevation_m) || 0;
  let coords, celestial, decl;
  try {
    [coords, celestial, decl] = await Promise.all([
      api.get(`/api/nav/coords?lat=${lat}&lon=${lon}`),
      api.get(`/api/nav/celestial?lat=${lat}&lon=${lon}&elevation=${elevation}`),
      api.get(`/api/nav/declination?lat=${lat}&lon=${lon}&elevation=${elevation}`),
    ]);
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }

  let disposed = false;
  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // NAVIGATION",
    title: "Navigation",
    desc: `Coordinate conversion, magnetic declination, celestial almanac and field resection. ${positionInfo.mapLabel}. Verify against a paper map and compass.`,
    actions: [
      btn("REFRESH", { icon: "refresh", onClick: () => render(root, params) }),
      btn("OPEN MAP", { icon: "map", onClick: () => { location.hash = "#/map"; } }),
      btn("GPS FIX", { icon: "satellite", tone: "primary", onClick: () => getGps() }),
    ],
  }));

  /* ------------------------------------------------------------ coordinate */
  const convertInput = input({ placeholder: "e.g. 39.8283, -98.5795", autocomplete: "off" });
  const convertType = select([
    { value: "dd", label: "Decimal degrees" },
    { value: "dms", label: "Degrees / minutes / seconds" },
    { value: "utm", label: "UTM (zone hemi E N)" },
    { value: "mgrs", label: "MGRS" },
  ], "dd");
  const convertOut = h("div", { class: "mt-10" });
  const convertBtn = btn("CONVERT", { tone: "primary", onClick: async () => {
    convertOut.replaceChildren(loading("CONVERTING"));
    try {
      const out = await api.post("/api/nav/convert", { type: convertType.value, value: convertInput.value });
      convertOut.replaceChildren(kv([
        ["DECIMAL", out.dd], ["DMS", out.dms], ["UTM", out.utm], ["MGRS", out.mgrs],
      ]), h("div", { class: "row mt-10" },
        btn("APPLY AS POSITION", { size: "sm", onClick: async () => {
          try {
            await store.saveSettings({ lat: out.lat, lon: out.lon, ...manualPositionMetadata() });
            toast("Position updated");
            render(root, params);
          } catch (err) { toast(err.message, "error"); }
        } }),
        btn("COPY MGRS", { size: "sm", tone: "ghost", onClick: () => navigator.clipboard?.writeText(out.mgrs).then(() => toast("MGRS copied")).catch(() => {}) }),
      ));
    } catch (err) {
      convertOut.replaceChildren(errorBox(err));
    }
  } });
  convertInput.addEventListener("keydown", (e) => { if (e.key === "Enter") convertBtn.click(); });

  const posBox = h("div", { class: "coord-block" });
  const hidePosition = opsecActive(s);
  posBox.append(
    coordLine("DECIMAL", hidePosition ? "OP//MASKED" : coords.dd),
    coordLine("DMS", hidePosition ? "OP//MASKED" : coords.dms),
    coordLine("UTM", hidePosition ? "OP//MASKED" : coords.utm),
    coordLine("MGRS", hidePosition ? "OP//MASKED" : coords.mgrs),
    coordLine("ELEVATION", hidePosition ? "OP//MASKED" : `${fmt.num(elevation, 0)} m`),
  );

  const converter = panel({
    title: "Position & conversion", icon: "globe",
    sub: "Configured coordinate · not live tracking",
    body: h("div", { class: "col" }, posBox, h("div", { class: "sep" }),
      h("div", { class: `callout ${positionInfo.tone} small`, text: positionInfo.text }),
      h("div", { class: "form-grid" }, field("Input format", convertType), field("Coordinate", convertInput, "Enter latitude/longitude in the selected format.")),
      h("div", { class: "row mt-10" }, convertBtn),
      convertOut),
  });

  /* ------------------------------------------------------------- compass */
  const compassPanel = panel({
    title: "Compass & declination", icon: "compass",
    sub: "Grid north is not magnetic north. Apply declination before plotting a magnetic bearing.",
    body: h("div", { class: "col" },
      h("div", { class: "compass-wrap" },
        COMPASS_SVG(decl.declination_deg, celestial.sun?.azimuth_deg, celestial.moon?.azimuth_deg),
        h("div", { class: "col" },
          kv([
            ["DECLINATION", `${Number(decl.declination_deg).toFixed(2)}° ${decl.declination_deg >= 0 ? "EAST" : "WEST"}`],
            ["INCLINATION", `${fmt.num(decl.inclination_deg, 2)}°`],
            ["HORIZONTAL INT.", `${fmt.num(decl.horizontal_intensity_nt, 0)} nT`],
            ["TOTAL INTENSITY", `${fmt.num(decl.total_intensity_nt, 0)} nT`],
            ["MODEL", `${decl.model} // ${decl.year}`],
          ]),
          h("div", { class: "callout small", text: "To convert a magnetic bearing to grid: add east declination, subtract west. Keep local steel and electronics clear of the compass." }),
        )),
      h("div", { class: "sun-table-wrap scroll-x mt-10" },
        h("table", { class: "data sun-table" },
          h("thead", {}, h("tr", {}, h("th", { text: "BODY" }), h("th", { text: "ALTITUDE" }), h("th", { text: "AZIMUTH" }))),
          h("tbody", {},
            bodyRow("SUN", celestial.sun?.altitude_deg, celestial.sun?.azimuth_deg),
            bodyRow("MOON", celestial.moon?.altitude_deg, celestial.moon?.azimuth_deg),
            ...(celestial.planets || []).map((p) => bodyRow(p.name, p.altitude_deg, p.azimuth_deg)),
          )))),
  });

  /* ----------------------------------------------------------------- GPS */
  const openPhoneSetup = () => {
    location.hash = "#/settings";
    toast("Open Position → Phone GPS · Browser to start a temporary phone fix", "warn");
  };
  const gpsOut = h("div", { class: "gps-box" }, h("div", { class: "small muted", text: "No GPS query yet. Uses local gpsd/gpspipe when installed, otherwise a fresh phone browser fix if a session is active." }));
  const gpsPanel = panel({
    title: "GPS receiver", icon: "satellite",
    sub: "Local gpsd or temporary phone browser session · no cloud service",
    tools: [
      btn("QUERY", { size: "sm", icon: "refresh", onClick: () => getGps() }),
      btn("PHONE GPS", { size: "sm", tone: "ghost", icon: "satellite", onClick: openPhoneSetup }),
    ],
    body: gpsOut,
  });

  async function getGps() {
    gpsOut.replaceChildren(loading("QUERYING GPS"));
    try {
      const gps = await api.get("/api/nav/gps");
      if (!gps.available) {
        gpsOut.replaceChildren(
          h("div", { class: "callout warn small", text: gps.note || "GPS tools are not installed." }),
          h("div", { class: "row mt-10" }, btn("OPEN PHONE GPS SETUP", { size: "sm", tone: "primary", icon: "satellite", onClick: openPhoneSetup })),
        );
        return;
      }
      if (!gps.fix) {
        gpsOut.replaceChildren(
          h("div", { class: "callout warn small", text: gps.note || "No GPS fix. Check antenna, sky view and gpsd." }),
          h("div", { class: "row mt-10" }, btn("OPEN PHONE GPS SETUP", { size: "sm", tone: "primary", icon: "satellite", onClick: openPhoneSetup })),
        );
        return;
      }
      gpsOut.replaceChildren(kv([
        ["FIX", gps.mode ? `MODE ${gps.mode}` : "YES"],
        ["LATITUDE", opsecActive(s) ? "OP//MASKED" : fmt.coord(gps.lat, gps.lon, 6)],
        ["ALTITUDE", opsecActive(s) ? "OP//MASKED" : `${fmt.num(gps.alt_m, 1)} m`],
        ["SPEED", `${fmt.num(gps.speed_mps, 2)} m/s`],
        ["TRACK", `${fmt.num(gps.track, 1)}°`],
        ["HORIZONTAL ERROR", gps.accuracy_m != null ? `±${fmt.num(gps.accuracy_m, 1)} m (estimate)` : "not reported"],
        ["TIME", timeOrDash(gps.time)],
        ["SOURCE", gps.source],
      ]), h("div", { class: "row mt-10" },
        btn("USE AS POSITION", { size: "sm", tone: "primary", onClick: async () => {
          try {
            const fromPhone = String(gps.source || "").toLowerCase().startsWith("phone");
            const metadata = fromPhone
              ? phoneGpsMetadata({ timestamp: gps.time, accuracy_m: gps.accuracy_m, received_at: new Date().toISOString() })
              : gpsPositionMetadata(gps);
            await store.saveSettings({
              lat: gps.lat, lon: gps.lon, elevation_m: gps.alt_m ?? elevation,
              ...metadata,
            });
            toast(fromPhone ? "Position updated from phone GPS" : "Position updated from GPS");
            render(root, params);
          } catch (err) { toast(err.message, "error"); }
        } }),
      ));
    } catch (err) {
      gpsOut.replaceChildren(errorBox(err));
    }
  }

  /* ---------------------------------------------------------- celestial */
  const sun = celestial.sun || {};
  const moon = celestial.moon || {};
  const celestialPanel = panel({
    title: "Celestial almanac", icon: "moon",
    sub: `Observer ${fmt.coord(lat, lon, 5)} · ${fmt.num(elevation, 0)} m · UTC ${fmt.date(celestial.utc)}`,
    body: h("div", { class: "col" },
      h("div", { class: "grid grid-3" },
        h("div", { class: "stat" }, h("div", { class: "stat-label", text: "SUN" }),
          h("div", { class: "stat-value", text: `${fmt.num(sun.altitude_deg, 1)}°` }),
          h("div", { class: "stat-foot", text: `az ${fmt.num(sun.azimuth_deg, 1)}°` })),
        h("div", { class: "stat" }, h("div", { class: "stat-label", text: "MOON" }),
          h("div", { class: "stat-value", text: moon.phase_name || "—" }),
          h("div", { class: "stat-foot", text: `${fmt.num(moon.illumination_pct, 0)}% lit · az ${fmt.num(moon.azimuth_deg, 0)}°` })),
        h("div", { class: "stat" }, h("div", { class: "stat-label", text: "DAYLIGHT" }),
          h("div", { class: "stat-value", text: celestial.week?.[0]?.day_length_h ? `${fmt.num(celestial.week[0].day_length_h, 1)} h` : "—" }),
          h("div", { class: "stat-foot", text: "today's sunrise to sunset" })),
      ),
      h("div", { class: "grid grid-2 mt-14" },
        h("div", {},
          h("div", { class: "section-title" }, h("h2", { text: "Sun events" }), h("div", { class: "line" })),
          kv([
            ["SUNRISE", timeOrDash(sun.next_rise)], ["SUNSET", timeOrDash(sun.next_set)],
            ["CIVIL DAWN", timeOrDash(sun.civil_dawn)], ["CIVIL DUSK", timeOrDash(sun.civil_dusk)],
            ["NAUTICAL DAWN", timeOrDash(sun.nautical_dawn)], ["NAUTICAL DUSK", timeOrDash(sun.nautical_dusk)],
            ["ASTRONOMICAL DAWN", timeOrDash(sun.astro_dawn)], ["ASTRONOMICAL DUSK", timeOrDash(sun.astro_dusk)],
          ])),
        h("div", {},
          h("div", { class: "section-title" }, h("h2", { text: "Moon events" }), h("div", { class: "line" })),
          kv([
            ["PHASE", moon.phase_name || "—"], ["ILLUMINATION", `${fmt.num(moon.illumination_pct, 1)}%`],
            ["AGE", `${fmt.num(moon.age_days, 1)} days`], ["DISTANCE", `${U.distKm(moon.distance_km, 0)}`],
            ["MOONRISE", timeOrDash(moon.next_rise)], ["MOONSET", timeOrDash(moon.next_set)],
            ["ALTITUDE", `${fmt.num(moon.altitude_deg, 1)}°`], ["AZIMUTH", `${fmt.num(moon.azimuth_deg, 1)}°`],
          ]))),
      h("div", { class: "section-title mt-14" }, h("h2", { text: "Seven-day planning table" }), h("div", { class: "line" })),
      h("div", { class: "scroll-x" }, h("table", { class: "data sun-table" },
        h("thead", {}, h("tr", {},
          ["DATE", "SUNRISE", "SUNSET", "DAY LENGTH", "MOONRISE", "MOONSET", "MOON LIT"].map((x) => h("th", { text: x })))),
        h("tbody", {}, ...(celestial.week || []).map((d) => h("tr", {},
          h("td", { text: d.date }),
          h("td", { text: timeOrDash(d.sunrise) }),
          h("td", { text: timeOrDash(d.sunset) }),
          h("td", { text: d.day_length_h ? `${fmt.num(d.day_length_h, 2)} h` : "—" }),
          h("td", { text: timeOrDash(d.moonrise) }),
          h("td", { text: timeOrDash(d.moonset) }),
          h("td", { text: `${fmt.num(d.moon_illum_pct, 0)}%` }),
        ))),
      ))),
  });

  /* --------------------------------------------------------- resection/tide */
  const resOut = h("div");
  const r1 = input({ type: "number", step: "any", placeholder: "lat" });
  const n1 = input({ type: "number", step: "any", placeholder: "lon" });
  const b1 = input({ type: "number", step: "any", placeholder: "bearing °" });
  const r2 = input({ type: "number", step: "any", placeholder: "lat" });
  const n2 = input({ type: "number", step: "any", placeholder: "lon" });
  const b2 = input({ type: "number", step: "any", placeholder: "bearing °" });
  const resectionPanel = panel({
    title: "Two-bearing resection", icon: "target",
    sub: "Enter two known points and the magnetic or grid bearings observed to each. Convert bearings consistently first.",
    body: h("div", { class: "col" },
      h("div", { class: "form-grid cols-3" },
        field("Point 1 latitude", r1), field("Point 1 longitude", n1), field("Bearing 1 (°)", b1),
        field("Point 2 latitude", r2), field("Point 2 longitude", n2), field("Bearing 2 (°)", b2)),
      h("div", { class: "row mt-10" }, btn("RESECT", { tone: "primary", onClick: async () => {
        resOut.replaceChildren(loading("RESECTING"));
        try {
          const out = await api.post("/api/nav/resection", {
            lat1: r1.value, lon1: n1.value, bearing1: b1.value,
            lat2: r2.value, lon2: n2.value, bearing2: b2.value,
          });
          resOut.replaceChildren(kv([
            ["POSITION", fmt.coord(out.lat, out.lon, 6)],
            ["MGRS", out.format?.mgrs || "—"],
            ["UTM", out.format?.utm || "—"],
            ["MISCLOSURE", `${fmt.num(out.error_m, 1)} m`],
            ["RANGE FROM P1", `${fmt.num(out.range_from_1_m, 0)} m`],
          ]), h("div", { class: "callout warn small mt-10", text: out.note || "Small bearing angles multiply error." }));
        } catch (err) { resOut.replaceChildren(errorBox(err)); }
      } })),
      resOut),
  });

  const tideHours = input({ type: "number", step: "0.1", placeholder: "hours since low water" });
  const tideRange = input({ type: "number", step: "0.1", placeholder: "range (m)" });
  const tideOut = h("div");
  const tidePanel = panel({
    title: "Rule of twelfths", icon: "drop",
    sub: "Approximation only. Use official tide tables where available.",
    body: h("div", { class: "col" },
      h("div", { class: "form-grid" }, field("Hours since low water", tideHours), field("Tidal range (m)", tideRange)),
      h("div", { class: "row mt-10" }, btn("COMPUTE", { tone: "primary", size: "sm", onClick: async () => {
        try {
          const out = await api.get(`/api/nav/tide?hours_since_low=${encodeURIComponent(tideHours.value)}&range_m=${encodeURIComponent(tideRange.value)}`);
          tideOut.replaceChildren(kv([
            ["TWELFTHS ELAPSED", fmt.num(out.twelfths_elapsed, 2)],
            ["HEIGHT ABOVE LOW", `${fmt.num(out.height_above_low_m, 2)} m`],
            ["APPROXIMATE", out.approx ? "YES" : "NO"],
          ]));
        } catch (err) { tideOut.replaceChildren(errorBox(err)); }
      } })), tideOut),
  });

  /* ---------------------------------------------------------------- layout */
  const top = h("div", { class: "grid grid-2 mt-14" }, converter, compassPanel);
  root.appendChild(top);
  root.appendChild(h("div", { class: "grid grid-2 mt-14" }, gpsPanel, resectionPanel));
  root.appendChild(h("div", { class: "mt-14" }, celestialPanel));
  root.appendChild(h("div", { class: "grid grid-2 mt-14" }, tidePanel, panel({
    title: "Navigation calculators", icon: "compass",
    sub: "Pace count, dead reckoning, slope distance and terrain estimates.",
    body: await calculatorGrid(["NAV"], { openFirst: true }),
  })));

  const navCards = await referenceSection("NAVIGATION");
  if (navCards) {
    root.appendChild(panel({ title: "Navigation field cards", icon: "book", body: navCards }));
  }

  return () => { disposed = true; };
}

function coordLine(k, v) {
  return h("div", { class: "coord-line" }, h("span", { text: k }), h("b", { text: v || "—" }));
}

function bodyRow(name, alt, az) {
  return h("tr", {}, h("td", { text: name }), h("td", { text: `${fmt.num(alt, 1)}°` }), h("td", { text: `${fmt.num(az, 1)}°` }));
}
