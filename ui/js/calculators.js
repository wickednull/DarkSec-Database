import { api } from "./api.js";
import { h, input, select, field, btn, loading } from "./components.js";
import * as U from "./units.js";

let catalogPromise = null;

export function loadCatalog() {
  if (!catalogPromise) {
    catalogPromise = api.get("/api/calc").then((r) => r.catalog || []).catch((err) => {
      catalogPromise = null;
      throw err;
    });
  }
  return catalogPromise;
}

const LABELS = {
  value_c: "Temperature", applicable: "Applies", note: "Note", km: "Distance (km)",
  miles: "Distance (mi)", force: "Beaufort force", name: "Name", effect: "Effect",
  kmh: "Wind (km/h)", change_hpa: "3-hour change (hPa)", tendency: "Tendency",
  interpretation: "Interpretation", bmi: "BMI", category: "Category", ibw_kg: "Ideal weight (kg)",
  index: "Index", risk: "Risk", score: "Score", high_risk: "High risk", severity: "Severity",
  intubate_consider: "Consider airway", urgent: "Urgent", total: "Total",
  drops_per_min: "Drops/min", ml_per_hr: "mL/hr", ml_day: "mL/day", ml_hr: "mL/hr",
  total_ml_24h: "24 h total (mL)", first_8h_ml: "First 8 h (mL)", next_16h_ml: "Next 16 h (mL)",
  rate_ml_hr: "Rate (mL/hr)", head: "Head %", trunk_front: "Trunk front %",
  trunk_back: "Trunk back %", each_arm: "Each arm %", each_leg: "Each leg %",
  groin: "Groin %", weight_kg: "Weight (kg)", mg: "Dose (mg)", ml_1mg_per_ml: "Volume (mL, 1 mg/mL)",
  adult_im_iv_mg: "Adult IM/IV (mg)", repeat: "Repeat", liters: "Litres", gallons: "Gallons",
  days: "Days", kcal_per_person_day: "kcal/person/day", array_watts: "Array (W)",
  derate: "Derate", bank_wh: "Bank (Wh)", bank_ah: "Bank (Ah)", voltage: "Voltage",
  hours: "Hours", usable_wh: "Usable (Wh)", awg: "AWG", voltage_drop_v: "Drop (V)",
  voltage_drop_pct: "Drop (%)", dose_rate_usv_h: "Dose rate (µSv/h)", decay_exponent: "Decay exponent",
  dose_usv: "Dose (µSv)", dose_msv: "Dose (mSv)", sheltered_usv: "Sheltered (µSv)",
  protection_factor: "Protection factor", sv: "Sv", msv: "mSv", usv: "µSv", rem: "rem",
  mrem: "mrem", rad: "rad", gy: "Gy", total_ft: "Total (ft)", each_leg_ft: "Each leg (ft)",
  ft: "Feet", m: "Metres", circumference_ft: "Circumference (ft)",
  circumference_m: "Circumference (m)", tx_dbm: "TX (dBm)", path_loss_db: "Path loss (dB)",
  eirp_dbm: "EIRP (dBm)", received_dbm: "Received (dBm)", margin_db: "Margin (dB)",
  viable: "Viable", morse: "Morse", text: "Text", distance_km: "Distance (km)",
  bearing_deg: "Bearing (°)", back_bearing_deg: "Back bearing (°)", paces: "Paces",
  paces_per_100m: "Paces/100 m", lat: "Latitude", lon: "Longitude", hours_elapsed: "Hours elapsed",
  slope_distance_m: "Slope distance (m)", slope_angle_deg: "Slope (°)", horizontal_m: "Horizontal (m)",
  vertical_m: "Vertical (m)", sfi: "Solar flux index", kp: "Kp", sun_altitude_deg: "Sun altitude (°)",
  band: "Band", outlook: "Outlook", confidence: "Confidence", phase_name: "Phase",
  illumination_pct: "Illumination (%)", age_days: "Age (days)", altitude_deg: "Altitude (°)",
  azimuth_deg: "Azimuth (°)", next_rise: "Next rise", next_set: "Next set", day_length_h: "Day length (h)",
  rolling_boil_minutes: "Rolling boil (min)", note: "Note", tidal_height_m: "Height above low (m)",
  twelfths_elapsed: "Twelfths elapsed", height_above_low_m: "Height above low (m)", approx: "Approximate",
  chlorine_ml: "Chlorine (mL)", chlorine_tsp: "Chlorine (tsp)", wait_minutes: "Wait (min)",
  mix: "Mix", osmolarity: "Osmolarity", calories: "Calories", snacks: "Snacks",
};

