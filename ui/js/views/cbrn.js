/* CBRN: radiological monitoring, fallout time/dose references and CBRN cards. */
import { api } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, stat, badge, btn, pageHead, alertStrip, emptyState, loading, fmt, toast,
} from "../components.js";
import { openRecordForm, deleteRecord, recordTable, rowActions } from "../records.js";
import { calculatorGrid } from "../calculators.js";
import { kv, referenceSection, positionConfidence, opsecActive } from "./shared.js?v=opsec-1";
import * as U from "../units.js";

const CBRN_AUTO_MS = 60_000;
let cbrnTimer = null;
let cbrnBusy = false;

function radTone(rate) {
  if (rate == null) return "";
  if (rate <= 1) return "green";
  if (rate <= 10) return "amber";
  return "red";
}

function radLabel(rate) {
  if (rate == null) return "NO READING";
  if (rate <= 0.5) return "NEAR BACKGROUND / VERIFY BASELINE";
  if (rate <= 1) return "LOW — COMPARE TO LOCAL BASELINE";
  if (rate <= 10) return "ELEVATED — INVESTIGATE SOURCE";
  if (rate <= 100) return "HIGH — LIMIT EXPOSURE / SHELTER";
  return "VERY HIGH — SHELTER AND SEEK OFFICIAL GUIDANCE";
}

function usv(value) {
  if (value == null) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1000) return `${fmt.num(n / 1000, 2)} mSv`;
  return `${fmt.num(n, 2)} µSv`;
}

function scaleBadges(scales) {
  const row = h("div", { class: "row gap-6 wrap" });
  for (const s of scales || []) {
    const label = String(s.label || "");
    const code = label.startsWith("RADIO") ? "R" : label.startsWith("SOLAR") ? "S" : "G";
    const tone = s.severity === "CRITICAL" ? "red" : s.severity === "ALERT" ? "amber" : "green";
    row.appendChild(badge(`${code}${s.level ?? "?"}`, tone));
  }
  return row;
}

