import { api } from "./api.js";
import {
  h, field, input, textarea, select, checkbox, modal, btn, toast, confirmDialog,
} from "./components.js";
import { store } from "./store.js";
import * as U from "./units.js";

/* Field definitions mirror the server's RECORD_TABLES. Timestamps are stamped
   server-side. `defaults` can supply live position or a related row id. */
export const RECORD_DEFS = {
  log_entries: {
    label: "Log entry",
    fields: [
      { name: "title", label: "Title", required: true },
      { name: "category", label: "Category", type: "select", options: ["OBSERVATION", "SECURITY", "MOVEMENT", "COMMS", "MEDICAL", "SUPPLY", "WEATHER", "POWER", "OTHER"] },
      { name: "body", label: "Detail", type: "textarea", rows: 4, wide: true },
      { name: "lat", label: "Latitude", type: "number", step: "0.000001" },
      { name: "lon", label: "Longitude", type: "number", step: "0.000001" },
      { name: "tags", label: "Tags" },
    ],
  },
  tasks: {
    label: "Task",
    fields: [
      { name: "title", label: "Task", required: true, wide: true },
      { name: "priority", label: "Priority", type: "select", options: ["IMMEDIATE", "PRIORITY", "ROUTINE"] },
      { name: "due", label: "Due / window", placeholder: "e.g. 2026-11-05 18:00" },
      { name: "recurrence", label: "Repeat", type: "select", options: [
        { value: "", label: "Does not repeat" },
        { value: "DAILY", label: "Daily" },
        { value: "WEEKLY", label: "Weekly" },
        { value: "MONTHLY", label: "Monthly" },
        { value: "QUARTERLY", label: "Quarterly" },
        { value: "YEARLY", label: "Yearly" },
      ] },
      { name: "category", label: "Category", placeholder: "SECURITY, WATER…" },
      { name: "detail", label: "Detail", type: "textarea", rows: 3, wide: true },
    ],
  },
  patients: {
    label: "Patient",
    fields: [
      { name: "callsign", label: "Callsign / ID", required: true },
      { name: "name", label: "Name" },
      { name: "age", label: "Age", type: "number", min: 0, max: 120 },
      { name: "sex", label: "Sex", type: "select", options: ["", "F", "M", "OTHER"] },
      { name: "weight_kg", label: "Weight", unitGroup: "weight", type: "number", step: "0.1" },
      { name: "blood_type", label: "Blood type", type: "select", options: ["", "O+", "O-", "A+", "A-", "B+", "B-", "AB+", "AB-", "UNKNOWN"] },
      { name: "triage", label: "Triage", type: "select", options: ["UNASSESSED", "RED", "YELLOW", "GREEN", "BLACK"] },
      { name: "status", label: "Status", placeholder: "stable, monitored…" },
      { name: "allergies", label: "Allergies", type: "textarea", rows: 2, wide: true },
      { name: "meds", label: "Medications", type: "textarea", rows: 2, wide: true },
      { name: "history", label: "History", type: "textarea", rows: 2, wide: true },
      { name: "note", label: "Notes", type: "textarea", rows: 2, wide: true },
    ],
  },
  vitals: {
    label: "Vitals",
    fields: [
      { name: "patient_id", label: "Patient ID", type: "number", required: true },
      { name: "hr", label: "Heart rate (bpm)", type: "number" },
      { name: "rr", label: "Respiratory rate (/min)", type: "number" },
      { name: "sys", label: "Systolic (mmHg)", type: "number" },
      { name: "dia", label: "Diastolic (mmHg)", type: "number" },
      { name: "spo2", label: "SpO₂ (%)", type: "number" },
      { name: "temp_c", label: "Temperature", unitGroup: "temp", type: "number", step: "0.1" },
      { name: "glucose", label: "Glucose", type: "number", step: "0.1" },
      { name: "pain", label: "Pain (0-10)", type: "number", min: 0, max: 10 },
      { name: "avpu", label: "AVPU", type: "select", options: ["", "A", "V", "P", "U"] },
      { name: "note", label: "Note", type: "textarea", rows: 2, wide: true },
    ],
  },
  medication_log: {
    label: "Medication administration",
    fields: [
      { name: "patient_id", label: "Patient ID", type: "number", required: true },
      { name: "medication", label: "Medication", required: true, wide: true },
      { name: "dose", label: "Dose", placeholder: "e.g. 500 mg, 2 tablets" },
      { name: "route", label: "Route", type: "select", options: ["", "PO", "IM", "IV", "SC", "SL", "INHALED", "TOPICAL", "PR", "NG", "OTHER"] },
      { name: "administered_by", label: "Given by" },
      { name: "indication", label: "Indication" },
      { name: "reaction", label: "Reaction / adverse effect", type: "textarea", rows: 2, wide: true },
      { name: "note", label: "Note", type: "textarea", rows: 2, wide: true },
    ],
  },
  inventory: {
    label: "Inventory item",
    fields: [
      { name: "name", label: "Item", required: true },
      { name: "category", label: "Category", type: "select", options: ["WATER", "FOOD", "MEDICAL", "POWER", "COMMS", "CBRN", "SECURITY", "TOOLS", "SANITATION", "OTHER"] },
      { name: "qty", label: "Quantity", type: "number", step: "0.01" },
      { name: "unit", label: "Unit", placeholder: "L, kg, each…" },
      { name: "min_qty", label: "Minimum", type: "number", step: "0.01" },
      { name: "expires", label: "Expires", placeholder: "YYYY-MM or free text" },
      { name: "location", label: "Location" },
      { name: "notes", label: "Notes", type: "textarea", rows: 2, wide: true },
    ],
  },
  caches: {
    label: "Cache",
    fields: [
      { name: "name", label: "Cache name", required: true },
      { name: "lat", label: "Latitude", type: "number", step: "0.000001" },
      { name: "lon", label: "Longitude", type: "number", step: "0.000001" },
      { name: "status", label: "Status", type: "select", options: ["ACTIVE", "SEALED", "PARTIAL", "EMPTY", "COMPROMISED"] },
      { name: "contents", label: "Contents", type: "textarea", rows: 3, wide: true },
      { name: "note", label: "Note", type: "textarea", rows: 2, wide: true },
    ],
  },
  incidents: {
    label: "Incident",
    fields: [
      { name: "title", label: "Title", required: true, wide: true },
      { name: "severity", label: "Severity", type: "select", options: ["INFO", "WARNING", "CRITICAL"] },
      { name: "category", label: "Category", placeholder: "SECURITY, MEDICAL…" },
      { name: "lat", label: "Latitude", type: "number", step: "0.000001" },
      { name: "lon", label: "Longitude", type: "number", step: "0.000001" },
      { name: "detail", label: "Detail", type: "textarea", rows: 4, wide: true },
    ],
  },
  weather_log: {
    label: "Weather observation",
    fields: [
      { name: "temp_c", label: "Temperature", unitGroup: "temp", type: "number", step: "0.1" },
      { name: "pressure_hpa", label: "Pressure", unitGroup: "pressure", type: "number", step: "0.1" },
      { name: "humidity", label: "Humidity (%)", type: "number" },
      { name: "wind_dir", label: "Wind direction (°)", type: "number" },
      { name: "wind_kmh", label: "Wind", unitGroup: "speed", type: "number", step: "0.1" },
      { name: "gust_kmh", label: "Gust", unitGroup: "speed", type: "number", step: "0.1" },
      { name: "precip_mm", label: "Precipitation", unitGroup: "precip", type: "number", step: "0.1" },
      { name: "cloud_cover", label: "Cloud cover (okta)", type: "number", min: 0, max: 8 },
      { name: "cloud_type", label: "Cloud type", placeholder: "CU, CB, AC…" },
      { name: "visibility_km", label: "Visibility", unitGroup: "dist", type: "number", step: "0.1" },
      { name: "lat", label: "Latitude", type: "number", step: "0.000001" },
      { name: "lon", label: "Longitude", type: "number", step: "0.000001" },
      { name: "note", label: "Note", type: "textarea", rows: 2, wide: true },
    ],
  },
  water_log: {
    label: "Water log",
    fields: [
      { name: "source", label: "Source" },
      { name: "volume_l", label: "Volume", unitGroup: "volume", type: "number", step: "0.1" },
      { name: "method", label: "Treatment", type: "select", options: ["BOILED", "CHLORINE", "FILTER", "UV", "RO", "UNTREATED", "OTHER"] },
      { name: "chlorine_ppm", label: "Free chlorine (ppm)", type: "number", step: "0.1" },
      { name: "note", label: "Note", type: "textarea", rows: 2, wide: true },
    ],
  },
  power_log: {
    label: "Power log",
    fields: [
      { name: "battery_pct", label: "Battery (%)", type: "number" },
      { name: "fuel_l", label: "Fuel", unitGroup: "volume", type: "number", step: "0.1" },
      { name: "generator_hours", label: "Generator hours", type: "number", step: "0.1" },
      { name: "solar_kwh", label: "Solar (kWh)", type: "number", step: "0.01" },
      { name: "load_w", label: "Load (W)", type: "number", step: "1" },
      { name: "note", label: "Note", type: "textarea", rows: 2, wide: true },
    ],
  },
  rad_log: {
    label: "Radiation reading",
    fields: [
      { name: "dose_rate_usv_h", label: "Dose rate (µSv/h)", type: "number", step: "0.01" },
      { name: "cumulative_usv", label: "Cumulative (µSv)", type: "number", step: "0.01" },
      { name: "location", label: "Location" },
      { name: "note", label: "Note", type: "textarea", rows: 2, wide: true },
    ],
  },
  waypoints: {
    label: "Waypoint",
    fields: [
      { name: "name", label: "Name", required: true },
      { name: "lat", label: "Latitude", type: "number", step: "0.000001", required: true },
      { name: "lon", label: "Longitude", type: "number", step: "0.000001", required: true },
      { name: "ele", label: "Elevation", unitGroup: "length", type: "number", step: "0.1" },
      { name: "type", label: "Type", type: "select", options: ["WAYPOINT", "CAMP", "WATER", "CACHE", "LZ", "HAZARD", "RV", "OBSERVATION"] },
      { name: "note", label: "Note", type: "textarea", rows: 2, wide: true },
    ],
  },
  radio_log: {
    label: "Radio log entry",
    fields: [
      { name: "freq_hz", label: "Frequency (Hz)", type: "number", step: "1", placeholder: "146520000" },
      { name: "mode", label: "Mode", type: "select", options: ["NFM", "FM", "AM", "USB", "LSB", "CW", "DIGITAL", "OTHER"] },
      { name: "callsign", label: "Callsign / station" },
      { name: "summary", label: "Traffic summary", type: "textarea", rows: 4, wide: true, required: true },
      { name: "lat", label: "Latitude", type: "number", step: "0.000001" },
      { name: "lon", label: "Longitude", type: "number", step: "0.000001" },
    ],
  },
  comms_plans: {
    label: "Signal operating instruction",
    fields: [
      { name: "name", label: "Plan name", required: true, wide: true },
      { name: "data", label: "Content", type: "textarea", rows: 8, wide: true, required: true, placeholder: "Net, frequency, mode, schedule, callsigns, authentication, fallback…" },
    ],
  },
  opords: {
    label: "Operations order",
    fields: [
      { name: "title", label: "Title", required: true, wide: true },
      { name: "data", label: "Content", type: "textarea", rows: 8, wide: true, required: true, placeholder: "Situation, mission, execution, sustainment, command & signal…" },
    ],
  },
  trip_plans: {
    label: "Field trip plan",
    fields: [
      { name: "name", label: "Trip / route name", required: true, wide: true },
      { name: "route", label: "Route and checkpoints", type: "textarea", rows: 3, wide: true },
      { name: "departure_at", label: "Planned departure", type: "datetime-local", required: true },
      { name: "return_at", label: "Expected return", type: "datetime-local", required: true },
      { name: "checkin_interval_min", label: "Check-in interval (minutes)", type: "number", min: 5, max: 1440, default: 60 },
      { name: "party_size", label: "Party size", type: "number", min: 1, max: 100, default: 1 },
      { name: "emergency_contact", label: "Off-site contact (name + phone)", wide: true },
      { name: "notes", label: "Plan / overdue action notes", type: "textarea", rows: 3, wide: true },
    ],
  },
};