function prettyKey(key) {
  return LABELS[key] || String(key).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatPrimitive(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "YES" : "NO";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return String(value);
    return Number.isInteger(value) ? String(value) : value.toLocaleString(undefined, { maximumFractionDigits: 3 });
  }
  if (typeof value === "string") return value;
  return null;
}

export function resultNode(result) {
  const wrap = h("div", { class: "calc-result" });
  const grid = h("div", { class: "cr-grid" });
  let note = "";
  const nested = [];

  for (const [key, value] of Object.entries(result || {})) {
    if (key === "note") { note = String(value || ""); continue; }
    const primitive = formatResultPrimitive(key, value);
    if (primitive === null) {
      nested.push([key, value]);
      continue;
    }
    const item = h("div", { class: "cr-item" });
    item.appendChild(h("div", { class: "k", text: prettyKey(key) }));
    item.appendChild(h("div", { class: "v", text: primitive }));
    grid.appendChild(item);
  }
  wrap.appendChild(grid);

  for (const [key, value] of nested) {
    if (Array.isArray(value)) {
      wrap.appendChild(h("div", { class: "small muted mt-10", text: prettyKey(key) }));
      const ul = h("ul", { class: "small" });
      for (const v of value) {
        const txt = typeof v === "object" ? JSON.stringify(v) : String(v);
        ul.appendChild(h("li", { text: txt }));
      }
      wrap.appendChild(ul);
    } else if (value && typeof value === "object") {
      wrap.appendChild(h("div", { class: "small muted mt-10", text: prettyKey(key) }));
      const sub = h("div", { class: "calc-result", style: { marginTop: "4px" } });
      const subgrid = h("div", { class: "cr-grid" });
      for (const [k, v] of Object.entries(value)) {
        const cell = h("div", { class: "cr-item" });
        cell.appendChild(h("div", { class: "k", text: prettyKey(k) }));
        cell.appendChild(h("div", { class: "v", text: formatResultPrimitive(k, v) ?? String(v) }));
        subgrid.appendChild(cell);
      }
      sub.appendChild(subgrid);
      wrap.appendChild(sub);
    }
  }
  if (note) wrap.appendChild(h("div", { class: "note", text: note }));
  return wrap;
}


function fieldDisplay(f) {
  const u = String(f.unit || "");
  if (!U.isImperial()) return { label: `${f.label}${u ? ` (${u})` : ""}`, unit: u, toCanonical: (v)=>v, fromCanonical: (v)=>v };
  if (u === "°C" || /temp|dew|wet.?bulb|outside air temperature/i.test(f.label+f.name)) return { label: `${f.label} (°F)`, unit: "°F", toCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?(n-32)*5/9:v;}, fromCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n*9/5+32:v;} };
  if (u === "km/h" || u === "kmh") return { label: `${f.label} (mph)`, unit: "mph", toCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n/0.621371:v;}, fromCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n*0.621371:v;} };
  if (u === "hPa") return { label: `${f.label} (inHg)`, unit: "inHg", toCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n/0.0295299830714:v;}, fromCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n*0.0295299830714:v;} };
  if (u === "km") return { label: `${f.label} (mi)`, unit: "mi", toCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n/0.621371:v;}, fromCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n*0.621371:v;} };
  if (u === "m") return { label: `${f.label} (ft)`, unit: "ft", toCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n/3.28084:v;}, fromCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n*3.28084:v;} };
  if (u === "mm") return { label: `${f.label} (in)`, unit: "in", toCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n*25.4:v;}, fromCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n/25.4:v;} };
  if (u === "L" || u === "l") return { label: `${f.label} (gal)`, unit: "gal", toCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n/0.264172:v;}, fromCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n*0.264172:v;} };
  if (u === "kg") return { label: `${f.label} (lb)`, unit: "lb", toCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n/2.20462:v;}, fromCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n*2.20462:v;} };
  if (u === "cm") return { label: `${f.label} (in)`, unit: "in", toCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n*2.54:v;}, fromCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n/2.54:v;} };
  if (u === "mL" || u === "ml") return { label: `${f.label} (fl oz)`, unit: "fl oz", toCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n*29.5735:v;}, fromCanonical: (v)=>{const n=Number(v); return Number.isFinite(n)?n/29.5735:v;} };
  return { label: `${f.label}${u ? ` (${u})` : ""}`, unit: u, toCanonical: (v)=>v, fromCanonical: (v)=>v };
}

