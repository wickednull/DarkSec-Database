/* Water, food and power: operator logs, stock awareness and field formulas. */
import { api } from "../api.js";
import {
  h, panel, stat, badge, btn, pageHead, alertStrip, emptyState, loading, fmt, toast, tabs,
} from "../components.js";
import { openRecordForm, deleteRecord, recordTable, rowActions } from "../records.js";
import { calculatorGrid } from "../calculators.js";
import { kv, progressBar, referenceSection } from "./shared.js";

let lastTab = "water";

export async function render(root, params = {}) {
  if (["water", "food", "power", "inventory"].includes(params.tab)) lastTab = params.tab;
  root.replaceChildren(loading("READING SUPPLY LOGS"));
  let water, power, inventory, low;
  try {
    [water, power, inventory, low] = await Promise.all([
      api.get("/api/records/water_log?limit=120"),
      api.get("/api/records/power_log?limit=120"),
      api.get("/api/records/inventory?limit=500"),
      api.get("/api/logistics/low-stock"),
    ]);
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }
  const waterItems = water.items || [];
  const powerItems = power.items || [];
  const inv = inventory.items || [];
  const lowItems = low.items || [];
  const waterLatest = waterItems[0] || null;
  const powerLatest = powerItems[0] || null;
  const water7 = waterItems.filter((r) => Date.now() - new Date(r.ts).getTime() < 7 * 864e5);
  const power7 = powerItems.filter((r) => Date.now() - new Date(r.ts).getTime() < 7 * 864e5);
  const liters7 = water7.reduce((sum, r) => sum + (Number(r.volume_l) || 0), 0);
  const solar7 = power7.reduce((sum, r) => sum + (Number(r.solar_kwh) || 0), 0);
  const waterStock = inv.filter((r) => r.category === "WATER");
  const foodStock = inv.filter((r) => r.category === "FOOD");
  const powerStock = inv.filter((r) => r.category === "POWER");

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // SUSTAINMENT",
    title: "Water / Food / Power",
    desc: "Treatment records, consumption awareness, stock levels and power budget calculations. Estimates are clearly labelled; instruments and labels win.",
    actions: [
      btn("LOG WATER", { icon: "drop", tone: "primary", onClick: () => addWater(root) }),
      btn("LOG POWER", { icon: "bolt", onClick: () => addPower(root) }),
      btn("ADD STOCK", { icon: "plus", tone: "ghost", onClick: () => addInventory(root) }),
      btn("MANUAL", { icon: "book", tone: "ghost", onClick: () => { location.hash = "#/manual?doc=water"; } }),
    ],
  }));

  root.appendChild(alertStrip(
    "Life-safety warning: untreated surface water can carry pathogens and chemicals. Boiling is the most reliable field method where fuel allows; "
    + "chlorine dosing depends on concentration and water quality. Never run a generator or cook with charcoal indoors. Verify fuel and battery ratings yourself.",
    "",
  ));

  const stats = h("div", { class: "grid grid-4 mt-14" });
  stats.appendChild(stat("Water treated / 7d", `${fmt.num(liters7, 1)} L`, `${water7.length} logged treatment(s)`));
  stats.appendChild(stat("Battery", powerLatest?.battery_pct != null ? `${fmt.num(powerLatest.battery_pct, 0)}%` : "—",
    powerLatest ? `last log ${fmt.ago(powerLatest.ts)}` : "no power log"));
  stats.appendChild(stat("Solar / 7d", `${fmt.num(solar7, 2)} kWh`, `${power7.length} power log(s)`));
  stats.appendChild(stat("Low stock", String(lowItems.length).padStart(2, "0"), lowItems.length ? "items at or below minimum" : "no minimum shortfalls", lowItems.length ? "warn" : ""));

  root.appendChild(stats);

  const bar = h("div", { class: "row between mt-14" },
    tabs([
      { id: "water", label: "WATER", count: waterItems.length },
      { id: "food", label: "FOOD", count: foodStock.length },
      { id: "power", label: "POWER", count: powerItems.length },
      { id: "inventory", label: "INVENTORY", count: inv.length },
    ], lastTab, (id) => { lastTab = id; try { history.replaceState(null, "", `#/supply?tab=${id}`); } catch {} render(root); }));
  root.appendChild(bar);

  if (lastTab === "water") await waterSection(root, waterItems, waterStock, lowItems);
  if (lastTab === "food") await foodSection(root, foodStock, lowItems);
  if (lastTab === "power") await powerSection(root, powerItems, powerStock, lowItems);
  if (lastTab === "inventory") inventorySection(root, inv, lowItems);
  return () => {};
}