function cbrnLivePanel(live) {
  const sw = live.space_weather || {};
  const rad = live.radiation || {};
  const vol = live.volcano || {};
  const haz = live.hazards || {};
  const air = live.air_quality || {};
  const sync = live.sync || {};
  const position = store.settings || {};
  const positionInfo = positionConfidence(position);
  const nearest = (rad.public || [])[0];
  const body = h("div", { class: "col gap-10" });

  body.appendChild(h("div", { class: "row between wrap gap-8" },
    h("div", { class: "row gap-8 wrap" },
      badge(live.online ? "LIVE · PUBLIC FEEDS" : "OFFLINE · CACHED", live.online ? "green" : "red"),
      h("span", { class: "tiny muted", text: `Last sync ${fmt.ago(sync.last_success)} · view auto-refreshes every 60 s` })),
    h("span", { class: "tiny muted", text: `Space weather ${fmt.ago(sw.fetched_at)} · radiation reports ${fmt.ago(rad.fetched_at)}` })));
  body.appendChild(h("div", {
    class: "tiny muted",
    title: positionInfo.text,
    text: `REFERENCE POSITION ${opsecActive(store.settings) ? "OP//MASKED" : fmt.coord(position.lat, position.lon, 4)} · ${positionInfo.mapLabel} · Safecast distance is measured from this stored reference`,
  }));

  const cols = h("div", { class: "grid grid-2 gap-14" });

  const space = h("div", { class: "col gap-6" });
  space.appendChild(h("div", { class: "side-label", text: "SPACE WEATHER // NOAA SWPC" }));
  space.appendChild(scaleBadges(sw.scales));
  space.appendChild(kv([
    ["Planetary K index", sw.kp != null ? fmt.num(sw.kp, 1) : "—"],
    ["Scales", (sw.scales || []).map((s) => `${s.label || ""}: ${s.level}`).join(" · ") || "no data"],
    ["Source age", fmt.ago(sw.fetched_at)],
  ]));
  space.appendChild(h("div", { class: "tiny muted", text: "R = radio blackout, S = solar radiation, G = geomagnetic storm. Elevated scales can disrupt HF radio, GNSS and power grids." }));
  cols.appendChild(space);

  const radiation = h("div", { class: "col gap-6" });
  radiation.appendChild(h("div", { class: "side-label", text: "RADIATION // LOCAL + PUBLIC REPORTS" }));
  radiation.appendChild(kv([
    ["Local instrument", rad.local ? `${fmt.num(rad.local.dose_rate_usv_h, 2)} µSv/h · ${fmt.ago(rad.local.ts)}` : "no local reading"],
    ["Nearest public report", nearest
      ? `${fmt.num(nearest.value, 1)} ${nearest.unit}${nearest.distance_km != null ? ` · ${U.distKm(nearest.distance_km)}` : ""} · measured ${fmt.ago(nearest.time)} (${fmt.date(nearest.time, false)})`
      : "—"],
    ["Public reports nearby", `${(rad.public || []).length} cached`],
  ]));
  radiation.appendChild(h("div", { class: "tiny muted", text: "Safecast reports are volunteer submissions, can be years old, and are sorted relative to the stored reference coordinate above—not live GPS tracking. They are not an official warning. Your calibrated instrument and official guidance take precedence." }));
  cols.appendChild(radiation);

  const volc = h("div", { class: "col gap-6" });
  volc.appendChild(h("div", { class: "side-label", text: "VOLCANIC ACTIVITY // SMITHSONIAN / USGS" }));
  if ((vol.items || []).length) {
    for (const it of vol.items.slice(0, 3)) {
      volc.appendChild(h("div", { class: "small", text: `${it.title}${it.time ? ` · ${fmt.date(it.time)}` : ""}` }));
    }
  } else {
    volc.appendChild(h("div", { class: "tiny muted", text: "No cached volcanic activity reports." }));
  }
  cols.appendChild(volc);

  const hazards = h("div", { class: "col gap-6" });
  hazards.appendChild(h("div", { class: "side-label", text: "SMOKE / FIRE / ASH // NASA EONET" }));
  if ((haz.items || []).length) {
    for (const it of haz.items.slice(0, 3)) {
      hazards.appendChild(h("div", { class: "small", text: `${it.title}${it.time ? ` · ${fmt.date(it.time)}` : ""}` }));
    }
  } else {
    hazards.appendChild(h("div", { class: "tiny muted", text: "No cached smoke, fire or ash events." }));
  }
  if (air.current) {
    hazards.appendChild(h("div", { class: "row gap-6 mt-6" },
      badge(`AQI ${air.current.aqi ?? "?"} · ${air.current.category || "—"}`,
        ["CRITICAL", "SEVERE"].includes(String(air.current.severity)) ? "red"
          : air.current.severity === "ALERT" ? "amber" : "green")));
  }
  cols.appendChild(hazards);

  body.appendChild(cols);
  body.appendChild(h("div", { class: "tiny muted", text: "Public feeds are advisory and may lag. CBRN decisions require calibrated instruments, local background measurements and official protective-action guidance." }));
  return panel({ title: "CBRN common operating picture", sub: "Auto-fetched public feeds · cached offline", icon: "radiation", cls: "mt-14", body });
}

