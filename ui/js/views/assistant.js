import { api, promptVaultUnlock } from "../api.js";
import { store } from "../store.js";
import { h, pageHead, panel, btn, input, textarea, field, select, badge, alertStrip, loading, toast, toggle, renderMarkdown, fmt } from "../components.js";

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)darksec_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

const chatSession = { conversationId: null, messages: [], selectedModel: "", think: null, includeContext: false, sending: false, creatingConversation: false, abortController: null, pendingImages: [], recognizing: false };

function nowTs() { return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }

async function timedGet(path, ms = 8000) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), ms);
  try { return await api.get(path, { signal: ctrl.signal }); }
  catch { return null; }
  finally { clearTimeout(t); }
}

async function buildContext() {
  const ctx = { generated_at: new Date().toISOString(), view: location.hash || "#/assistant", note: "Cached/partial app context. Do not treat as live sensor truth." };
  try {
    const [status, settings, intel, dashboard, cbrn, sync, feeds, checklists, incidents] = await Promise.all([
      timedGet("/api/status", 4000),
      timedGet("/api/settings", 3000),
      timedGet("/api/intel/overview", 5000),
      timedGet("/api/dashboard", 5000),
      timedGet("/api/cbrn/status", 4000),
      timedGet("/api/sync/status", 3000),
      timedGet("/api/feeds", 3000),
      timedGet("/api/checklists", 3000),
      timedGet("/api/records/incidents", 3000),
    ]);
    if (status) ctx.status = { ok: status.ok, online: status.online, sync: status.sync ? { running: status.sync.running, phase: status.sync.phase, last_error: status.sync.last_error } : null };
    if (settings) ctx.settings = { units: settings.units, callsign: settings.callsign, opsec_mode: settings.opsec_mode, lat: settings.opsec_mode ? null : settings.lat, lon: settings.opsec_mode ? null : settings.lon, grid_format: settings.grid_format, timezone: settings.timezone };
    if (dashboard) ctx.dashboard = { alerts: (dashboard.alerts || []).slice(0, 8), readiness: dashboard.readiness, counts: dashboard.counts, feed_critical: (dashboard.feed_critical || []).slice(0, 5) };
    if (intel) ctx.intel = {
      threat: intel.threat ? { score: intel.threat.score, level: intel.threat.level, band: intel.threat.band } : null,
      critical_count: intel.critical_count,
      aircraft_count: (intel.aircraft || []).length,
      satellite_count: (intel.satellites || []).length,
      air_quality: intel.air_quality ? { aqi: intel.air_quality.aqi, category: intel.air_quality.category } : null,
      weather: intel.weather ? { temp_c: intel.weather.temp_c, summary: intel.weather.summary, wind_kph: intel.weather.wind_kph } : null,
      latest_events: (intel.events || []).filter(e => {
        const s = store.settings || {};
        const la = Number(s.lat), lo = Number(s.lon);
        if (!Number.isFinite(la) || !Number.isFinite(lo) || e.lat == null || e.lon == null) return true;
        const R = 6371, toRad = x => x * Math.PI / 180;
        const dlat = toRad(e.lat - la), dlon = toRad(e.lon - lo);
        const a = Math.sin(dlat/2)**2 + Math.cos(toRad(la))*Math.cos(toRad(e.lat))*Math.sin(dlon/2)**2;
        return 2 * R * Math.asin(Math.sqrt(a)) <= 2000;
      }).slice(0, 6).map(e => ({ title: e.title, severity: e.severity, time: e.time, source: e.source, place: e.place })),
      latest_war: (intel.war || intel.conflict_news || []).slice(0, 4).map(e => ({ title: e.title, source: e.source, time: e.time })),
    };
    if (cbrn) ctx.cbrn = {
      kp: cbrn.space_weather?.kp ?? null,
      scales: cbrn.space_weather?.scales ?? null,
      radiation_reports: (cbrn.radiation?.public || []).length,
      local_dose: cbrn.radiation?.local ? { dose_rate_usv_h: cbrn.radiation.local.dose_rate_usv_h, ts: cbrn.radiation.local.ts } : null,
      volcano_reports: (cbrn.volcano?.items || []).length,
      hazard_events: (cbrn.hazards?.items || []).length,
      air_quality: cbrn.air_quality?.current ? { aqi: cbrn.air_quality.current.aqi, category: cbrn.air_quality.current.category } : null,
    };
    if (sync) ctx.sync = { running: sync.running, phase: sync.phase, last_ok: sync.last_ok, last_error: sync.last_error };
    const feedItems = Array.isArray(feeds) ? feeds : (feeds?.feeds || []);
    if (Array.isArray(feedItems)) {
      const probs = feedItems.filter(f => f && (f.error || f.status === "ERROR" || f.status === "STALE")).slice(0, 6)
        .map(f => ({ name: f.name || f.id, status: f.status, error: f.error }));
      ctx.feeds = { total: feedItems.length, issues: probs };
    }
    if (checklists?.kits) {
      const state = checklists.state || {};
      ctx.checklists = checklists.kits.slice(0, 8).map(k => {
        const items = k.items || [];
        const done = items.filter(i => state[`${k.id}:${i.id}`]).length;
        return { name: k.name, done, total: items.length };
      });
    }
    if (Array.isArray(incidents)) ctx.incidents = incidents.slice(0, 6).map(i => ({ title: i.title || i.name, status: i.status, severity: i.severity, updated_at: i.updated_at })).filter(i => i.title);
  } catch {}
  return ctx;
}

function imageUrl(p) {
  if (!p || typeof p !== "string") return "";
  if (p.startsWith("data:") || p.startsWith("http")) return p;
  const name = p.split("/").pop();
  return `/api/assistant/image/${encodeURIComponent(name)}`;
}

