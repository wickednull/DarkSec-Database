import { api } from "./api.js";
import { h, input, select, field, btn, loading } from "./components.js";

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
    const primitive = formatPrimitive(value);
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
        cell.appendChild(h("div", { class: "v", text: formatPrimitive(v) ?? String(v) }));
        subgrid.appendChild(cell);
      }
      sub.appendChild(subgrid);
      wrap.appendChild(sub);
    }
  }
  if (note) wrap.appendChild(h("div", { class: "note", text: note }));
  return wrap;
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
  return input({
    type: f.type === "text" ? "text" : "number",
    value: f.default ?? "",
    step: f.type === "text" ? undefined : "any",
    min: f.min,
    max: f.max,
    placeholder: f.default === undefined ? "" : String(f.default),
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
    form.appendChild(field(`${f.label}${f.unit ? ` (${f.unit})` : ""}`, ctrl));
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
        // Convert numeric inputs to numbers
        if (f.type === "number" || ctrl.type === "number") {
          value = parseFloat(value);
          if (isNaN(value)) {
            continue; // Skip invalid numbers
          }
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
    form.appendChild(field(`${f.label}${f.unit ? ` (${f.unit})` : ""}`, ctrl));
  }
  const out = h("div");
  const run = btn(buttonLabel, { tone: "primary", size: "sm" });
  run.addEventListener("click", async () => {
    const payload = {};
    for (const [name, { ctrl, f }] of Object.entries(controls)) {
      if (f.type === "boolean") payload[name] = !!(ctrl._control || ctrl.querySelector("input"))?.checked;
      else if (ctrl.value !== "") {
        let value = ctrl.value;
        // Convert numeric inputs to numbers
        if (f.type === "number" || ctrl.type === "number") {
          value = parseFloat(value);
          if (isNaN(value)) {
            continue; // Skip invalid numbers
          }
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