export async function render(root) {
  if (cbrnTimer) { clearInterval(cbrnTimer); cbrnTimer = null; }
  root.replaceChildren(loading("LOADING RADIOLOGICAL LOG"));
  let logs, live = null;
  try {
    [logs, live] = await Promise.all([
      api.get("/api/records/rad_log?limit=200"),
      api.get("/api/cbrn/status").catch(() => null),
    ]);
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }
  const items = logs.items || [];
  const latest = items[0] || null;
  const day = items.filter((r) => Date.now() - new Date(r.ts).getTime() < 864e5);
  const max24 = day.reduce((m, r) => Math.max(m, Number(r.dose_rate_usv_h) || 0), 0);
  const cum = latest?.cumulative_usv != null ? Number(latest.cumulative_usv) : null;

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // CBRN",
    title: "CBRN",
    desc: "Radiological readings, fallout time/dose math and chemical-biological response cards. Field estimates only — instruments, official warnings and trained specialists take precedence.",
    actions: [
      btn("LOG READING", { icon: "radiation", tone: "primary", onClick: () => addReading(root) }),
      btn("CBRN MANUAL", { icon: "book", tone: "ghost", onClick: () => { location.hash = "#/manual?doc=cbrn"; } }),
      btn("FIELD CARDS", { icon: "book", tone: "ghost", onClick: () => { location.hash = "#/manual?card=radiation"; } }),
    ],
  }));

  root.appendChild(alertStrip(
    "CBRN warning: this is not a substitute for calibrated instruments, dosimetry, public-health orders or hazardous-materials training. "
    + "Do not enter a contaminated area to take a reading. Time, distance and shielding are the primary protections. KI is not a general radiation antidote.",
    "",
  ));

  const stats = h("div", { class: "grid grid-4 mt-14" });
  stats.appendChild(stat("Latest dose rate", latest?.dose_rate_usv_h != null ? `${fmt.num(latest.dose_rate_usv_h, 2)} µSv/h` : "—",
    latest ? fmt.ago(latest.ts) : "no local reading", latest?.dose_rate_usv_h > 1 ? "warn" : ""));
  stats.appendChild(stat("Cumulative", cum != null ? usv(cum) : "—", "as recorded; not dosimetry-grade"));
  stats.appendChild(stat("24 h peak", day.length ? `${fmt.num(max24, 2)} µSv/h` : "—", `${day.length} reading(s) in 24 h`, max24 > 10 ? "warn" : ""));
  stats.appendChild(stat("Readings", String(items.length).padStart(2, "0"), "local rad log"));
  root.appendChild(stats);

  if (live) root.appendChild(cbrnLivePanel(live));

  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);

  /* --------------------------------------------------------------- current */
  const current = h("div", { class: "col" });
  const rate = latest?.dose_rate_usv_h != null ? Number(latest.dose_rate_usv_h) : null;
  current.appendChild(h("div", { class: "row between wrap" },
    h("div", { class: "row" }, badge(radLabel(rate), radTone(rate))),
    latest ? h("span", { class: "tiny muted", text: fmt.date(latest.ts) }) : null));
  if (latest) {
    current.appendChild(kv([
      ["Dose rate", `${fmt.num(latest.dose_rate_usv_h, 2)} µSv/h`],
      ["Cumulative", latest.cumulative_usv != null ? usv(latest.cumulative_usv) : "—"],
      ["Location", latest.location || "—"],
      ["Note", latest.note || "—"],
      ["Equivalent", latest.dose_rate_usv_h != null ? `${fmt.num(Number(latest.dose_rate_usv_h) * 24 / 1000, 2)} mSv per 24 h at this rate` : "—"],
    ]));
    if (rate > 1) {
      current.appendChild(h("div", { class: "callout danger small",
        text: "A rate above 1 µSv/h is above many natural baselines but not by itself a definitive hazard. Confirm instrument function, subtract background, and follow official protective-action guidance. If levels are rising or a release is suspected, shelter and monitor." }));
    }
  } else {
    current.appendChild(emptyState("No radiation readings", "Enter instrument readings and cumulative dose. Never invent a reading.", "radiation"));
  }
  current.appendChild(h("div", { class: "row wrap mt-10" },
    btn("LOG READING", { size: "sm", tone: "primary", icon: "plus", onClick: () => addReading(root) }),
    btn("SHELTER CARD", { size: "sm", icon: "book", onClick: () => { location.hash = "#/manual?card=fallout"; } })));
  grid.appendChild(panel({ title: "Radiological status", sub: "Latest instrument reading", icon: "radiation", body: current }));

  /* ----------------------------------------------------------------- chart */
  const chartWrap = h("div", { class: "col" });
  const chart = h("canvas", { class: "vital-chart" });
  chart.style.height = "170px";
  chartWrap.appendChild(h("div", { class: "tiny muted", text: "DOSE RATE // LOG SCALE (µSv/h) — oldest → newest" }));
  chartWrap.appendChild(chart);
  chartWrap.appendChild(h("div", { class: "hint mt-6",
    text: "Log scale preserves both background-level and high-range readings. A rising trend matters more than a single point. Authored data only." }));
  setTimeout(() => drawRadChart(chart, items), 30);
  grid.appendChild(panel({ title: "Dose-rate trend", sub: `${items.length} readings`, icon: "activity", body: chartWrap }));

  /* ------------------------------------------------------------- read log */
  root.appendChild(panel({
    title: "Radiation log",
    sub: "Instrument readings and cumulative dose",
    icon: "note",
    cls: "span-2 mt-14",
    tools: [btn("NEW READING", { size: "sm", icon: "plus", onClick: () => addReading(root) })],
    body: recordTable(items, [
      { label: "TIME", key: "ts", render: (r) => fmt.date(r.ts) },
      { label: "RATE", key: "dose_rate_usv_h", num: true, render: (r) => r.dose_rate_usv_h != null ? `${fmt.num(r.dose_rate_usv_h, 2)} µSv/h` : "—" },
      { label: "CUMULATIVE", key: "cumulative_usv", num: true, render: (r) => r.cumulative_usv != null ? usv(r.cumulative_usv) : "—" },
      { label: "LOCATION", key: "location" },
      { label: "NOTE", key: "note", render: (r) => r.note || "—" },
      { label: "", key: "actions", render: (r) => rowActions({
        onEdit: () => openRecordForm({ table: "rad_log", item: r, onSaved: () => render(root) }),
        onDelete: () => deleteRecord("rad_log", r.id, "radiation reading", () => render(root)),
      }) },
    ], { empty: "No radiation readings recorded", emptyIcon: "radiation" }),
  }));

  /* ------------------------------------------------------------ guidance */
  const guide = h("div", { class: "col" });
  guide.appendChild(h("div", { class: "side-label", text: "PROTECTIVE ACTIONS // GENERAL" }));
  for (const [title, body] of [
    ["SHELTER", "Move indoors, away from windows and exterior walls. Shut ventilation, seal gaps if advised, and listen for official instructions. A basement or interior room typically offers more shielding than a wooden upper floor."],
    ["TIME", "Minimise time in a contaminated or high-dose area. Rotate exposed people where operationally possible and log their cumulative exposure."],
    ["DISTANCE", "Increase distance from the source. Dose rate falls with distance and intervening material; this is not a licence to approach an unknown source."],
    ["DECONTAMINATION", "Remove outer clothing, bag it, wash skin with soap and water, and avoid spreading dust. Treat contaminated wounds medically first. Do not scrub hard or use harsh chemicals on skin."],
    ["INGESTION", "Do not eat, drink or smoke in a contaminated area. Cover food and water. Seek official advice about potassium iodide — do not self-dose."],
  ]) {
    const row = h("div", { class: "device-card" });
    row.appendChild(h("div", { class: "dev-name", text: title }));
    row.appendChild(h("div", { class: "small muted mt-6", text: body }));
    guide.appendChild(row);
  }
  grid.appendChild(panel({ title: "Protect / monitor", sub: "Time, distance, shielding", icon: "shield", body: guide }));

  const ki = h("div", { class: "col" });
  ki.appendChild(h("div", { class: "callout danger",
    text: "Potassium iodide (KI) protects only the thyroid from radioactive iodine, and only when taken at the right time and dose under public-health direction. It does not protect against external radiation, caesium, strontium or other isotopes." }));
  ki.appendChild(kv([
    ["Use", "Only on official instruction during a radioiodine release."],
    ["Not for", "General radiation exposure, decontamination, or stockpiling without guidance."],
    ["Children/pregnancy", "Higher risk from thyroid exposure; follow specific official dosing."],
    ["Reference calc", "The CBRN calculator gives age-based reference doses; verify against current national guidance."],
  ]));
  grid.appendChild(panel({ title: "Potassium iodide", sub: "Thyroid blocking — restricted indication", icon: "flask", body: ki }));

  root.appendChild(panel({
    title: "CBRN calculators",
    sub: "Fallout, shielding, decay and unit conversion",
    icon: "flask",
    cls: "mt-14",
    body: await calculatorGrid("CBRN"),
  }));

  const refs = await referenceSection("CBRN");
  if (refs) root.appendChild(panel({ title: "CBRN field cards", icon: "book", cls: "mt-14", body: refs }));

  cbrnTimer = setInterval(async () => {
    if (cbrnBusy || document.hidden) return;
    if (!location.hash.startsWith("#/cbrn")) return;
    if (document.querySelector(".modal.open, .modal")) return;
    if (document.activeElement && ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
    cbrnBusy = true;
    const y = window.scrollY;
    try {
      await render(root);
      window.scrollTo(0, y);
    } catch {}
    finally { cbrnBusy = false; }
  }, CBRN_AUTO_MS);

  return () => {
    if (cbrnTimer) { clearInterval(cbrnTimer); cbrnTimer = null; }
  };
}