export async function render(root) {
  root.replaceChildren(loading("Loading survival AI…"));
  let status = null;
  try {
    status = await api.get("/api/assistant/status").catch(e => ({ error: e.message }));
  } catch (e) {
    root.replaceChildren(h("div", { class: "callout danger", text: e.message }));
    return;
  }

  function modelNames() {
    const list = (status?.models || []).map(m => (typeof m === "string" ? m : m?.name)).filter(Boolean);
    return [...new Set(list)];
  }
  const initialModel = (store.settings?.ollama_model || status?.model || "").trim();
  if (!chatSession.selectedModel && initialModel) chatSession.selectedModel = initialModel;
  if (chatSession.think === null) chatSession.think = false;
  const state = chatSession;
  root.replaceChildren();

  const focusBtn = btn("FOCUS", { tone: "ghost", onClick: () => {
    const on = layout.classList.toggle("assistant-focus");
    const label = focusBtn.querySelector("span");
    if (label) label.textContent = on ? "SHOW PANEL" : "FOCUS";
    else focusBtn.textContent = on ? "SHOW PANEL" : "FOCUS";
  } });
  focusBtn.id = "assistantFocusBtn";

  root.appendChild(pageHead({
    kicker: "COMMAND // DELAMAIN",
    title: "Delamain",
    desc: "Local, operator-controlled AI assistant backed by Ollama. Supports chat history, image attachments for vision models, voice input/output (browser-dependent), and indexed document retrieval.",
    actions: [
      focusBtn,
      btn("SETTINGS", { icon: "settings", tone: "ghost", onClick: () => { location.hash = "#/settings"; } }),
      btn("DETECT MODELS", { icon: "refresh", onClick: refreshModels }),
    ],
  }));

  if (!status?.reachable || !status?.model) {
    root.appendChild(alertStrip(
      status?.error || "Ollama is not reachable. Start Ollama locally and configure a model in Settings.",
      "amber",
    ));
  }

  const layout = h("div", { class: "assistant-layout mt-14" });
  const main = h("div", { class: "col gap-12 assistant-main" });
  const side = h("div", { class: "col gap-12 assistant-side" });
  layout.append(main, side);
  root.appendChild(layout);

  // Conversation picker
  const newConvBtn = btn("NEW CHAT", { size: "sm", icon: "plus", onClick: () => createConversation() });
  const renameConvBtn = btn("RENAME", { size: "sm", tone: "ghost", icon: "edit", onClick: () => renameConversation() });
  const saveChatBtn = btn("SAVE CHAT TO VAULT", { size: "sm", tone: "ghost", icon: "lock", onClick: saveChatToVault });
  const delConvBtn = btn("DELETE", { size: "sm", tone: "danger", icon: "trash", onClick: () => deleteConversation() });
  const convState = h("div", { class: "col gap-6" });
  const convContainer = h("div", { class: "assistant-convbar" });
  convContainer.append(convState, newConvBtn, renameConvBtn, saveChatBtn, delConvBtn);
  async function saveChatToVault() {
    const msgs = (state.messages || []).filter((m) => m && (m.role === "user" || m.role === "assistant") && String(m.content || "").trim());
    if (!msgs.length) { toast("No chat messages to save", "warn"); return; }
    const option = convContainer?.querySelector("select")?.selectedOptions?.[0]?.textContent || "";
    const label = option.replace(/\s*\(\d+\)\s*$/, "").trim();
    const fallback = `Delamain chat ${new Date().toISOString().slice(0, 16).replace("T", " ")}`;
    const title = (label && label !== "Select conversation…" ? label : fallback).slice(0, 300);
    const body = msgs.map((m) => {
      const who = m.role === "user" ? "OPERATOR" : "DELAMAIN";
      return `${who}${m.meta ? ` [${m.meta}]` : ""}\n${String(m.content || "").trim()}`;
    }).join("\n\n");
    try {
      const res = await api.post("/api/assistant/vault-note", { title, content: body, tags: "delamain,chat-log" });
      toast(`Chat saved to encrypted vault (#${res.id})`, "ok");
    } catch (e) {
      toast(e.message || "Unlock the vault to save chat history", "error");
    }
  }

  const transcript = h("div", { class: "col gap-10 assistant-transcript" });
  const intro = h("div", { class: "callout" },
    h("div", { class: "small", style: { whiteSpace: "pre-wrap" }, text: "Ask for field planning help, SITREP drafting, checklists, triage prompts, comms/logistics planning, map/intel interpretation, or gear/water/food/power reasoning. Attach images for vision models, use the mic for voice input, and review past chats below. I can include a cached app-context snapshot if enabled. I won’t invent live readings." })
  );
  transcript.appendChild(intro);
  for (const m of (state.messages || [])) addMsg(m.role, m.content, m.meta || "", m.images || [], { record: false });
  if (state.sending) {
    const note = h("div", { class: "callout small amber" }, h("div", { class: "small", style: { whiteSpace: "pre-wrap" }, text: "Delamain is responding… switching tabs won't cancel it; the reply will appear here when it finishes." }));
    transcript.appendChild(note);
    transcript.scrollTop = transcript.scrollHeight;
  }

  // Live-refresh transcript when an in-flight request finishes elsewhere.
  const refreshTranscript = () => {
    transcript.replaceChildren(intro);
    for (const m of (state.messages || [])) addMsg(m.role, m.content, m.meta || "", m.images || [], { record: false });
    if (state.sending) {
      const note = h("div", { class: "callout small amber" }, h("div", { class: "small", style: { whiteSpace: "pre-wrap" }, text: "Delamain is responding… switching tabs won't cancel it; the reply will appear here when it finishes." }));
      transcript.appendChild(note);
    }
    transcript.scrollTop = transcript.scrollHeight;
  };
  const onUpdate = () => { try { refreshTranscript(); } catch {} };
  window.addEventListener("darksec:assistant-update", onUpdate);

  const promptInput = textarea({ rows: 4, placeholder: "Ask the local survival AI… (Ctrl/Cmd+Enter to send)" });

  // Image attach
  const fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.accept = "image/*";
  fileInput.multiple = true;
  fileInput.style.display = "none";
  document.body.appendChild(fileInput);
  const previewRow = h("div", { class: "row wrap gap-8" });
  const attachBtn = btn("ATTACH IMAGE", { size: "sm", icon: "image", onClick: () => fileInput.click() });
  fileInput.addEventListener("change", async () => {
    for (const f of (fileInput.files || [])) {
      const dataUrl = await new Promise((resolve, reject) => {
        const r = new FileReader();
        r.onload = () => resolve(String(r.result || ""));
        r.onerror = () => reject(new Error("read failed"));
        r.readAsDataURL(f);
      }).catch(() => null);
      if (dataUrl) state.pendingImages.push(dataUrl);
    }
    fileInput.value = "";
    renderPreviews();
  });
  function renderPreviews() {
    previewRow.replaceChildren();
    state.pendingImages.forEach((src, idx) => {
      const wrap = h("div", { style: { position: "relative" } });
      const img = h("img", { src, style: { width: "64px", height: "64px", objectFit: "cover", borderRadius: "6px", border: "1px solid var(--border)" } });
      const rm = btn("×", { size: "sm", tone: "danger", onClick: () => { state.pendingImages.splice(idx, 1); renderPreviews(); } });
      rm.style.position = "absolute"; rm.style.top = "-6px"; rm.style.right = "-6px";
      wrap.append(img, rm);
      previewRow.appendChild(wrap);
    });
  }

  const ctxToggle = toggle("Include cached app context (status/intel/dashboard/settings)", !!state.includeContext, (v) => { state.includeContext = v; });
  const thinkToggle = toggle("Enable thinking/reasoning (slower on CPU)", !!state.think, (v) => { state.think = v; }, { title: "Per-request override. Slower on CPU-only systems; disable if responses stall." });
  const sendBtn = btn("SEND", { icon: "arrow-right", tone: "primary", onClick: send });
  const cancelBtn = btn("CANCEL", { icon: "close", tone: "danger", onClick: cancelSend });
  cancelBtn.style.display = "none";
  const clearBtn = btn("CLEAR", { icon: "trash", tone: "ghost", onClick: () => { state.messages = []; transcript.replaceChildren(intro); } });

  // Voice
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition = null;
  const micBtn = btn("MIC", { size: "sm", icon: "mic", onClick: () => {
    if (!SpeechRec) { toast("Speech recognition not supported in this browser", "warn"); return; }
    if (state.recognizing) {
      try { recognition?.stop(); } catch {}
      state.recognizing = false;
      micBtn.textContent = "MIC";
      return;
    }
    recognition = new SpeechRec();
    recognition.lang = (store.settings?.language || "en-US");
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    recognition.onstart = () => { state.recognizing = true; micBtn.textContent = "LISTENING…"; };
    recognition.onend = () => { state.recognizing = false; micBtn.textContent = "MIC"; };
    recognition.onerror = (e) => { toast(`Mic error: ${e.error || e.message}`, "error"); state.recognizing = false; micBtn.textContent = "MIC"; };
    recognition.onresult = (e) => {
      const t = Array.from(e.results).map(r => r[0].transcript).join(" ").trim();
      if (t) promptInput.value = (promptInput.value ? promptInput.value.trim() + " " : "") + t;
    };
    try { recognition.start(); } catch (e) { toast(e.message, "error"); }
  } });
  const speakBtn = btn("READ ALOUD", { size: "sm", icon: "volume-2", onClick: () => {
    if (!window.speechSynthesis) { toast("Text-to-speech not supported", "warn"); return; }
    const last = [...state.messages].reverse().find(m => m.role === "assistant");
    if (!last) { toast("No assistant message to read", "warn"); return; }
    try { window.speechSynthesis.cancel(); } catch {}
    const u = new SpeechSynthesisUtterance(String(last.content || ""));
    window.speechSynthesis.speak(u);
  } });

  function setSending(on) {
    state.sending = !!on;
    sendBtn.style.display = on ? "none" : "";
    cancelBtn.style.display = on ? "" : "none";
    sendBtn.disabled = !!on;
  }
  function cancelSend() {
    try { state.abortController?.abort(); } catch {}
  }

  promptInput.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); send(); }
  });

  const statusBox = h("div", { class: "col gap-8" });
  const modelBox = h("div", { class: "col gap-8" });
  function renderModelPicker() {
    const names = modelNames();
    if (state.selectedModel && !names.includes(state.selectedModel)) names.unshift(state.selectedModel);
    const opts = names.length
      ? [{ value: "", label: "Select installed model…" }, ...names.map(n => ({ value: n, label: n }))]
      : [{ value: "", label: "No installed models detected" }];
    const sel = select(opts, state.selectedModel || "");
    sel.addEventListener("change", () => { state.selectedModel = sel.value; warmModel(sel.value); renderStatus(); });
    modelBox.replaceChildren(
      field("INSTALLED MODEL", sel, names.length ? "Detected from local Ollama (`/api/tags`). Select a model to use for chat." : "Start Ollama and pull a model (e.g. `ollama pull llama3.2`), then click Detect Models."),
      h("div", { class: "row wrap gap-8" },
        btn("USE SELECTED", { size: "sm", icon: "cpu", onClick: () => { if (!state.selectedModel) return toast("Select a model first", "warn"); toast(`Using model: ${state.selectedModel}`); renderStatus(); } }),
        btn("SET AS DEFAULT", { size: "sm", tone: "ghost", icon: "save", onClick: async () => {
          if (!state.selectedModel) return toast("Select a model first", "warn");
          try { await store.saveSettings({ ollama_model: state.selectedModel }); status = { ...(status||{}), model: state.selectedModel }; toast("Default model saved"); renderStatus(); } catch (e) { toast(e.message, "error"); }
        }}))
    );
  }
  function renderStatus() {
    const names = modelNames();
    statusBox.replaceChildren(
      h("div", { class: "row between wrap gap-8" }, h("span", { class: "side-label", text: "OLLAMA" }), badge(status?.reachable ? "REACHABLE" : "OFFLINE", status?.reachable ? "ok" : "warn")),
      h("div", { class: "tiny muted", style: { overflowWrap: "anywhere" }, text: status?.base_url || "—" }),
      h("div", { class: "tiny muted", text: `Active chat model: ${state.selectedModel || status?.model || "(none selected)"}` }),
      h("div", { class: "tiny muted", text: `Default (settings): ${store.settings?.ollama_model || status?.model || "(none)"}` }),
      h("div", { class: "tiny muted", text: `Timeout: ${status?.timeout_sec || store.settings?.assistant_timeout_sec || 300}s · keep-alive: ${status?.keep_alive || "30m"}` }),
      status?.warning ? h("div", { class: "tiny amber", text: status.warning }) : null,
      h("div", { class: "tiny muted", text: `Installed models detected: ${names.length}${names.length ? ` — ${names.slice(0, 5).join(", ")}${names.length > 5 ? "…" : ""}` : ""}` })
    );
  }
  async function refreshModels() {
    toast("Detecting Ollama models…", "warn");
    try {
      status = await api.get("/api/assistant/status?fresh=1");
      const names = modelNames();
      if (!state.selectedModel || !names.includes(state.selectedModel)) {
        state.selectedModel = (store.settings?.ollama_model && names.includes(store.settings.ollama_model) ? store.settings.ollama_model : (status?.model && names.includes(status.model) ? status.model : (names[0] || state.selectedModel)));
      }
      renderStatus();
      renderModelPicker();
      if (status?.reachable) toast(`Detected ${names.length} installed model(s)`, names.length ? "ok" : "warn");
      else toast(status?.error || "Ollama not reachable", "error");
    } catch (e) { toast(e.message, "error"); }
  }
  renderStatus();
  renderModelPicker();

  // Preload the configured model in the background. Ollama keeps it in memory
  // for 30 minutes, so the first message does not pay the cold-load cost while
  // the operator is still typing.
  function warmModel(model) {
    if (!status?.reachable || !model) return;
    api.post("/api/assistant/warm", { model }).catch(() => {});
  }
  warmModel(state.selectedModel || status?.model);

  function renderImages(images) {
    const arr = Array.isArray(images) ? images : [];
    if (!arr.length) return null;
    return h("div", { class: "row wrap gap-6 mt-6" },
      ...arr.map(p => h("img", { src: imageUrl(p), style: { maxWidth: "180px", maxHeight: "180px", borderRadius: "6px", border: "1px solid var(--border)" } }))
    );
  }

  function addMsg(role, content, meta = "", images = [], opts = {}) {
    const record = opts.record !== false;
    const ts = meta || nowTs();
    if (record && (role === "user" || role === "assistant")) {
      state.messages.push({ role, content: String(content || ""), meta: ts, images: Array.isArray(images) ? images : [] });
      if (state.messages.length > 200) state.messages = state.messages.slice(-160);
    }
    const isUser = role === "user";
    const box = h("div", { class: "callout " + (isUser ? "cyan" : ""), style: { borderLeft: isUser ? "3px solid rgba(72,215,255,.45)" : "3px solid rgba(93,255,154,.35)" } });
    box.appendChild(h("div", { class: "row between gap-8 mb-6" },
      h("span", { class: "side-label", text: isUser ? "OPERATOR" : "DELAMAIN" }),
      h("span", { class: "tiny muted", text: ts })
    ));
    if (isUser) {
      box.appendChild(h("div", { class: "small", style: { whiteSpace: "pre-wrap" }, text: content }));
    } else {
      const body = h("div", { class: "small", style: { whiteSpace: "pre-wrap" } });
      try {
        const html = renderMarkdown(String(content || ""));
        if (typeof html === "string") body.innerHTML = html;
        else if (html instanceof Node) body.appendChild(html);
        else body.textContent = String(content || "");
      } catch (e) {
        body.textContent = String(content || "(failed to render response)");
      }
      box.appendChild(body);
      const saveBtn = btn("SAVE TO VAULT", { size: "sm", tone: "ghost", icon: "lock", onClick: async () => {
        try {
          const title = String(content || "").split("\n")[0].slice(0, 120) || "Delamain note";
          const res = await api.post("/api/assistant/vault-note", { title, content: String(content || ""), tags: "delamain,chat-log" });
          toast(`Saved to encrypted vault (#${res.id})`, "ok");
        } catch (e) { toast(e.message || "Unlock the vault to save chat logs", "error"); }
      } });
      box.appendChild(h("div", { class: "row wrap gap-6 mt-6" }, saveBtn));
    }
    const imgs = renderImages(images);
    if (imgs) box.appendChild(imgs);
    try { transcript.appendChild(box); transcript.scrollTop = transcript.scrollHeight; } catch {}
  }

  function createAssistantBox(metaText = "") {
    const box = h("div", { class: "callout", style: { borderLeft: "3px solid rgba(93,255,154,.35)" } });
    const meta = h("span", { class: "tiny muted", text: metaText || nowTs() });
    box.appendChild(h("div", { class: "row between gap-8 mb-6" },
      h("span", { class: "side-label", text: "DELAMAIN" }),
      meta
    ));
    const body = h("div", { class: "small", style: { whiteSpace: "pre-wrap" } });
    box.appendChild(body);
    return { box, body, meta };
  }

  function renderToolEvents(events) {
    const list = Array.isArray(events) ? events.filter((e) => e && e.name) : [];
    if (!list.length) return null;
    const lines = list.map((e) => {
      const r = e.result || {};
      const ok = r.ok !== false && !r.error;
      const detail = r.error ? `error: ${r.error}` : (r.id ? `#${r.id}` : (Array.isArray(r.results) ? `${r.results.length} result(s)` : "done"));
      return `${ok ? "✔" : "✖"} ${e.name}${detail ? " — " + detail : ""}`;
    });
    const details = h("details", { class: "small", open: true, style: { marginTop: "8px" } },
      h("summary", { style: { cursor: "pointer", color: "var(--muted)" }, text: `Actions taken (${list.length})` }),
      h("pre", { style: { whiteSpace: "pre-wrap", marginTop: "6px", color: "var(--muted)", fontSize: "11px" }, text: lines.join("\n") })
    );
    const undo = list.map((e) => e.result?.undo).find((u) => u && u.token);
    if (undo) {
      const undoBtn = btn("UNDO", { size: "sm", tone: "danger", onClick: async () => {
        undoBtn.disabled = true;
        try {
          await api.post("/api/assistant/undo", { token: undo.token });
          undoBtn.textContent = "UNDONE";
          toast(`Undone: ${undo.label || "last action"}`, "ok");
        } catch (err) {
          undoBtn.disabled = false;
          toast(err.message || "Undo failed or expired", "error");
        }
      } });
      details.appendChild(h("div", { class: "row gap-6 mt-6 items-center" }, undoBtn,
        h("span", { class: "tiny muted", text: undo.label || "" })));
    }
    return details;
  }

  function needsVaultUnlock(events) {
    return (events || []).some((e) => /vault is (locked|not configured)/i.test(String(e?.result?.error || "")));
  }

  async function recoverVaultSave(events) {
    const failed = (events || []).find((e) => e?.name === "save_note"
      && /vault is (locked|not configured)/i.test(String(e?.result?.error || "")));
    if (!failed) return false;
    const unlocked = await promptVaultUnlock(
      "Delamain tried to save to the encrypted vault, but it is locked. Unlock it here and the note will be saved automatically.");
    if (!unlocked) return false;
    const args = failed.args || {};
    const title = String(args.title || "Delamain note");
    const body = String(args.body || "");
    if (!body) {
      toast("Vault unlocked. Ask Delamain to retry the save.", "warn");
      return true;
    }
    try {
      await api.post("/api/assistant/vault-note", {
        title, content: body, tags: args.tags || "delamain",
      });
      toast(`Saved to encrypted vault: ${title.slice(0, 60)}`, "ok");
    } catch (err) {
      toast(err.message || "Vault unlocked, but the note could not be saved", "warn");
    }
    return true;
  }

  async function loadConversation(id) {
    try {
      state.conversationId = id;
      const res = await api.get(`/api/assistant/conversations/${id}/messages`);
      if (res?.locked || res?.unconfigured) {
        state.conversationId = null;
        state.messages = [];
        transcript.replaceChildren(intro);
        toast(res.error || "Conversation history is unavailable", "warn");
        return;
      }
      const items = res?.items || [];
      state.messages = items.map(m => ({ role: m.role, content: m.content || "", meta: m.created_at || nowTs(), images: Array.isArray(m.images) ? m.images : [] }));
      transcript.replaceChildren(intro);
      for (const m of state.messages) addMsg(m.role, m.content, m.meta, m.images, { record: false });
    } catch (e) { toast(e.message, "error"); }
  }

  async function rebuildConvPicker() {
    try {
      const res = await api.get("/api/assistant/conversations");
      const old = convContainer.querySelector("select");
      if (res?.locked || res?.unconfigured) {
        if (old) old.remove();
        convState.style.display = "";
        convState.replaceChildren(h("div", { class: "callout small amber", text: res.error || "Encrypted chat history is unavailable." }));
        convState.appendChild(res.locked
          ? btn("UNLOCK VAULT", { size: "sm", tone: "primary", icon: "lock", onClick: async () => {
              if (await promptVaultUnlock("Unlock the vault to load encrypted chat history.")) {
                await rebuildConvPicker();
              }
            } })
          : btn("OPEN SECURITY", { size: "sm", icon: "settings", onClick: () => { location.hash = "#/security"; } }));
        return;
      }
      const items = res?.items || [];
      convState.replaceChildren();
      convState.style.display = "none";
      if (res?.encrypted) {
        convState.style.display = "";
        convState.appendChild(h("span", { class: "badge", text: "ENCRYPTED HISTORY" }));
      }
      const opts = [
        { value: "", label: items.length ? "Select conversation…" : "No saved conversations" },
        ...items.map(c => ({ value: String(c.id), label: `${c.title || "Chat"} (${c.msg_count || 0})` })),
      ];
      const newSel = select(opts, state.conversationId ? String(state.conversationId) : "");
      newSel.addEventListener("change", () => {
        const v = newSel.value;
        if (v) loadConversation(parseInt(v, 10));
        else { state.conversationId = null; state.messages = []; transcript.replaceChildren(intro); }
      });
      if (old) old.replaceWith(newSel);
      else convContainer.insertBefore(newSel, convContainer.firstChild);
    } catch (e) { toast(e.message, "error"); }
  }
  await rebuildConvPicker();

  main.appendChild(panel({
    title: "Assistant Chat", icon: "cpu", flush: true, cls: "assistant-chat",
    body: h("div", { class: "assistant-chat-body" },
      transcript,
      h("div", { class: "assistant-composer" },
        field("PROMPT", promptInput),
        previewRow,
        h("div", { class: "row wrap gap-12 mt-6" }, ctxToggle, thinkToggle),
        h("div", { class: "row wrap gap-8 mt-6", style: { justifyContent: "flex-end" } },
          attachBtn, micBtn, speakBtn, clearBtn, cancelBtn, sendBtn)
      )
    )
  }));

  const quick = [
    ["Live status", "Use get_live_status to give me a current operational brief: feed freshness, weather, air quality, space weather, severe alerts, readiness and top actions. State how old cached data is."],
    ["Draft SITREP", "Draft a concise SITREP from the included app context. Mark unknowns, stale data, and assumptions clearly."],
    ["Intel summary", "Summarize the included intel/dashboard context into immediate risks, watch items, and next checks. Separate confirmed cached data from inference."],
    ["72-hour priorities", "Give a practical 72-hour survival priority checklist (life safety, shelter, water, medical, comms, security, logistics). Ask 2-3 clarifying questions only if essential."],
    ["MARCH / first aid", "Give a conservative MARCH/first-aid priority checklist for a field responder. Do not diagnose or prescribe; recommend qualified medical care when available."],
    ["Water plan", "Help plan water procurement, treatment, storage, and rationing priorities for field conditions. State assumptions and contamination risks."],
    ["Comms discipline", "Create a short comms check and message-discipline checklist for a small team (brevity, authentication/confirmation habits, fallback plan). Avoid sensitive specifics."],
    ["Go-bag check", "Build a practical go-bag / sustainment checklist prioritized by mission length and likely hazards. Mark critical vs optional items."],
    ["CBRN caution", "Using included context if present, give conservative CBRN/hazard precautions and immediate next checks. Do not invent readings or declare areas safe."],
    ["Route planning", "Help me think through route planning risks and checks (weather, hazards, timing, comms, contingencies). Ask for origin/destination/constraints if needed."],
  ];
  side.appendChild(panel({
    title: "Conversations", icon: "cpu", cls: "assistant-side-panel",
    body: convContainer,
  }));
  side.appendChild(panel({
    title: "Quick Prompts", icon: "cpu", cls: "assistant-side-panel",
    body: h("div", { class: "quick-grid" }, ...quick.map(([label, text]) => btn(label, { tone: "ghost", size: "sm", class: "quick-btn", onClick: () => { promptInput.value = text; promptInput.focus(); } })))
  }));
  side.appendChild(panel({ title: "Model", icon: "cpu", cls: "assistant-side-panel", body: modelBox }));
  side.appendChild(panel({ title: "Connection", icon: "info", cls: "assistant-side-panel", body: statusBox }));
  side.appendChild(panel({
    title: "Local Setup", icon: "settings", cls: "assistant-side-panel",
    body: h("div", { class: "col gap-6 small" },
      h("div", { class: "muted", text: "1. Install/start Ollama locally." }),
      h("div", { class: "muted", text: "2. Pull a model (example: `ollama pull llama3.2` or another model you trust)." }),
      h("div", { class: "muted", text: "3. Use ATTACH IMAGE for vision models, MIC for voice input, READ ALOUD for speech output." }),
      h("div", { class: "callout amber tiny", text: "Safety: AI guidance can be wrong. Verify critical medical, navigation, legal, and hazard decisions against training, authoritative references, and real conditions." }),
    )
  }));

  async function createConversation() {
    if (state.creatingConversation) return;
    state.creatingConversation = true;
    newConvBtn.disabled = true;
    try {
      const stamp = new Date().toISOString().slice(0, 16).replace("T", " ");
      const c = await api.post("/api/assistant/conversations", { title: `Chat ${stamp}` });
      if (!c?.id) { toast(c?.error || "Could not create conversation", "warn"); return; }
      state.conversationId = c.id;
      state.messages = [];
      transcript.replaceChildren(intro);
      await rebuildConvPicker();
      const sel = convContainer.querySelector("select");
      if (sel) sel.value = String(c.id);
      toast("New conversation created", "ok");
    } catch (e) { toast(e.message, "error"); }
    finally {
      state.creatingConversation = false;
      newConvBtn.disabled = false;
    }
  }

  async function renameConversation() {
    if (!state.conversationId) { toast("Select a conversation first", "warn"); return; }
    const sel = convContainer.querySelector("select");
    const current = sel?.selectedOptions?.[0]?.textContent?.replace(/\s*\(\d+\)\s*$/, "").trim() || "";
    const title = prompt("Conversation title", current);
    if (title === null) return;
    const clean = title.trim().slice(0, 120);
    if (!clean) { toast("Title cannot be empty", "warn"); return; }
    try {
      const res = await api.patch(`/api/assistant/conversations/${state.conversationId}`, { title: clean });
      if (res?.ok === false) { toast(res.error || "Rename failed", "warn"); return; }
      await rebuildConvPicker();
      const next = convContainer.querySelector("select");
      if (next) next.value = String(state.conversationId);
      toast("Conversation renamed", "ok");
    } catch (e) { toast(e.message, "error"); }
  }

  async function deleteConversation() {
    if (!state.conversationId) { toast("No conversation selected", "warn"); return; }
    if (!confirm("Delete this conversation and its messages?")) return;
    try {
      await api.del(`/api/assistant/conversations/${state.conversationId}`);
      state.conversationId = null;
      state.messages = [];
      transcript.replaceChildren(intro);
      await rebuildConvPicker();
      toast("Conversation deleted", "ok");
    } catch (e) { toast(e.message, "error"); }
  }

  async function send() {
    const text = (promptInput.value || "").trim();
    if (!text && state.pendingImages.length === 0) return;
    if (state.sending) return;
    const model = state.selectedModel || store.settings?.ollama_model || status?.model || "";
    if (!model) { toast("Select an installed model first (Detect Models)", "warn"); return; }
    // Ensure a conversation exists. If encrypted history is locked or the
    // vault is unconfigured, continue without persistence instead of blocking
    // the operator's question.
    if (!state.conversationId) {
      try {
        const c = await api.post("/api/assistant/conversations", { title: (text || "Image chat").slice(0, 60) });
        if (c?.id) {
          state.conversationId = c.id;
          await rebuildConvPicker();
          const sel = convContainer.querySelector("select");
          if (sel) sel.value = String(c.id);
        } else {
          toast(`${c?.error || "Chat history unavailable"}; this chat will not be saved.`, "warn");
        }
      } catch (e) { toast(`Chat will not be saved: ${e.message}`, "warn"); }
    }
    let pending = null;
    let pendingTicker = null;
    let stage = "Building context and connecting…";
    const startedAt = Date.now();
    const controller = new AbortController();
    state.abortController = controller;
    // Match the backend: assistant_timeout_sec is the per-request Ollama read
    // timeout. Abort only when the stream has been silent for that long, so a
    // configured 900s timeout is not cut short by a hardcoded client value.
    const timeoutSec = Math.max(30, Number(status?.timeout_sec || store.settings?.assistant_timeout_sec || 300));
    const stallMs = timeoutSec * 1000;
    let stallLimit = stallMs;
    let lastActivity = Date.now();
    const touch = () => { lastActivity = Date.now(); };
    const watchdog = setInterval(() => {
      if (Date.now() - lastActivity > stallLimit) {
        try { controller.abort(new DOMException(`No activity from Ollama for ${timeoutSec}s`, "TimeoutError")); }
        catch { try { controller.abort(); } catch {} }
      }
    }, 2000);
    setSending(true);
    try {
      const priorHistory = (state.messages || []).slice(-12).map(m => ({ role: m.role, content: m.content }));
      addMsg("user", text || "(image)", nowTs(), state.pendingImages.slice(), { record: true });
      const sentImages = state.pendingImages.slice();
      promptInput.value = "";
      state.pendingImages = [];
      renderPreviews();
      const waitNote = state.think ? " Thinking/reasoning is enabled and can take much longer on CPU." : "";
      const paintWait = () => {
        if (!pending) return;
        const secs = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
        pending.textContent = `Waiting for ${model}…${waitNote} ${stage} (${secs}s)`;
      };
      pending = h("div", { class: "callout small amber" });
      paintWait();
      transcript.appendChild(pending); transcript.scrollTop = transcript.scrollHeight;
      pendingTicker = setInterval(paintWait, 1000);
      const payload = { prompt: text || "Describe the attached image.", history: priorHistory, model, think: !!state.think, conversation_id: state.conversationId, images: sentImages };
      if (state.includeContext) {
        stage = "Collecting cached app context…"; paintWait();
        payload.context = await buildContext();
      }
      if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
      touch();
      stage = "Model is reading the prompt (first words can take 10–40s on CPU)…"; paintWait();

      let usedStream = false;
      try {
        const headers = { "Content-Type": "application/json", "Accept": "application/x-ndjson" };
        const token = csrfToken(); if (token) headers["X-DarkSec-CSRF"] = token;
        let resp;
        resp = await fetch("/api/assistant/chat/stream", {
          method: "POST", credentials: "same-origin", headers, signal: controller.signal,
          body: JSON.stringify(payload),
        });
        touch();
        if (resp.status === 401) { window.dispatchEvent(new CustomEvent("darksec:auth")); throw new Error("Session expired. Relaunch from the DarkSec application."); }
        if (resp.ok && resp.body) {
          usedStream = true;
          if (pendingTicker) { clearInterval(pendingTicker); pendingTicker = null; }
          try { pending?.remove(); } catch {}
          pending = null;
          const ui = createAssistantBox("streaming…");
          transcript.appendChild(ui.box); transcript.scrollTop = transcript.scrollHeight;
          const reader = resp.body.getReader();
          const decoder = new TextDecoder();
          let buf = "", content = "", thinking = "", hadError = null, doneData = null, toolEvents = [];
          while (true) {
            const { value, done } = await reader.read();
            touch();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const lines = buf.split("\n"); buf = lines.pop() || "";
            for (const line of lines) {
              if (!line.trim()) continue;
              let evt; try { evt = JSON.parse(line); } catch { continue; }
              if (evt.type === "delta") {
                if (evt.channel === "thinking") {
                  thinking += evt.delta || "";
                  const secs = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
                  ui.meta.textContent = `reasoning · ${thinking.length} chars · ${secs}s`;
                  transcript.scrollTop = transcript.scrollHeight;
                } else {
                  content += evt.delta || ""; ui.body.textContent = content;
                  const secs = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
                  ui.meta.textContent = `streaming · ${content.length} chars · ${secs}s`;
                  transcript.scrollTop = transcript.scrollHeight;
                }
              } else if (evt.type === "error") {
                hadError = evt.error || "Streaming error";
              } else if (evt.type === "vault_unlock_required") {
                ui.meta.textContent = "vault locked — unlock to save";
                const unlocked = await promptVaultUnlock(evt.detail || "Delamain needs to save an encrypted note. Unlock the vault to continue.");
                if (!unlocked) {
                  hadError = "Vault unlock cancelled";
                  try { controller.abort(); } catch {}
                }
              } else if (evt.type === "vault_unlock_result") {
                if (evt.unlocked) ui.meta.textContent = "vault unlocked · saving…";
              } else if (evt.type === "tool_confirm_required") {
                ui.meta.textContent = "awaiting operator approval…";
                const approved = await confirmDialog({
                  title: "Confirm Delamain action",
                  message: `${evt.summary || "Delamain wants to change operator records."}${evt.detail ? `\n\n${evt.detail}` : ""}`,
                  confirmText: "APPROVE",
                });
                try {
                  await api.post("/api/assistant/confirm", { id: evt.id, approved: !!approved });
                } catch (err) {
                  toast(err.message || "Could not send confirmation", "error");
                }
              } else if (evt.type === "tool_start") {
                const secs = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
                ui.meta.textContent = `running ${evt.name || "tool"} · ${secs}s`;
              } else if (evt.type === "tool") {
                toolEvents.push({ name: evt.name, result: evt.result });
                const secs = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
                ui.meta.textContent = `running ${evt.name || "tool"} · ${secs}s`;
              } else if (evt.type === "done") {
                doneData = evt; content = evt.content || content; thinking = evt.thinking || thinking;
                if (Array.isArray(evt.tools) && evt.tools.length) toolEvents = evt.tools;
              }
            }
          }
          if (hadError && !String(content).trim()) throw new Error(hadError);
          const finalContent = String(content || "").trim() || "(model returned empty content)";
          const truncNote = (doneData?.truncated || doneData?.done_reason === "length") ? " · truncated (increase Context or Max reply tokens)" : "";
          try {
            const html = renderMarkdown(finalContent);
            if (typeof html === "string") ui.body.innerHTML = html;
            else ui.body.textContent = finalContent;
          } catch { ui.body.textContent = finalContent; }
          ui.meta.textContent = `${nowTs()}${truncNote}`;
          state.messages.push({ role: "assistant", content: finalContent, meta: ui.meta.textContent, images: [] });
          if (state.messages.length > 200) state.messages = state.messages.slice(-160);
          if (thinking && String(thinking).trim()) {
            const details = h("details", { class: "small", style: { marginTop: "8px" } },
              h("summary", { style: { cursor: "pointer", color: "var(--muted)" }, text: "Model thinking/reasoning" }),
              h("pre", { style: { whiteSpace: "pre-wrap", marginTop: "6px", color: "var(--muted)", fontSize: "11px", maxHeight: "240px", overflow: "auto" }, text: String(thinking).slice(0, 12000) })
            );
            ui.box.appendChild(details);
          }
          const toolNode = renderToolEvents(toolEvents);
          if (toolNode) ui.box.appendChild(toolNode);
          if (needsVaultUnlock(toolEvents)) {
            recoverVaultSave(toolEvents);
          }
          if (hadError) toast(`Stream warning: ${hadError}`, "warn");
          transcript.scrollTop = transcript.scrollHeight;
        } else if (resp.status !== 404 && resp.status !== 405) {
          let detail = "";
          try { const j = await resp.json(); detail = j?.detail || j?.error || ""; } catch {}
          if (detail) throw new Error(detail);
        }
      } catch (streamErr) {
        if (streamErr?.name === "AbortError" || streamErr?.name === "TimeoutError") throw streamErr;
        if (usedStream) throw streamErr;
        console.warn("assistant stream fallback", streamErr);
      }

      if (!usedStream) {
        // Non-stream requests produce no bytes until the end, and tool rounds
        // can legitimately take several backend timeout windows. Give the
        // fallback a wider budget while the user can still cancel manually.
        stallLimit = stallMs * 4 + 30000;
        touch();
        const res = await api.post("/api/assistant/chat", payload, { signal: controller.signal });
        const content = (res?.content || "").trim() || "(model returned empty content)";
        const truncNote = (res?.truncated || res?.done_reason === "length") ? " · truncated (increase Context or Max reply tokens)" : "";
        try { pending?.remove(); } catch {}
        pending = null;
        addMsg("assistant", content, `${nowTs()}${truncNote}`, [], { record: true });
        if (res?.thinking && String(res.thinking).trim()) {
          const details = h("details", { class: "small", style: { marginTop: "8px" } },
            h("summary", { style: { cursor: "pointer", color: "var(--muted)" }, text: "Model thinking/reasoning" }),
            h("pre", { style: { whiteSpace: "pre-wrap", marginTop: "6px", color: "var(--muted)", fontSize: "11px", maxHeight: "240px", overflow: "auto" }, text: String(res.thinking).slice(0, 12000) })
          );
          try { transcript.lastChild?.appendChild(details); transcript.scrollTop = transcript.scrollHeight; } catch {}
        }
        const toolNode = renderToolEvents(res?.tools);
        if (toolNode) { try { transcript.lastChild?.appendChild(toolNode); } catch {} }
        if (needsVaultUnlock(res?.tools)) {
          recoverVaultSave(res?.tools);
        }
      }
    } catch (e) {
      try { pending?.remove(); } catch {}
      if (e?.name === "TimeoutError") {
        const msg = `No activity from Ollama for ${timeoutSec}s. The model may still be loading or the prompt may be too large. Raise Timeout in Settings if CPU inference is slow.`;
        try { addMsg("assistant", `**Timeout:** ${msg}`, "", [], { record: false }); } catch {}
        toast(msg, "error");
      } else if (e?.name === "AbortError" || /aborted|AbortError/i.test(String(e?.message || e || ""))) {
        addMsg("assistant", "_Request cancelled._", nowTs(), [], { record: false });
        toast("Cancelled", "warn");
      } else {
        const msg = e?.message || String(e);
        console.error("assistant chat failed", e);
        try {
          addMsg("assistant", `**Error:** ${msg}`, "", [], { record: false });
        } catch {}
        toast(msg, "error");
      }
    } finally {
      clearInterval(watchdog);
      if (pendingTicker) { clearInterval(pendingTicker); pendingTicker = null; }
      if (state.abortController === controller) state.abortController = null;
      setSending(false);
      try { window.dispatchEvent(new CustomEvent("darksec:assistant-update")); } catch {}
    }
  }

  // Restore sending UI state if a request is still in flight.
  if (state.sending) { try { setSending(true); } catch {} }

  return () => { try { window.removeEventListener("darksec:assistant-update", onUpdate); } catch {} };
}
