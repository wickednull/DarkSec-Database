/* Communications: channels, emergency, bands, plans, radio log and field references. */
import { api } from "../api.js";
import {
  h, panel, pageHead, btn, input, loading, toast, badge, modal, fmt,
  emptyState, iconBtn, icon,
} from "../components.js";
import { openRecordForm, deleteRecord, recordTable, rowActions } from "../records.js";
import { calculatorGrid } from "../calculators.js";
import { errorBox, referenceSection, printText } from "./shared.js?v=opsec-1";

function priorityTone(p) {
  const v = String(p || "").toUpperCase();
  if (["EMERGENCY", "DISTRESS", "GUARD"].includes(v)) return "red";
  if (["CALLING", "SAFETY", "WEATHER"].includes(v)) return "amber";
  return "muted";
}

function channelText(c) {
  return `${c.name || ""} ${c.use || ""} ${c.mode || ""} ${c.region || ""} ${c.priority || ""}`.toLowerCase();
}

function categoryMatches(c, category) {
  if (category === "ALL") return true;
  const text = channelText(c);
  const priority = String(c.priority || "").toUpperCase();
  const emergency = /EMERGENCY|DISTRESS|GUARD|CALLING|SAFETY/.test(priority)
    || /distress|guard|calling|emergency/.test(text);
  if (category === "EMERGENCY") return emergency;
  if (category === "WEATHER") return /weather|noaa|wx|forecast/.test(text);
  if (category === "AVIATION") return /aviation|airband|airport|atis|awos|unicom|ctaf|volmet|air/.test(text);
  if (category === "MARINE") return /marine|maritime|vessel|ship|channel 16|vhf ch/.test(text);
  if (category === "UTILITY") return !emergency
    && !/weather|noaa|wx|forecast/.test(text)
    && !/aviation|airband|airport|atis|awos|unicom|ctaf|volmet/.test(text)
    && !/marine|maritime|vessel|ship|channel 16/.test(text);
  return true;
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

  const channels = freq.channels || [];
  const bands = freq.bands || [];
  const state = {
    bandQ: "",
    channelQ: "",
    category: "ALL",
  };

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // SIGNAL",
    title: "Communications",
    desc: "Channels, bands, nets, logs and field references. Receive reference only; transmitting requires authorisation.",
    actions: [
      btn("NEW SOI", { icon: "plus", tone: "primary", onClick: () => newPlan() }),
      btn("NEW LOG", { icon: "note", onClick: () => newRadioLog() }),
      btn("SDR CONSOLE", { icon: "antenna", onClick: () => { location.hash = "#/sdr"; } }),
    ],
  }));

  // ------------------------------------------------------------- tabs
  const tabDefs = [
    ["channels", "CHANNELS", "radio"],
    ["emergency", "EMERGENCY", "warning"],
    ["bands", "BANDS", "signal"],
    ["plans", "PLANS", "note"],
    ["log", "LOG", "clock"],
    ["tools", "TOOLS", "box"],
  ];
  const tabBar = h("div", { class: "sdr-tabs comms-tabs", role: "tablist" });
  const tabButtons = {};
  const paneChannels = h("div", { class: "sdr-pane", id: "comms-pane-channels" });
  const paneEmergency = h("div", { class: "sdr-pane", id: "comms-pane-emergency" });
  const paneBands = h("div", { class: "sdr-pane", id: "comms-pane-bands" });
  const panePlans = h("div", { class: "sdr-pane", id: "comms-pane-plans" });
  const paneLog = h("div", { class: "sdr-pane", id: "comms-pane-log" });
  const paneTools = h("div", { class: "sdr-pane panel-grid grid-2", id: "comms-pane-tools" });
  const panes = {
    channels: paneChannels, emergency: paneEmergency, bands: paneBands,
    plans: panePlans, log: paneLog, tools: paneTools,
  };
  function showTab(name) {
    const active = panes[name] ? name : "channels";
    for (const [id, button] of Object.entries(tabButtons)) {
      button.classList.toggle("active", id === active);
      button.setAttribute("aria-selected", id === active ? "true" : "false");
    }
    for (const [id, pane] of Object.entries(panes)) {
      pane.classList.toggle("active", id === active);
    }
    try { localStorage.setItem("darksec.comms.tab", active); } catch {}
  }
  for (const [id, label, iconName] of tabDefs) {
    const button = h("button", { class: "sdr-tab", role: "tab",
      onClick: () => showTab(id) }, icon(iconName), h("span", { text: label }));
    tabButtons[id] = button;
    tabBar.appendChild(button);
  }
  root.appendChild(tabBar);
  root.appendChild(h("div", { class: "sdr-panes" }, ...Object.values(panes)));
  let initialTab = "channels";
  try { initialTab = localStorage.getItem("darksec.comms.tab") || "channels"; } catch {}
  showTab(initialTab);

  const disclaimer = freq.disclaimer
    ? h("div", { class: "callout warn small mb-10" }, freq.disclaimer) : null;

  // ------------------------------------------------------------- channels
  const channelSearch = input({ type: "search", placeholder: "Search name, use or region…", autocomplete: "off" });
  channelSearch.addEventListener("input", () => { state.channelQ = channelSearch.value.trim().toLowerCase(); paintChannels(); });
  const filterBar = h("div", { class: "comms-chips" });
  const filters = [
    ["ALL", "ALL"], ["EMERGENCY", "EMERGENCY"], ["WEATHER", "WEATHER"],
    ["AVIATION", "AVIATION"], ["MARINE", "MARINE"], ["UTILITY", "UTILITY"],
  ];
  const filterButtons = {};
  for (const [value, label] of filters) {
    const chip = h("button", { class: "comms-chip" + (value === "ALL" ? " active" : ""),
      text: label, onClick: () => {
        state.category = value;
        for (const [key, node] of Object.entries(filterButtons)) {
          node.classList.toggle("active", key === value);
        }
        paintChannels();
      } });
    filterButtons[value] = chip;
    filterBar.appendChild(chip);
  }
  const channelOut = h("div");
  const channelCount = h("span", { class: "tiny muted mono" });
  const channelsPanel = panel({
    title: "Channels",
    sub: "Calling, weather, aviation, marine and utility frequencies.",
    icon: "radio",
    tools: [channelCount, channelSearch],
    body: h("div", { class: "comms-channels-body" }, filterBar, channelOut),
  });
  paneChannels.appendChild(channelsPanel);

  function channelList(list, { emergency = false } = {}) {
    if (!list.length) {
      return emptyState("No matching channels", "Clear the filter or search for a different name.", "radio");
    }
    const wrap = h("div", { class: "comms-channel-list" });
    for (const c of list) {
      const title = h("div", { class: "comms-channel-title" },
        h("span", { text: c.name }),
        c.region ? h("span", { class: "comms-region", text: c.region }) : null);
      const main = h("div", { class: "comms-channel-main" }, title,
        h("div", { class: "comms-channel-use", text: c.use || "Reference channel" }));
      const freq = h("div", { class: "comms-channel-freq" },
        h("b", { text: fmt.num(c.mhz, 4) }), h("span", { text: "MHz" }));
      const meta = h("div", { class: "comms-channel-meta" },
        badge(String(c.mode || "—").toUpperCase(), "muted"),
        badge(c.priority || "INFO", priorityTone(c.priority)),
        badge(c.tx ? "TX LICENSED" : "RX", c.tx ? "amber" : "muted"));
      const row = h("div", { class: `comms-channel${emergency ? " emergency" : ""}` },
        main, freq, meta,
        btn("TUNE", { size: "sm", tone: "ghost", icon: "antenna", onClick: () => tune(c.mhz, c.mode) }));
      wrap.appendChild(row);
    }
    return wrap;
  }

  function paintChannels() {
    let list = channels;
    if (state.channelQ) list = list.filter((c) => channelText(c).includes(state.channelQ));
    list = list.filter((c) => categoryMatches(c, state.category));
    channelCount.textContent = `${list.length} CH`;
    channelOut.replaceChildren(channelList(list));
  }
  paintChannels();

  // ------------------------------------------------------------ emergency
  const emergency = channels.filter((c) => categoryMatches(c, "EMERGENCY"));
  const emergencyPanel = panel({
    title: "Emergency & calling",
    sub: "Know how to summon help. Do not transmit test calls on distress channels.",
    icon: "warning",
    tools: [badge(`${emergency.length} CHANNELS`, "red")],
    body: channelList(emergency, { emergency: true }),
  });
  paneEmergency.appendChild(emergencyPanel);

  // ---------------------------------------------------------------- bands
  const bandOut = h("div");
  const bandSearch = input({ type: "search", placeholder: "Filter bands…", autocomplete: "off" });
  bandSearch.addEventListener("input", () => { state.bandQ = bandSearch.value.trim().toLowerCase(); paintBands(); });
  const bandsPanel = panel({
    title: "Frequency bands",
    sub: `${bands.length} allocations · receive reference only`,
    icon: "signal",
    tools: [bandSearch],
    body: bandOut,
  });
  paneBands.appendChild(bandsPanel);

  function paintBands() {
    let list = bands;
    if (state.bandQ) list = list.filter((b) => `${b.name} ${b.use} ${b.mode}`.toLowerCase().includes(state.bandQ));
    bandOut.replaceChildren();
    if (!list.length) { bandOut.appendChild(emptyState("No matching bands", "", "signal")); return; }
    const grid = h("div", { class: "comms-band-grid" });
    for (const b of list) {
      grid.appendChild(h("div", { class: "comms-band-card" },
        h("div", { class: "comms-band-head" },
          h("span", { class: "comms-band-name", text: b.name }),
          badge(String(b.mode || "—").toUpperCase(), "muted")),
        h("div", { class: "comms-band-range" },
          h("b", { text: `${fmt.num(b.start_mhz, 4)}–${fmt.num(b.end_mhz, 3)}` }), h("span", { text: "MHz" })),
        h("div", { class: "comms-band-use", text: b.use || "" }),
        btn("TUNE", { size: "sm", tone: "ghost", icon: "antenna",
          onClick: () => tune((b.start_mhz + b.end_mhz) / 2, b.mode) })));
    }
    bandOut.appendChild(grid);
  }
  paintBands();

  // ---------------------------------------------------------------- plans
  const planWrap = h("div", { class: "comms-plan-list" });
  const planPanel = panel({
    title: "Signal operating instructions",
    sub: "Net structure, frequencies, schedules, callsigns, authentication and fallbacks.",
    icon: "note",
    tools: [btn("NEW PLAN", { size: "sm", icon: "plus", onClick: newPlan })],
    body: planWrap,
  });
  panePlans.appendChild(planPanel);

  function paintPlans() {
    planWrap.replaceChildren();
    const items = soi.items || [];
    if (!items.length) {
      planWrap.appendChild(emptyState("No signal plan", "Write down how the group communicates when phones fail.", "radio"));
      return;
    }
    for (const plan of items) {
      planWrap.appendChild(h("div", { class: "comms-plan-card" },
        h("div", { class: "comms-plan-main" },
          h("div", { class: "comms-plan-name", text: plan.name }),
          h("div", { class: "comms-plan-meta",
            text: `created ${fmt.date(plan.created)} · ${String(plan.data || "").length} chars` })),
        h("div", { class: "comms-plan-actions" },
          btn("VIEW", { size: "sm", tone: "ghost", icon: "eye", onClick: () => viewPlan(plan) }),
          iconBtn("edit", "Edit plan", () => openRecordForm({ table: "comms_plans", item: plan, onSaved: reloadPlans })),
          iconBtn("trash", "Delete plan", () => deleteRecord("comms_plans", plan.id, "signal plan", reloadPlans)))));
    }
  }

  function viewPlan(plan) {
    const pre = h("pre", { class: "markdown", style: { whiteSpace: "pre-wrap", maxHeight: "60vh", overflow: "auto" } });
    pre.textContent = plan.data || "";
    const body = h("div", { class: "col" },
      h("div", { class: "row wrap gap-6" },
        btn("COPY", { size: "sm", icon: "copy", onClick: () => navigator.clipboard?.writeText(plan.data || "").then(() => toast("SOI copied")).catch(() => toast("Copy failed", "error")) }),
        btn("PRINT", { size: "sm", icon: "print", onClick: () => printText(`SOI // ${plan.name}`, plan.data || "") })),
      pre);
    const m = modal({ title: plan.name, sub: "SIGNAL OPERATING INSTRUCTION", wide: true, body,
      actions: [btn("CLOSE", { tone: "ghost", onClick: () => m.close() })] });
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

  // ------------------------------------------------------------------ log
  const logWrap = h("div");
  const logPanel = panel({
    title: "Radio log",
    sub: "Who, when, frequency, mode and a short summary. Keep it factual.",
    icon: "note",
    tools: [btn("NEW ENTRY", { size: "sm", icon: "plus", onClick: newRadioLog })],
    body: logWrap,
  });
  paneLog.appendChild(logPanel);

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
    openRecordForm({ table: "radio_log", defaults: { mode: "NFM", freq_hz: 0 }, onSaved: reloadLogs });
  }
  async function reloadLogs() {
    try {
      logs = await api.get("/api/records/radio_log?limit=40");
      paintLogs(logs.items || []);
    } catch (err) { toast(err.message, "error"); }
  }
  paintLogs(logs.items || []);

  // ---------------------------------------------------------------- tools
  const phonetic = freq.phonetic || {};
  const phonPanel = panel({
    title: "Phonetic alphabet", icon: "mic",
    body: h("div", { class: "comms-kv" }, ...Object.entries(phonetic).flatMap(([letter, word]) => [
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
  paneTools.appendChild(phonPanel);
  paneTools.appendChild(proPanel);
  paneTools.appendChild(qPanel);

  if (disclaimer) {
    disclaimer.classList.add("span-2");
    paneTools.appendChild(disclaimer);
  }
  if (freq.wilderness_protocol) {
    paneTools.appendChild(h("div", { class: "callout warn small span-2" },
      h("strong", { text: "Wilderness Protocol // " }), freq.wilderness_protocol));
  }

  const calcPanel = panel({
    title: "Communications calculators", icon: "signal",
    cls: "span-2",
    sub: "Radio horizon, antenna dimensions, path loss, link budget, Morse and HF outlook.",
    body: await calculatorGrid(["COMMS"]),
  });
  paneTools.appendChild(calcPanel);

  const commCards = await referenceSection("COMMS");
  if (commCards) paneTools.appendChild(panel({ title: "Communications field cards", icon: "book", cls: "span-2", body: commCards }));

  function tune(mhz, mode) {
    location.hash = `#/sdr?freq=${encodeURIComponent(mhz)}&mode=${encodeURIComponent(String(mode || "").toLowerCase())}`;
  }

  return null;
}
