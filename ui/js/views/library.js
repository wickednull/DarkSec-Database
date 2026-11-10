/* Offline document library: metadata, FTS5 search and a bundled PDF.js reader. */
import { api } from "../api.js";
import {
  h, panel, pageHead, btn, input, select, loading, emptyState, toast, fmt, badge, field,
} from "../components.js";
import { errorBox, progressBar, downloadText } from "./shared.js";

let pdfjsPromise = null;

function loadPdfJs() {
  if (!pdfjsPromise) {
    pdfjsPromise = import("/ui/vendor/pdfjs/pdf.min.mjs").then((lib) => {
      lib.GlobalWorkerOptions.workerSrc = "/ui/vendor/pdfjs/pdf.worker.min.mjs";
      return lib;
    }).catch((err) => {
      pdfjsPromise = null;
      throw new Error(`PDF engine unavailable: ${err.message}`);
    });
  }
  return pdfjsPromise;
}

function docIcon(name, ext) {
  return /\.pdf$/i.test(name || ext || "") ? "file" : "book";
}

function sizeLabel(n) {
  return fmt.bytes(n || 0);
}

export async function render(root, params) {
  if (params.doc) return renderDocument(root, params);
  root.replaceChildren(loading("READING LIBRARY INDEX"));
  let stats;
  try {
    stats = await api.get("/api/library/stats");
  } catch (err) {
    root.replaceChildren(pageHead({ kicker: "COMMAND // LIBRARY", title: "Document Library" }), errorBox(err));
    return;
  }
  root.replaceChildren();

  let disposed = false;
  const state = { q: params.q || "", category: params.category || "", items: [], searching: false };
  const searchOut = h("div", { class: "col" });
  const resultMeta = h("span", { class: "panel-head sub" });

  const searchInput = input({
    type: "search", placeholder: "Search titles and full text…", value: state.q,
    autocomplete: "off", spellcheck: false,
  });
  const categorySelect = select(
    [{ value: "", label: "All categories" },
      ...(stats.categories || []).map((c) => ({ value: c.category, label: `${c.category} (${c.count})` }))],
    state.category,
  );

  const searchForm = h("div", { class: "row wrap gap-6" },
    h("div", { class: "search-box grow", style: { minWidth: "220px" } },
      searchInput),
    field("", categorySelect),
    btn("SEARCH", { icon: "search", tone: "primary", onClick: () => runSearch() }),
  );

  async function runSearch() {
    state.q = searchInput.value.trim();
    state.category = categorySelect.value;
    state.searching = true;
    resultMeta.textContent = "SEARCHING…";
    searchOut.replaceChildren(loading("SEARCHING LIBRARY"));
    try {
      const res = await api.get(`/api/library/search?q=${encodeURIComponent(state.q)}&category=${encodeURIComponent(state.category)}&limit=1000`);
      if (disposed) return;
      state.items = res.items || [];
      renderResults();
    } catch (err) {
      if (!disposed) searchOut.replaceChildren(errorBox(err));
    } finally {
      state.searching = false;
    }
  }

  function renderResults() {
    searchOut.replaceChildren();
    const items = state.items;
    resultMeta.textContent = `${items.length} DOCUMENT${items.length === 1 ? "" : "S"}${state.q ? ` // “${state.q}”` : ""}`;
    if (!items.length) {
      searchOut.appendChild(emptyState(
        state.q ? "No matching documents" : "Library index is empty",
        state.q ? "Try fewer words, or index the library for full-text search." : "Run METADATA or DEEP INDEX to populate the catalogue.",
        "library"));
      return;
    }
    const list = h("div", { class: "list" });
    for (const item of items) {
      const row = h("div", { class: "list-item" });
      const main = h("div", { class: "li-main" });
      const title = h("a", { href: `#/library?doc=${item.id}`, class: "li-title", text: item.name, title: "Open document" });
      main.appendChild(title);
      const meta = [
        item.category || "UNFILED",
        sizeLabel(item.size),
        item.chars ? `${fmt.num(item.chars, 0)} chars indexed` : "metadata only",
        item.text_ts ? `text ${fmt.ago(item.text_ts)}` : "",
      ].filter(Boolean).join(" · ");
      main.appendChild(h("div", { class: "li-sub", text: meta }));
      if (item.snippet) {
        const snippet = h("div", { class: "small muted mt-6 wrap-any" });
        const text = String(item.snippet).replace(/\[\[\[/g, "⟦").replace(/\]\]\]/g, "⟧").replace(/\s+/g, " ");
        snippet.textContent = text.slice(0, 340);
        main.appendChild(snippet);
      }
      row.appendChild(main);
      row.appendChild(btn("OPEN", { size: "sm", onClick: () => { location.hash = `#/library?doc=${item.id}`; } }));
      list.appendChild(row);
    }
    searchOut.appendChild(list);
  }

  let timer;
  searchInput.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(runSearch, 260);
  });
  searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") runSearch(); });
  categorySelect.addEventListener("change", runSearch);

  /* --------------------------------------------------------------- header */
  root.appendChild(pageHead({
    kicker: "COMMAND // OFFLINE ARCHIVE",
    title: "Document Library",
    desc: "Local PDF, EPUB and text archive. Full-text extraction runs on this machine; nothing is uploaded.",
    actions: [
      btn("RELOAD", { icon: "refresh", onClick: () => render(root, params) }),
      btn("METADATA INDEX", { icon: "database", onClick: () => startIndex(false) }),
      btn("DEEP INDEX", { icon: "search", tone: "primary", onClick: () => startIndex(true) }),
    ],
  }));

  /* ---------------------------------------------------------------- stats */
  const statsRow = h("div", { class: "grid grid-4" });
  const job = stats.job || {};
  statsRow.append(
    statBox("Documents", fmt.num(stats.documents || 0, 0), `${stats.categories?.length || 0} categories`),
    statBox("Full-text indexed", fmt.num(stats.text_indexed || 0, 0), `${fmt.num(stats.chars || 0, 0)} characters`),
    statBox("Index size", fmt.bytes(stats.db_bytes || 0), "SQLite FTS5"),
    statBox("Indexer", job.running ? "RUNNING" : "IDLE", job.message || "ready", job.running ? "acc" : ""),
  );
  root.appendChild(statsRow);

  /* -------------------------------------------------------------- indexing */
  const jobBox = h("div", { class: "col" });
  const jobPanel = panel({
    title: "Index control", icon: "database",
    sub: "Deep indexing extracts text with pdftotext and can take a long time on a large archive.",
    body: jobBox,
  });
  root.appendChild(jobPanel);

  const bar = progressBar(job.running ? Math.round((job.done / Math.max(1, job.total)) * 100) : (stats.text_indexed ? 100 : 0));
  const jobLine = h("div", { class: "small muted", text: job.running
    ? `${job.message || "working"} · ${fmt.num(job.done, 0)} / ${fmt.num(job.total, 0)} · ${job.current || ""}`
    : `${fmt.num(stats.documents || 0, 0)} documents catalogued. Metadata is fast; deep text extraction is not.` });
  jobBox.append(bar, jobLine);
  const indexActions = h("div", { class: "row wrap mt-10" });
  indexActions.append(
    btn("INDEX METADATA", { size: "sm", onClick: () => startIndex(false) }),
    btn("INDEX FULL TEXT", { size: "sm", tone: "primary", onClick: () => startIndex(true) }),
    btn("FORCE RE-EXTRACT", { size: "sm", tone: "amber", onClick: () => startIndex(true, true) }),
  );
  jobBox.appendChild(indexActions);

  async function startIndex(deep, force = false) {
    try {
      const stateOut = await api.post("/api/library/index", { deep, force });
      toast(`${deep ? "Full-text" : "Metadata"} index started`);
      updateJob(stateOut);
      pollJob();
    } catch (err) {
      toast(err.message || "Could not start index", "error");
    }
  }

  function updateJob(j) {
    if (!j) return;
    const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
    bar.replaceChildren();
    const fill = h("span");
    fill.style.width = `${j.running ? pct : (j.finished ? 100 : 0)}%`;
    bar.appendChild(fill);
    jobLine.textContent = j.running
      ? `${j.message || "working"} · ${fmt.num(j.done, 0)} / ${fmt.num(j.total, 0)} · ${j.current || ""} · ${j.errors || 0} errors`
      : `Indexer ${j.message || "idle"}${j.finished ? ` · finished ${fmt.ago(j.finished)}` : ""}`;
  }

  let pollTimer = null;
  async function pollJob() {
    clearTimeout(pollTimer);
    if (disposed) return;
    try {
      const s = await api.get("/api/library/stats");
      if (disposed) return;
      updateJob(s.job);
      if (s.job?.running) pollTimer = setTimeout(pollJob, 2500);
      else if (s.documents !== stats.documents || s.text_indexed !== stats.text_indexed) {
        toast("Library index updated");
        render(root, params);
      }
    } catch {
      pollTimer = setTimeout(pollJob, 5000);
    }
  }
  if (job.running) pollJob();

  /* --------------------------------------------------------------- search */
  const searchPanel = panel({
    title: "Archive search", icon: "search",
    sub: "FTS5 supports words and prefixes. Search finds filenames even before text extraction.",
    tools: [resultMeta],
    body: h("div", { class: "col" }, searchForm, searchOut),
  });
  root.appendChild(searchPanel);

  /* ------------------------------------------------------------ categories */
  if ((stats.categories || []).length) {
    const catPanel = panel({ title: "Holdings by category", icon: "layers", body: h("div", { class: "row wrap gap-6" }) });
    for (const cat of stats.categories) {
      catPanel.querySelector(".panel-body").appendChild(
        btn(`${cat.category} · ${cat.count}`, {
          size: "sm", tone: "ghost",
          onClick: () => { categorySelect.value = cat.category; state.q = ""; searchInput.value = ""; runSearch(); searchPanel.scrollIntoView({ behavior: "smooth" }); },
        }),
      );
    }
    root.appendChild(catPanel);
  }

  await runSearch();

  return () => {
    disposed = true;
    clearTimeout(timer);
    clearTimeout(pollTimer);
  };
}

