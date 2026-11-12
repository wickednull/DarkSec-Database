/* Security & vault: encrypted notes, incidents, OPSEC posture and destructive purge. */
import { api } from "../api.js";
import { store } from "../store.js";
import {
  h, panel, stat, badge, btn, pageHead, alertStrip, emptyState, loading, fmt, toast, tabs,
  field, input, textarea, checkbox, modal, confirmDialog,
} from "../components.js";
import { openRecordForm, deleteRecord, recordTable, rowActions, severityTone } from "../records.js";
import { kv, referenceSection, severityBadge, printText } from "./shared.js?v=opsec-1";

let lastTab = "vault";
let selectedNoteId = null;
let pendingNewNote = false;
let currentRoot = null;

function rerender(tab = lastTab) {
  lastTab = tab;
  if (currentRoot) render(currentRoot, { tab });
}

export async function render(root, params = {}) {
  currentRoot = root;
  const tab = ["vault", "incidents", "opsec", "purge"].includes(params.tab) ? params.tab : lastTab;
  lastTab = tab;
  root.replaceChildren(loading("CHECKING SECURITY POSTURE"));
  let vault, notesOut, incidents, audit;
  try {
    [vault, notesOut, incidents, audit] = await Promise.all([
      api.get("/api/vault/status"),
      api.get("/api/notes").catch(() => ({ items: [], vault: {} })),
      api.get("/api/records/incidents?limit=300"),
      api.get("/api/audit?limit=60"),
    ]);
    vault = notesOut.vault || vault;
  } catch (err) {
    root.replaceChildren(h("div", { class: "callout danger", text: err.message }));
    return;
  }
  const notes = notesOut.items || [];
  const incidentItems = incidents.items || [];
  const openIncidents = incidentItems.filter((i) => !i.resolved);
  const auditItems = audit.items || [];

  root.replaceChildren();
  root.appendChild(pageHead({
    kicker: "OPERATIONS // PROTECTIVE SECURITY",
    title: "Security & Vault",
    desc: "Encrypted local notes, incident tracking, OPSEC controls and a deliberate, phrase-gated purge. Everything stays on this machine.",
    actions: [
      vault.unlocked ? btn("LOCK VAULT", { icon: "lock", tone: "amber", onClick: () => lockVault(root) }) : null,
      btn("NEW INCIDENT", { icon: "warning", onClick: () => addIncident(root) }),
      btn("SECURITY MANUAL", { icon: "book", tone: "ghost", onClick: () => { location.hash = "#/manual?doc=security"; } }),
    ].filter(Boolean),
  }));

  root.appendChild(alertStrip(
    "Security warning: encryption protects notes only while the vault is locked and the machine is trusted. A compromised host, screen capture, keylogger or weak passphrase defeats it. "
    + "This application does not provide duress codes, remote wipe, plausible deniability or secure erase. Keep independent backups.",
    "",
  ));

  const stats = h("div", { class: "grid grid-4 mt-14" });
  stats.appendChild(stat("Vault", !vault.configured ? "NOT SET" : vault.unlocked ? "UNLOCKED" : "LOCKED",
    vault.configured ? "manual lock only — no idle auto-lock" : "AES-256-GCM not yet configured",
    vault.unlocked ? "warn" : ""));
  stats.appendChild(stat("Encrypted notes", String(notes.length).padStart(2, "0"), vault.unlocked ? "decrypted in memory" : "ciphertext at rest"));
  stats.appendChild(stat("Open incidents", String(openIncidents.length).padStart(2, "0"), "unresolved local reports", openIncidents.length ? "warn" : ""));
  stats.appendChild(stat("Audit events", String(auditItems.length).padStart(2, "0"), "most recent local actions"));
  root.appendChild(stats);

  root.appendChild(h("div", { class: "row between mt-14" },
    tabs([
      { id: "vault", label: "VAULT" },
      { id: "incidents", label: "INCIDENTS", count: openIncidents.length },
      { id: "opsec", label: "OPSEC" },
      { id: "purge", label: "PURGE" },
    ], tab, (id) => { lastTab = id; render(root, { tab: id }); })));

  if (tab === "vault") vaultSection(root, vault, notes);
  if (tab === "incidents") incidentsSection(root, incidentItems, openIncidents);
  if (tab === "opsec") await opsecSection(root);
  if (tab === "purge") purgeSection(root);

  root.appendChild(panel({
    title: "Recent audit trail",
    sub: "Local accountability log · newest first",
    icon: "terminal",
    cls: "span-2 mt-14",
    body: recordTable(auditItems, [
      { label: "TIME", key: "ts", render: (r) => fmt.date(r.ts) },
      { label: "ACTION", key: "action", render: (r) => h("span", { class: "mono", text: r.action || "—" }) },
      { label: "DETAIL", key: "detail", render: (r) => h("span", { class: "small", text: r.detail || "—" }) },
    ], { empty: "No audit events", emptyIcon: "terminal" }),
  }));

  const onVaultUnlocked = () => { try { rerender(lastTab); } catch {} };
  window.addEventListener("darksec:vault-unlocked", onVaultUnlocked);

  return () => {
    window.removeEventListener("darksec:vault-unlocked", onVaultUnlocked);
  };
}

