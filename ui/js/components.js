import { icon, hydrateIcons } from "./icons.js";

export { icon, hydrateIcons } from "./icons.js";

const SVG_NS = "http://www.w3.org/2000/svg";

export function h(tag, props = null, ...children) {
  const node = document.createElement(tag);
  if (props && typeof props === "object") {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class" || key === "className") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key === "style" && typeof value === "object") Object.assign(node.style, value);
      else if (key === "on" && typeof value === "object") {
        for (const [evt, handler] of Object.entries(value)) node.addEventListener(evt, handler);
      } else if (key === "attrs" && typeof value === "object") {
        for (const [attr, val] of Object.entries(value)) if (val !== null && val !== undefined) node.setAttribute(attr, val);
      } else if (key in node && !["list", "type", "form", "size"].includes(key)) {
        try { node[key] = value; } catch { node.setAttribute(key, value); }
      } else {
        node.setAttribute(key === "for" ? "for" : key, value === true ? "" : value);
      }
    }
  }
  append(node, children);
  return node;
}

export function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    if (child instanceof Node) parent.appendChild(child);
    else parent.appendChild(document.createTextNode(String(child)));
  }
  return parent;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

export function ftag(tag, props, ...children) {
  return h(tag, props, ...children);
}

export function btn(label, opts = {}) {
  const { icon: ic, tone = "", size = "", onClick, title, type = "button", disabled = false, class: extra = "" } = opts;
  const node = h("button", {
    class: `btn ${tone === "primary" ? "btn-primary" : tone === "danger" ? "btn-danger" : tone === "amber" ? "btn-amber" : tone === "ghost" ? "btn-ghost" : ""} ${size === "sm" ? "btn-sm" : size === "lg" ? "btn-lg" : ""} ${extra}`.trim(),
    type, title, disabled, on: onClick ? { click: onClick } : {},
  });
  if (ic) node.appendChild(icon(ic));
  if (label) node.appendChild(h("span", { text: label }));
  return node;
}

