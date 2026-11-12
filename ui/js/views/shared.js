/* Shared view helpers. No external network access; everything is local. */
import { api, openFilePicker, fileText } from "../api.js";
import {
  h, modal, btn, toast, renderMarkdown, fmt,
} from "../components.js";
import { severityTone } from "../records.js";

export function kv(rows) {
  const wrap = h("div", { class: "kv" });
  for (const [k, v] of rows) {
    wrap.appendChild(h("span", { class: "k", text: k }));
    const val = h("span", { class: "v" });
    if (v instanceof Node) val.appendChild(v);
    else val.textContent = v === null || v === undefined || v === "" ? "—" : String(v);
    wrap.appendChild(val);
  }
  return wrap;
}

export function manualPositionMetadata() {
  return {
    position_source: "manual",
    position_updated_at: new Date().toISOString(),
    position_fix_at: "",
    position_accuracy_m: null,
    position_mode: 0,
  };
}

export function gpsPositionMetadata(gps) {
  const now = new Date().toISOString();
  const fixTime = gps?.time && Number.isFinite(Date.parse(gps.time))
    ? new Date(gps.time).toISOString() : now;
  const accuracy = gps?.accuracy_m == null ? NaN : Number(gps.accuracy_m);
  return {
    position_source: "gpsd",
    position_updated_at: now,
    position_fix_at: fixTime,
    position_accuracy_m: Number.isFinite(accuracy) && accuracy >= 0 ? accuracy : null,
    position_mode: Number(gps?.mode) || 0,
  };
}

export function phoneGpsMetadata(fix) {
  const now = new Date().toISOString();
  const timestamp = fix?.timestamp && Number.isFinite(Date.parse(fix.timestamp))
    ? new Date(fix.timestamp).toISOString() : now;
  const accuracy = fix?.accuracy_m == null ? NaN : Number(fix.accuracy_m);
  return {
    position_source: "phone",
    position_updated_at: fix?.received_at || now,
    position_fix_at: timestamp,
    position_accuracy_m: Number.isFinite(accuracy) && accuracy >= 0 ? accuracy : null,
    position_mode: 3,
  };
}

export function geocodedPositionMetadata() {
  return {
    position_source: "geocoded",
    position_updated_at: new Date().toISOString(),
    position_fix_at: "",
    position_accuracy_m: null,
    position_mode: 0,
  };
}

/* A saved coordinate is never presented as live tracking. */
export function opsecActive(settings = null) {
  try {
    if (document.body?.classList.contains("opsec")) return true;
  } catch {}
  return !!(settings && settings.opsec_mode);
}

export function maskLocation(value, settings = null, fallback = "OP//MASKED") {
  return opsecActive(settings) ? fallback : value;
}

export function positionConfidence(settings = {}) {
  const source = String(settings.position_source || "unverified").toLowerCase();
  const updated = Date.parse(settings.position_updated_at || "");
  if (source === "gpsd" || source === "phone") {
    const fixAt = Date.parse(settings.position_fix_at || settings.position_updated_at || "");
    if (!Number.isFinite(fixAt)) {
      return { tone: "danger", mapLabel: "GPS TIME UNKNOWN", text: "GPS source recorded, but the fix time is missing. Verify this position before navigating." };
    }
    const ageMs = Date.now() - fixAt;
    const accuracy = settings.position_accuracy_m == null ? NaN : Number(settings.position_accuracy_m);
    const accuracyText = Number.isFinite(accuracy) ? ` · estimated horizontal error ±${fmt.num(accuracy, 1)} m` : " · receiver accuracy unavailable";
    if (ageMs < -5 * 60_000) {
      return { tone: "danger", mapLabel: "GPS CLOCK CHECK", text: `GPS timestamp ${fmt.date(new Date(fixAt).toISOString())} is ahead of the device clock. Check time and verify position.` };
    }
    const ageText = fmt.ago(fixAt);
    const savedText = Number.isFinite(updated) ? ` · saved ${fmt.ago(updated)}` : "";
    const stale = ageMs > (source === "phone" ? 24 : 6) * 60 * 60_000;
    const old = ageMs > 15 * 60_000;
    const sourceLabel = source === "phone" ? "phone GPS" : "GPS receiver";
    const mapLabel = source === "phone" ? `${stale ? "STALE PHONE" : "PHONE GPS"} ${ageText}` : `${stale ? "STALE GPS" : "GPS"} ${ageText}`;
    return {
      tone: stale ? "danger" : old || (Number.isFinite(accuracy) && accuracy > 50) ? "warn" : "",
      mapLabel,
      text: `Last ${sourceLabel} fix ${ageText}${savedText}${accuracyText}. This is a saved fix, not live tracking; refresh and verify if you have moved.`,
    };
  }
  if (source === "manual") {
    const savedText = Number.isFinite(updated) ? ` Saved ${fmt.ago(updated)}.` : "";
    return {
      tone: "warn",
      mapLabel: "MANUAL REFERENCE",
      text: `Manually entered reference coordinate; it is not a live GPS position and has no accuracy estimate.${savedText} Verify datum and current position on the ground.`,
    };
  }
  if (source === "geocoded") {
    const savedText = Number.isFinite(updated) ? ` Saved ${fmt.ago(updated)}.` : "";
    return {
      tone: "warn",
      mapLabel: "GEOCODED HOME",
      text: `Address lookup reference coordinate; it is not a live GPS position and has no accuracy estimate.${savedText} Verify the selected point on the offline map and against terrain. The query is not saved to disk; it may remain in memory cache for up to 24 hours.`,
    };
  }
  return {
    tone: "danger",
    mapLabel: "UNVERIFIED POSITION",
    text: "Position source and fix age are unknown. These configured coordinates may be wrong; verify them before relying on navigation.",
  };
}