function formatResultPrimitive(key, value) {
  if (value == null) return "—";
  if (typeof value === "boolean") return value ? "YES" : "NO";
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return String(value);
    if (U.isImperial()) {
      if (/_c$|^temp_c$|temp|dew|wet|chill|heat_index|humidex|wind_chill/.test(key) && !/pct|percent|percentile/.test(key)) return `${(value*9/5+32).toLocaleString(undefined,{maximumFractionDigits:2})} °F`;
      if (/kmh|speed_kmh|wind_kmh|velocity_kmh|_kmh$/.test(key)) return `${(value*0.621371).toLocaleString(undefined,{maximumFractionDigits:2})} mph`;
      if (/^km$|distance_km|_km$|radius_km/.test(key)) return `${(value*0.621371).toLocaleString(undefined,{maximumFractionDigits:2})} mi`;
      if (/pressure_hpa|hpa/.test(key)) return `${(value*0.0295299830714).toLocaleString(undefined,{maximumFractionDigits:3})} inHg`;
      if (/precip_mm|_mm$/.test(key) && !/mmhg/.test(key)) return `${(value/25.4).toLocaleString(undefined,{maximumFractionDigits:2})} in`;
      if (/_m$|distance_m|alt_m|elevation_m|height_m/.test(key) && !/_mm$|km/.test(key)) return `${(value*3.28084).toLocaleString(undefined,{maximumFractionDigits:1})} ft`;
      if (/weight_kg|_kg$/.test(key)) return `${(value*2.20462).toLocaleString(undefined,{maximumFractionDigits:2})} lb`;
      if (/volume_l|liters|_l$/.test(key) && !/level|label|ml|fuel_l_hr/.test(key)) return `${(value*0.264172).toLocaleString(undefined,{maximumFractionDigits:2})} gal`;
    }
    return Number.isInteger(value) ? String(value) : value.toLocaleString(undefined, { maximumFractionDigits: 3 });
  }
  if (typeof value === "string") return value;
  return null;
}

function controlFor(f) {
  if (f.type === "select") return select(f.options || [], f.default ?? "");
  if (f.type === "boolean") {
    const label = h("label", { class: "check" });
    const cb = input({ type: "checkbox", checked: !!f.default });
    label.appendChild(cb);
    label.appendChild(h("span", { text: f.label }));
    label._control = cb;
    return label;
  }
  const disp = fieldDisplay(f);
  const dv = f.default == null ? "" : (typeof f.default === "number" ? disp.fromCanonical(f.default) : f.default);
  return input({
    type: f.type === "text" ? "text" : "number",
    value: dv ?? "",
    step: f.type === "text" ? undefined : "any",
    min: f.min,
    max: f.max,
    placeholder: dv === "" ? "" : String(dv),
  });
}

