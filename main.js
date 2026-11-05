/* DarkSec NIGHTWATCH — Electron shell.
 *
 * The desktop shell starts the local-only Python API on a random loopback
 * port, reads the one-time handshake line, and loads the console with the
 * launch token. The token is exchanged for an HttpOnly cookie by the server
 * and disappears from the address bar. No remote content is ever loaded.
 */
const { app, BrowserWindow, Menu, ipcMain, shell, dialog } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const ROOT = __dirname;
const IS_WIN = process.platform === "win32";
let backend = null;
let mainWindow = null;
let quitting = false;

function dataDir() {
  if (process.env.DARKSEC_DATA) return process.env.DARKSEC_DATA;
  if (app.isPackaged) return path.join(app.getPath("userData"), "fielddata");
  return path.join(ROOT, "fielddata");
}

function backendCandidates() {
  const out = [];
  if (process.env.DARKSEC_BACKEND) out.push({ cmd: process.env.DARKSEC_BACKEND, args: [], label: "DARKSEC_BACKEND" });
  if (app.isPackaged) {
    const bundled = path.join(process.resourcesPath, IS_WIN ? "darksec-backend.exe" : "darksec-backend");
    if (fs.existsSync(bundled)) out.push({ cmd: bundled, args: [], label: "bundled backend" });
  }
  if (process.env.DARKSEC_PYTHON) out.push({ cmd: process.env.DARKSEC_PYTHON, args: ["-m", "darksec"], label: "DARKSEC_PYTHON" });
  out.push({ cmd: IS_WIN ? "py" : "python3", args: IS_WIN ? ["-3", "-m", "darksec"] : ["-m", "darksec"], label: "python3" });
  if (IS_WIN) out.push({ cmd: "python", args: ["-m", "darksec"], label: "python" });
  return out;
}

function launchBackend() {
  return new Promise((resolve, reject) => {
    const candidates = backendCandidates();
    let index = 0;
    let settled = false;
    let timer = null;

    const fail = (message) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      reject(new Error(message));
    };

    const tryNext = (lastError = "") => {
      if (settled) return;
      if (index >= candidates.length) {
        fail(`Could not start the local DarkSec backend. ${lastError}`.trim());
        return;
      }
      const candidate = candidates[index++];
      const env = {
        ...process.env,
        PYTHONPATH: process.env.PYTHONPATH ? `${ROOT}${path.delimiter}${process.env.PYTHONPATH}` : ROOT,
        PYTHONUNBUFFERED: "1",
        DARKSEC_DATA: dataDir(),
      };
      let child;
      try {
        child = spawn(candidate.cmd, [...candidate.args, "--handshake", "--port", "0"], {
          cwd: ROOT,
          env,
          windowsHide: true,
          stdio: ["ignore", "pipe", "pipe"],
        });
      } catch (err) {
        tryNext(`${candidate.label}: ${err.message}`);
        return;
      }

      let buf = "";
      let stderr = "";
      timer = setTimeout(() => {
        try { child.kill("SIGTERM"); } catch {}
        tryNext(`${candidate.label}: handshake timed out`);
      }, 45000);

      child.stdout.setEncoding("utf8");
      child.stdout.on("data", (chunk) => {
        buf += chunk;
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          try {
            const msg = JSON.parse(line);
            if (msg && msg.ready && msg.port && msg.token) {
              settled = true;
              clearTimeout(timer);
              backend = child;
              child.stdout.removeAllListeners("data");
              child.stdout.on("data", (d) => process.stdout.write(`[backend] ${d}`));
              child.stderr.on("data", (d) => process.stderr.write(`[backend] ${d}`));
              child.once("exit", (code, signal) => {
                backend = null;
                if (!quitting && mainWindow && !mainWindow.isDestroyed()) {
                  dialog.showErrorBox("Backend stopped",
                    `The local DarkSec service exited unexpectedly (${signal || code}). Relaunch the application.`);
                }
              });
              resolve({ port: msg.port, token: msg.token, child });
              return;
            }
          } catch {
            process.stdout.write(`[backend] ${line}\n`);
          }
        }
      });

      child.stderr.setEncoding("utf8");
      child.stderr.on("data", (d) => {
        stderr += d;
        if (stderr.length > 8000) stderr = stderr.slice(-8000);
        process.stderr.write(`[backend] ${d}`);
      });

      child.once("error", (err) => {
        clearTimeout(timer);
        tryNext(`${candidate.label}: ${err.message}`);
      });
      child.once("exit", (code, signal) => {
        if (settled) return;
        clearTimeout(timer);
        tryNext(`${candidate.label}: exited ${signal || code}${stderr ? ` — ${stderr.split("\n").slice(-3).join(" ")}` : ""}`);
      });
    };

    tryNext();
  });
}

