/* Field medicine: patient registry, triage, vitals trends and references.
   This is a record-keeping and reference tool, not a diagnostic device. */
import { api } from "../api.js";
import {
  h, panel, stat, badge, btn, pageHead, alertStrip, emptyState, loading, fmt, toast,
  field, input, modal,
} from "../components.js";
import { openRecordForm, deleteRecord, recordTable, rowActions } from "../records.js";
import { calculatorGrid } from "../calculators.js";
import { kv, referenceSection } from "./shared.js";

let lastSelected = null;

const TRIAGE = [
  { id: "RED", tone: "red", label: "IMMEDIATE" },
  { id: "YELLOW", tone: "amber", label: "DELAYED" },
  { id: "GREEN", tone: "green", label: "MINOR" },
  { id: "BLACK", tone: "black", label: "EXPECTANT" },
];

function triageTone(value) {
  return ({ RED: "red", YELLOW: "amber", GREEN: "green", BLACK: "muted" })[String(value || "").toUpperCase()] || "";
}

export async function render(root) {
  root.replaceChildren(loading("LOADING MEDICAL REGISTRY"));
  let patients, summary;
  try {
    [patients, summary] = await Promise.all([
      api.get("/api/records/patients?limit=500"),
      api.get("/api/medical/summary").catch(() => ({ patients: 0, triage: {}, recent_vitals: [] })),
    ]);
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }
  const items = patients.items || [];
  if (!items.some((p) => p.id === lastSelected)) lastSelected = items[0]?.id || null;

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // CASUALTY CARE",
    title: "Field Medicine",
    desc: "Patient registry, triage board, vitals trends and clinical calculators. Local records only — no telemetry or cloud sync.",
    actions: [
      btn("NEW PATIENT", { icon: "plus", tone: "primary", onClick: () => addPatient(root) }),
      btn("TRIAGE MANUAL", { icon: "book", tone: "ghost", onClick: () => { location.hash = "#/manual?doc=medical"; } }),
    ],
  }));

  root.appendChild(alertStrip(
    "Medical warning: this software does not diagnose, prescribe or replace a trained clinician. In a real emergency call local emergency services. "
    + "Drug doses, fluids and procedures must be verified against current protocols and your scope of practice. Record allergies and interventions accurately.",
    "",
  ));

  const counts = summary.triage || {};
  const red = counts.RED || 0;
  const unassessed = (counts.UNASSESSED || 0) + items.filter((p) => !p.triage).length;
  const stats = h("div", { class: "grid grid-4 mt-14" });
  stats.appendChild(stat("Patients", String(summary.patients ?? items.length).padStart(2, "0"), "local registry"));
  stats.appendChild(stat("Immediate", String(red).padStart(2, "0"), "RED triage", red ? "warn" : ""));
  stats.appendChild(stat("Unassessed", String(unassessed).padStart(2, "0"), "assign triage early", unassessed ? "warn" : ""));
  stats.appendChild(stat("Vitals entries", String((summary.recent_vitals || []).length).padStart(2, "0"), "most recent 10 shown"));
  root.appendChild(stats);

  const triageWrap = h("div", { class: "triage-grid mt-14" });
  for (const cat of TRIAGE) {
    const card = h("div", { class: `triage-card ${cat.tone}` });
    card.appendChild(h("div", { class: "count", text: String(counts[cat.id] || 0) }));
    card.appendChild(h("div", { class: "lbl", text: `${cat.label} // ${cat.id}` }));
    triageWrap.appendChild(card);
  }
  root.appendChild(panel({
    title: "Triage board",
    sub: "Counts from the local patient registry",
    icon: "medical",
    body: triageWrap,
  }));

  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);

  /* ----------------------------------------------------------- patient list */
  const rosterBody = h("div", { class: "col" });
  const search = input({ type: "search", placeholder: "Search callsign, name, allergy…" });
  const list = h("div", { class: "col gap-4" });
  rosterBody.append(search, list);
  grid.appendChild(panel({
    title: "Patient roster",
    sub: `${items.length} names on file`,
    icon: "heart",
    tools: [btn("NEW", { size: "sm", icon: "plus", onClick: () => addPatient(root) })],
    body: rosterBody,
  }));
  function paintRoster(filter = "") {
    list.replaceChildren();
    const q = filter.trim().toLowerCase();
    const filtered = items.filter((p) => !q || `${p.callsign} ${p.name || ""} ${p.allergies || ""} ${p.status || ""}`.toLowerCase().includes(q));
    if (!filtered.length) {
      list.appendChild(emptyState("No matching patients", "Add a patient or clear the search.", "medical"));
      return;
    }
    const order = { RED: 0, YELLOW: 1, GREEN: 2, BLACK: 3, UNASSESSED: 4 };
    filtered.sort((a, b) => (order[a.triage] ?? 4) - (order[b.triage] ?? 4));
    for (const p of filtered) {
      const card = h("div", { class: `patient-card ${p.id === lastSelected ? "selected" : ""}`, role: "button", tabindex: "0" });
      card.appendChild(h("div", { class: "pc-name", text: p.callsign || `PATIENT ${p.id}` }));
      card.appendChild(h("div", { class: "pc-sub", text: [p.name, p.age ? `${p.age}y` : "", p.sex].filter(Boolean).join(" · ") || "no demographics" }));
      card.appendChild(h("div", { class: "pc-sub", text: p.status || "status not recorded" }));
      card.appendChild(h("div", { class: "pc-triage" }, badge(p.triage || "UNASSESSED", triageTone(p.triage))));
      card.addEventListener("click", () => { lastSelected = p.id; render(root); });
      card.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); lastSelected = p.id; render(root); } });
      list.appendChild(card);
    }
  }
  let searchTimer;
  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => paintRoster(search.value), 120);
  });
  paintRoster();

  /* --------------------------------------------------------- selected chart */
  const selected = items.find((p) => p.id === lastSelected) || null;
  const detailBody = h("div", { class: "col" });
  const detailPanel = panel({
    title: selected ? (selected.callsign || `Patient #${selected.id}`) : "Patient detail",
    sub: selected ? (selected.name || "no name recorded") : "select a patient",
    icon: "pulse",
    tools: selected ? [
      btn("EDIT", { size: "sm", icon: "edit", onClick: () => openRecordForm({ table: "patients", item: selected, onSaved: () => render(root) }) }),
      btn("DELETE", { size: "sm", tone: "danger", onClick: () => deleteRecord("patients", selected.id, selected.callsign || "patient", () => { lastSelected = null; render(root); }) }),
    ] : [],
    body: detailBody,
  });
  grid.appendChild(detailPanel);

  if (!selected) {
    detailBody.appendChild(emptyState("No patient selected", "Create a patient record to begin tracking vitals and interventions.", "medical"));
  } else {
    const head = h("div", { class: "row between wrap" });
    head.appendChild(h("div", { class: "row" }, badge(selected.triage || "UNASSESSED", triageTone(selected.triage)),
      h("span", { class: "tiny muted", text: `Record #${selected.id} · created ${fmt.ago(selected.created)}` })));
    head.appendChild(h("div", { class: "row wrap" },
      ...TRIAGE.map((t) => btn(t.id, { size: "sm", tone: t.tone === "red" ? "danger" : t.tone === "amber" ? "amber" : "ghost", onClick: () => setTriage(selected, t.id, root) }))));
    detailBody.appendChild(head);
    detailBody.appendChild(kv([
      ["Age / sex", [selected.age != null ? `${selected.age} y` : "", selected.sex || ""].filter(Boolean).join(" / ") || "—"],
      ["Weight", selected.weight_kg != null ? `${fmt.num(selected.weight_kg, 1)} kg` : "—"],
      ["Blood type", selected.blood_type || "—"],
      ["Status", selected.status || "—"],
      ["Allergies", selected.allergies || "NOT RECORDED"],
      ["Medications", selected.meds || "—"],
      ["History", selected.history || "—"],
      ["Notes", selected.note || "—"],
    ]));
    if (!selected.allergies) {
      detailBody.appendChild(h("div", { class: "callout warn small", text: "Allergies are not recorded. Confirm before giving any medication." }));
    }
    const actionRow = h("div", { class: "row wrap mt-10" },
      btn("ADD VITALS", { icon: "plus", tone: "primary", size: "sm", onClick: () => addVitals(selected, root) }),
      btn("NEW NOTE", { icon: "note", size: "sm", onClick: () => { location.hash = "#/security?tab=vault"; } }),
      btn("TRIAGE CARD", { icon: "book", size: "sm", tone: "ghost", onClick: () => { location.hash = "#/manual?card=start-triage"; } }),
    );
    detailBody.appendChild(actionRow);

    const vitalsWrap = h("div", { class: "col mt-10" });
    detailBody.appendChild(vitalsWrap);
    loadVitals(selected.id, vitalsWrap);
  }

  /* ---------------------------------------------------------- recent vitals */
  const pname = new Map(items.map((p) => [p.id, p.callsign || `#${p.id}`]));
  const recent = summary.recent_vitals || [];
  const recentPanel = panel({
    title: "Recent vitals",
    sub: "Latest entries across all patients",
    icon: "activity",
    cls: "span-2",
    tools: [btn("ADD VITALS", { size: "sm", icon: "plus", onClick: () => {
      if (!items.length) { toast("Create a patient first", "warn"); return; }
      addVitals(items.find((p) => p.id === lastSelected) || items[0], root);
    } })],
    body: recordTable(recent, [
      { label: "TIME", key: "ts", render: (r) => fmt.date(r.ts) },
      { label: "PATIENT", key: "patient_id", render: (r) => pname.get(r.patient_id) || `#${r.patient_id}` },
      { label: "HR", key: "hr", num: true },
      { label: "BP", key: "bp", num: true, render: (r) => r.sys != null && r.dia != null ? `${r.sys}/${r.dia}` : "—" },
      { label: "RR", key: "rr", num: true },
      { label: "SpO₂", key: "spo2", num: true, render: (r) => r.spo2 != null ? `${fmt.num(r.spo2, 0)}%` : "—" },
      { label: "TEMP", key: "temp_c", num: true, render: (r) => r.temp_c != null ? `${fmt.num(r.temp_c, 1)}°C` : "—" },
      { label: "AVPU", key: "avpu" },
      { label: "NOTE", key: "note", render: (r) => r.note || "—" },
    ], { empty: "No vitals recorded", emptyIcon: "pulse" }),
  });
  root.appendChild(recentPanel);

  /* ----------------------------------------------------------- calculations */
  const calc = panel({
    title: "Clinical calculators",
    sub: "Screening and dose references — verify against protocol",
    icon: "flask",
    cls: "mt-14",
    body: await calculatorGrid("MEDICAL"),
  });
  root.appendChild(calc);

  const refs = await referenceSection("MEDICAL");
  if (refs) root.appendChild(panel({ title: "Medical field cards", icon: "book", cls: "mt-14", body: refs }));

  return () => { clearTimeout(searchTimer); };
}