/* ------------------------------------------------------------------ water */
async function waterSection(root, logs, stock, lowItems) {
  const latest = logs[0] || null;
  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);

  const now = h("div", { class: "col" });
  if (latest) {
    now.appendChild(kv([
      ["Last treatment", fmt.date(latest.ts)],
      ["Volume", latest.volume_l != null ? `${fmt.num(latest.volume_l, 1)} L` : "—"],
      ["Method", latest.method || "—"],
      ["Free chlorine", latest.chlorine_ppm != null ? `${fmt.num(latest.chlorine_ppm, 1)} ppm` : "—"],
      ["Source", latest.source || "—"],
      ["Note", latest.note || "—"],
    ]));
    if (String(latest.method).toUpperCase() === "UNTREATED") {
      now.appendChild(h("div", { class: "callout danger small", text: "Latest batch is marked UNTREATED. Do not drink unless it is known-safe and verified." }));
    }
  } else {
    now.appendChild(emptyState("No water log", "Record source, volume and treatment method for every batch.", "drop"));
  }
  const waterLow = lowItems.filter((r) => r.category === "WATER");
  if (waterLow.length) {
    now.appendChild(h("div", { class: "callout warn small", text: `${waterLow.length} water stock item(s) at or below minimum.` }));
  }
  now.appendChild(h("div", { class: "row wrap mt-10" },
    btn("LOG TREATMENT", { size: "sm", tone: "primary", icon: "plus", onClick: () => addWater(root) }),
    btn("WATER MANUAL", { size: "sm", icon: "book", onClick: () => { location.hash = "#/manual?doc=water"; } })));
  grid.appendChild(panel({
    title: "Water status",
    sub: "Latest logged batch",
    icon: "drop",
    body: now,
  }));

  const trend = h("div", { class: "col" });
  const chart = h("canvas", { class: "vital-chart" });
  trend.appendChild(h("div", { class: "tiny muted", text: "LOGGED VOLUME (L)" }));
  trend.appendChild(chart);
  trend.appendChild(h("div", { class: "side-label mt-10", text: "STORED WATER ITEMS" }));
  if (!stock.length) trend.appendChild(h("div", { class: "small muted", text: "No inventory items in WATER category." }));
  for (const item of stock.slice(0, 8)) {
    trend.appendChild(h("div", { class: "row between small" },
      h("span", { text: item.name }),
      h("span", { class: "mono", text: `${fmt.num(item.qty, 2)} ${item.unit || ""}` })));
  }
  setTimeout(() => drawSeries(chart, logs, "volume_l", "#5dff9a"), 30);
  grid.appendChild(panel({ title: "Water trend & stores", sub: "Logged batches", icon: "activity", body: trend }));

  grid.appendChild(panel({
    title: "Water treatment log",
    sub: `${logs.length} entries`,
    icon: "note",
    cls: "span-2",
    tools: [btn("NEW", { size: "sm", icon: "plus", onClick: () => addWater(root) })],
    body: recordTable(logs, [
      { label: "TIME", key: "ts", render: (r) => fmt.date(r.ts) },
      { label: "SOURCE", key: "source" },
      { label: "VOLUME", key: "volume_l", num: true, render: (r) => r.volume_l != null ? `${fmt.num(r.volume_l, 1)} L` : "—" },
      { label: "METHOD", key: "method", render: (r) => badge(r.method || "—", r.method === "UNTREATED" ? "red" : "green") },
      { label: "Cl ppm", key: "chlorine_ppm", num: true },
      { label: "NOTE", key: "note", render: (r) => r.note || "—" },
      { label: "", key: "actions", render: (r) => rowActions({
        onEdit: () => openRecordForm({ table: "water_log", item: r, onSaved: () => render(root) }),
        onDelete: () => deleteRecord("water_log", r.id, "water log", () => render(root)),
      }) },
    ], { empty: "No water treatment records", emptyIcon: "drop" }),
  }));

  root.appendChild(panel({
    title: "Water treatment formulas",
    sub: "Verify concentration and local guidance",
    icon: "flask",
    cls: "mt-14",
    body: await calculatorGrid("WATER"),
  }));
  const refs = await referenceSection("WATER");
  if (refs) root.appendChild(panel({ title: "Water field cards", icon: "book", cls: "mt-14", body: refs }));
}