function createWindow(port, token) {
  mainWindow = new BrowserWindow({
    width: 1500,
    height: 960,
    minWidth: 960,
    minHeight: 640,
    show: false,
    backgroundColor: "#050806",
    title: "DarkSec NIGHTWATCH",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(ROOT, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      spellcheck: false,
      devTools: !app.isPackaged,
    },
  });

  mainWindow.setMenuBarVisibility(false);
  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
    mainWindow.focus();
  });

  const allowed = new Set([`127.0.0.1:${port}`, `localhost:${port}`]);
  mainWindow.webContents.on("will-navigate", (event, url) => {
    try {
      const parsed = new URL(url);
      if (parsed.protocol === "http:" && allowed.has(parsed.host)) return;
    } catch {}
    event.preventDefault();
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    try {
      const parsed = new URL(url);
      if (parsed.protocol === "https:") shell.openExternal(url);
    } catch {}
    return { action: "deny" };
  });

  mainWindow.on("closed", () => { mainWindow = null; });
  mainWindow.loadURL(`http://127.0.0.1:${port}/?token=${encodeURIComponent(token)}`)
    .catch((err) => showFailure(err));
  return mainWindow;
}

function showFailure(err) {
  const message = String(err && err.message ? err.message : err || "Unknown startup failure");
  const safe = message.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  const win = new BrowserWindow({
    width: 760, height: 520, backgroundColor: "#050806", title: "DarkSec startup failure",
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true },
  });
  win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(`<!doctype html>
<meta charset="utf-8"><title>DarkSec startup failure</title>
<style>
  body{margin:0;background:#050806;color:#c9d6c9;font:13px/1.6 ui-monospace,monospace;padding:36px}
  h1{font-size:16px;letter-spacing:.18em;color:#ff5555;margin:0 0 14px}
  pre{white-space:pre-wrap;border:1px solid #24352a;background:#0a130d;padding:14px;color:#ffb4b4}
  .hint{color:#7d8f80;margin-top:14px}
</style>
<h1>DARKSEC // STARTUP FAILURE</h1>
<pre>${safe}</pre>
<div class="hint">Check that Python 3.11+ is installed and the required packages are available, or set DARKSEC_PYTHON to a working interpreter. The backend never leaves 127.0.0.1.</div>`));
}

function stopBackend() {
  quitting = true;
  if (!backend) return;
  const child = backend;
  backend = null;
  try { child.kill("SIGTERM"); } catch {}
  const timer = setTimeout(() => { try { child.kill("SIGKILL"); } catch {} }, 4000);
  child.once("exit", () => clearTimeout(timer));
}

ipcMain.handle("darksec:info", () => ({
  version: app.getVersion(),
  platform: process.platform,
  packaged: app.isPackaged,
  dataDir: dataDir(),
}));

ipcMain.handle("darksec:open-external", (_event, url) => {
  try {
    const parsed = new URL(String(url));
    if (!["https:", "http:"].includes(parsed.protocol)) return { ok: false };
    shell.openExternal(parsed.toString());
    return { ok: true };
  } catch {
    return { ok: false };
  }
});

app.whenReady().then(async () => {
  Menu.setApplicationMenu(null);
  try {
    const { port, token } = await launchBackend();
    createWindow(port, token);
  } catch (err) {
    showFailure(err);
  }
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      launchBackend().then(({ port, token }) => createWindow(port, token)).catch(showFailure);
    }
  });
});

app.on("before-quit", stopBackend);
app.on("window-all-closed", () => {
  stopBackend();
  if (process.platform !== "darwin") app.quit();
});