function localDateTimeValue(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (v) => String(v).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function displayValue(f, value) {
  if (value == null || value === "") return "";
  if (f.unitGroup && U.GROUP[f.unitGroup]) return U.GROUP[f.unitGroup].to(value);
  return value;
}

function controlFor(f, value) {
  if (f.type === "textarea") {
    return textarea({ value: value ?? "", rows: f.rows || 3, placeholder: f.placeholder || "" });
  }
  if (f.type === "select") {
    return select(f.options || [], value ?? "");
  }
  if (f.type === "checkbox") {
    return checkbox(f.label, !!value);
  }
  const dv = displayValue(f, value);
  return input({
    type: f.type || "text",
    value: f.type === "datetime-local" ? localDateTimeValue(value) : (dv ?? ""),
    step: f.step,
    min: f.min,
    max: f.max,
    placeholder: f.placeholder || "",
    autocomplete: "off",
  });
}

export function openRecordForm({ table, item = null, defaults = {}, fields = null, title = "", intro = null, onSaved = null }) {
  const def = RECORD_DEFS[table] || { label: table, fields: [] };
  const spec = fields || def.fields;
  const controls = {};
  const form = h("div", { class: "form-grid" });
  for (const f of spec) {
    const raw = item ? item[f.name] : (defaults[f.name] ?? f.default ?? "");
    const ctrl = controlFor(f, raw);
    controls[f.name] = { ctrl, spec: f };
    const label = f.unitGroup && U.GROUP[f.unitGroup] ? `${f.label} (${U.GROUP[f.unitGroup].unit()})` : f.label;
    form.appendChild(h("div", { class: f.wide ? "wide" : "" }, field(label, ctrl, f.hint || "")));
  }
  const body = h("div", { class: "col" }, intro, form);
  let saving = false;

  const saveBtn = btn(item ? "SAVE CHANGES" : "CREATE", { tone: "primary" });
  const m = modal({
    title: title || (item ? `Edit ${def.label}` : `New ${def.label}`),
    sub: item ? `Record #${item.id}` : "Local database",
    body,
    wide: spec.length > 7,
    actions: [btn("CANCEL", { tone: "ghost", onClick: () => m.close() }), saveBtn],
  });

  async function save() {
    if (saving) return;
    const payload = {};
    for (const [name, { ctrl, spec: f }] of Object.entries(controls)) {
      if (!f) continue;
      let value;
      if (f.type === "checkbox") value = ctrl.querySelector("input")?.checked ?? false;
      else value = ctrl.value;
      if (value === "" || value === null || value === undefined) {
        if (item) payload[name] = null;
        continue;
      }
      if (f.type === "datetime-local") {
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
          toast(`${f.label} is not a valid date and time`, "warn");
          return;
        }
        value = date.toISOString();
      }
      if (f.unitGroup && U.GROUP[f.unitGroup] && value !== null && value !== undefined && value !== "") {
        const cv = U.GROUP[f.unitGroup].from(value);
        if (cv == null) { toast(`${f.label} has an invalid number`, "warn"); return; }
        value = cv;
      }
      payload[name] = value;
    }
    for (const f of spec) {
      if (f.required && (payload[f.name] === undefined || payload[f.name] === null || payload[f.name] === "")) {
        toast(`${f.label} is required`, "warn");
        return;
      }
    }
    saving = true;
    saveBtn.disabled = true;
    try {
      const out = item
        ? await api.patch(`/api/records/${table}/${item.id}`, payload)
        : await api.post(`/api/records/${table}`, payload);
      toast(item ? "Record updated" : "Record created");
      m.close();
      if (onSaved) onSaved(out.item || out);
    } catch (err) {
      toast(err.message || "Could not save record", "error");
    } finally {
      saving = false;
      saveBtn.disabled = false;
    }
  }
  saveBtn.addEventListener("click", save);
  return m;
}

