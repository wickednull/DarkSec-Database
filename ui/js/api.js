import { modal, btn, input, field, h, toast } from "./components.js";

const JSON_HEADERS = { "Accept": "application/json" };

function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)darksec_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

function vaultErrorKind(detail) {
  const text = String(detail || "").toLowerCase();
  if (text.includes("vault is locked")) return "locked";
  if (text.includes("vault is not configured")) return "unconfigured";
  return null;
}

export function promptVaultUnlock(detail = "") {
  const kind = vaultErrorKind(detail) || "locked";
  return new Promise((resolve) => {
    let m = null;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      try { m?.close(); } catch {}
      resolve(value);
    };

    if (kind === "unconfigured") {
      const body = h("div", { class: "col" },
        h("div", { class: "small", text: detail || "The encrypted vault is not configured yet." }),
        h("div", { class: "callout small", text: "Set it up in Security → Vault, then retry the save." }));
      m = modal({
        title: "Vault not configured", sub: "A vault is required for encrypted saves", narrow: true, body,
        actions: [
          btn("CANCEL", { tone: "ghost", onClick: () => finish(false) }),
          btn("SET UP VAULT", { tone: "primary", icon: "lock", onClick: () => {
            finish(false);
            location.hash = "#/security?tab=vault";
          } }),
        ],
        onClose: () => finish(false),
      });
      return;
    }

    const pass = input({ type: "password", autocomplete: "current-password", placeholder: "Vault passphrase" });
    const body = h("div", { class: "col" },
      h("div", { class: "small", text: detail || "The vault is locked. Unlock it to continue." }),
      field("PASSPHRASE", pass));
    const unlockBtn = btn("UNLOCK & RETRY", { tone: "primary", icon: "unlock", onClick: () => submit() });
    const unlockLabel = unlockBtn.querySelector("span");
    const setUnlockLabel = (text) => {
      if (unlockLabel) unlockLabel.textContent = text;
      else unlockBtn.textContent = text;
    };
    m = modal({
      title: "Vault locked", sub: "Unlock without leaving this screen", narrow: true, body,
      actions: [
        btn("CANCEL", { tone: "ghost", onClick: () => finish(false) }),
        unlockBtn,
      ],
      onClose: () => finish(false),
    });

    pass.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submit(); }
    });

    async function submit() {
      if (!pass.value) { toast("Enter the vault passphrase", "warn"); pass.focus(); return; }
      unlockBtn.disabled = true;
      setUnlockLabel("UNLOCKING…");
      try {
        const res = await fetch("/api/vault/unlock", {
          method: "POST", credentials: "same-origin",
          headers: { ...JSON_HEADERS, "Content-Type": "application/json", "X-DarkSec-CSRF": csrfToken() },
          body: JSON.stringify({ passphrase: pass.value }),
        });
        if (res.status === 401) window.dispatchEvent(new CustomEvent("darksec:auth"));
        let payload = null;
        try { payload = await res.json(); } catch {}
        if (!res.ok) {
          throw new ApiError(payload?.detail || payload?.error || "Unlock failed", res.status, payload);
        }
        toast("Vault unlocked");
        window.dispatchEvent(new CustomEvent("darksec:vault-unlocked"));
        finish(true);
      } catch (err) {
        toast(err.message || "Unlock failed", "error");
        pass.value = "";
        pass.focus();
      } finally {
        unlockBtn.disabled = false;
        setUnlockLabel("UNLOCK & RETRY");
      }
    }
  });
}

async function request(path, opts = {}) {
  const { method = "GET", body, headers = {}, signal, raw = false, _allowVaultUnlock = true } = opts;
  const reqOpts = { method, credentials: "same-origin", signal, headers: { ...JSON_HEADERS, ...headers } };
  if (body !== undefined) {
    reqOpts.headers["Content-Type"] = "application/json";
    reqOpts.body = JSON.stringify(body);
  }
  if (method !== "GET" && method !== "HEAD") {
    const token = csrfToken();
    if (token) reqOpts.headers["X-DarkSec-CSRF"] = token;
  }
  let res;
  try {
    res = await fetch(path, reqOpts);
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError("Console backend unreachable. Is the local service running?", 0, null);
  }
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent("darksec:auth"));
    throw new ApiError("Session expired. Relaunch from the DarkSec application.", 401, null);
  }
  if (raw) {
    if (!res.ok) throw new ApiError(`Request failed (${res.status})`, res.status, null);
    return res;
  }
  const type = res.headers.get("content-type") || "";
  let payload = null;
  if (type.includes("application/json")) {
    try { payload = await res.json(); } catch { payload = null; }
  } else {
    payload = await res.text();
  }
  if (!res.ok) {
    const detail = payload && (payload.detail || payload.error);
    if (res.status === 403 && _allowVaultUnlock && vaultErrorKind(detail)) {
      const unlocked = await promptVaultUnlock(detail);
      if (unlocked) return request(path, { ...opts, _allowVaultUnlock: false });
    }
    throw new ApiError(detail || `Request failed (${res.status})`, res.status, payload);
  }
  return payload;
}

export const api = {
  get: (path, opts = {}) => request(path, opts),
  post: (path, body = {}, opts = {}) => request(path, { ...opts, method: "POST", body }),
  put: (path, body = {}, opts = {}) => request(path, { ...opts, method: "PUT", body }),
  patch: (path, body = {}, opts = {}) => request(path, { ...opts, method: "PATCH", body }),
  del: (path, opts = {}) => request(path, { ...opts, method: "DELETE" }),
  raw: (path, opts = {}) => request(path, { ...opts, raw: true }),
  text: (path, opts = {}) => request(path, { ...opts, raw: false }),
};

export function download(path, filename = "") {
  const a = document.createElement("a");
  a.href = path;
  if (filename) a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export function openFilePicker(accept = "") {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    if (accept) input.accept = accept;
    input.style.display = "none";
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      input.remove();
      resolve(file || null);
    }, { once: true });
    document.body.appendChild(input);
    input.click();
  });
}

export function fileText(file, maxBytes = 5_000_000) {
  return new Promise((resolve, reject) => {
    if (!file) return reject(new Error("No file selected"));
    if (file.size > maxBytes) return reject(new Error("File is too large"));
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("Could not read file"));
    reader.readAsText(file);
  });
}