async function loadVitals(patientId, target) {
  target.replaceChildren(loading("READING VITALS"));
  let out;
  try {
    out = await api.get(`/api/medical/patients/${patientId}/vitals`);
  } catch (err) {
    target.replaceChildren(h("div", { class: "callout danger small", text: err.message }));
    return;
  }
  const vitals = out.items || [];
  target.replaceChildren();
  target.appendChild(h("div", { class: "side-label", text: "VITAL SIGNS // OLDEST → NEWEST" }));
  if (!vitals.length) {
    target.appendChild(emptyState("No vitals for this patient", "Record HR, BP, RR, SpO₂ and temperature to build a trend.", "pulse"));
    return;
  }
  const ordered = [...vitals].reverse();
  const charts = [
    { title: "HEART RATE (bpm)", series: [{ data: ordered.map((v) => v.hr), color: "#5dff9a" }] },
    { title: "BLOOD PRESSURE (mmHg)", series: [
      { data: ordered.map((v) => v.sys), color: "#5dff9a" },
      { data: ordered.map((v) => v.dia), color: "#ffb02e" },
    ], legend: ["SYS", "DIA"] },
    { title: "SpO₂ (%)", series: [{ data: ordered.map((v) => v.spo2), color: "#5dff9a" }] },
    { title: "TEMPERATURE (°C)", series: [{ data: ordered.map((v) => v.temp_c), color: "#ffb02e" }] },
  ];
  const canvases = [];
  for (const chart of charts) {
    const block = h("div", { class: "col gap-4" });
    block.appendChild(h("div", { class: "tiny muted", text: chart.title }));
    const canvas = h("canvas", { class: "vital-chart" });
    block.appendChild(canvas);
    if (chart.legend) {
      const legend = h("div", { class: "row gap-10 tiny muted" });
      chart.legend.forEach((label, i) => {
        const item = h("span", { class: "row gap-4" });
        const dot = h("span");
        dot.style.cssText = `width:7px;height:7px;background:${chart.series[i].color};display:inline-block`;
        item.append(dot, document.createTextNode(label));
        legend.appendChild(item);
      });
      block.appendChild(legend);
    }
    target.appendChild(block);
    canvases.push({ canvas, chart });
  }
  const latest = vitals[0];
  target.appendChild(h("div", { class: "side-label mt-10", text: "LATEST READING" }));
  target.appendChild(kv([
    ["Time", fmt.date(latest.ts)],
    ["HR / RR", `${latest.hr ?? "—"} bpm / ${latest.rr ?? "—"} /min`],
    ["BP", latest.sys != null && latest.dia != null ? `${latest.sys}/${latest.dia} mmHg` : "—"],
    ["SpO₂", latest.spo2 != null ? `${fmt.num(latest.spo2, 0)} %` : "—"],
    ["Temperature", latest.temp_c != null ? `${fmt.num(latest.temp_c, 1)} °C` : "—"],
    ["Glucose", latest.glucose != null ? fmt.num(latest.glucose, 1) : "—"],
    ["Pain / AVPU", `${latest.pain ?? "—"} / ${latest.avpu || "—"}`],
    ["Note", latest.note || "—"],
  ]));
  setTimeout(() => {
    for (const { canvas, chart } of canvases) drawChart(canvas, chart.series, ordered);
  }, 30);
}

