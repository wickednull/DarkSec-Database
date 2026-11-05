import { api } from "./api.js";

class Store {
  constructor() {
    this.state = {
      settings: null,
      status: null,
      dashboard: null,
      catalog: null,
      authFailed: false,
      opsec: false,
      lastRefresh: 0,
      view: "dashboard",
    };
    this.listeners = new Set();
  }

  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  emit(type = "change", detail = null) {
    for (const fn of this.listeners) {
      try { fn(this.state, type, detail); } catch (err) { console.error(err); }
    }
  }

  get settings() { return this.state.settings || {}; }

  async loadStatus({ force = false } = {}) {
    try {
      const status = await api.get(`/api/status${force ? "?force=true" : ""}`);
      this.state.status = status;
      this.state.settings = { ...(this.state.settings || {}), ...(status.settings || {}) };
      this.state.opsec = !!status.settings?.opsec_mode;
      this.state.lastRefresh = Date.now();
      this.emit("status");
      return status;
    } catch (err) {
      if (err.status === 401) this.state.authFailed = true;
      this.emit("error", err);
      throw err;
    }
  }

  async loadSettings() {
    const settings = await api.get("/api/settings");
    this.state.settings = { ...(this.state.settings || {}), ...settings };
    this.emit("settings");
    return settings;
  }

  async saveSettings(patch) {
    const settings = await api.put("/api/settings", patch);
    this.state.settings = { ...this.state.settings, ...settings };
    this.state.opsec = !!settings.opsec_mode;
    this.emit("settings");
    return settings;
  }

  async loadDashboard() {
    const dash = await api.get("/api/dashboard");
    this.state.dashboard = dash;
    if (dash.settings) this.state.settings = { ...(this.state.settings || {}), ...dash.settings };
    this.emit("dashboard");
    return dash;
  }

  async loadCatalog() {
    if (this.state.catalog) return this.state.catalog;
    const { catalog } = await api.get("/api/calc");
    this.state.catalog = catalog;
    return catalog;
  }

  setView(id) {
    this.state.view = id;
    this.emit("view", id);
  }

  setOpsec(value) {
    this.state.opsec = !!value;
    document.body.classList.toggle("opsec", this.state.opsec);
    this.emit("opsec", this.state.opsec);
  }
}

export const store = new Store();

export function calcGroup(group) {
  if (!store.state.catalog) return [];
  return store.state.catalog.filter((c) => c.group === group);
}