/* ------------------------------------------------------------------ vault */
function vaultSection(root, vault, notes) {
  if (!vault.configured) return setupVault(root);
  if (!vault.unlocked) return unlockVault(root, vault);
  if (!notes.some((n) => n.id === selectedNoteId)) {
    selectedNoteId = notes[0]?.id || null;
    pendingNewNote = !notes.length;
  }

  const grid = h("div", { class: "panel-grid grid-3 mt-14" });
  root.appendChild(grid);

  const side = h("div", { class: "col" });
  const toolbar = h("div", { class: "row wrap" });
  toolbar.append(
    btn("NEW", { size: "sm", icon: "plus", tone: "primary", onClick: () => {
      pendingNewNote = true;
      selectedNoteId = null;
      rerender("vault");
    } }),
    btn("LOCK", { size: "sm", icon: "lock", onClick: () => lockVault(root) }),
    btn("PASSPHRASE", { size: "sm", icon: "key", onClick: () => changePassphrase(root) }),
  );
  side.appendChild(toolbar);
  const list = h("div", { class: "note-list" });
  if (!notes.length) {
    list.appendChild(h("div", { class: "small muted", text: "No encrypted notes yet." }));
  }
  for (const note of notes) {
    const item = h("button", { type: "button", class: `note-item ${note.id === selectedNoteId && !pendingNewNote ? "active" : ""}` });
    item.appendChild(h("div", { class: "ni-title", text: note.title || "UNTITLED" }));
    item.appendChild(h("div", { class: "ni-sub", text: [note.tags, fmt.ago(note.updated)].filter(Boolean).join(" · ") }));
    item.addEventListener("click", () => {
      selectedNoteId = note.id;
      pendingNewNote = false;
      rerender("vault");
    });
    list.appendChild(item);
  }
  side.appendChild(list);
  side.appendChild(h("div", { class: "hint mt-10",
    text: "Notes are encrypted with AES-256-GCM. The key exists only in the backend process memory and is discarded on manual lock or shutdown." }));
  grid.appendChild(panel({ title: "Encrypted notes", sub: `${notes.length} stored`, icon: "lock", body: side }));

  const current = notes.find((n) => n.id === selectedNoteId) || null;
  const editorBody = h("div", { class: "col" });
  const title = input({ type: "text", value: pendingNewNote ? "" : current?.title || "", placeholder: "Note title", maxlength: "300" });
  const tags = input({ type: "text", value: pendingNewNote ? "" : current?.tags || "", placeholder: "tags, comma separated" });
  const body = textarea({ value: pendingNewNote ? "" : current?.body || "", rows: 14, placeholder: "Encrypted operator note…" });
  editorBody.append(
    field("TITLE", title),
    field("TAGS", tags),
    field("BODY", body),
    h("div", { class: "row between wrap mt-6" },
      h("span", { class: "tiny muted", text: current ? `Updated ${fmt.date(current.updated)}` : "New note — not yet saved" }),
      h("div", { class: "row" },
        current ? btn("DELETE", { size: "sm", tone: "danger", onClick: () => deleteNote(current, root) }) : null,
        btn("SAVE NOTE", { size: "sm", tone: "primary", icon: "save", onClick: saveNote }))),
  );
  grid.appendChild(panel({
    title: current ? (current.title || "Untitled note") : "New encrypted note",
    sub: current ? `Note #${current.id}` : "Plaintext exists only until saved",
    icon: "note",
    cls: "span-2",
    body: editorBody,
  }));

  async function saveNote() {
    if (!title.value.trim()) { toast("A note title is required", "warn"); title.focus(); return; }
    try {
      if (current) {
        await api.put(`/api/notes/${current.id}`, { title: title.value, body: body.value, tags: tags.value });
        toast("Note updated and encrypted");
      } else {
        const out = await api.post("/api/notes", { title: title.value, body: body.value, tags: tags.value });
        selectedNoteId = out.id;
        pendingNewNote = false;
        toast("Note encrypted and stored");
      }
      rerender("vault");
    } catch (err) { toast(err.message || "Could not save note", "error"); }
  }
}