export function iconBtn(name, title, onClick, tone = "") {
  const node = h("button", { class: `icon-btn ${tone}`, type: "button", title, "aria-label": title });
  node.appendChild(icon(name));
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

export function badge(text, tone = "") {
  return h("span", { class: `badge ${tone}`.trim(), text });
}

export function panel({ title, sub, icon: ic, tools = [], body = null, cls = "", flush = false, foot = null, id = "" }) {
  const head = h("div", { class: "panel-head" });
  if (title) {
    const titleNode = h("h2");
    if (ic) titleNode.appendChild(icon(ic));
    titleNode.appendChild(h("span", { text: title }));
    head.appendChild(titleNode);
  }
  if (sub) head.appendChild(h("span", { class: "sub", text: sub }));
  if (tools.length) {
    const toolWrap = h("div", { class: "panel-tools" });
    tools.forEach((t) => toolWrap.appendChild(t instanceof Node ? t : h("span", { text: String(t) })));
    head.appendChild(toolWrap);
  }
  const section = h("section", { class: `panel ${cls}`.trim(), id });
  if (title || sub || tools.length) section.appendChild(head);
  if (body !== null) {
    const bodyEl = h("div", { class: `panel-body ${flush ? "flush" : ""}`.trim() });
    append(bodyEl, [body]);
    section.appendChild(bodyEl);
  }
  if (foot) section.appendChild(h("div", { class: "panel-foot" }, foot));
  return section;
}

export function stat(label, value, foot = "", tone = "") {
  return h("div", { class: `stat ${tone}`.trim() },
    h("div", { class: "stat-label", text: label }),
    h("div", { class: "stat-value" }, value),
    foot ? h("div", { class: "stat-foot" }, foot) : null);
}

export function emptyState(text, sub = "", iconName = "info") {
  return h("div", { class: "empty-state" },
    icon(iconName),
    h("strong", { text }),
    sub ? h("div", { text: sub }) : null);
}

export function field(label, control, hint = "") {
  return h("div", { class: "field" },
    label ? h("label", {}, label) : null,
    control,
    hint ? h("div", { class: "hint", text: hint }) : null);
}

export function input(props = {}) {
  return h("input", { class: "input", ...props });
}

export function textarea(props = {}) {
  if (props.text !== undefined) props.value = props.text;
  return h("textarea", { class: "textarea", ...props });
}

export function select(options, value = "", props = {}) {
  const node = h("select", { class: "select", ...props });
  for (const opt of options) {
    const item = typeof opt === "string" ? { value: opt, label: opt } : opt;
    const option = h("option", { value: item.value, text: item.label });
    if (String(item.value) === String(value)) option.selected = true;
    node.appendChild(option);
  }
  return node;
}

export function checkbox(label, checked = false, onChange = null) {
  const inp = h("input", { type: "checkbox", checked: !!checked });
  if (onChange) inp.addEventListener("change", () => onChange(inp.checked));
  return h("label", { class: "check" }, inp, h("span", { text: label }));
}

export function tabs(items, active, onSelect) {
  const wrap = h("div", { class: "tabs", role: "tablist" });
  items.forEach((item) => {
    const { id, label, count } = typeof item === "string" ? { id: item, label: item } : item;
    const button = h("button", { type: "button", class: id === active ? "active" : "", role: "tab" });
    button.appendChild(h("span", { text: label }));
    if (count !== undefined) button.appendChild(h("span", { class: "count", text: String(count) }));
    button.addEventListener("click", () => onSelect(id));
    wrap.appendChild(button);
  });
  return wrap;
}

export function pageHead({ kicker = "", title, desc = "", actions = [] }) {
  const head = h("div", { class: "view-head" });
  const copy = h("div", {},
    kicker ? h("div", { class: "view-kicker", text: kicker }) : null,
    h("h1", { text: title }),
    desc ? h("div", { class: "view-desc", text: desc }) : null);
  head.appendChild(copy);
  if (actions.length) {
    const wrap = h("div", { class: "view-actions" });
    actions.forEach((a) => wrap.appendChild(a instanceof Node ? a : h("span", { text: String(a) })));
    head.appendChild(wrap);
  }
  return head;
}

export function sectionTitle(title, sub = "") {
  return h("div", { class: "section-title" },
    h("h2", { text: title }),
    h("div", { class: "line" }),
    sub ? h("span", { class: "sub", text: sub }) : null);
}

export function alertStrip(text, tone = "", onClose = null) {
  const strip = h("div", { class: `alert-strip ${tone}`.trim() }, icon(tone === "red" ? "warning" : "info"), h("span", { text }));
  if (onClose) strip.appendChild(iconBtn("close", "Dismiss", onClose, "close"));
  return strip;
}

/* ------------------------------------------------------------------ toasts */
let toastSeq = 0;
export function toast(message, tone = "", ttl = 4200) {
  const stack = document.getElementById("toasts");
  if (!stack) return;
  const id = ++toastSeq;
  const node = h("div", { class: `toast ${tone}`, dataset: { id } });
  node.appendChild(icon(tone === "error" ? "warning" : tone === "warn" ? "warning" : "check"));
  node.appendChild(h("div", { class: "grow", text: message }));
  const close = iconBtn("close", "Dismiss", () => remove());
  node.appendChild(close);
  stack.appendChild(node);
  const timer = setTimeout(remove, ttl);
  function remove() {
    clearTimeout(timer);
    if (!node.isConnected) return;
    node.classList.add("out");
    setTimeout(() => node.remove(), 220);
  }
  return remove;
}

/* ------------------------------------------------------------------ modal */
export function modal({ title, sub = "", body, actions = [], wide = false, narrow = false, flush = false, onClose = null, id = "" }) {
  const root = document.getElementById("modalRoot");
  const backdrop = h("div", { class: "modal-backdrop", role: "presentation" });
  const box = h("div", { class: `modal ${wide ? "wide" : ""} ${narrow ? "narrow" : ""}`.trim(), role: "dialog", "aria-modal": "true", id });
  const head = h("div", { class: "modal-head" });
  const titleWrap = h("div", { class: "grow" }, h("h2", { text: title }));
  if (sub) titleWrap.appendChild(h("div", { class: "sub", text: sub }));
  head.appendChild(titleWrap);
  head.appendChild(iconBtn("close", "Close", close));
  const bodyEl = h("div", { class: `modal-body ${flush ? "flush" : ""}`.trim() });
  append(bodyEl, [body]);
  box.appendChild(head);
  box.appendChild(bodyEl);
  if (actions.length) {
    const foot = h("div", { class: "modal-foot" });
    actions.forEach((a) => foot.appendChild(a));
    box.appendChild(foot);
  }
  backdrop.appendChild(box);
  root.appendChild(backdrop);
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", esc);
    backdrop.remove();
    if (onClose) onClose();
  }
  function esc(e) { if (e.key === "Escape") close(); }
  document.addEventListener("keydown", esc);
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
  const focusable = box.querySelector("input, textarea, select, button");
  setTimeout(() => focusable && focusable.focus(), 50);
  return { close, box, body: bodyEl };
}

