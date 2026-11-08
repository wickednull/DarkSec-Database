import { api } from "../api.js";
import { store } from "../store.js";
import { h, pageHead, panel, btn, input, textarea, field, select, badge, alertStrip, loading, toast, toggle, renderMarkdown, fmt } from "../components.js";

function csrfToken() {
  const m = document.cookie.match(/(?:^|;\s*)darksec_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

const chatSession = { messages: [], selectedModel: "", think: null, includeContext: false, sending: false, abortController: null };

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
      timedGet("/api/status", 6000),
      timedGet("/api/settings", 6000),
      timedGet("/api/intel/overview", 8000),
      timedGet("/api/dashboard", 8000),
      timedGet("/api/cbrn/status", 6000),
      timedGet("/api/sync/status", 5000),
      timedGet("/api/feeds", 5000),
      timedGet("/api/checklists", 6000),
      timedGet("/api/records/incidents", 6000),
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
      latest_events: (intel.events || []).slice(0, 6).map(e => ({ title: e.title, severity: e.severity, time: e.time, source: e.source, place: e.place })),
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

export async function render(root) {
  root.replaceChildren(loading("Loading survival AI…"));
  let status = null, modelsResp = null;
  try {
    [status, modelsResp] = await Promise.all([
      api.get("/api/assistant/status").catch(e => ({ error: e.message })),
      api.get("/api/assistant/models").catch(e => ({ error: e.message })),
    ]);
  } catch (e) {
    root.replaceChildren(h("div", { class: "callout danger", text: e.message }));
    return;
  }

  function modelNames() {
    const list = (status?.models || modelsResp?.models || []).map(m => (typeof m === "string" ? m : m?.name)).filter(Boolean);
    return [...new Set(list)];
  }
  const initialModel = (store.settings?.ollama_model || status?.model || modelsResp?.configured_model || "").trim();
  if (!chatSession.selectedModel && initialModel) chatSession.selectedModel = initialModel;
  // Thinking off by default for responsiveness (especially CPU). User can toggle per chat.
  if (chatSession.think === null) chatSession.think = false;
  const state = chatSession; // persist across tab navigation within app session
  root.replaceChildren();

  root.appendChild(pageHead({
    kicker: "COMMAND // DELAMAIN",
    title: "Delamain",
    desc: "Local, operator-controlled AI assistant backed by Ollama. Detects installed Ollama models, lets you select one, and can include cached app context (intel/status/dashboard). Do not treat AI output as live sensor truth.",
    actions: [
      btn("SETTINGS", { icon: "settings", tone: "ghost", onClick: () => { location.hash = "#/settings"; } }),
      btn("DETECT MODELS", { icon: "refresh", onClick: refreshModels }),
    ],
  }));

  const alert = status?.reachable
    ? alertStrip(`Connected to Ollama at ${status.base_url}${status.model ? ` · model: ${status.model}` : " · no model configured"}`, "green")
    : alertStrip(status?.error || "Ollama is not reachable. Start Ollama locally and configure a model in Settings.", "amber");
  root.appendChild(alert);

  const layout = h("div", { class: "grid-2 mt-14", style: { alignItems: "start" } });
  const main = h("div", { class: "col gap-12" });
  const side = h("div", { class: "col gap-12" });
  layout.append(main, side);
  root.appendChild(layout);

  const transcript = h("div", { class: "col gap-10", style: { minHeight: "320px", maxHeight: "56vh", overflow: "auto", padding: "4px 2px" } });
  const intro = h("div", { class: "callout" },
    h("div", { class: "small", style: { whiteSpace: "pre-wrap" }, text: "Ask for field planning help, SITREP drafting, checklists, triage prompts, comms/logistics planning, map/intel interpretation, or gear/water/food/power reasoning. I can include a cached app-context snapshot if enabled. I won’t invent live readings." })
  );
  transcript.appendChild(intro);
  for (const m of (state.messages || [])) addMsg(m.role, m.content, m.meta || "", { record: false });

  const promptInput = textarea({ rows: 4, placeholder: "Ask the local survival AI… (Ctrl/Cmd+Enter to send)" });
  const ctxToggle = toggle("Include cached app context (status/intel/dashboard/settings)", !!state.includeContext, (v) => { state.includeContext = v; });
  const thinkToggle = toggle("Enable thinking/reasoning (slower on CPU)", !!state.think, (v) => { state.think = v; }, { title: "Per-request override. Slower on CPU-only systems; disable if responses stall." });
  const sendBtn = btn("SEND", { icon: "arrow-right", tone: "primary", onClick: send });
  const cancelBtn = btn("CANCEL", { icon: "close", tone: "danger", onClick: cancelSend });
  cancelBtn.style.display = "none";
  const clearBtn = btn("CLEAR", { icon: "trash", tone: "ghost", onClick: () => { state.messages = []; transcript.replaceChildren(intro); } });

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
    sel.addEventListener("change", () => { state.selectedModel = sel.value; renderStatus(); });
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
      status?.warning ? h("div", { class: "tiny amber", text: status.warning }) : null,
      h("div", { class: "tiny muted", text: `Installed models detected: ${names.length}${names.length ? ` — ${names.slice(0, 5).join(", ")}${names.length > 5 ? "…" : ""}` : ""}` })
    );
  }
  async function refreshModels() {
    toast("Detecting Ollama models…", "warn");
    try {
      [status, modelsResp] = await Promise.all([
        api.get("/api/assistant/status"),
        api.get("/api/assistant/models"),
      ]);
      const names = modelNames();
      if (!state.selectedModel || !names.includes(state.selectedModel)) {
        state.selectedModel = (store.settings?.ollama_model && names.includes(store.settings.ollama_model) ? store.settings.ollama_model : (status?.model && names.includes(status.model) ? status.model : (names[0] || state.selectedModel)));
      }
      renderStatus();
      renderModelPicker();
      const alert = root.querySelector(".alert-strip, .callout.alert, .alert");
      if (status?.reachable) toast(`Detected ${names.length} installed model(s)`, names.length ? "ok" : "warn");
      else toast(status?.error || "Ollama not reachable", "error");
    } catch (e) { toast(e.message, "error"); }
  }
  renderStatus();
  renderModelPicker();

  function addMsg(role, content, meta = "", opts = {}) {
    const record = opts.record !== false;
    const ts = meta || nowTs();
    if (record && (role === "user" || role === "assistant")) {
      state.messages.push({ role, content: String(content || ""), meta: ts });
      if (state.messages.length > 100) state.messages = state.messages.slice(-80);
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
    }
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

  async function send() {
    const text = (promptInput.value || "").trim();
    if (!text || state.sending) return;
    const model = state.selectedModel || store.settings?.ollama_model || status?.model || "";
    if (!model) { toast("Select an installed model first (Detect Models)", "warn"); return; }
    let pending = null;
    let pendingTicker = null;
    let stage = "Building context and connecting…";
    const startedAt = Date.now();
    const controller = new AbortController();
    state.abortController = controller;
    setSending(true);
    try {
      const priorHistory = (state.messages || []).slice(-12).map(m => ({ role: m.role, content: m.content }));
      addMsg("user", text);
      promptInput.value = "";
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
      const payload = { prompt: text, history: priorHistory, model, think: !!state.think };
      if (state.includeContext) {
        stage = "Collecting cached app context…"; paintWait();
        payload.context = await buildContext();
      }
      if (controller.signal.aborted) throw new DOMException("Aborted", "AbortError");
      stage = "Connecting to local AI (first words can take 10–30s on CPU)…"; paintWait();

      // Try streaming endpoint first (best UX on CPU); fall back to non-streaming.
      let usedStream = false;
      try {
        const headers = { "Content-Type": "application/json", "Accept": "application/x-ndjson" };
        const token = csrfToken(); if (token) headers["X-DarkSec-CSRF"] = token;
        const headerTimeout = setTimeout(() => { try { controller.abort(); } catch {} }, 45000);
        let resp;
        try {
          resp = await fetch("/api/assistant/chat/stream", {
            method: "POST", credentials: "same-origin", headers, signal: controller.signal,
            body: JSON.stringify(payload),
          });
        } finally { clearTimeout(headerTimeout); }
        if (resp.status === 401) { window.dispatchEvent(new CustomEvent("darksec:auth")); throw new Error("Session expired. Relaunch from the DarkSec application."); }
        if (resp.ok && resp.body) {
          usedStream = true;
          if (pendingTicker) { clearInterval(pendingTicker); pendingTicker = null; }
          try { pending?.remove(); } catch {}
          pending = null;
          const ui = createAssistantBox(`${model} · streaming…`);
          transcript.appendChild(ui.box); transcript.scrollTop = transcript.scrollHeight;
          const reader = resp.body.getReader();
          const decoder = new TextDecoder();
          let buf = "", content = "", thinking = "", finalModel = model, hadError = null, doneData = null;
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const lines = buf.split("\n"); buf = lines.pop() || "";
            for (const line of lines) {
              if (!line.trim()) continue;
              let evt; try { evt = JSON.parse(line); } catch { continue; }
              if (evt.model) finalModel = evt.model;
              if (evt.type === "delta") {
                if (evt.channel === "thinking") thinking += evt.delta || "";
                else { content += evt.delta || ""; ui.body.textContent = content; }
                const secs = Math.max(0, Math.round((Date.now() - startedAt) / 1000));
                ui.meta.textContent = `${finalModel} · streaming · ${content.length} chars · ${secs}s`;
                transcript.scrollTop = transcript.scrollHeight;
              } else if (evt.type === "error") {
                hadError = evt.error || "Streaming error";
              } else if (evt.type === "done") {
                doneData = evt; content = evt.content || content; thinking = evt.thinking || thinking; finalModel = evt.model || finalModel;
              }
            }
          }
          if (hadError && !String(content).trim()) throw new Error(hadError);
          const finalContent = String(content || "").trim() || "(model returned empty content)";
          const truncNote = (doneData?.truncated || doneData?.done_reason === "length") ? " · truncated (increase Max reply tokens)" : "";
          // Render markdown at end for nicer formatting
          try {
            const html = renderMarkdown(finalContent);
            if (typeof html === "string") ui.body.innerHTML = html;
            else ui.body.textContent = finalContent;
          } catch { ui.body.textContent = finalContent; }
          ui.meta.textContent = `${finalModel} · ${nowTs()}${truncNote}`;
          state.messages.push({ role: "assistant", content: finalContent, meta: ui.meta.textContent });
          if (state.messages.length > 100) state.messages = state.messages.slice(-80);
          if (thinking && String(thinking).trim()) {
            const details = h("details", { class: "small", style: { marginTop: "8px" } },
              h("summary", { style: { cursor: "pointer", color: "var(--muted)" }, text: "Model thinking/reasoning" }),
              h("pre", { style: { whiteSpace: "pre-wrap", marginTop: "6px", color: "var(--muted)", fontSize: "11px", maxHeight: "240px", overflow: "auto" }, text: String(thinking).slice(0, 12000) })
            );
            ui.box.appendChild(details);
          }
          if (hadError) toast(`Stream warning: ${hadError}`, "warn");
          transcript.scrollTop = transcript.scrollHeight;
        } else if (resp.status !== 404 && resp.status !== 405) {
          // streaming endpoint exists but failed - try to parse error and fall back
          let detail = "";
          try { const j = await resp.json(); detail = j?.detail || j?.error || ""; } catch {}
          if (detail) throw new Error(detail);
        }
      } catch (streamErr) {
        if (streamErr?.name === "AbortError") throw streamErr;
        if (usedStream) throw streamErr; // already committed to stream UI
        console.warn("assistant stream fallback", streamErr);
      }

      if (!usedStream) {
        const res = await api.post("/api/assistant/chat", payload, { signal: controller.signal });
        const content = (res?.content || "").trim() || "(model returned empty content)";
        const truncNote = (res?.truncated || res?.done_reason === "length") ? " · truncated (increase Max reply tokens)" : "";
        try { pending?.remove(); } catch {}
        pending = null;
        addMsg("assistant", content, res?.model ? `${res.model} · ${nowTs()}${truncNote}` : `${nowTs()}${truncNote}`);
        if (res?.thinking && String(res.thinking).trim()) {
          const details = h("details", { class: "small", style: { marginTop: "8px" } },
            h("summary", { style: { cursor: "pointer", color: "var(--muted)" }, text: "Model thinking/reasoning" }),
            h("pre", { style: { whiteSpace: "pre-wrap", marginTop: "6px", color: "var(--muted)", fontSize: "11px", maxHeight: "240px", overflow: "auto" }, text: String(res.thinking).slice(0, 12000) })
          );
          try { transcript.lastChild?.appendChild(details); transcript.scrollTop = transcript.scrollHeight; } catch {}
        }
      }
    } catch (e) {
      try { pending?.remove(); } catch {}
      if (e?.name === "AbortError" || /aborted|AbortError/i.test(String(e?.message || e || ""))) {
        addMsg("assistant", "_Request cancelled._", nowTs(), { record: false });
        toast("Cancelled", "warn");
      } else {
        const msg = e?.message || String(e);
        console.error("assistant chat failed", e);
        try {
          addMsg("assistant", `**Error:** ${msg}\n\nIf this was a timeout, increase **Assistant timeout** in Settings (CPU-only models can be slow) or use \`qwen3.5:2b\`/\`qwen3.5:4b\` with shorter prompts. Also confirm Ollama is running and the selected model is installed.`, "", { record: false });
        } catch {}
        toast(msg, "error");
      }
    } finally {
      if (pendingTicker) { clearInterval(pendingTicker); pendingTicker = null; }
      if (state.abortController === controller) state.abortController = null;
      setSending(false);
    }
  }

  main.appendChild(panel({
    title: "Assistant Chat", icon: "cpu", flush: true,
    body: h("div", { class: "col gap-10", style: { padding: "12px" } },
      transcript,
      field("PROMPT", promptInput),
      h("div", { class: "col gap-6" },
        h("div", { class: "row wrap gap-12" }, ctxToggle, thinkToggle),
        h("div", { class: "row wrap gap-8", style: { justifyContent: "flex-end" } }, clearBtn, cancelBtn, sendBtn))
    )
  }));

  const quick = [
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
    title: "Quick Prompts", icon: "cpu",
    body: h("div", { class: "col gap-6" }, ...quick.map(([label, text]) => btn(label, { tone: "ghost", size: "sm", onClick: () => { promptInput.value = text; promptInput.focus(); } })))
  }));
  side.appendChild(panel({ title: "Model", icon: "cpu", body: modelBox }));
  side.appendChild(panel({ title: "Connection", icon: "info", body: statusBox }));
  side.appendChild(panel({
    title: "Local Setup", icon: "settings",
    body: h("div", { class: "col gap-6 small" },
      h("div", { class: "muted", text: "1. Install/start Ollama locally." }),
      h("div", { class: "muted", text: "2. Pull a model (example: `ollama pull llama3.2` or another model you trust)." }),
      h("div", { class: "muted", text: "3. Set the model/base URL in System Settings if needed." }),
      h("div", { class: "callout amber tiny", text: "Safety: AI guidance can be wrong. Verify critical medical, navigation, legal, and hazard decisions against training, authoritative references, and real conditions." }),
    )
  }));
}