function setupVault(root) {
  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);
  const body = h("div", { class: "col" });
  body.appendChild(h("div", { class: "callout small",
    text: "Create a strong passphrase. There is no recovery mechanism and no backdoor. If it is lost, encrypted notes cannot be recovered." }));
  const pass = input({ type: "password", autocomplete: "new-password", minlength: "8" });
  const confirm = input({ type: "password", autocomplete: "new-password", minlength: "8" });
  body.append(field("PASSPHRASE (min 8 characters)", pass), field("CONFIRM PASSPHRASE", confirm));
  const go = btn("CREATE VAULT", { tone: "primary", icon: "lock", onClick: async () => {
    if (pass.value.length < 8) { toast("Passphrase must be at least 8 characters", "warn"); return; }
    if (pass.value !== confirm.value) { toast("Passphrases do not match", "warn"); return; }
    go.disabled = true;
    try {
      await api.post("/api/vault/setup", { passphrase: pass.value });
      toast("Encrypted vault created");
      root.dispatchEvent(new CustomEvent("darksec:refresh"));
      rerender("vault");
      try { await store.loadStatus(); } catch {}
    } catch (err) { toast(err.message || "Vault setup failed", "error"); }
    finally { go.disabled = false; }
  } });
  body.appendChild(h("div", { class: "row mt-10" }, go));
  body.appendChild(h("div", { class: "hint", text: "Use a long, unique passphrase — ideally a multi-word phrase plus a random component. Do not reuse a password from another service." }));
  grid.appendChild(panel({ title: "Create encrypted vault", sub: "One-way setup", icon: "key", body }));
  const info = h("div", { class: "col" });
  info.appendChild(kv([
    ["Cipher", "AES-256-GCM"],
    ["KDF", "PBKDF2-HMAC-SHA256 · 600,000 iterations"],
    ["Key lifetime", "RAM only; dropped on manual lock or shutdown"],
    ["Threat model", "Data at rest on a powered-off or locked machine"],
    ["Not covered", "Compromised OS, keyloggers, screen capture, memory forensics, weak passphrase"],
  ]));
  info.appendChild(h("div", { class: "callout warn small mt-10", text: "The vault does not encrypt the document library, logs or recordings. Use full-disk encryption and physical security." }));
  grid.appendChild(panel({ title: "Crypto posture", sub: "Know the limits", icon: "shield", body: info }));
}