export function confirmDialog({ title = "Confirm action", message = "", confirmText = "CONFIRM", danger = false, requireText = "" }) {
  return new Promise((resolve) => {
    const phraseInput = requireText ? input({ placeholder: `Type ${requireText}` }) : null;
    const body = h("div", { class: "col" },
      h("div", { class: "small", text: message }),
      phraseInput ? field(`Type ${requireText} to proceed`, phraseInput) : null);
    let settled = false;
    const finish = (val) => { if (settled) return; settled = true; resolve(val); m.close(); };
    const confirmBtn = btn(confirmText, { tone: danger ? "danger" : "primary", onClick: () => {
      if (requireText && phraseInput.value.trim().toUpperCase() !== requireText.toUpperCase()) {
        toast(`Type ${requireText} exactly to confirm`, "warn");
        phraseInput.focus();
        return;
      }
      finish(true);
    } });
    const m = modal({
      title, narrow: true, body,
      actions: [btn("CANCEL", { tone: "ghost", onClick: () => finish(false) }), confirmBtn],
      onClose: () => { if (!settled) { settled = true; resolve(false); } },
    });
  });
}

/* ------------------------------------------------------------------ format */
export const fmt = {
  date(value, withTime = true) {
    if (!value) return "—";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    const opts = withTime
      ? { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }
      : { year: "numeric", month: "short", day: "2-digit" };
    return d.toLocaleString(undefined, opts);
  },
  time(value) {
    if (!value) return "—";
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  },
  ago(value) {
    if (!value) return "never";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    const s = Math.round((Date.now() - d.getTime()) / 1000);
    if (s < 10) return "just now";
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  },
  bytes(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = Number(n), i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v >= 100 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
  },
  num(n, digits = 1) {
    if (n === null || n === undefined || n === "" || Number.isNaN(Number(n))) return "—";
    return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
  },
  coord(lat, lon, digits = 5) {
    if (lat === null || lon === null || lat === undefined || lon === undefined) return "—";
    return `${Number(lat).toFixed(digits)}, ${Number(lon).toFixed(digits)}`;
  },
  duration(seconds) {
    if (seconds === null || seconds === undefined) return "—";
    const s = Math.max(0, Math.round(seconds));
    const d = Math.floor(s / 86400), hh = Math.floor((s % 86400) / 3600), mm = Math.floor((s % 3600) / 60);
    if (d) return `${d}d ${hh}h`;
    if (hh) return `${hh}h ${mm}m`;
    return `${mm}m ${s % 60}s`;
  },
  pct(n) {
    if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
    return `${Math.round(Number(n))}%`;
  },
};

export function debounce(fn, wait = 250) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

export function loading(label = "LOADING") {
  return h("div", { class: "empty-state" }, h("span", { class: "spinner" }), h("div", { class: "tiny mt-10", text: label }));
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to clipboard");
  } catch {
    const area = h("textarea", { value: text, style: { position: "fixed", opacity: "0" } });
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); toast("Copied to clipboard"); }
    catch { toast("Copy failed", "error"); }
    area.remove();
  }
}

/* ------------------------------------------------------------------ gauge */
export function gauge(score, band = "") {
  const wrap = h("div", { class: "gauge" });
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 200 200");
  const c = 2 * Math.PI * 76;
  const track = document.createElementNS(SVG_NS, "circle");
  track.setAttribute("cx", "100"); track.setAttribute("cy", "100"); track.setAttribute("r", "76");
  track.setAttribute("fill", "none"); track.setAttribute("stroke-width", "8");
  track.setAttribute("class", "track");
  track.setAttribute("stroke-dasharray", `0 ${c}`);
  const ticks = document.createElementNS(SVG_NS, "g");
  ticks.setAttribute("class", "ticks");
  for (let i = 0; i < 36; i++) {
    const line = document.createElementNS(SVG_NS, "line");
    const angle = (i * 10 - 90) * Math.PI / 180;
    const r1 = 88, r2 = i % 3 === 0 ? 96 : 92;
    line.setAttribute("x1", 100 + Math.cos(angle) * r1);
    line.setAttribute("y1", 100 + Math.sin(angle) * r1);
    line.setAttribute("x2", 100 + Math.cos(angle) * r2);
    line.setAttribute("y2", 100 + Math.sin(angle) * r2);
    line.setAttribute("stroke-width", i % 3 === 0 ? "1.5" : "0.7");
    ticks.appendChild(line);
  }
  const value = document.createElementNS(SVG_NS, "circle");
  value.setAttribute("cx", "100"); value.setAttribute("cy", "100"); value.setAttribute("r", "76");
  value.setAttribute("fill", "none"); value.setAttribute("stroke-width", "8");
  value.setAttribute("stroke-linecap", "butt"); value.setAttribute("transform", "rotate(-90 100 100)");
  value.setAttribute("class", "value");
  const pct = Math.max(0, Math.min(100, Number(score) || 0));
  value.setAttribute("stroke-dasharray", `${c * pct / 100} ${c}`);
  svg.appendChild(track); svg.appendChild(ticks); svg.appendChild(value);
  const center = h("div", { class: "gauge-center" },
    h("strong", { text: score === null || score === undefined ? "—" : String(Math.round(score)) }),
    h("span", { text: "READINESS" }),
    h("div", { class: "band", text: band || "" }));
  wrap.appendChild(svg); wrap.appendChild(center);
  return wrap;
}

