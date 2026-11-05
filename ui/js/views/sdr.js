/* SDR / spectrum: real HackRF tooling only. No synthetic spectrum is drawn. */
import { api } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, stat, btn, badge, pageHead, alertStrip, emptyState, loading, fmt, toast,
  field, input, select, checkbox, modal, confirmDialog,
} from "../components.js";
import { kv, progressBar, referenceSection, statusTone } from "./shared.js";

const DEMODS = [
  { value: "nfm", label: "NFM — narrowband voice" },
  { value: "am", label: "AM — aviation / broadcast" },
  { value: "wfm", label: "WFM — broadcast FM" },
  { value: "usb", label: "USB — upper sideband" },
  { value: "lsb", label: "LSB — lower sideband" },
  { value: "apt", label: "APT — NOAA weather satellite (experimental)" },
  { value: "iq", label: "IQ only — retain raw samples" },
];

function mhz(hz) {
  return hz == null ? "—" : `${fmt.num(Number(hz) / 1e6, 4)} MHz`;
}

export async function render(root, params = {}) {
  root.replaceChildren(loading("INITIALISING SDR CHAIN"));
  let devices, jobs, recordings, adsb;
  try {
    [devices, jobs, recordings, adsb] = await Promise.all([
      api.get("/api/sdr/devices"),
      api.get("/api/sdr/jobs?limit=15"),
      api.get("/api/sdr/recordings"),
      api.get("/api/sdr/adsb").catch(() => ({ available: false, aircraft: [] })),
    ]);
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }

  const txEnabled = !!store.state.status?.tx_enabled;
  const hackrf = devices.hackrf || {};
  const tools = devices.tools || {};
  const recs = recordings.items || [];
  const recentJobs = jobs.items || [];
  const paramFreq = Number(params.freq || 0);
  const paramMode = String(params.mode || "").toLowerCase();

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // RADIO SPECTRUM",
    title: "SDR / Spectrum",
    desc: "HackRF receive chain, IQ capture, demodulation, recordings and gated transmit. Every value comes from the radio or the local filesystem.",
    actions: [
      btn("CHECK HARDWARE", { icon: "refresh", onClick: () => rescan() }),
      btn("FIELD MANUAL", { icon: "book", onClick: () => { location.hash = "#/manual?doc=sdr"; } }),
      btn("SETTINGS", { icon: "settings", tone: "ghost", onClick: () => { location.hash = "#/settings"; } }),
    ],
  }));

  root.appendChild(alertStrip(
    "Receive first. Transmitting without a licence, on the wrong frequency, or near sensitive systems can be illegal and dangerous. "
    + "HackRF is not a calibrated instrument. Use the spectrum display to find signals, then verify with a dedicated receiver.",
    "",
  ));

  const stats = h("div", { class: "grid grid-4 mt-14" });
  stats.appendChild(stat("HackRF", hackrf.connected ? "CONNECTED" : "NO DEVICE",
    hackrf.connected ? (hackrf.board_id || "HackRF One") : "connect radio + check USB permissions",
    hackrf.connected ? "acc" : "warn"));
  stats.appendChild(stat("Tools", String(Object.values(tools).filter(Boolean).length).padStart(2, "0"),
    "hackrf_sweep / transfer / sox / dump1090"));
  stats.appendChild(stat("Recordings", String(recs.length).padStart(2, "0"), "local captures retained"));
  stats.appendChild(stat("Transmit", txEnabled ? "ARMED" : "HARD LOCK",
    txEnabled ? "legal attestation recorded" : "settings gate closed", txEnabled ? "warn" : ""));
  root.appendChild(stats);

  const topGrid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(topGrid);

  /* ------------------------------------------------------------- hardware */
  const devWrap = h("div", { class: "col" });
  const dev = h("div", { class: "device-card" });
  dev.appendChild(h("div", { class: "row between" },
    h("div", { class: "dev-name", text: hackrf.connected ? (hackrf.board_id || "HACKRF") : "HACKRF NOT DETECTED" }),
    badge(hackrf.connected ? "ONLINE" : "OFFLINE", hackrf.connected ? "green" : "red")));
  dev.appendChild(kv([
    ["Firmware", hackrf.firmware || "—"],
    ["Serial", hackrf.serial || "—"],
    ["Part ID", hackrf.part_id || "—"],
    ["RTL-SDR", devices.rtl?.connected ? "connected" : devices.rtl?.present ? "present / not detected" : "not installed"],
    ["ADS-B", tools.dump1090 ? "dump1090 installed" : "dump1090 missing"],
    ["GPS", tools.gpspipe ? "gpspipe installed" : "gpspipe missing"],
  ]));
  devWrap.appendChild(dev);
  if (hackrf.raw) {
    const raw = h("details", { class: "mt-10" });
    raw.appendChild(h("summary", { class: "tiny muted", text: "hackrf_info output" }));
    raw.appendChild(h("pre", { class: "tiny mono wrap-any", text: hackrf.raw }));
    devWrap.appendChild(raw);
  }
  if (!hackrf.connected) {
    devWrap.appendChild(h("div", { class: "callout warn small mt-10",
      text: "No HackRF detected. Spectrum and capture controls will fail honestly instead of showing invented data. "
        + "Check the USB cable, use a powered hub, and verify udev permissions for the HackRF." }));
  }
  const toolWrap = h("div", { class: "col gap-4 mt-10" });
  for (const [name, path] of Object.entries(tools)) {
    toolWrap.appendChild(h("div", { class: "row between small" },
      h("span", { class: "mono", text: name }),
      badge(path ? "OK" : "MISSING", path ? "green" : "muted")));
  }
  devWrap.appendChild(h("div", { class: "side-label mt-10", text: "TOOLCHAIN" }));
  devWrap.appendChild(toolWrap);
  const hardwarePanel = panel({
    title: "Radio hardware",
    sub: "Local device inventory",
    icon: "antenna",
    tools: [btn("RESCAN", { size: "sm", icon: "refresh", onClick: () => rescan(true) })],
    body: devWrap,
  });

  /* ------------------------------------------------------------- spectrum */
  const spectrumBody = h("div", { class: "col" });
  const spectrumShell = h("div", { class: "spectrum-shell" });
  const spectrumCanvas = h("canvas");
  spectrumShell.appendChild(spectrumCanvas);
  const waterfallShell = h("div", { class: "waterfall-shell" });
  const waterfallCanvas = h("canvas");
  waterfallShell.appendChild(waterfallCanvas);
  const spectrumReadout = h("div", { class: "row between tiny mono muted mt-6" },
    h("span", { text: "IDLE // no sweep running" }),
    h("span", { text: "—" }));
  const controls = h("div", { class: "sdr-controls mt-10" });
  const startInput = input({ type: "number", value: paramFreq ? Math.max(1, paramFreq - 1).toFixed(3) : "88", step: "0.1", min: "0.1", max: "6000" });
  const endInput = input({ type: "number", value: paramFreq ? Math.min(6000, paramFreq + 1).toFixed(3) : "108", step: "0.1", min: "0.1", max: "6000" });
  const binInput = input({ type: "number", value: "100", step: "10", min: "10", max: "5000" });
  const lnaInput = input({ type: "number", value: String(store.settings?.sdr_lna_gain ?? 32), min: "0", max: "40" });
  const vgaInput = input({ type: "number", value: String(store.settings?.sdr_rx_gain ?? 40), min: "0", max: "62" });
  const ampInput = checkbox("RF amp +14 dB", !!store.settings?.sdr_amp);
  controls.append(
    field("START (MHz)", startInput),
    field("END (MHz)", endInput),
    field("BIN (kHz)", binInput),
    field("LNA (0-40)", lnaInput),
    field("VGA (0-62)", vgaInput),
    h("div", { class: "field" }, h("label", { text: "AMP" }), ampInput),
  );
  const sweepBtn = btn("START SWEEP", { tone: "primary", icon: "play" });
  const stopSweepBtn = btn("STOP", { icon: "stop", tone: "danger", disabled: true });
  spectrumBody.append(
    spectrumShell,
    waterfallShell,
    spectrumReadout,
    controls,
    h("div", { class: "row wrap mt-10" }, sweepBtn, stopSweepBtn,
      h("span", { class: "tiny muted", text: "Sweep requires hackrf_sweep. A full 1–6000 MHz sweep can take time and congest USB." })),
  );
  topGrid.appendChild(panel({
    title: "Wideband spectrum",
    sub: "hackrf_sweep · live NDJSON",
    icon: "signal",
    cls: "span-2",
    body: spectrumBody,
  }));
  topGrid.appendChild(hardwarePanel);

  let sweepAbort = null;
  let sweepHistory = [];
  let bounds = null;
  function setSweepRunning(running) {
    sweepBtn.disabled = running;
    stopSweepBtn.disabled = !running;
    sweepBtn.replaceChildren(h("span", { text: running ? "SWEEPING…" : "START SWEEP" }));
  }
  async function startSweep() {
    if (sweepAbort) return;
    const start = Number(startInput.value), end = Number(endInput.value);
    if (!(start >= 0.1 && end <= 6000 && start < end)) {
      toast("Enter a valid start/end range within 0.1–6000 MHz", "warn");
      return;
    }
    sweepHistory = [];
    bounds = null;
    const controller = new AbortController();
    sweepAbort = controller;
    setSweepRunning(true);
    spectrumReadout.firstChild.textContent = "CONNECTING // waiting for first frame";
    try {
      const qs = new URLSearchParams({
        start_mhz: String(start), end_mhz: String(end), bin_khz: String(Number(binInput.value) || 100),
        lna: String(Number(lnaInput.value) || 32), vga: String(Number(vgaInput.value) || 40),
        amp: ampInput.querySelector("input")?.checked ? "true" : "false",
      });
      const res = await fetch(`/api/sdr/sweep?${qs}`, {
        credentials: "same-origin", signal: controller.signal,
        headers: { Accept: "application/x-ndjson" },
      });
      if (!res.ok) {
        let detail = `Sweep failed (${res.status})`;
        try { const data = await res.json(); detail = data.detail || data.error || detail; } catch {}
        throw new Error(detail);
      }
      if (!res.body) throw new Error("Streaming response is unavailable in this browser");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (!line.trim()) continue;
          let frame;
          try { frame = JSON.parse(line); } catch { continue; }
          if (frame.type === "error") throw new Error(frame.error || "Sweep error");
          if (frame.type === "sweep") acceptSweep(frame);
        }
      }
      spectrumReadout.firstChild.textContent = "SWEEP STOPPED // stream closed";
    } catch (err) {
      if (err.name === "AbortError") {
        spectrumReadout.firstChild.textContent = "SWEEP STOPPED // operator";
      } else {
        spectrumReadout.firstChild.textContent = `ERROR // ${err.message}`;
        toast(err.message || "Sweep failed", "error");
      }
    } finally {
      if (sweepAbort === controller) sweepAbort = null;
      setSweepRunning(false);
      drawSpectrum(spectrumCanvas, null, bounds);
    }
  }
  function acceptSweep(frame) {
    const db = Array.isArray(frame.db) ? frame.db : [];
    if (!db.length) return;
    const localMin = Math.min(...db), localMax = Math.max(...db);
    if (!bounds) bounds = { min: localMin - 7, max: localMax + 7 };
    else {
      bounds.min = bounds.min * .88 + (localMin - 7) * .12;
      bounds.max = bounds.max * .88 + (localMax + 7) * .12;
    }
    drawSpectrum(spectrumCanvas, frame, bounds);
    sweepHistory.push(db);
    if (sweepHistory.length > 150) sweepHistory.shift();
    drawWaterfall(waterfallCanvas, sweepHistory, bounds);
    spectrumReadout.firstChild.textContent =
      `LIVE // ${fmt.num(frame.start_hz / 1e6, 3)}–${fmt.num(frame.end_hz / 1e6, 3)} MHz`;
    const peakHz = peakFrequency(frame);
    spectrumReadout.lastChild.textContent =
      `PEAK ${peakHz ? `${fmt.num(peakHz / 1e6, 4)} MHz` : "—"} / ${fmt.num(frame.peak_db, 1)} dB`;
  }
  sweepBtn.addEventListener("click", startSweep);
  stopSweepBtn.addEventListener("click", () => { if (sweepAbort) sweepAbort.abort(); });
  setTimeout(() => drawSpectrum(spectrumCanvas, null, null), 30);

  /* -------------------------------------------------------------- capture */
  const captureBody = h("div", { class: "col" });
  const capFreq = input({ type: "number", value: paramFreq ? String(paramFreq) : "146.520", step: "0.0001", min: "0.1", max: "6000" });
  const capSeconds = input({ type: "number", value: "5", step: "0.5", min: "0.2", max: "180" });
  const capRate = input({ type: "number", value: "2.4", step: "0.1", min: "2", max: "20" });
  const capDemod = select(DEMODS, DEMODS.some((d) => d.value === paramMode) ? paramMode : "nfm");
  const capName = input({ type: "text", placeholder: "optional label", maxlength: "120" });
  const capKeepIq = checkbox("Keep IQ file (large; enables TX replay)", false);
  const capGrid = h("div", { class: "form-grid" });
  capGrid.append(
    field("FREQUENCY (MHz)", capFreq),
    field("DURATION (s)", capSeconds),
    field("SAMPLE RATE (Msps)", capRate, "2–20 Msps; 2.4 is a safe default"),
    field("DEMOD", capDemod),
    field("LABEL", capName),
    h("div", { class: "wide" }, capKeepIq),
  );
  const captureBtn = btn("START CAPTURE", { tone: "primary", icon: "play" });
  const captureStatus = h("div", { class: "col mt-10" });
  captureBody.append(capGrid, h("div", { class: "row mt-10" }, captureBtn), captureStatus);
  if (paramFreq) {
    captureBody.insertBefore(h("div", { class: "callout small" },
      `Tuned from Communications: ${fmt.num(paramFreq, 4)} MHz ${paramMode ? `// ${paramMode.toUpperCase()}` : ""}`), capGrid);
  }
  topGrid.appendChild(panel({
    title: "Capture & demodulate",
    sub: "hackrf_transfer → local DSP",
    icon: "radio",
    body: captureBody,
  }));

  let pollTimer = null;
  let activeJob = null;
  function showJob(job) {
    activeJob = job;
    captureStatus.replaceChildren();
    const wrap = h("div", { class: "device-card" });
    wrap.appendChild(h("div", { class: "row between" },
      h("div", { class: "dev-name", text: job.name || job.id }),
      badge(String(job.status || "queued").toUpperCase(), statusTone(job.status))));
    wrap.appendChild(kv([
      ["Frequency", mhz(job.freq_hz)],
      ["Rate", job.rate ? `${fmt.num(Number(job.rate) / 1e6, 2)} Msps` : "—"],
      ["Duration", job.seconds != null ? `${fmt.num(job.seconds, 1)} s` : "—"],
      ["Mode", String(job.demod || job.kind || "—").toUpperCase()],
      ["Job ID", job.id],
    ]));
    const pct = Math.round((Number(job.progress) || 0) * 100);
    wrap.appendChild(h("div", { class: "row between tiny muted mt-6" },
      h("span", { text: pct >= 100 ? "COMPLETE" : "CAPTURING" }),
      h("span", { text: `${pct}%` })));
    wrap.appendChild(progressBar(pct, job.status === "error" ? "bad" : ""));
    if (job.error) wrap.appendChild(h("div", { class: "callout danger small mt-10", text: job.error }));
    if (job.status === "complete") {
      if (job.audio_path) {
        wrap.appendChild(h("div", { class: "side-label mt-10", text: "AUDIO" }));
        const audio = h("audio", { controls: true, src: `/api/sdr/jobs/${encodeURIComponent(job.id)}/audio` });
        audio.style.width = "100%";
        wrap.appendChild(audio);
      }
      if (job.image_path) {
        wrap.appendChild(h("div", { class: "side-label mt-10", text: "APT IMAGE // EXPERIMENTAL" }));
        wrap.appendChild(h("img", { src: `/api/sdr/jobs/${encodeURIComponent(job.id)}/image`, alt: "APT decode", style: { width: "100%", border: "1px solid var(--line)" } }));
      }
      if (!job.audio_path && !job.image_path) {
        wrap.appendChild(h("div", { class: "small muted mt-10", text: "Job complete. See the recording list below." }));
      }
      loadRecordings();
    } else if (job.status === "running" || job.status === "processing" || job.status === "queued") {
      const stop = btn("STOP JOB", { size: "sm", tone: "danger", icon: "stop", onClick: async () => {
        try { await api.post(`/api/sdr/jobs/${encodeURIComponent(job.id)}/stop`, {}); toast("Stop requested"); }
        catch (err) { toast(err.message, "error"); }
      } });
      wrap.appendChild(h("div", { class: "row mt-10" }, stop));
    }
    captureStatus.appendChild(wrap);
  }
  async function startCapture() {
    const freqMhz = Number(capFreq.value);
    if (!(freqMhz >= 1 && freqMhz <= 6000)) { toast("Frequency must be 1–6000 MHz", "warn"); return; }
    captureBtn.disabled = true;
    captureStatus.replaceChildren(loading("STARTING CAPTURE"));
    try {
      const job = await api.post("/api/sdr/capture", {
        freq_hz: freqMhz * 1e6,
        seconds: Number(capSeconds.value),
        rate: Number(capRate.value) * 1e6,
        demod: capDemod.value,
        lna: Number(lnaInput.value) || 32,
        vga: Number(vgaInput.value) || 40,
        amp: !!ampInput.querySelector("input")?.checked,
        keep_iq: !!capKeepIq.querySelector("input")?.checked,
        name: capName.value.trim(),
      });
      showJob(job);
      clearInterval(pollTimer);
      pollTimer = setInterval(async () => {
        if (!activeJob) return;
        try {
          const j = await api.get(`/api/sdr/jobs/${encodeURIComponent(activeJob.id)}`);
          showJob(j);
          if (["complete", "error", "stopped"].includes(j.status)) {
            clearInterval(pollTimer);
            pollTimer = null;
            if (j.status === "complete") toast("Capture complete");
            if (j.status === "error") toast(j.error || "Capture failed", "error");
          }
        } catch (err) {
          clearInterval(pollTimer); pollTimer = null;
          captureStatus.appendChild(h("div", { class: "callout danger small mt-10", text: err.message }));
        }
      }, 700);
    } catch (err) {
      captureStatus.replaceChildren(h("div", { class: "callout danger small", text: err.message }));
      toast(err.message || "Capture failed", "error");
    } finally {
      captureBtn.disabled = false;
    }
  }
  captureBtn.addEventListener("click", startCapture);

  /* ----------------------------------------------------------- recordings */
  const recPanel = panel({
    title: "Recordings",
    sub: "Local audio, images and retained IQ",
    icon: "database",
    cls: "span-3",
    tools: [btn("REFRESH", { size: "sm", icon: "refresh", onClick: () => loadRecordings() })],
    body: h("div", { class: "col", id: "sdr-rec-list" }),
  });
  root.appendChild(recPanel);
  root.appendChild(h("div", { class: "grid grid-2 mt-14", id: "sdr-lower" }));
  const lower = root.querySelector("#sdr-lower");

  async function loadRecordings() {
    try {
      const out = await api.get("/api/sdr/recordings");
      paintRecordings(out.items || []);
    } catch (err) { toast(err.message, "error"); }
  }
  function paintRecordings(items) {
    const target = recPanel.querySelector("#sdr-rec-list");
    target.replaceChildren();
    if (!items.length) {
      target.appendChild(emptyState("No recordings", "Capture a signal to create a real local recording.", "radio"));
      return;
    }
    for (const rec of items) {
      const wrap = h("div", { class: "rec-item" });
      const info = h("div", { class: "grow" });
      info.appendChild(h("div", { class: "rec-name", text: rec.name || `Recording #${rec.id}` }));
      info.appendChild(h("div", { class: "rec-sub",
        text: `${mhz(rec.freq_hz)} · ${String(rec.kind || "").toUpperCase()} · ${fmt.bytes(rec.size || 0)} · ${fmt.ago(rec.ts)}` }));
      wrap.appendChild(info);
      if (rec.audio_path) {
        const audio = h("audio", { controls: true, preload: "none", src: `/api/sdr/recordings/${rec.id}/audio` });
        wrap.appendChild(audio);
      }
      if (rec.image_path) {
        wrap.appendChild(h("img", {
          src: `/api/sdr/recordings/${rec.id}/image`, alt: rec.name || "recording",
          style: { height: "38px", border: "1px solid var(--line)" },
        }));
      }
      if (rec.iq_path) {
        wrap.appendChild(btn("IQ", { size: "sm", tone: "ghost", icon: "download", title: "Download IQ",
          onClick: () => { location.href = `/api/sdr/recordings/${rec.id}/iq?download=true`; } }));
        wrap.appendChild(btn("TX", { size: "sm", tone: "danger", title: "Replay this IQ",
          onClick: () => openTx(rec) }));
      }
      wrap.appendChild(btn("DEL", { size: "sm", tone: "ghost", onClick: async () => {
        const ok = await confirmDialog({
          title: "Delete recording", confirmText: "DELETE", danger: true,
          message: `Delete recording #${rec.id} and all of its local files? This cannot be undone.`,
        });
        if (!ok) return;
        try { await api.del(`/api/sdr/recordings/${rec.id}`); toast("Recording deleted"); loadRecordings(); }
        catch (err) { toast(err.message, "error"); }
      } }));
      target.appendChild(wrap);
    }
  }

  /* ---------------------------------------------------------------- ADS-B */
  const adsbBody = h("div", { class: "col" });
  if (!adsb.available) {
    adsbBody.appendChild(emptyState("ADS-B feed unavailable", adsb.note || "Start dump1090 and expose its JSON feed on 127.0.0.1:8080.", "satellite"));
  } else if (!(adsb.aircraft || []).length) {
    adsbBody.appendChild(emptyState("No aircraft heard", "dump1090 is reachable but reports no aircraft.", "satellite"));
  } else {
    const table = h("table", { class: "data" });
    table.innerHTML = "<thead><tr><th>FLIGHT</th><th>HEX</th><th>ALT</th><th>GS</th><th>TRK</th></tr></thead>";
    const tbody = h("tbody");
    for (const a of adsb.aircraft) {
      const tr = h("tr");
      tr.append(
        h("td", { text: a.flight || "—" }),
        h("td", { class: "mono", text: a.hex || "—" }),
        h("td", { class: "num", text: a.alt_baro != null ? String(a.alt_baro) : "—" }),
        h("td", { class: "num", text: a.gs != null ? fmt.num(a.gs, 0) : "—" }),
        h("td", { class: "num", text: a.track != null ? `${fmt.num(a.track, 0)}°` : "—" }),
      );
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    adsbBody.appendChild(h("div", { class: "scroll-x" }, table));
  }
  adsbBody.appendChild(h("div", { class: "hint mt-10",
    text: "ADS-B is receive-only. dump1090 typically needs an RTL-SDR; a HackRF IQ capture can be fed to dump1090 --ifile separately." }));
  lower.appendChild(panel({ title: "ADS-B / air picture", sub: "dump1090 local feed", icon: "satellite", body: adsbBody }));

  /* ------------------------------------------------------------------- TX */
  const txBody = h("div", { class: "col" });
  const iqRecs = recs.filter((r) => r.iq_path);
  if (!txEnabled) {
    txBody.appendChild(h("div", { class: "tx-warning",
      text: "TRANSMIT IS HARD-LOCKED. Enable it in Settings only after you have read the legal and RF-safety warnings and recorded the legal attestation." }));
    txBody.appendChild(btn("OPEN TX GATE SETTINGS", { tone: "amber", icon: "settings", onClick: () => { location.hash = "#/settings"; } }));
  } else {
    txBody.appendChild(h("div", { class: "tx-warning",
      text: "TX is armed. Replay only IQ you are authorised to transmit. Use a dummy load or shielded setup for testing, never an antenna indoors." }));
    if (!iqRecs.length) {
      txBody.appendChild(h("div", { class: "small muted", text: "No recordings with retained IQ. Enable “Keep IQ” when capturing." }));
    } else {
      const sel = select(iqRecs.map((r) => ({ value: String(r.id), label: `${r.name || `#${r.id}`} — ${mhz(r.freq_hz)}` })), String(iqRecs[0].id));
      txBody.appendChild(field("RETAINED IQ RECORDING", sel));
      txBody.appendChild(btn("ARM REPLAY", { tone: "danger", icon: "send", onClick: () => openTx(iqRecs.find((r) => String(r.id) === sel.value) || iqRecs[0]) }));
    }
  }
  lower.appendChild(panel({ title: "Transmit gate", sub: "Audited and confirmation-gated", icon: "warning", cls: "tx-gate", body: txBody }));

  function openTx(rec) {
    if (!rec) return;
    if (!txEnabled) { toast("Transmit is disabled in Settings", "warn"); return; }
    const attest = checkbox("I am licensed/authorised for this frequency and accept responsibility for this transmission.", false);
    const phrase = input({ placeholder: "Type TRANSMIT", autocomplete: "off" });
    const amp = checkbox("Enable RF amplifier (+14 dB) — only with proper shielding/load", false);
    const gain = input({ type: "number", value: "40", min: "0", max: "47" });
    const body = h("div", { class: "col" },
      h("div", { class: "tx-warning", text:
        "This will key the HackRF and replay raw IQ into whatever is connected. Confirm the frequency, filter, antenna/load and legal authority before proceeding." }),
      kv([["Recording", rec.name || `#${rec.id}`], ["Frequency", mhz(rec.freq_hz)], ["Rate", rec.rate ? `${fmt.num(rec.rate / 1e6, 2)} Msps` : "—"]]),
      attest,
      field("Confirmation", phrase),
      field("TX VGA gain (0-47)", gain),
      amp,
    );
    const m = modal({
      title: "Transmit confirmation",
      sub: "Hard gate // audited",
      body,
      actions: [btn("CANCEL", { tone: "ghost", onClick: () => m.close() }), btn("TRANSMIT", { tone: "danger", icon: "send" })],
    });
    const go = m.box.querySelector(".modal-foot .btn-danger");
    go.addEventListener("click", async () => {
      if (phrase.value.trim().toUpperCase() !== "TRANSMIT") { toast("Type TRANSMIT exactly", "warn"); return; }
      if (!attest.querySelector("input")?.checked) { toast("Legal attestation is required", "warn"); return; }
      go.disabled = true;
      try {
        const out = await api.post("/api/sdr/tx", {
          recording_id: rec.id, vga: Number(gain.value) || 40,
          amp: !!amp.querySelector("input")?.checked, confirm: "TRANSMIT", attest: true,
        });
        toast(out.note || "Transmission complete");
        m.close();
      } catch (err) {
        toast(err.message || "Transmit failed", "error");
      } finally { go.disabled = false; }
    });
  }

  paintRecordings(recs);

  /* ---------------------------------------------------------------- jobs */
  if (recentJobs.length) {
    const jobsPanel = panel({
      title: "Recent jobs",
      sub: "Capture history",
      icon: "clock",
      cls: "span-3",
      body: h("div", { class: "scroll-x" }, (() => {
        const table = h("table", { class: "data" });
        table.innerHTML = "<thead><tr><th>ID</th><th>NAME</th><th>FREQ</th><th>MODE</th><th>STATUS</th><th>CREATED</th></tr></thead>";
        const tbody = h("tbody");
        for (const j of recentJobs) {
          const tr = h("tr");
          tr.append(
            h("td", { class: "mono", text: j.id }),
            h("td", { text: j.name || "—" }),
            h("td", { class: "num", text: mhz(j.freq_hz) }),
            h("td", { text: String(j.demod || j.kind || "—").toUpperCase() }),
            h("td", {}, badge(String(j.status || "").toUpperCase(), statusTone(j.status))),
            h("td", { text: fmt.date(j.created) }),
          );
          tbody.appendChild(tr);
        }
        table.appendChild(tbody);
        return table;
      })()),
    });
    root.appendChild(jobsPanel);
  }

  const cards = await referenceSection("COMMS", { limit: 6 });
  if (cards) root.appendChild(panel({ title: "Radio field cards", sub: "Receive and procedure reference", icon: "book", cls: "mt-14", body: cards }));

  loadRecordings();

  return () => {
    if (sweepAbort) { try { sweepAbort.abort(); } catch {} }
    if (pollTimer) clearInterval(pollTimer);
  };

  async function rescan(force = false) {
    try {
      const d = await api.get(`/api/sdr/devices?force=${force ? "true" : "false"}`);
      toast(d.hackrf?.connected ? "HackRF detected" : "No HackRF detected", d.hackrf?.connected ? "" : "warn");
      render(root, params);
    } catch (err) { toast(err.message, "error"); }
  }
}

/* --------------------------------------------------------------- rendering */
function peakFrequency(frame) {
  const db = frame.db || [];
  if (!db.length) return null;
  let idx = 0;
  for (let i = 1; i < db.length; i++) if (db[i] > db[idx]) idx = i;
  const span = Number(frame.end_hz) - Number(frame.start_hz);
  return Number(frame.start_hz) + (idx / Math.max(1, db.length - 1)) * span;
}

function chartSize(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const w = Math.max(280, rect.width || 800);
  const hh = Math.max(100, rect.height || 220);
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(hh * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: hh };
}

function drawSpectrum(canvas, frame, bounds) {
  const { ctx, w, h: hh } = chartSize(canvas);
  ctx.clearRect(0, 0, w, hh);
  ctx.fillStyle = "#020604";
  ctx.fillRect(0, 0, w, hh);
  ctx.strokeStyle = "rgba(93,255,154,.09)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 8; i++) {
    const x = (w / 8) * i;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, hh); ctx.stroke();
  }
  for (let i = 1; i < 5; i++) {
    const y = (hh / 5) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  if (!frame || !frame.db?.length || !bounds) {
    ctx.fillStyle = "rgba(123,154,136,.7)";
    ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.fillText("NO SWEEP DATA", 12, hh / 2);
    return;
  }
  const db = frame.db;
  const lo = bounds.min, hi = bounds.max;
  ctx.beginPath();
  for (let i = 0; i < db.length; i++) {
    const x = 4 + (i / (db.length - 1)) * (w - 8);
    const y = hh - 18 - ((db[i] - lo) / Math.max(0.001, hi - lo)) * (hh - 30);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.lineTo(w - 4, hh - 18);
  ctx.lineTo(4, hh - 18);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, 0, 0, hh);
  grad.addColorStop(0, "rgba(93,255,154,.24)");
  grad.addColorStop(1, "rgba(93,255,154,.015)");
  ctx.fillStyle = grad;
  ctx.fill();
  ctx.beginPath();
  for (let i = 0; i < db.length; i++) {
    const x = 4 + (i / (db.length - 1)) * (w - 8);
    const y = hh - 18 - ((db[i] - lo) / Math.max(0.001, hi - lo)) * (hh - 30);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = "#5dff9a";
  ctx.lineWidth = 1.4;
  ctx.stroke();
  ctx.fillStyle = "rgba(205,236,218,.72)";
  ctx.font = "9px 'JetBrains Mono', monospace";
  ctx.fillText(`${(frame.start_hz / 1e6).toFixed(3)} MHz`, 6, hh - 5);
  const mid = (frame.start_hz + frame.end_hz) / 2 / 1e6;
  ctx.textAlign = "center";
  ctx.fillText(`${mid.toFixed(3)} MHz`, w / 2, hh - 5);
  ctx.textAlign = "right";
  ctx.fillText(`${(frame.end_hz / 1e6).toFixed(3)} MHz`, w - 6, hh - 5);
  ctx.textAlign = "left";
  ctx.fillStyle = "rgba(123,154,136,.78)";
  ctx.fillText(`${Math.round(hi)} dB`, 6, 12);
  ctx.fillStyle = "rgba(123,154,136,.55)";
  ctx.fillText(`${Math.round(lo)} dB`, 6, hh - 21);
}

function drawWaterfall(canvas, history, bounds) {
  if (!history.length) {
    const { ctx, w, h: hh } = chartSize(canvas);
    ctx.fillStyle = "#020604";
    ctx.fillRect(0, 0, w, hh);
    return;
  }
  const width = Math.min(1024, Math.max(64, history[0].length));
  canvas.width = width;
  canvas.height = Math.max(40, history.length);
  const ctx = canvas.getContext("2d");
  const img = ctx.createImageData(width, canvas.height);
  const lo = bounds?.min ?? -110, hi = bounds?.max ?? -10;
  for (let row = 0; row < history.length; row++) {
    const frame = history[row];
    const src = frame.length;
    const y = history.length - 1 - row;
    for (let x = 0; x < width; x++) {
      const idx = Math.min(src - 1, Math.floor((x / width) * src));
      let t = (frame[idx] - lo) / Math.max(0.001, hi - lo);
      t = Math.max(0, Math.min(1, t));
      let r, g, b;
      if (t < .45) { const k = t / .45; r = 2 + 10 * k; g = 8 + 80 * k; b = 6 + 24 * k; }
      else if (t < .8) { const k = (t - .45) / .35; r = 12 + 80 * k; g = 88 + 167 * k; b = 30 + 40 * k; }
      else { const k = (t - .8) / .2; r = 92 + 163 * k; g = 255; b = 70 + 150 * k; }
      const p = (y * width + x) * 4;
      img.data[p] = r; img.data[p + 1] = g; img.data[p + 2] = b; img.data[p + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
}