function drawChart(canvas, series, ordered) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(260, rect.width || 600), hh = Math.max(90, rect.height || 130);
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(hh * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#020604"; ctx.fillRect(0, 0, w, hh);
  const all = series.flatMap((s) => s.data).filter((v) => v != null && Number.isFinite(Number(v))).map(Number);
  ctx.strokeStyle = "rgba(93,255,154,.07)";
  for (let i = 1; i < 4; i++) { const y = (hh / 4) * i; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  if (!all.length) {
    ctx.fillStyle = "rgba(123,154,136,.65)"; ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.fillText("NO DATA", 10, hh / 2);
    return;
  }
  let min = Math.min(...all), max = Math.max(...all);
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * .12; min -= pad; max += pad;
  const n = Math.max(...series.map((s) => s.data.length));
  series.forEach((s) => {
    ctx.beginPath(); ctx.strokeStyle = s.color; ctx.lineWidth = 1.6;
    let started = false;
    s.data.forEach((raw, i) => {
      if (raw == null || !Number.isFinite(Number(raw))) return;
      const x = 7 + (n <= 1 ? 0 : (i / (n - 1)) * (w - 14));
      const y = hh - 8 - ((Number(raw) - min) / (max - min)) * (hh - 18);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
  ctx.fillStyle = "rgba(123,154,136,.7)"; ctx.font = "8px 'JetBrains Mono', monospace";
  ctx.fillText(String(Math.round(max)), 3, 9);
  ctx.fillText(String(Math.round(min)), 3, hh - 3);
  if (ordered.length) {
    ctx.textAlign = "right";
    ctx.fillText(fmt.time(ordered[0].ts), w - 4, hh - 3);
    ctx.textAlign = "left";
  }
}

function addPatient(root) {
  openRecordForm({
    table: "patients",
    defaults: { triage: "UNASSESSED" },
    onSaved: (item) => {
      if (item?.id) lastSelected = item.id;
      render(root);
    },
  });
}

function addVitals(patient, root) {
  openRecordForm({
    table: "vitals",
    defaults: { patient_id: patient.id },
    title: `Vitals — ${patient.callsign || `Patient #${patient.id}`}`,
    onSaved: () => {
      lastSelected = patient.id;
      render(root);
    },
  });
}

async function setTriage(patient, level, root) {
  try {
    await api.patch(`/api/records/patients/${patient.id}`, { triage: level });
    toast(`${patient.callsign || "Patient"} triaged ${level}`);
    lastSelected = patient.id;
    render(root);
  } catch (err) { toast(err.message || "Could not update triage", "error"); }
}