function unlockVault(root, vault) {
  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);
  const body = h("div", { class: "col" });
  const pass = input({ type: "password", autocomplete: "current-password", placeholder: "Vault passphrase" });
  const go = btn("UNLOCK", { tone: "primary", icon: "unlock", onClick: submit });
  body.append(h("div", { class: "vault-locked" }, h("div", { class: "ic" }), h("strong", { text: "VAULT LOCKED" }),
    h("div", { class: "small muted", text: "Enter the passphrase to decrypt notes into memory." })), field("PASSPHRASE", pass), h("div", { class: "row mt-10" }, go));
  pass.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  async function submit() {
    if (!pass.value) { toast("Enter the vault passphrase", "warn"); return; }
    go.disabled = true;
    try {
      await api.post("/api/vault/unlock", { passphrase: pass.value });
      toast("Vault unlocked");
      try { await store.loadStatus(); } catch {}
      rerender("vault");
    } catch (err) {
      toast(err.message || "Unlock failed", "error");
      pass.value = "";
      pass.focus();
    } finally { go.disabled = false; }
  }
  grid.appendChild(panel({ title: "Unlock vault", sub: "Manual unlock — stays open until you lock it", icon: "lock", body }));

  const info = h("div", { class: "col" });
  info.appendChild(h("div", { class: "callout small", text: "Failed attempts are written to the audit log. The vault stays open after unlock until you manually lock it or the app exits." }));
  info.appendChild(kv([
    ["Status", "LOCKED"],
    ["Notes", "ciphertext only"],
    ["Locking", "Manual only — no idle auto-lock"],
    ["Recovery", "None — passphrase is the only key"],
  ]));
  grid.appendChild(panel({ title: "Vault state", icon: "shield", body: info }));
}

async function lockVault(root) {
  try {
    await api.post("/api/vault/lock", {});
    toast("Vault locked; key cleared from memory");
    try { await store.loadStatus(); } catch {}
    selectedNoteId = null;
    pendingNewNote = false;
    rerender("vault");
  } catch (err) { toast(err.message || "Could not lock vault", "error"); }
}

function changePassphrase(root) {
  const old = input({ type: "password", autocomplete: "current-password" });
  const next = input({ type: "password", autocomplete: "new-password", minlength: "8" });
  const confirm = input({ type: "password", autocomplete: "new-password" });
  const body = h("div", { class: "col" },
    h("div", { class: "callout warn small", text: "Changing the passphrase re-wraps the vault key. Existing notes remain readable after the change. There is still no recovery if the new passphrase is lost." }),
    field("CURRENT PASSPHRASE", old), field("NEW PASSPHRASE", next), field("CONFIRM NEW PASSPHRASE", confirm));
  const m = modal({
    title: "Change vault passphrase", narrow: true, body,
    actions: [btn("CANCEL", { tone: "ghost", onClick: () => m.close() }), btn("CHANGE", { tone: "primary" })],
  });
  const go = m.box.querySelector(".modal-foot .btn-primary");
  go.addEventListener("click", async () => {
    if (next.value.length < 8) { toast("New passphrase must be at least 8 characters", "warn"); return; }
    if (next.value !== confirm.value) { toast("New passphrases do not match", "warn"); return; }
    go.disabled = true;
    try {
      await api.post("/api/vault/change", { old: old.value, new: next.value });
      toast("Passphrase changed");
      m.close();
      rerender("vault");
    } catch (err) { toast(err.message || "Passphrase change failed", "error"); }
    finally { go.disabled = false; }
  });
}

async function deleteNote(note, root) {
  const ok = await confirmDialog({
    title: "Delete encrypted note", confirmText: "DELETE", danger: true,
    message: `Permanently delete “${note.title || "Untitled"}”? The ciphertext will be removed from the local database.`,
  });
  if (!ok) return;
  try {
    await api.del(`/api/notes/${note.id}`);
    toast("Note deleted");
    selectedNoteId = null;
    pendingNewNote = false;
    rerender("vault");
  } catch (err) { toast(err.message || "Delete failed", "error"); }
}

