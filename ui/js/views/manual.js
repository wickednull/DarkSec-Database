/* Field manual, quick-reference cards and equipment checklists. */
import { api } from "../api.js";
import {
  h, panel, pageHead, btn, input, loading, emptyState, badge, toast, renderMarkdown,
  confirmDialog, tabs, modal, fmt,
} from "../components.js";
import {
  errorBox, printText, downloadText, referenceGrid, openReferenceCard, loadReference,
} from "./shared.js";

export async function render(root, params) {
  root.replaceChildren(loading("LOADING FIELD MANUAL"));
  let index, cards, checklist;
  try {
    [index, cards, checklist] = await Promise.all([
      api.get("/api/manual"),
      loadReference(),
      api.get("/api/checklists"),
    ]);
  } catch (err) {
    root.replaceChildren(pageHead({ kicker: "COMMAND // MANUAL", title: "Field Manual" }), errorBox(err));
    return;
  }

  const docs = (index.docs || []).slice().sort((a, b) => (a.order || 0) - (b.order || 0));
  let activeTab = params.card ? "reference" : (params.tab === "checklists" ? "checklists" : params.tab === "reference" ? "reference" : "manual");
  let activeDoc = params.doc || docs[0]?.id || "";
  let category = "ALL";
  let cardQuery = "";

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "COMMAND // KNOWLEDGE BASE",
    title: "Field Manual",
    desc: "Concise local procedures, verified reference cards and loadout checks. Read the legal and safety limits before acting.",
    actions: [
      btn("PRINT DOCUMENT", { icon: "print", onClick: () => printCurrentDoc() }),
      btn("DOWNLOAD MARKDOWN", { icon: "download", onClick: () => downloadCurrentDoc() }),
    ],
  }));

  const tabBar = tabs([
    { id: "manual", label: "FIELD MANUAL", count: docs.length },
    { id: "reference", label: "REFERENCE CARDS", count: cards.length },
    { id: "checklists", label: "CHECKLISTS", count: (checklist.kits || []).length },
  ], activeTab, (id) => {
    activeTab = id;
    params.card = undefined;
    location.hash = `#/manual?tab=${id}${id === "manual" && activeDoc ? `&doc=${activeDoc}` : ""}`;
  });
  root.appendChild(tabBar);

  const content = h("div", { class: "col" });
  root.appendChild(content);

  function printCurrentDoc() {
    if (activeTab === "manual") {
      const text = content.querySelector(".markdown")?.innerText || "";
      printText(`DARKSEC MANUAL // ${currentDoc()?.title || "FIELD MANUAL"}`, text);
    } else if (activeTab === "reference") {
      printText("DARKSEC // REFERENCE CARDS", cards.map((c) => `## ${c.title} [${c.category}]\n${c.body}`).join("\n\n"));
    } else {
      printText("DARKSEC // CHECKLISTS", (checklist.kits || []).map((kit) => {
        const lines = kit.items.map((it) => `[${stateFor(kit.id, it.id) ? "x" : " "}] ${it.text}`);
        return `## ${kit.name}\n${kit.description || ""}\n${lines.join("\n")}`;
      }).join("\n\n"));
    }
  }

  function downloadCurrentDoc() {
    if (activeTab === "manual") {
      const doc = currentDoc();
      const text = content.querySelector(".markdown")?.innerText || "";
      downloadText(`${doc?.id || "manual"}.txt`, text, "text/plain;charset=utf-8");
    } else if (activeTab === "reference") {
      downloadText("darksec-reference-cards.json", JSON.stringify(cards, null, 2), "application/json");
    } else {
      downloadText("darksec-checklists.json", JSON.stringify(checklist, null, 2), "application/json");
    }
  }

  function currentDoc() {
    return docs.find((d) => d.id === activeDoc);
  }

  function stateFor(kit, item) {
    return !!checklist.state?.[`${kit}:${item}`];
  }

  /* --------------------------------------------------------------- manual */
  function renderManual() {
    content.replaceChildren();
    const layout = h("div", { class: "reader-layout" });
    const nav = h("div", { class: "doc-nav" });
    for (const doc of docs) {
      const b = h("button", { type: "button", class: doc.id === activeDoc ? "active" : "" });
      b.appendChild(h("div", { text: doc.title }));
      b.appendChild(h("div", { class: "tiny dim", text: doc.tagline || "" }));
      b.addEventListener("click", () => {
        activeDoc = doc.id;
        location.hash = `#/manual?tab=manual&doc=${encodeURIComponent(doc.id)}`;
      });
      nav.appendChild(b);
    }
    const reader = h("div", { class: "panel", style: { minHeight: "400px" } });
    const readerBody = h("div", { class: "panel-body" });
    const md = h("div", { class: "markdown" }, loading("OPENING MANUAL"));
    readerBody.appendChild(md);
    reader.appendChild(readerBody);
    layout.append(nav, reader);
    content.appendChild(layout);

    const doc = currentDoc();
    if (!doc) {
      md.replaceChildren(emptyState("No manual documents", "The manual index is empty.", "book"));
      return;
    }
    reader.insertBefore(h("div", { class: "panel-head" },
      h("h2", {}, h("span", { text: doc.title })),
      h("span", { class: "sub", text: doc.tagline || "" }),
      h("div", { class: "panel-tools" },
        btn("PRINT", { size: "sm", icon: "print", onClick: printCurrentDoc }),
        btn("DOWNLOAD", { size: "sm", icon: "download", onClick: downloadCurrentDoc }),
      )), reader.firstChild);

    api.text(`/api/manual/${encodeURIComponent(doc.id)}`).then((text) => {
      md.innerHTML = renderMarkdown(text);
      md.querySelectorAll("a[href]").forEach((a) => {
        a.setAttribute("target", "_blank");
        a.setAttribute("rel", "noopener noreferrer");
      });
    }).catch((err) => {
      md.replaceChildren(errorBox(err));
    });
  }

  /* ------------------------------------------------------------ references */
  function renderReference() {
    content.replaceChildren();
    const cats = ["ALL", ...Array.from(new Set(cards.map((c) => c.category))).sort()];
    const filterRow = h("div", { class: "row wrap gap-6 mb-10" });
    const search = input({ type: "search", placeholder: "Filter cards…", value: cardQuery, autocomplete: "off" });
    search.style.maxWidth = "260px";
    filterRow.appendChild(search);
    const selectCat = h("div", { class: "pill-row" });
    filterRow.appendChild(selectCat);

    const gridWrap = h("div");
    content.append(filterRow, gridWrap);

    function paintCats() {
      selectCat.replaceChildren();
      for (const cat of cats) {
        selectCat.appendChild(btn(cat, {
          size: "sm", tone: cat === category ? "primary" : "ghost",
          onClick: () => { category = cat; paintCats(); paint(); },
        }));
      }
    }

    function paint() {
      const q = search.value.trim().toLowerCase();
      let list = cards;
      if (category !== "ALL") list = list.filter((c) => c.category === category);
      if (q) list = list.filter((c) => `${c.title} ${c.category} ${c.body}`.toLowerCase().includes(q));
      gridWrap.replaceChildren();
      if (!list.length) {
        gridWrap.appendChild(emptyState("No matching cards", "Change the filter or search term.", "search"));
        return;
      }
      gridWrap.appendChild(referenceGrid(list));
    }
    search.addEventListener("input", paint);
    paintCats();
    paint();
  }

  /* ------------------------------------------------------------ checklists */
  function renderChecklists() {
    content.replaceChildren();
    content.appendChild(h("div", { class: "callout small", text: checklist.note || "Quantities are planning baselines. Adjust for your household and climate." }));
    const totalEl = h("div", { class: "row wrap gap-6 mt-10 mb-10" });
    content.appendChild(totalEl);

    for (const kit of checklist.kits || []) {
      const items = kit.items || [];
      const doneCount = () => items.filter((it) => stateFor(kit.id, it.id)).length;
      const head = h("div", { class: "checklist-head" });
      const titleWrap = h("div", {});
      titleWrap.appendChild(h("h3", { text: kit.name }));
      titleWrap.appendChild(h("div", { class: "tiny muted", text: kit.description || "" }));
      head.appendChild(titleWrap);
      const status = h("span", { class: "badge" });
      head.appendChild(status);

      const itemsWrap = h("div", { class: "checklist-items" });
      const updateStatus = () => {
        const n = doneCount();
        status.textContent = `${n}/${items.length} READY`;
        status.className = `badge ${n === items.length ? "green" : n ? "amber" : "muted"}`;
        updateOverall();
      };
      for (const item of items) {
        const checked = stateFor(kit.id, item.id);
        const label = h("label", { class: `check ${checked ? "done" : ""} ${item.critical ? "critical" : ""}`.trim() });
        const cb = input({ type: "checkbox", checked });
        cb.addEventListener("change", async () => {
          label.classList.toggle("done", cb.checked);
          checklist.state[`${kit.id}:${item.id}`] = cb.checked;
          updateStatus();
          try {
            await api.post("/api/checklists/toggle", { kit_id: kit.id, item_id: item.id, checked: cb.checked });
          } catch (err) {
            cb.checked = !cb.checked;
            label.classList.toggle("done", cb.checked);
            checklist.state[`${kit.id}:${item.id}`] = cb.checked;
            updateStatus();
            toast(err.message || "Could not save checklist item", "error");
          }
        });
        label.append(cb, h("span", { text: item.text }));
        itemsWrap.appendChild(label);
      }
      updateStatus();

      const reset = btn("RESET", { size: "sm", tone: "ghost", onClick: async () => {
        if (!(await confirmDialog({ title: `Reset ${kit.name}`, message: "Clear every tick in this kit?", confirmText: "RESET", danger: true }))) return;
        try {
          await api.post("/api/checklists/reset", { kit_id: kit.id });
          for (const item of items) checklist.state[`${kit.id}:${item.id}`] = false;
          toast("Checklist reset");
          renderChecklists();
        } catch (err) { toast(err.message || "Reset failed", "error"); }
      } });
      const print = btn("PRINT", { size: "sm", tone: "ghost", icon: "print", onClick: () => printKit(kit) });
      head.appendChild(h("div", { class: "row gap-4" }, print, reset));

      const box = h("div", { class: "checklist" }, head, itemsWrap);
      box.style.marginBottom = "10px";
      content.appendChild(box);
    }

    function updateOverall() {
      const kits = checklist.kits || [];
      let total = 0, done = 0;
      for (const kit of kits) for (const it of kit.items) { total++; if (stateFor(kit.id, it.id)) done++; }
      totalEl.replaceChildren(
        badge(`${done}/${total} ITEMS READY`, done === total ? "green" : done ? "amber" : "muted"),
        h("span", { class: "small muted", text: "Critical items are marked red. Tick items only after physical verification." }),
      );
    }
    updateOverall();
  }

  function printKit(kit) {
    const lines = kit.items.map((it) => `[${stateFor(kit.id, it.id) ? "x" : " "}] ${it.critical ? "* " : "  "}${it.text}`);
    printText(`DARKSEC // ${kit.name}`, `${kit.description || ""}\n\n${lines.join("\n")}`);
  }

  /* --------------------------------------------------------------- dispatch */
  function paint() {
    if (activeTab === "manual") renderManual();
    else if (activeTab === "reference") renderReference();
    else renderChecklists();
  }
  paint();

  if (params.card) {
    const card = cards.find((c) => c.id === params.card || c.title === params.card);
    if (card) setTimeout(() => openReferenceCard(card), 120);
  }

  return null;
}