function statBox(label, value, foot, tone = "") {
  const box = h("div", { class: `stat ${tone}`.trim() },
    h("div", { class: "stat-label", text: label }),
    h("div", { class: "stat-value", text: value }),
    h("div", { class: "stat-foot", text: foot }));
  return box;
}

/* ------------------------------------------------------------------ reader */

async function renderDocument(root, params) {
  const docId = Number(params.doc);
  if (!Number.isFinite(docId) || docId <= 0) {
    root.replaceChildren(pageHead({ kicker: "COMMAND // LIBRARY", title: "Document" }),
      h("div", { class: "callout danger", text: "Invalid document id." }));
    return;
  }
  root.replaceChildren(loading("LOADING DOCUMENT RECORD"));
  let doc;
  try {
    doc = await api.get(`/api/library/doc/${docId}`);
  } catch (err) {
    root.replaceChildren(pageHead({ kicker: "COMMAND // LIBRARY", title: "Document" }),
      h("div", { class: "panel" }, h("div", { class: "panel-body" },
        errorBox(err), h("div", { class: "row mt-10" }, btn("BACK TO LIBRARY", { onClick: () => { location.hash = "#/library"; } })))));
    return;
  }

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: `COMMAND // LIBRARY // ${doc.category || "UNFILED"}`,
    title: doc.name,
    desc: `${doc.rel_path || doc.path} · ${fmt.bytes(doc.size)} · ${doc.chars ? `${fmt.num(doc.chars, 0)} characters indexed` : "no extracted text"}`,
    actions: [
      btn("BACK", { icon: "chevron-left", onClick: () => { location.hash = "#/library"; } }),
      btn("DOWNLOAD", { icon: "download", onClick: () => { location.href = `/api/library/doc/${docId}/file?download=true`; } }),
      btn("OPEN RAW", { icon: "link", onClick: () => window.open(`/api/library/doc/${docId}/file`, "_blank", "noopener") }),
    ],
  }));

  const meta = panel({
    title: "Document metadata", icon: "info",
    body: h("div", { class: "grid grid-2" },
      kvLine("File", doc.name),
      kvLine("Category", doc.category || "—"),
      kvLine("Size", fmt.bytes(doc.size)),
      kvLine("Text indexed", doc.text_available ? "YES" : "NO"),
      kvLine("Relative path", doc.rel_path || "—"),
      kvLine("Modified", doc.mtime ? new Date(doc.mtime * 1000).toLocaleString() : "—"),
    ),
  });

  const isPdf = /\.pdf$/i.test(doc.name || doc.path || "");
  const isTexty = /\.(txt|md|html|htm)$/i.test(doc.name || doc.path || "");

  if (isPdf) {
    const viewer = await pdfViewer(docId, doc);
    root.appendChild(viewer.node);
    root.appendChild(meta);
    return viewer.cleanup;
  }

  root.appendChild(meta);
  if (isTexty && doc.text_available) {
    const pre = h("pre", { class: "markdown", style: { whiteSpace: "pre-wrap", maxHeight: "72vh", overflow: "auto" } });
    const body = panel({ title: "Document text", icon: "file", body: pre, flush: false });
    root.appendChild(body);
    try {
      const text = await api.text(`/api/library/doc/${docId}/text`);
      pre.textContent = String(text || "(empty)");
    } catch (err) {
      pre.textContent = `Could not load text: ${err.message}`;
    }
    return;
  }

  const body = panel({ title: "Preview not available", icon: "file", body: h("div", { class: "col" },
    h("div", { class: "small muted", text: "This format is not rendered in the console. The original file is still available offline." }),
    h("div", { class: "row wrap mt-10" },
      btn("DOWNLOAD ORIGINAL", { icon: "download", tone: "primary", onClick: () => { location.href = `/api/library/doc/${docId}/file?download=true`; } }),
      btn("OPEN IN BROWSER", { icon: "link", onClick: () => window.open(`/api/library/doc/${docId}/file`, "_blank", "noopener") }),
    ),
    doc.text_available ? h("div", { class: "callout mt-10 small", text: "Extracted text is available. Use the archive search to read excerpts, or download the original." }) : null,
  ) });
  root.appendChild(body);
  return null;
}