/* -------------------------------------------------------------- incidents */
function incidentsSection(root, items, open) {
  const grid = h("div", { class: "panel-grid grid-3 mt-14" });
  root.appendChild(grid);
  const counts = { CRITICAL: 0, WARNING: 0, INFO: 0 };
  for (const i of open) {
    const key = String(i.severity || "INFO").toUpperCase();
    counts[key] = (counts[key] || 0) + 1;
  }
  const summary = h("div", { class: "col" });
  summary.appendChild(kv([
    ["Open", String(open.length)],
    ["Critical", String(counts.CRITICAL || 0)],
    ["Warning", String(counts.WARNING || 0)],
    ["Info", String(counts.INFO || 0)],
    ["Total", String(items.length)],
  ]));
  summary.appendChild(h("div", { class: "row wrap mt-10" }, btn("NEW INCIDENT", { size: "sm", tone: "primary", icon: "plus", onClick: () => addIncident(root) })));
  grid.appendChild(panel({ title: "Incident posture", sub: "Unresolved reports", icon: "warning", body: summary }));

  const critical = open.filter((i) => String(i.severity).toUpperCase() === "CRITICAL");
  const focus = h("div", { class: "col" });
  if (!critical.length) focus.appendChild(emptyState("No critical incidents open", "Continue reporting suspicious activity and hazards.", "shield"));
  for (const item of critical.slice(0, 5)) {
    const card = h("div", { class: "device-card" });
    card.appendChild(h("div", { class: "row between" }, h("div", { class: "dev-name", text: item.title }), severityBadge(item.severity)));
    card.appendChild(h("div", { class: "small muted mt-6", text: `${fmt.date(item.ts)} · ${item.category || "UNCATEGORISED"}` }));
    if (item.detail) card.appendChild(h("div", { class: "small mt-6", text: item.detail }));
    card.appendChild(btn("MARK RESOLVED", { size: "sm", icon: "check", onClick: () => resolveIncident(item, root) }));
    focus.appendChild(card);
  }
  grid.appendChild(panel({ title: "Critical focus", sub: "Resolve or escalate", icon: "target", body: focus }));

  const guidance = h("div", { class: "col" });
  for (const [title, text] of [
    ["REPORT", "Record time, place, people, vehicles, direction of travel and what changed. Keep it factual; separate observation from inference."],
    ["PRESERVE", "Do not touch unknown devices or tamper with a scene. Photograph safely from a distance if it does not increase risk."],
    ["ESCALATE", "Use the appropriate emergency channel for imminent violence, fire, medical or hazardous-material emergencies. This app does not dispatch help."],
    ["PRIVACY", "Incident records are plaintext on disk. Avoid storing sensitive personal data you do not need."],
  ]) {
    const row = h("div", { class: "device-card" });
    row.appendChild(h("div", { class: "dev-name", text: title }));
    row.appendChild(h("div", { class: "small muted mt-6", text }));
    guidance.appendChild(row);
  }
  grid.appendChild(panel({ title: "Incident discipline", icon: "shield", body: guidance }));

  root.appendChild(panel({
    title: "Incident log",
    sub: `${items.length} total · ${open.length} open`,
    icon: "note",
    cls: "span-2 mt-14",
    tools: [btn("NEW", { size: "sm", icon: "plus", onClick: () => addIncident(root) })],
    body: recordTable(items, [
      { label: "TIME", key: "ts", render: (r) => fmt.date(r.ts) },
      { label: "SEVERITY", key: "severity", render: (r) => severityBadge(r.severity) },
      { label: "TITLE", key: "title" },
      { label: "CATEGORY", key: "category" },
      { label: "LOCATION", key: "lat", render: (r) => r.lat != null && r.lon != null ? fmt.coord(r.lat, r.lon, 4) : "—" },
      { label: "STATUS", key: "resolved", render: (r) => badge(r.resolved ? "RESOLVED" : "OPEN", r.resolved ? "green" : "amber") },
      { label: "", key: "actions", render: (r) => rowActions({
        onEdit: () => openRecordForm({ table: "incidents", item: r, onSaved: () => rerender("incidents") }),
        onDelete: () => deleteRecord("incidents", r.id, r.title || "incident", () => rerender("incidents")),
        extra: [btn(r.resolved ? "REOPEN" : "RESOLVE", { size: "sm", tone: "ghost", onClick: () => resolveIncident(r, root) })],
      }) },
    ], { empty: "No incidents logged", emptyIcon: "shield" }),
    foot: "Incident records are plaintext. Use the encrypted vault for sensitive notes.",
  }));
}