export function calcCard(spec, { open = false } = {}) {
  const details = h("details", { class: "calc-card" });
  if (open) details.open = true;
  const summary = h("summary");
  summary.appendChild(h("span", { text: spec.label }));
  details.appendChild(summary);

  const body = h("div", { class: "calc-body" });
  const form = h("div", { class: "form-grid" });
  const controls = {};
  for (const f of spec.fields || []) {
    const ctrl = controlFor(f);
    controls[f.name] = { ctrl, f };
    form.appendChild(field(fieldDisplay(f).label, ctrl));
  }
  if (!spec.fields?.length) form.appendChild(h("div", { class: "hint", text: "Reference output — no inputs required." }));
  body.appendChild(form);
  const out = h("div", { class: "mt-10" });
  const run = btn("COMPUTE", { size: "sm", tone: "primary" });
  const row = h("div", { class: "row mt-10" }, run);
  body.appendChild(row);
  body.appendChild(out);
  details.appendChild(body);

  async function compute() {
    const payload = {};
    for (const [name, { ctrl, f }] of Object.entries(controls)) {
      if (f.type === "boolean") {
        payload[name] = !!(ctrl._control || ctrl.querySelector("input"))?.checked;
      } else if (ctrl.value !== "") {
        let value = ctrl.value;
        if (f.type === "number" || ctrl.type === "number") {
          value = parseFloat(value);
          if (isNaN(value)) continue;
          value = fieldDisplay(f).toCanonical(value);
        }
        payload[name] = value;
      }
    }
    run.disabled = true;
    out.replaceChildren(loading("COMPUTING"));
    try {
      const res = await api.post(`/api/calc/${encodeURIComponent(spec.name)}`, payload);
      out.replaceChildren(resultNode(res.result || {}));
    } catch (err) {
      out.replaceChildren(h("div", { class: "callout danger small", text: err.message || "Calculation failed" }));
    } finally {
      run.disabled = false;
    }
  }
  run.addEventListener("click", compute);
  form.addEventListener("keydown", (e) => { if (e.key === "Enter") compute(); });
  return details;
}

export async function calculatorGrid(groups, opts = {}) {
  const container = h("div", { class: "calc-forms" });
  try {
    const catalog = await loadCatalog();
    const wanted = Array.isArray(groups) ? groups : [groups];
    const specs = catalog.filter((c) => wanted.includes(c.group));
    if (!specs.length) {
      container.appendChild(h("div", { class: "empty-state", text: "No calculators in this section." }));
      return container;
    }
    specs.forEach((spec, i) => container.appendChild(calcCard(spec, { open: opts.openFirst && i === 0 })));
  } catch (err) {
    container.appendChild(h("div", { class: "callout danger small", text: err.message }));
  }
  return container;
}

export function calcQuick(spec, buttonLabel = "COMPUTE") {
  const wrap = h("div", { class: "col" });
  const form = h("div", { class: "form-grid" });
  const controls = {};
  for (const f of spec.fields) {
    const ctrl = controlFor(f);
    controls[f.name] = { ctrl, f };
    form.appendChild(field(fieldDisplay(f).label, ctrl));
  }
  const out = h("div");
  const run = btn(buttonLabel, { tone: "primary", size: "sm" });
  run.addEventListener("click", async () => {
    const payload = {};
    for (const [name, { ctrl, f }] of Object.entries(controls)) {
      if (f.type === "boolean") payload[name] = !!(ctrl._control || ctrl.querySelector("input"))?.checked;
      else if (ctrl.value !== "") {
        let value = ctrl.value;
        if (f.type === "number" || ctrl.type === "number") {
          value = parseFloat(value);
          if (isNaN(value)) continue;
          value = fieldDisplay(f).toCanonical(value);
        }
        payload[name] = value;
      }
    }
    out.replaceChildren(loading("COMPUTING"));
    try {
      const res = await api.post(`/api/calc/${spec.name}`, payload);
      out.replaceChildren(resultNode(res.result));
    } catch (err) {
      out.replaceChildren(h("div", { class: "callout danger small", text: err.message }));
    }
  });
  wrap.appendChild(form);
  wrap.appendChild(h("div", { class: "row mt-10" }, run));
  wrap.appendChild(out);
  return wrap;
}