export function errorBox(err) {
  return h("div", { class: "callout danger", text: err?.message || String(err || "Request failed") });
}

export function feedBadge(status) {
  const tone = status === "OK" ? "green" : status === "STALE" ? "amber"
    : status === "ERROR" ? "red" : "muted";
  return h("span", { class: `badge ${tone}`, text: status || "NEVER" });
}

export function severityBadge(value) {
  return h("span", { class: `badge ${severityTone(value)}`, text: String(value || "INFO") });
}

export function downloadBlob(filename, content, mime = "text/plain;charset=utf-8") {
  const blob = content instanceof Blob ? content : new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

export function downloadText(filename, text, mime = "text/plain;charset=utf-8") {
  downloadBlob(filename, text, mime);
}

export function printText(title, text) {
  const win = window.open("", "_blank", "width=820,height=900");
  if (!win) { toast("Popup blocked — download instead", "warn"); return; }
  const safe = String(text).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  win.document.write(`<!doctype html><title>${title}</title><style>
    body{font:12px/1.55 "Courier New",monospace;color:#111;background:#fff;padding:24px;white-space:pre-wrap}
    h1{font:600 15px/1.3 monospace;letter-spacing:.14em;border-bottom:2px solid #111;padding-bottom:8px}
    @media print{body{padding:0}}
  </style><h1>${title}</h1><pre>${safe}</pre>`);
  win.document.close();
  setTimeout(() => { try { win.focus(); win.print(); } catch {} }, 250);
}

export async function readTextFile(accept = ".gpx,.txt,.md,.json,.csv") {
  const file = await openFilePicker(accept);
  if (!file) return null;
  return { name: file.name, text: await fileText(file, 8_000_000) };
}

export function openReferenceCard(card) {
  const body = h("div", { class: "markdown" });
  body.innerHTML = renderMarkdown(`## ${card.title}

${card.body || ""}`);
  return modal({
    title: card.title,
    sub: `${card.category || "REFERENCE"} // FIELD CARD`,
    wide: true,
    body,
    actions: [btn("CLOSE", { tone: "ghost", onClick: () => m.close() })],
  });
}

let referencePromise = null;
export function loadReference() {
  if (!referencePromise) {
    referencePromise = api.get("/api/reference").then((r) => r.cards || []).catch((err) => {
      referencePromise = null;
      throw err;
    });
  }
  return referencePromise;
}

export function referenceGrid(cards, { limit = 0 } = {}) {
  const grid = h("div", { class: "card-grid" });
  const list = limit ? cards.slice(0, limit) : cards;
  for (const card of list) {
    const node = h("div", { class: "ref-card", role: "button", tabindex: "0", title: "Open field card" });
    node.appendChild(h("div", { class: "rc-cat", text: card.category || "REFERENCE" }));
    node.appendChild(h("div", { class: "rc-title", text: card.title }));
    const body = h("div", { class: "rc-body" });
    body.textContent = String(card.body || "").replace(/[*_`#>|]/g, "").replace(/\s+/g, " ").slice(0, 180);
    node.appendChild(body);
    node.addEventListener("click", () => openReferenceCard(card));
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openReferenceCard(card); }
    });
    grid.appendChild(node);
  }
  return grid;
}

export async function referenceSection(category, { limit = 0, title = "Field cards" } = {}) {
  try {
    const cards = await loadReference();
    const subset = cards.filter((c) => c.category === category);
    if (!subset.length) return null;
    return referenceGrid(subset, { limit });
  } catch {
    return null;
  }
}

export function copyButton(text, label = "COPY") {
  return btn(label, {
    size: "sm", tone: "ghost",
    onClick: async () => {
      try { await navigator.clipboard.writeText(String(text)); toast("Copied"); }
      catch { toast("Copy failed", "error"); }
    },
  });
}

export function timeAgo(value) {
  return fmt.ago(value);
}

export function statusTone(value) {
  const v = String(value || "").toUpperCase();
  return ["OK", "COMPLETE", "COMPLETED", "CONNECTED", "ACTIVE", "GREEN", "NOMINAL"].includes(v) ? "green"
    : ["RUNNING", "STALE", "WARNING", "AMBER", "YELLOW", "PARTIAL"].includes(v) ? "amber"
    : ["ERROR", "FAILED", "CRITICAL", "RED", "EMPTY", "COMPROMISED"].includes(v) ? "red" : "muted";
}

export function progressBar(value, tone = "") {
  const pct = Math.max(0, Math.min(100, Number(value) || 0));
  const bar = h("div", { class: `progress ${tone}`.trim() });
  const fill = h("span");
  fill.style.width = `${pct}%`;
  bar.appendChild(fill);
  return bar;
}

export { downloadBlob as download, printText as print };
