/* Minimal, sandboxed bridge. The renderer never receives Node or ipcRenderer. */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("darksecDesktop", {
  info: () => ipcRenderer.invoke("darksec:info"),
  openExternal: (url) => ipcRenderer.invoke("darksec:open-external", url),
  setZoom: (factor) => ipcRenderer.invoke("darksec:set-zoom", factor),
});