/* ------------------------------------------------------------------ charts */
export function sparkline(canvas, series, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const w = rect.width, hh = rect.height;
  ctx.clearRect(0, 0, w, hh);
  ctx.strokeStyle = "rgba(93,255,154,.08)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = (hh / 4) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
  }
  const all = series.flatMap((s) => s.data).filter((v) => typeof v === "number" && !Number.isNaN(v));
  if (!all.length) {
    ctx.fillStyle = "rgba(123,154,136,.6)";
    ctx.font = "10px JetBrains Mono, monospace";
    ctx.fillText("NO DATA", 10, hh / 2);
    return;
  }
  let min = Math.min(...all), max = Math.max(...all);
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * 0.12;
  min -= pad; max += pad;
  const n = Math.max(...series.map((s) => s.data.length));
  series.forEach((s) => {
    ctx.beginPath();
    ctx.strokeStyle = s.color || "#5dff9a";
    ctx.lineWidth = s.width || 1.6;
    let started = false;
    s.data.forEach((v, i) => {
      if (typeof v !== "number" || Number.isNaN(v)) return;
      const x = n <= 1 ? 0 : (i / (n - 1)) * (w - 8) + 4;
      const y = hh - 6 - ((v - min) / (max - min)) * (hh - 12);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
  if (opts.labels) {
    ctx.fillStyle = "rgba(123,154,136,.7)";
    ctx.font = "8px JetBrains Mono, monospace";
    ctx.fillText(String(Math.round(max)), 3, 9);
    ctx.fillText(String(Math.round(min)), 3, hh - 3);
  }
}

/* ------------------------------------------------------------------ markdown */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function inlineMarkdown(text) {
  let s = text;
  s = s.replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  return s;
}

export function renderMarkdown(md) {
  const escaped = escapeHtml(String(md || ""));
  const lines = escaped.split(/\r?\n/);
  const out = [];
  let list = null;
  let inCode = false;
  let codeLines = [];
  let inQuote = false;
  let tableRows = null;
  let para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${inlineMarkdown(para.join(" "))}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const flushQuote = () => { if (inQuote) { out.push("</blockquote>"); inQuote = false; } };
  const flushTable = () => {
    if (!tableRows) return;
    const rows = tableRows.filter((r) => !/^\|?[\s:|-]+\|?$/.test(r));
    if (rows.length) {
      const cells = (row) => row.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      out.push("<table><thead><tr>" + cells(rows[0]).map((c) => `<th>${inlineMarkdown(c)}</th>`).join("") + "</tr></thead><tbody>");
      rows.slice(1).forEach((row) => { out.push("<tr>" + cells(row).map((c) => `<td>${inlineMarkdown(c)}</td>`).join("") + "</tr>"); });
      out.push("</tbody></table>");
    }
    tableRows = null;
  };
  for (const raw of lines) {
    const line = raw;
    if (line.startsWith("```")) {
      flushPara(); flushList(); flushQuote(); flushTable();
      if (inCode) { out.push(`<pre><code>${codeLines.join("\n")}</code></pre>`); codeLines = []; inCode = false; }
      else inCode = true;
      continue;
    }
    if (inCode) { codeLines.push(line); continue; }
    if (!line.trim()) { flushPara(); flushList(); flushQuote(); flushTable(); continue; }
    if (/^\|.*\|$/.test(line.trim())) { flushPara(); flushList(); flushQuote(); (tableRows = tableRows || []).push(line.trim()); continue; }
    flushTable();
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { flushPara(); flushList(); flushQuote(); out.push(`<h${heading[1].length}>${inlineMarkdown(heading[2])}</h${heading[1].length}>`); continue; }
    if (/^(---|\*\*\*|___)\s*$/.test(line.trim())) { flushPara(); flushList(); flushQuote(); out.push("<hr>"); continue; }
    if (/^&gt;\s?/.test(line)) { flushPara(); flushList(); if (!inQuote) { out.push("<blockquote>"); inQuote = true; } out.push(`<p>${inlineMarkdown(line.replace(/^&gt;\s?/, ""))}</p>`); continue; }
    flushQuote();
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      flushPara();
      const type = ul ? "ul" : "ol";
      if (list !== type) { flushList(); list = type; out.push(`<${type}>`); }
      out.push(`<li>${inlineMarkdown((ul || ol)[1])}</li>`);
      continue;
    }
    flushList();
    para.push(line.trim());
  }
  flushPara(); flushList(); flushQuote(); flushTable();
  if (inCode) out.push(`<pre><code>${codeLines.join("\n")}</code></pre>`);
  return out.join("\n");
}

export function hydrate(root = document) {
  hydrateIcons(root);
  return root;
}