function drawRadChart(canvas, items) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(320, rect.width || 600), hh = Math.max(120, rect.height || 170);
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(hh * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#020604"; ctx.fillRect(0, 0, w, hh);
  const ordered = [...items].reverse();
  const values = ordered.map((r) => {
    const v = Number(r.dose_rate_usv_h);
    return Number.isFinite(v) && v > 0 ? v : null;
  });
  const real = values.filter((v) => v != null);
  ctx.strokeStyle = "rgba(93,255,154,.07)"; ctx.lineWidth = 1;
  for (let i = 1; i < 5; i++) { const y = (hh / 5) * i; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  if (!real.length) {
    ctx.fillStyle = "rgba(123,154,136,.65)"; ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.fillText("NO DATA", 10, hh / 2);
    return;
  }
  const logs = real.map((v) => Math.log10(v));
  let lo = Math.min(...logs), hi = Math.max(...logs);
  if (lo === hi) { lo -= .3; hi += .3; }
  const pad = Math.max(.1, (hi - lo) * .12); lo -= pad; hi += pad;
  ctx.beginPath(); ctx.strokeStyle = "#5dff9a"; ctx.lineWidth = 1.8;
  let started = false;
  values.forEach((v, i) => {
    if (v == null) return;
    const x = 7 + (ordered.length <= 1 ? 0 : (i / (ordered.length - 1)) * (w - 14));
    const y = hh - 9 - ((Math.log10(v) - lo) / (hi - lo)) * (hh - 20);
    if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = "rgba(123,154,136,.75)"; ctx.font = "8px 'JetBrains Mono', monospace";
  ctx.fillText(`${Math.pow(10, hi).toFixed(2)} µSv/h`, 3, 9);
  ctx.fillText(`${Math.pow(10, lo).toFixed(2)} µSv/h`, 3, hh - 3);
  if (ordered.length) {
    ctx.textAlign = "right";
    ctx.fillText(fmt.time(ordered[ordered.length - 1].ts), w - 4, hh - 3);
    ctx.textAlign = "left";
  }
}

function addReading(root) {
  openRecordForm({
    table: "rad_log",
    title: "Log radiation reading",
    intro: h("div", { class: "callout danger small",
      text: "Enter exactly what the instrument displays. Note the model/calibration if known. Do not extrapolate a reading to a different location or time without a documented method." }),
    onSaved: () => render(root),
  });
}