/* ------------------------------------------------------------------- food */
async function foodSection(root, food, lowItems) {
  const foodLow = lowItems.filter((r) => r.category === "FOOD");
  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);
  const summary = h("div", { class: "col" });
  summary.appendChild(kv([
    ["Food line items", String(food.length)],
    ["Below minimum", String(foodLow.length)],
    ["Low-stock names", foodLow.map((r) => r.name).join(", ") || "none"],
    ["Rotation", "Use oldest stock first; check dates and seals."],
  ]));
  summary.appendChild(h("div", { class: "callout warn small mt-10",
    text: "Calories on hand cannot be inferred from item counts. Enter quantities and units consistently, and use the ration calculator for planning — not as a nutrition prescription." }));
  summary.appendChild(h("div", { class: "row wrap mt-10" },
    btn("ADD FOOD STOCK", { size: "sm", tone: "primary", icon: "plus", onClick: () => addInventory(root, "FOOD") }),
    btn("FOOD MANUAL", { size: "sm", icon: "book", onClick: () => { location.hash = "#/manual?doc=food"; } })));
  grid.appendChild(panel({ title: "Food status", sub: "Inventory category FOOD", icon: "leaf", body: summary }));

  const list = h("div", { class: "col" });
  if (!food.length) list.appendChild(emptyState("No food inventory", "Add food items with quantity, unit and minimum stock.", "leaf"));
  for (const item of food.slice(0, 14)) {
    const pct = item.min_qty > 0 ? Math.min(100, (Number(item.qty) / Number(item.min_qty)) * 100) : 0;
    list.appendChild(h("div", { class: "col gap-4" },
      h("div", { class: "row between small" },
        h("span", { text: item.name }),
        h("span", { class: "mono", text: `${fmt.num(item.qty, 2)} ${item.unit || ""}` })),
      item.min_qty > 0 ? progressBar(pct, pct <= 100 ? "warn" : "") : null,
      h("div", { class: "tiny muted", text: [item.expires ? `exp ${item.expires}` : "", item.location || ""].filter(Boolean).join(" · ") || "no expiry/location" })));
  }
  grid.appendChild(panel({ title: "Food inventory", sub: "Closest to minimum first", icon: "box", body: list }));

  const calc = panel({
    title: "Ration planning",
    sub: "Calorie and water estimates",
    icon: "scale",
    cls: "span-2 mt-14",
    body: await calculatorGrid(["FOOD", "WATER"]),
  });
  root.appendChild(calc);
  const refs = await referenceSection("FOOD");
  if (refs) root.appendChild(panel({ title: "Food field cards", icon: "book", cls: "mt-14", body: refs }));
}

