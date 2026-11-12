/* Communications: frequencies, signal plans, radio log and procedure references. */
import { api } from "../api.js";
import {
  h, panel, pageHead, btn, input, select, field, loading, toast, badge, modal, fmt,
  emptyState, confirmDialog, iconBtn,
} from "../components.js";
import { openRecordForm, deleteRecord, recordTable, rowActions } from "../records.js";
import { calculatorGrid } from "../calculators.js";
import { kv, errorBox, referenceSection, printText, severityBadge } from "./shared.js?v=opsec-1";

function priorityTone(p) {
  const v = String(p || "").toUpperCase();
  if (["EMERGENCY", "DISTRESS", "GUARD"].includes(v)) return "red";
  if (["CALLING", "SAFETY", "WEATHER"].includes(v)) return "amber";
  return "muted";
}

export async function render(root, params) {
  root.replaceChildren(loading("LOADING COMMUNICATIONS REFERENCE"));
  let freq, soi, logs;
  try {
    [freq, soi, logs] = await Promise.all([
      api.get("/api/comms/reference"),
      api.get("/api/comms/soi"),
      api.get("/api/records/radio_log?limit=40"),
    ]);
  } catch (err) {
    root.replaceChildren(pageHead({ kicker: "OPERATIONS // COMMS", title: "Communications" }), errorBox(err));
    return;
  }

  const state = { bandQ: "", channelQ: "", channelPriority: "ALL", plan: null };
  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // SIGNAL",
    title: "Communications",
    desc: "Frequencies, nets and procedures. Listening may be restricted; transmitting requires authorisation. This is a reference, not a licence.",
    actions: [
      btn("NEW SOI", { icon: "plus", tone: "primary", onClick: () => newPlan() }),
      btn("NEW RADIO LOG", { icon: "note", onClick: () => newRadioLog() }),
      btn("SDR CONSOLE", { icon: "antenna", onClick: () => { location.hash = "#/sdr"; } }),
    ],
  }));

  if (freq.disclaimer) {
    root.appendChild(h("div", { class: "callout warn small mb-10", text: freq.disclaimer }));
  }

  /* --------------------------------------------------------------- SOI */
  const planWrap = h("div", { class: "col" });
  const planPanel = panel({
    title: "Signal operating instructions", icon: "radio",
    sub: "Net structure, frequencies, schedules, callsigns, authentication and fallbacks.",
    tools: [btn("NEW", { size: "sm", icon: "plus", onClick: newPlan })],
    body: planWrap,
  });
  root.appendChild(planPanel);

  function paintPlans() {
    planWrap.replaceChildren();
    const items = soi.items || [];
    if (!items.length) {
      planWrap.appendChild(emptyState("No signal plan", "Write down how the group communicates when phones fail.", "radio"));
      return;
    }
    for (const plan of items) {
      const item = h("div", { class: "list-item" });
      const main = h("div", { class: "li-main" });
      main.appendChild(h("div", { class: "li-title", text: plan.name }));
      main.appendChild(h("div", { class: "li-sub", text: `created ${fmt.date(plan.created)} · ${String(plan.data || "").length} chars` }));
      item.appendChild(main);
      item.appendChild(btn("VIEW", { size: "sm", tone: "ghost", onClick: () => viewPlan(plan) }));
      item.appendChild(iconBtn("edit", "Edit plan", () => openRecordForm({ table: "comms_plans", item: plan, onSaved: reloadPlans })));
      item.appendChild(iconBtn("trash", "Delete plan", () => deleteRecord("comms_plans", plan.id, "signal plan", reloadPlans)));
      planWrap.appendChild(item);
    }
  }

  function viewPlan(plan) {
    const body = h("div", { class: "col" });
    const pre = h("pre", { class: "markdown", style: { whiteSpace: "pre-wrap", maxHeight: "60vh", overflow: "auto" } });
    pre.textContent = plan.data || "";
    body.append(
      h("div", { class: "row wrap gap-6" },
        btn("COPY", { size: "sm", onClick: () => navigator.clipboard?.writeText(plan.data || "").then(() => toast("SOI copied")).catch(() => toast("Copy failed", "error")) }),
        btn("PRINT", { size: "sm", icon: "print", onClick: () => printText(`SOI // ${plan.name}`, plan.data || "") }),
      ), pre);
    const m = modal({ title: plan.name, sub: "SIGNAL OPERATING INSTRUCTION", wide: true, body, actions: [btn("CLOSE", { tone: "ghost", onClick: () => m.close() })] });
  }

  function newPlan() {
    openRecordForm({
      table: "comms_plans",
      title: "New signal operating instruction",
      intro: h("div", { class: "callout small", text: "Define primary and alternate frequencies, daily schedule, callsigns, authentication and rendezvous if comms fail." }),
      onSaved: reloadPlans,
    });
  }

  async function reloadPlans() {
    try {
      soi = await api.get("/api/comms/soi");
      paintPlans();
    } catch (err) { toast(err.message, "error"); }
  }
  paintPlans();

  /* ------------------------------------------------------ emergency channels */
  const channels = freq.channels || [];
  const emergency = channels.filter((c) => /EMERGENCY|DISTRESS|CALLING|SAFETY/i.test(c.priority || "") || /distress|guard|calling|emergency/i.test(c.use || ""));
  const emergencyPanel = panel({
    title: "Emergency, calling & guard", icon: "warning",
    sub: "Know how to summon help. Do not transmit test calls on distress channels.",
    tools: [btn("ALL CHANNELS", { size: "sm", onClick: () => document.getElementById("channelPanel")?.scrollIntoView({ behavior: "smooth" }) })],
    body: channelTable(emergency, true),
  });
  root.appendChild(emergencyPanel);

  /* ------------------------------------------------------------- bands */
  const bandOut = h("div");
  const bandSearch = input({ type: "search", placeholder: "Filter bands…", autocomplete: "off" });
  bandSearch.style.maxWidth = "260px";
  bandSearch.addEventListener("input", () => { state.bandQ = bandSearch.value.trim().toLowerCase(); paintBands(); });
  const bandsPanel = panel({
    title: "Frequency bands", icon: "signal",
    sub: `${(freq.bands || []).length} allocations · receive reference only`,
    tools: [bandSearch],
    body: bandOut,
  });
  root.appendChild(bandsPanel);

  function paintBands() {
    let list = freq.bands || [];
    if (state.bandQ) list = list.filter((b) => `${b.name} ${b.use} ${b.mode}`.toLowerCase().includes(state.bandQ));
    bandOut.replaceChildren();
    if (!list.length) { bandOut.appendChild(emptyState("No matching bands", "", "signal")); return; }
    bandOut.appendChild(h("div", { class: "scroll-x" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, ["BAND", "RANGE", "MODE", "USE", ""].map((x) => h("th", { text: x })))),
      h("tbody", {}, ...list.map((b) => h("tr", {},
        h("td", {}, h("div", { class: "strong", text: b.name })),
        h("td", { class: "mono nowrap", text: `${fmt.num(b.start_mhz, 4)}–${fmt.num(b.end_mhz, 3)} MHz` }),
        h("td", { text: b.mode }),
        h("td", { class: "small muted", text: b.use }),
        h("td", { class: "actions" }, btn("TUNE", { size: "sm", tone: "ghost", icon: "antenna", onClick: () => tune((b.start_mhz + b.end_mhz) / 2, b.mode) })),
      ))))));
  }
  paintBands();

  /* ----------------------------------------------------------- channels */
  const channelOut = h("div");
  const channelSearch = input({ type: "search", placeholder: "Filter channels…", autocomplete: "off" });
  const channelPriority = select([
    { value: "ALL", label: "All priorities" },
    ...Array.from(new Set(channels.map((c) => c.priority).filter(Boolean))).map((p) => ({ value: p, label: p })),
  ], "ALL");
  channelSearch.addEventListener("input", () => { state.channelQ = channelSearch.value.trim().toLowerCase(); paintChannels(); });
  channelPriority.addEventListener("change", () => { state.channelPriority = channelPriority.value; paintChannels(); });
  const channelsPanel = panel({
    title: "Known channels", icon: "radio",
    sub: "Common calling, weather, aviation, marine and utility channels. Region-specific allocations vary.",
    id: "channelPanel",
    tools: [channelSearch, channelPriority],
    body: channelOut,
  });
  root.appendChild(channelsPanel);

  function paintChannels() {
    let list = channels;
    if (state.channelQ) list = list.filter((c) => `${c.name} ${c.use} ${c.mode} ${c.region}`.toLowerCase().includes(state.channelQ));
    if (state.channelPriority !== "ALL") list = list.filter((c) => c.priority === state.channelPriority);
    channelOut.replaceChildren();
    if (!list.length) { channelOut.appendChild(emptyState("No matching channels", "", "radio")); return; }
    channelOut.appendChild(channelTable(list));
  }
  paintChannels();

  function channelTable(list) {
    const head = h("tr", {}, ...["CHANNEL", "FREQ", "MODE", "PRIORITY", "USE", "TX", ""].map((x) => h("th", { text: x })));
    const rows = list.map((c) => h("tr", {},
      h("td", {}, h("div", { class: "strong", text: c.name }), h("div", { class: "tiny dim", text: c.region || "" })),
      h("td", { class: "mono nowrap", text: `${fmt.num(c.mhz, 4)} MHz` }),
      h("td", { text: c.mode }),
      h("td", {}, badge(c.priority || "INFO", priorityTone(c.priority))),
      h("td", { class: "small muted", text: c.use || "" }),
      h("td", {}, badge(c.tx ? "LICENSED" : "RX", c.tx ? "amber" : "muted")),
      h("td", { class: "actions" }, btn("TUNE", { size: "sm", tone: "ghost", icon: "antenna", onClick: () => tune(c.mhz, c.mode) })),
    ));
    return h("div", { class: "scroll-x" }, h("table", { class: "data" }, h("thead", {}, head), h("tbody", {}, ...rows)));
  }


  function tune(mhz, mode) {
    location.hash = `#/sdr?freq=${encodeURIComponent(mhz)}&mode=${encodeURIComponent(String(mode || "").toLowerCase())}`;
  }

  /* ------------------------------------------------------------ radio log */
  const logWrap = h("div");
  const logPanel = panel({
    title: "Radio log", icon: "note",
    sub: "Who, when, frequency, mode and a short summary. Keep it factual.",
    tools: [btn("NEW ENTRY", { size: "sm", icon: "plus", onClick: newRadioLog })],
    body: logWrap,
  });
  root.appendChild(logPanel);

  function paintLogs(items) {
    logWrap.replaceChildren(recordTable(items || [], [
      { label: "TIME", key: "ts", render: (r) => h("span", { class: "mono", text: fmt.date(r.ts) }) },
      { label: "FREQ", key: "freq_hz", render: (r) => h("span", { class: "mono", text: r.freq_hz ? `${fmt.num(r.freq_hz / 1e6, 4)} MHz` : "—" }) },
      { label: "MODE", key: "mode" },
      { label: "CALLSIGN", key: "callsign" },
      { label: "SUMMARY", key: "summary" },
      { label: "", key: "actions", render: (r) => rowActions({
        onEdit: () => openRecordForm({ table: "radio_log", item: r, onSaved: reloadLogs }),
        onDelete: () => deleteRecord("radio_log", r.id, "radio log", reloadLogs),
      }) },
    ], { empty: "No radio traffic logged yet", emptyIcon: "radio" }));
  }
  function newRadioLog() {
    openRecordForm({
      table: "radio_log",
      defaults: { mode: "NFM", freq_hz: 0 },
      onSaved: reloadLogs,
    });
  }
  async function reloadLogs() {
    try {
      logs = await api.get("/api/records/radio_log?limit=40");
      paintLogs(logs.items || []);
    } catch (err) { toast(err.message, "error"); }
  }
  paintLogs(logs.items || []);

  /* ------------------------------------------------------- procedures etc */
  const refs = h("div", { class: "grid grid-3 mt-14" });
  const phonetic = freq.phonetic || {};
  const phonPanel = panel({
    title: "Phonetic alphabet", icon: "mic",
    body: h("div", { class: "kv" }, ...Object.entries(phonetic).flatMap(([letter, word]) => [
      h("span", { class: "k mono", text: letter }), h("span", { class: "v", text: word }),
    ])),
  });
  const prowords = freq.prowords || [];
  const proPanel = panel({
    title: "Prowords", icon: "radio",
    body: h("div", { class: "col" }, ...prowords.map((p) => h("div", { class: "coord-line" },
      h("span", { class: "strong", text: p.word || p.term || "" }),
      h("b", { class: "small muted", text: p.meaning || p.text || "" })))),
  });
  const qcodes = freq.qcodes || [];
  const qPanel = panel({
    title: "Q-codes", icon: "terminal",
    body: h("div", { class: "col" }, ...qcodes.map((q) => h("div", { class: "coord-line" },
      h("span", { class: "strong mono", text: q.code || q.q || "" }),
      h("b", { class: "small muted", text: q.meaning || q.text || "" })))),
  });
  refs.append(phonPanel, proPanel, qPanel);
  root.appendChild(refs);

  if (freq.wilderness_protocol) {
    root.appendChild(h("div", { class: "callout warn small mt-14" },
      h("strong", { text: "Wilderness Protocol // " }), freq.wilderness_protocol));
  }

  root.appendChild(panel({
    title: "Communications calculators", icon: "signal",
    sub: "Radio horizon, antenna dimensions, path loss, link budget, Morse and HF outlook.",
    body: await calculatorGrid(["COMMS"]),
  }));

  const commCards = await referenceSection("COMMS");
  if (commCards) root.appendChild(panel({ title: "Communications field cards", icon: "book", body: commCards }));

  return null;
}
