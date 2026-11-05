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

async function request(path, { method = "GET", body, headers = {}, signal, raw = false } = {}) {
  const opts = { method, credentials: "same-origin", signal, headers: { ...JSON_HEADERS, ...headers } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  if (method !== "GET" && method !== "HEAD") {
    const token = csrfToken();
    if (token) opts.headers["X-DarkSec-CSRF"] = token;
  }
  let res;
  try {
    res = await fetch(path, opts);
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