async function resolveIncident(item, root) {
  try {
    await api.patch(`/api/records/incidents/${item.id}`, { resolved: !item.resolved });
    toast(item.resolved ? "Incident reopened" : "Incident resolved");
    rerender("incidents");
  } catch (err) { toast(err.message || "Could not update incident", "error"); }
}

function addIncident(root) {
  const s = store.settings || {};
  openRecordForm({
    table: "incidents",
    defaults: { severity: "WARNING", lat: s.lat, lon: s.lon },
    onSaved: () => rerender("incidents"),
  });
}

/* ------------------------------------------------------------------ OPSEC */
async function opsecSection(root) {
  const settings = store.settings || {};
  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);

  const controls = h("div", { class: "col" });
  for (const [key, label, help] of [
    ["opsec_mode", "OPSEC mode", "Masks grid/coordinate readouts, the status-bar GPS source, position fields and the map position marker; also disables phone GPS/LAN access and pauses feed refresh. It does not hide open windows, the map itself, or encrypt the screen."],
    ["scanlines", "CRT scanlines", "Display effect only; turn off for maximum legibility or reduced visual noise."],
    ["sound", "Interface sound", "Reserved for audible cues. Verify device volume before relying on it."],
    ["feed_auto_refresh", "Automatic feed refresh", "When online and OPSEC is off, the console pulls public feeds in the background. Set the cadence in System Settings. Treat all network activity as an emissions risk."],
  ]) {
    const row = h("div", { class: "device-card" });
    const cb = checkbox(label, !!settings[key], async (checked) => {
      row.querySelector(".hint").textContent = "Saving…";
      try {
        await store.saveSettings({ [key]: checked });
        row.querySelector(".hint").textContent = help;
        toast(`${label} ${checked ? "enabled" : "disabled"}`);
      } catch (err) {
        row.querySelector(".hint").textContent = err.message || "Could not save";
        toast(err.message || "Could not save setting", "error");
      }
    });
    row.appendChild(cb);
    row.appendChild(h("div", { class: "hint", text: help }));
    controls.appendChild(row);
  }
  controls.appendChild(h("div", { class: "row wrap mt-10" },
    btn("SYSTEM SETTINGS", { size: "sm", icon: "settings", onClick: () => { location.hash = "#/settings"; } }),
    btn("CHANGE VAULT TIMEOUT", { size: "sm", icon: "clock", onClick: () => { location.hash = "#/settings"; } })));
  grid.appendChild(panel({ title: "Display & posture controls", sub: "Local preferences", icon: "eye", body: controls }));

  const guidance = h("div", { class: "col" });
  for (const [title, text] of [
    ["SCREEN", "Lock the workstation when unattended. Use full-disk encryption. Do not rely on this app to hide a screen from a person standing behind you."],
    ["RADIO", "Every transmission and many receiver local oscillators can be detected. Use the lowest necessary power, directional antennas where lawful, and strict net discipline."],
    ["DATA", "Assume plaintext records, backups and recordings can be read if the device is seized or backed up to an untrusted location."],
    ["MOVEMENT", "Vary routes and timings. Avoid publishing live location. Photographs can leak terrain, reflections and metadata."],
    ["SOCIAL", "Do not discuss stockpiles, routes or vulnerabilities on open channels. Compartment information on a need-to-know basis."],
    ["LEGAL", "Self-defence, detention, search and use of force are governed by local law. This software does not authorise any action."],
  ]) {
    const row = h("div", { class: "device-card" });
    row.appendChild(h("div", { class: "row between" }, h("div", { class: "dev-name", text: title }), badge("OPSEC", "muted")));
    row.appendChild(h("div", { class: "small muted mt-6", text }));
    guidance.appendChild(row);
  }
  grid.appendChild(panel({ title: "Protective measures", sub: "Physical, RF and data", icon: "shield", body: guidance }));

  const refs = await referenceSection("SECURITY");
  if (refs) root.appendChild(panel({ title: "Security field cards", icon: "book", cls: "mt-14", body: refs }));
}