/* ------------------------------------------------------------------ power */
async function powerSection(root, logs, stock, lowItems) {
  const latest = logs[0] || null;
  const powerLow = lowItems.filter((r) => r.category === "POWER");
  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);

  const now = h("div", { class: "col" });
  now.appendChild(kv([
    ["Last log", latest ? fmt.date(latest.ts) : "—"],
    ["Battery", latest?.battery_pct != null ? `${fmt.num(latest.battery_pct, 0)} %` : "—"],
    ["Fuel", latest?.fuel_l != null ? `${fmt.num(latest.fuel_l, 1)} L` : "—"],
    ["Generator hours", latest?.generator_hours != null ? fmt.num(latest.generator_hours, 1) : "—"],
    ["Solar", latest?.solar_kwh != null ? `${fmt.num(latest.solar_kwh, 2)} kWh` : "—"],
    ["Load", latest?.load_w != null ? `${fmt.num(latest.load_w, 0)} W` : "—"],
    ["Note", latest?.note || "—"],
  ]));
  if (powerLow.length) now.appendChild(h("div", { class: "callout warn small", text: `${powerLow.length} power stock item(s) below minimum.` }));
  now.appendChild(h("div", { class: "row wrap mt-10" },
    btn("LOG POWER", { size: "sm", tone: "primary", icon: "plus", onClick: () => addPower(root) }),
    btn("POWER MANUAL", { size: "sm", icon: "book", onClick: () => { location.hash = "#/manual?doc=power"; } })));
  grid.appendChild(panel({ title: "Power status", sub: "Latest system log", icon: "bolt", body: now }));

  const charts = h("div", { class: "col" });
  const fields = [
    ["BATTERY (%)", "battery_pct", "#5dff9a"],
    ["FUEL (L)", "fuel_l", "#ffb02e"],
    ["SOLAR (kWh)", "solar_kwh", "#5dff9a"],
    ["LOAD (W)", "load_w", "#ffb02e"],
  ];
  const canvases = [];
  for (const [title, key, color] of fields) {
    charts.appendChild(h("div", { class: "tiny muted mt-6", text: title }));
    const canvas = h("canvas", { class: "vital-chart" });
    charts.appendChild(canvas);
    canvases.push({ canvas, key, color });
  }
  setTimeout(() => { for (const c of canvases) drawSeries(c.canvas, logs, c.key, c.color); }, 30);
  grid.appendChild(panel({ title: "Power trends", sub: "Chronological system logs", icon: "activity", body: charts }));

  grid.appendChild(panel({
    title: "Power log",
    sub: `${logs.length} entries`,
    icon: "note",
    cls: "span-2",
    tools: [btn("NEW", { size: "sm", icon: "plus", onClick: () => addPower(root) })],
    body: recordTable(logs, [
      { label: "TIME", key: "ts", render: (r) => fmt.date(r.ts) },
      { label: "BATTERY", key: "battery_pct", num: true, render: (r) => r.battery_pct != null ? `${fmt.num(r.battery_pct, 0)}%` : "—" },
      { label: "FUEL", key: "fuel_l", num: true, render: (r) => r.fuel_l != null ? `${fmt.num(r.fuel_l, 1)} L` : "—" },
      { label: "GEN HRS", key: "generator_hours", num: true },
      { label: "SOLAR", key: "solar_kwh", num: true, render: (r) => r.solar_kwh != null ? `${fmt.num(r.solar_kwh, 2)} kWh` : "—" },
      { label: "LOAD", key: "load_w", num: true, render: (r) => r.load_w != null ? `${fmt.num(r.load_w, 0)} W` : "—" },
      { label: "NOTE", key: "note", render: (r) => r.note || "—" },
      { label: "", key: "actions", render: (r) => rowActions({
        onEdit: () => openRecordForm({ table: "power_log", item: r, onSaved: () => render(root) }),
        onDelete: () => deleteRecord("power_log", r.id, "power log", () => render(root)),
      }) },
    ], { empty: "No power logs", emptyIcon: "bolt" }),
  }));

  root.appendChild(panel({
    title: "Power system calculators",
    sub: "Conservative planning estimates",
    icon: "flask",
    cls: "mt-14",
    body: await calculatorGrid("POWER"),
  }));
  const refs = await referenceSection("POWER");
  if (refs) root.appendChild(panel({ title: "Power field cards", icon: "book", cls: "mt-14", body: refs }));
}