function kvLine(k, v) {
  return h("div", { class: "coord-line" }, h("span", { text: k }), h("b", { text: v }));
}

async function pdfViewer(docId, doc) {
  const canvas = h("canvas");
  const stage = h("div", { class: "pdf-stage" }, h("div", { class: "pdf-loading" }, h("span", { class: "spinner" }), " LOADING PDF"));
  const pageInfo = h("span", { class: "pdf-page-info", text: "— / —" });
  const title = h("span", { class: "small truncate grow", text: doc.name || "Document" });

  const prev = btn("PREV", { size: "sm", icon: "chevron-left" });
  const next = btn("NEXT", { size: "sm", icon: "chevron-right" });
  const pageInput = input({ type: "number", min: "1", value: "1", style: { width: "58px" } });
  const zoomOut = btn("−", { size: "sm", title: "Zoom out" });
  const zoomIn = btn("+", { size: "sm", title: "Zoom in" });
  const rotLeft = btn("↺", { size: "sm", title: "Rotate left" });
  const rotRight = btn("↻", { size: "sm", title: "Rotate right" });
  const fit = btn("FIT WIDTH", { size: "sm", tone: "ghost" });
  const fitPage = btn("FIT PAGE", { size: "sm", tone: "ghost" });
  const zoomSelect = select([
    { value: "fit", label: "Fit width" },
    { value: "page", label: "Fit page" },
    { value: "75", label: "75%" },
    { value: "100", label: "100%" },
    { value: "125", label: "125%" },
    { value: "150", label: "150%" },
    { value: "200", label: "200%" },
  ], "fit");

  const fsBtn = btn("FULL SCREEN", { size: "sm", icon: "maximize", onClick: async () => { shell.classList.toggle("pdf-fs", !shell.classList.contains("pdf-fs")); } });
  const toolbar = h("div", { class: "pdf-toolbar" },
    prev, next, pageInput, pageInfo, h("span", { class: "grow" }), title,
    rotLeft, rotRight, zoomOut, zoomIn, fit, fitPage, zoomSelect,
    fsBtn,
    btn("DOWNLOAD", { size: "sm", icon: "download", onClick: () => { location.href = `/api/library/doc/${docId}/file?download=true`; } }),
  );
  const shell = h("div", { class: "pdf-viewer" }, toolbar, stage);
  const wrap = h("div", { class: "panel mt-14" }, shell);

  let pdf = null;
  let pageNum = 1;
  let zoom = 1.2;
  let fitMode = true;
  let fitType = "width";
  let rotation = 0;
  let renderTask = null;
  let disposed = false;

  try {
    const lib = await loadPdfJs();
    if (disposed) return { node: wrap, cleanup: () => {} };
    const task = lib.getDocument({
      url: `/api/library/doc/${docId}/file`,
      withCredentials: true,
      cMapUrl: "/ui/vendor/pdfjs/cmaps/",
      cMapPacked: true,
      standardFontDataUrl: "/ui/vendor/pdfjs/standard_fonts/",
    });
    pdf = await task.promise;
    stage.replaceChildren(canvas);
    updateControls();
    await draw();
  } catch (err) {
    stage.replaceChildren(h("div", { class: "callout danger", text: `Could not render PDF: ${err.message || err}` }));
    return { node: wrap, cleanup: () => { disposed = true; try { pdf?.destroy(); } catch {} } };
  }

  function updateControls() {
    if (!pdf) return;
    prev.disabled = pageNum <= 1;
    next.disabled = pageNum >= pdf.numPages;
    pageInput.max = String(pdf.numPages);
    pageInput.value = String(pageNum);
  }

  async function draw() {
    if (!pdf || disposed) return;
    try {
      if (renderTask) { try { renderTask.cancel(); } catch {} renderTask = null; }
      const page = await pdf.getPage(pageNum);
      const base = page.getViewport({ scale: 1, rotation });
      let scale = zoom;
      if (fitMode) {
        const width = Math.max(200, stage.clientWidth - 26);
        const height = Math.max(200, stage.clientHeight - 26);
        if (fitType === "page") scale = Math.max(0.2, Math.min(4, Math.min(width / base.width, height / base.height)));
        else scale = Math.max(0.2, Math.min(4, width / base.width));
      }
      const viewport = page.getViewport({ scale, rotation });
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(viewport.width * dpr));
      canvas.height = Math.max(1, Math.floor(viewport.height * dpr));
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      const ctx = canvas.getContext("2d");
      ctx.save();
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      renderTask = page.render({
        canvasContext: ctx,
        viewport,
        transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null,
      });
      await renderTask.promise;
      renderTask = null;
      pageInfo.textContent = `${pageNum} / ${pdf.numPages} · ${Math.round(scale * 100)}%`;
      updateControls();
    } catch (err) {
      if (err?.name !== "RenderingCancelledException" && !disposed) {
        pageInfo.textContent = "RENDER ERROR";
        toast(err.message || "PDF render failed", "error");
      }
    }
  }

  prev.addEventListener("click", () => { if (pageNum > 1) { pageNum--; draw(); } });
  next.addEventListener("click", () => { if (pdf && pageNum < pdf.numPages) { pageNum++; draw(); } });
  pageInput.addEventListener("change", () => {
    const n = parseInt(pageInput.value, 10);
    if (pdf && Number.isFinite(n) && n >= 1 && n <= pdf.numPages) { pageNum = n; draw(); }
    else pageInput.value = String(pageNum);
  });
  zoomOut.addEventListener("click", () => { fitMode = false; zoom = Math.max(0.25, zoom * 0.82); zoomSelect.value = "custom"; draw(); });
  zoomIn.addEventListener("click", () => { fitMode = false; zoom = Math.min(5, zoom * 1.22); zoomSelect.value = "custom"; draw(); });
  rotLeft.addEventListener("click", () => { rotation = (rotation + 270) % 360; fitMode = false; draw(); });
  rotRight.addEventListener("click", () => { rotation = (rotation + 90) % 360; fitMode = false; draw(); });
  fit.addEventListener("click", () => { fitMode = true; fitType = "width"; zoomSelect.value = "fit"; draw(); });
  fitPage.addEventListener("click", () => { fitMode = true; fitType = "page"; zoomSelect.value = "page"; draw(); });
  zoomSelect.addEventListener("change", () => {
    const v = zoomSelect.value;
    if (v === "fit") { fitMode = true; fitType = "width"; draw(); return; }
    if (v === "page") { fitMode = true; fitType = "page"; draw(); return; }
    fitMode = false; zoom = (parseInt(v, 10) || 100) / 100; draw();
  });

  const keyHandler = (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    if (e.key === "ArrowLeft" || e.key === "PageUp") { if (pageNum > 1) { e.preventDefault(); pageNum--; draw(); } }
    else if (e.key === "ArrowRight" || e.key === "PageDown") { if (pdf && pageNum < pdf.numPages) { e.preventDefault(); pageNum++; draw(); } }
  };
  document.addEventListener("keydown", keyHandler);
  const onResize = () => { if (fitMode) draw(); };
  window.addEventListener("resize", onResize);

  return {
    node: wrap,
    cleanup() {
      disposed = true;
      shell.classList.remove("pdf-fs");
      document.removeEventListener("keydown", keyHandler);
      window.removeEventListener("resize", onResize);
      try { renderTask?.cancel(); } catch {}
      try { pdf?.destroy(); } catch {}
    },
  };
}