export async function deleteRecord(table, id, label = "record", onDone = null) {
  const ok = await confirmDialog({
    title: "Delete record",
    message: `Permanently delete ${label} (#${id}) from the local database? This cannot be undone.`,
    confirmText: "DELETE",
    danger: true,
  });
  if (!ok) return false;
  try {
    await api.del(`/api/records/${table}/${id}`);
    toast("Record deleted");
    if (onDone) onDone();
    return true;
  } catch (err) {
    toast(err.message || "Delete failed", "error");
    return false;
  }
}

export function recordTable(items, columns, opts = {}) {
  const { onRow = null, empty = "No records", emptyIcon = "info" } = opts;
  const table = h("table", { class: "data" });
  const thead = h("thead");
  const hrow = h("tr");
  for (const col of columns) {
    hrow.appendChild(h("th", { class: col.num ? "num" : "", text: col.label }));
  }
  thead.appendChild(hrow);
  table.appendChild(thead);
  const tbody = h("tbody");
  if (!items.length) {
    const tr = h("tr", { class: "empty" });
    tr.appendChild(h("td", { attrs: { colspan: String(columns.length) } }, empty));
    tbody.appendChild(tr);
  }
  for (const item of items) {
    const tr = h("tr", { class: onRow ? "clickable" : "" });
    for (const col of columns) {
      const td = h("td", { class: col.num ? "num" : "" });
      const val = col.render ? col.render(item) : item[col.key];
      if (val instanceof Node) td.appendChild(val);
      else td.textContent = val === null || val === undefined || val === "" ? "—" : String(val);
      tr.appendChild(td);
    }
    if (onRow) tr.addEventListener("click", (e) => {
      if (e.target.closest("button, a, input, select")) return;
      onRow(item);
    });
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  return table;
}

export function rowActions({ onEdit, onDelete, extra = [] }) {
  const wrap = h("div", { class: "row gap-4" });
  for (const node of extra) wrap.appendChild(node);
  if (onEdit) {
    const b = btn("", { size: "sm", tone: "ghost", title: "Edit" });
    b.innerHTML = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg><span>EDIT</span>';
    b.addEventListener("click", onEdit);
    wrap.appendChild(b);
  }
  if (onDelete) {
    const b = btn("DEL", { size: "sm", tone: "danger", onClick: onDelete });
    wrap.appendChild(b);
  }
  return wrap;
}

export function timestamp(value) {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString(undefined, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function severityTone(value) {
  const v = String(value || "").toUpperCase();
  if (["CRITICAL", "RED", "EXTREME", "SEVERE"].includes(v)) return "red";
  if (["WARNING", "AMBER", "YELLOW", "ALERT"].includes(v)) return "amber";
  if (["INFO", "GREEN", "MINOR", "OK"].includes(v)) return "green";
  return "";
}