/* -------------------------------------------------------------- inventory */
function inventorySection(root, items, lowItems) {
  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);
  const lowWrap = h("div", { class: "col" });
  if (!lowItems.length) lowWrap.appendChild(emptyState("Stock levels nominal", "No item is at or below its minimum quantity.", "box"));
  for (const item of lowItems) {
    const pct = item.min_qty > 0 ? Math.min(100, (Number(item.qty) / Number(item.min_qty)) * 100) : 0;
    const row = h("div", { class: "device-card" });
    row.appendChild(h("div", { class: "row between" },
      h("div", { class: "dev-name", text: item.name }),
      badge(`${fmt.num(item.qty, 2)} ${item.unit || ""}`, "red")));
    row.appendChild(progressBar(pct, "bad"));
    row.appendChild(h("div", { class: "row between tiny muted mt-6" },
      h("span", { text: `MIN ${fmt.num(item.min_qty, 2)} ${item.unit || ""}` }),
      h("span", { text: item.location || "no location" })));
    lowWrap.appendChild(row);
  }
  grid.appendChild(panel({ title: "Low stock", sub: "At or below minimum", icon: "warning", body: lowWrap }));

  const totals = h("div", { class: "col" });
  const cats = {};
  for (const item of items) cats[item.category || "OTHER"] = (cats[item.category || "OTHER"] || 0) + 1;
  totals.appendChild(kv(Object.entries(cats).sort((a, b) => b[1] - a[1]).map(([k, v]) => [k, `${v} item(s)`])));
  totals.appendChild(h("div", { class: "row wrap mt-10" }, btn("ADD ITEM", { size: "sm", tone: "primary", icon: "plus", onClick: () => addInventory(root) })));
  grid.appendChild(panel({ title: "Inventory composition", sub: "Line items by category", icon: "box", body: totals }));

  root.appendChild(panel({
    title: "Full inventory",
    sub: `${items.length} line items · ${lowItems.length} low`,
    icon: "database",
    cls: "span-2 mt-14",
    tools: [btn("NEW ITEM", { size: "sm", icon: "plus", onClick: () => addInventory(root) })],
    body: recordTable(items, [
      { label: "CATEGORY", key: "category", render: (r) => badge(r.category || "OTHER", r.category === "WATER" ? "cyan" : "") },
      { label: "ITEM", key: "name" },
      { label: "QTY", key: "qty", num: true, render: (r) => `${fmt.num(r.qty, 2)} ${r.unit || ""}` },
      { label: "MIN", key: "min_qty", num: true, render: (r) => r.min_qty != null ? `${fmt.num(r.min_qty, 2)} ${r.unit || ""}` : "—" },
      { label: "EXPIRES", key: "expires" },
      { label: "LOCATION", key: "location" },
      { label: "UPDATED", key: "updated", render: (r) => fmt.ago(r.updated) },
      { label: "", key: "actions", render: (r) => rowActions({
        onEdit: () => openRecordForm({ table: "inventory", item: r, onSaved: () => render(root) }),
        onDelete: () => deleteRecord("inventory", r.id, r.name || "inventory item", () => render(root)),
      }) },
    ], { empty: "Inventory is empty", emptyIcon: "box" }),
  }));
}

/* ------------------------------------------------------------------ charts */
function drawSeries(canvas, logs, key, color) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(260, rect.width || 600), hh = Math.max(80, rect.height || 130);
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(hh * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = "#020604"; ctx.fillRect(0, 0, w, hh);
  const ordered = [...logs].reverse();
  const values = ordered.map((r) => r[key] == null ? null : Number(r[key]));
  const real = values.filter((v) => v != null && Number.isFinite(v));
  ctx.strokeStyle = "rgba(93,255,154,.07)"; ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) { const y = (hh / 4) * i; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke(); }
  if (!real.length) {
    ctx.fillStyle = "rgba(123,154,136,.65)"; ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.fillText("NO DATA", 10, hh / 2);
    return;
  }
  let min = Math.min(...real), max = Math.max(...real);
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * .14; min -= pad; max += pad;
  ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 1.6;
  let started = false;
  values.forEach((v, i) => {
    if (v == null) return;
    const x = 6 + (ordered.length <= 1 ? 0 : (i / (ordered.length - 1)) * (w - 12));
    const y = hh - 7 - ((v - min) / (max - min)) * (hh - 15);
    if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = "rgba(123,154,136,.7)"; ctx.font = "8px 'JetBrains Mono', monospace";
  ctx.fillText(String(Math.round(max)), 3, 9);
  ctx.fillText(String(Math.round(min)), 3, hh - 3);
}

/* ----------------------------------------------------------------- actions */
function addWater(root) {
  openRecordForm({
    table: "water_log",
    defaults: { method: "BOILED" },
    title: "Log water treatment",
    intro: h("div", { class: "callout small", text: "Record the actual source, volume and treatment. Do not mark water treated unless the process was completed." }),
    onSaved: () => { lastTab = "water"; render(root); },
  });
}

function addPower(root) {
  openRecordForm({ table: "power_log", title: "Log power system", onSaved: () => { lastTab = "power"; render(root); } });
}

function addInventory(root, category = "") {
  openRecordForm({
    table: "inventory",
    defaults: category ? { category } : {},
    onSaved: () => { lastTab = "inventory"; render(root); },
  });
}