/* ------------------------------------------------------------------ purge */
function purgeSection(root) {
  const grid = h("div", { class: "panel-grid grid-2 mt-14" });
  root.appendChild(grid);

  const operator = h("div", { class: "col" });
  operator.appendChild(h("div", { class: "callout danger",
    text: "OPERATOR PURGE deletes all logs, tasks, patients, vitals, weather, water, power, radiation, waypoints, tracks, radio logs, incidents, inventory, caches, plans, op-orders, feeds, SDR jobs/recordings, notes and the vault. The document library and its index are preserved. There is no undo." }));
  operator.appendChild(kv([
    ["Deleted", "Operator-entered data + vault + recordings"],
    ["Preserved", "PDF library files and search index"],
    ["Confirmation", "Type PURGE"],
    ["Backup", "Create and verify an export first if there is any chance you need it"],
  ]));
  operator.appendChild(h("div", { class: "row mt-10" },
    btn("RUN OPERATOR PURGE", { tone: "danger", icon: "trash", onClick: () => runPurge("operator", root) })));
  grid.appendChild(panel({ title: "Operator purge", sub: "Deny data, keep the books", icon: "shield-off", cls: "alert-panel", body: operator }));

  const everything = h("div", { class: "col" });
  everything.appendChild(h("div", { class: "callout danger",
    text: "FULL PURGE also destroys the library search index. PDF files are not deleted, but search and metadata must be rebuilt, which can take hours for a large library. There is no undo." }));
  everything.appendChild(kv([
    ["Deleted", "Everything in operator purge"],
    ["Also deleted", "Library metadata and full-text index"],
    ["PDF files", "Kept on disk"],
    ["Recovery", "Re-index required"],
  ]));
  everything.appendChild(h("div", { class: "row mt-10" },
    btn("RUN FULL PURGE", { tone: "danger", icon: "trash", onClick: () => runPurge("everything", root) })));
  grid.appendChild(panel({ title: "Full purge", sub: "Last-resort reset", icon: "warning", cls: "alert-panel", body: everything }));

  const advice = h("div", { class: "danger-zone mt-14" });
  advice.appendChild(h("h3", { text: "Before you purge" }));
  advice.appendChild(h("div", { class: "small muted mt-6",
    text: "Purge is for compromise, handover or a deliberate reset — not routine cleanup. It does not securely erase free disk blocks, SSD wear-levelling areas, filesystem journals, snapshots or backups already copied elsewhere. "
      + "For a credible physical-security response, power down, preserve the device for whoever owns the incident, and follow your legal/security plan." }));
  root.appendChild(advice);
}

async function runPurge(mode, root) {
  const label = mode === "everything" ? "FULL PURGE (operator data + library index)" : "OPERATOR PURGE (keep library index)";
  const ok = await confirmDialog({
    title: "Destructive purge",
    message: `${label}. This cannot be undone. Create a backup first if you might need this data.`,
    confirmText: "PURGE",
    danger: true,
    requireText: "PURGE",
  });
  if (!ok) return;
  try {
    const out = await api.post("/api/security/purge", { mode, confirm: "PURGE" });
    toast(`Purge complete${out.library_preserved ? " — library preserved" : ""}`, "warn");
    selectedNoteId = null;
    pendingNewNote = false;
    lastTab = "vault";
    try { await store.loadStatus({ force: true }); } catch {}
    rerender("vault");
  } catch (err) { toast(err.message || "Purge failed", "error"); }
}
