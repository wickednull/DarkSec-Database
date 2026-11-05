const SVG_NS = "http://www.w3.org/2000/svg";

export const ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>',
  map: '<path d="M9 4 3 6v15l6-2 6 2 6-2V4l-6 2-6-2Z"/><path d="M9 4v15M15 6v15"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/><path d="M9 7h7M9 11h7"/>',
  library: '<path d="M4 4h5v16H4zM10 4h5v16h-5z"/><path d="m16.5 4.8 3.8-1 4.2 15.6-3.8 1z" transform="translate(-1.5)"/>',
  compass: '<circle cx="12" cy="12" r="9"/><path d="m16 8-2.6 5.4L8 16l2.6-5.4L16 8Z"/>',
  radio: '<rect x="3" y="8" width="18" height="12" rx="2"/><path d="m7 8 10-5M7 13h.01M11 13h6M7 16h.01M11 16h6"/>',
  antenna: '<path d="M12 13a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z"/><path d="M12 13v8M8.5 8.5a5 5 0 0 1 7 0M5.6 5.6a9 9 0 0 1 12.8 0M18.4 18.4l-3-3M5.6 18.4l3-3"/>',
  medical: '<path d="M9 3h6v6h6v6h-6v6H9v-6H3V9h6z"/>',
  cross: '<path d="M9 3h6v6h6v6h-6v6H9v-6H3V9h6z"/>',
  drop: '<path d="M12 2.7S5.5 9.5 5.5 14a6.5 6.5 0 0 0 13 0C18.5 9.5 12 2.7 12 2.7Z"/>',
  leaf: '<path d="M20.5 3.5c-8.5 0-16 3.6-16 11 0 2.4.9 4.3 2.3 5.7C8.6 15.3 13 12 20.5 11c-5.6 2.7-9.2 6.4-10.4 12"/><path d="M20.5 3.5C20 11 15 16 8.5 18.8"/>',
  bolt: '<path d="m13 2-9 12h7l-1 8 9-12h-7l1-8Z"/>',
  radiation: '<circle cx="12" cy="12" r="2.2"/><path d="M12 2.5a9.5 9.5 0 0 1 4.8 17.7l-2.4-4.2A4.7 4.7 0 0 0 12 17.2V21a9.5 9.5 0 0 1 0-19Z"/><path d="M7.2 20.2a9.5 9.5 0 0 1-4.7-8.2c0-3.4 1.8-6.4 4.5-8.1l2.4 4.1a4.7 4.7 0 0 0-2.2 4v.1l-4.3-1.3"/><path d="M16.8 3.9a9.5 9.5 0 0 1 4.7 8.1c0 3.4-1.8 6.4-4.5 8.1l-2.4-4.2a4.7 4.7 0 0 0 2.2-4v-.1l4.3 1.3"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/>',
  box: '<path d="m21 8-9 5-9-5 9-5 9 5Z"/><path d="m3 8 9 5v9l-9-5V8ZM21 8l-9 5v9l9-5V8Z"/>',
  cloud: '<path d="M17.5 19a4.5 4.5 0 0 0 .4-9A7 7 0 0 0 4.3 12 4 4 0 0 0 5 19h12.5Z"/><path d="M8 23h8"/>',
  weather: '<path d="M17.5 19a4.5 4.5 0 0 0 .4-9A7 7 0 0 0 4.3 12 4 4 0 0 0 5 19h12.5Z"/><path d="M12 22v-6M9 19l3 3 3-3"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h8"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.2a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.2A1.7 1.7 0 0 0 4.7 9a1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.4 1Z"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  close: '<path d="M18 6 6 18M6 6l12 12"/>',
  play: '<path d="m6 3 14 9-14 9V3Z"/>',
  stop: '<rect x="5" y="5" width="14" height="14" rx="1"/>',
  lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  unlock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 7.5-2"/>',
  download: '<path d="M12 3v13M7 11l5 5 5-5M4 21h16"/>',
  upload: '<path d="M12 16V3M7 8l5-5 5 5M4 21h16"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 15h10l1-15M10 11v6M14 11v6"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6"/>',
  warning: '<path d="M10.3 3.2 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.2a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
  check: '<path d="m20 6-11 11-5-5"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
  'chevron-left': '<path d="m15 18-6-6 6-6"/>',
  'chevron-right': '<path d="m9 18 6-6-6-6"/>',
  target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
  layers: '<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5M3 17l9 5 9-5"/>',
  satellite: '<path d="m13 7 4 4M9 11l4 4M5 15l4 4M3 21l3-3M6 18l3 3M14 6l4 4M16 4l4 4M2 22l4-4"/><path d="m11 5 3-3 8 8-3 3zM5 11l3-3 8 8-3 3z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8Z"/>',
  wifi: '<path d="M5 12.5a10 10 0 0 1 14 0M8.5 16a5 5 0 0 1 7 0M12 20h.01M2 9a15 15 0 0 1 20 0"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2"/>',
  battery: '<rect x="2" y="7" width="18" height="10" rx="2"/><path d="M22 11v2M6 10v4M10 10v4M14 10v4"/>',
  database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
  terminal: '<path d="m4 17 6-6-6-6M12 19h8"/>',
  'arrow-up': '<path d="M12 20V4M6 10l6-6 6 6"/>',
  'arrow-down': '<path d="M12 4v16M6 14l6 6 6-6"/>',
  menu: '<path d="M3 6h18M3 12h18M3 18h18"/>',
  maximize: '<path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/>',
  pin: '<path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 0 1 16 0Z"/><circle cx="12" cy="10" r="2.5"/>',
  ruler: '<path d="m3 16 5 5L21 8l-5-5-13 13Z"/><path d="m7 14 2 2M10 11l2 2M13 8l2 2M16 5l2 2"/>',
  route: '<circle cx="5" cy="6" r="2.5"/><circle cx="19" cy="18" r="2.5"/><path d="M7.5 6H15a4 4 0 0 1 0 8H9a4 4 0 0 0 0 8"/>',
  mic: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 17v5M8 22h8"/>',
  signal: '<path d="M4 20V10M9 20V4M14 20v-7M19 20v-3"/>',
  print: '<path d="M6 9V2h12v7M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8"/>',
  copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8M7 3v5h8"/>',
  link: '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1.5 1.5"/><path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1.5-1.5"/>',
  heart: '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8l1.1 1.1L12 21l7.8-7.5 1.1-1.1a5.5 5.5 0 0 0-.1-7.8Z"/>',
  wind: '<path d="M3 8h11a3 3 0 1 0-3-3M3 12h15a3 3 0 1 1-3 3M3 16h8a2.5 2.5 0 1 1-2.5 2.5"/>',
  flame: '<path d="M12 22c4.4 0 7-2.8 7-6.5 0-3-1.7-5.2-3.4-7.2C14 6.4 12.8 4.3 12.8 2c-1.4 2.4-3.8 4-5.2 6.5C6.2 10.6 5 12.6 5 15.5 5 19.2 7.6 22 12 22Z"/><path d="M9.5 16.2c0 1.7 1.1 2.8 2.5 2.8s2.5-1.1 2.5-2.8c0-1.4-.8-2.4-1.6-3.3-.5-.6-1.2-1.5-1.6-2.4-.7 1-1.2 1.8-1.5 2.4-.2.4-.3.7-.3 1.1"/>',
  tent: '<path d="M12 3 2 21h20L12 3Z"/><path d="M12 9v12M7 21l5-7 5 7"/>',
  flask: '<path d="M9 3h6M10 3v6L4.5 18a2 2 0 0 0 1.7 3h11.6a2 2 0 0 0 1.7-3L14 9V3"/><path d="M7.5 15h9"/>',
  thermometer: '<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0Z"/>',
  pulse: '<path d="M2 12h4l2-6 4 12 2-6h8"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  'shield-off': '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m4 4 16 16"/>',
  key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.5 12.5 9-9M17 6l3 3M14 9l3 3"/>',
  note: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h5"/>',
  send: '<path d="m22 2-7 20-4-9-9-4 20-7Z"/><path d="M22 2 11 13"/>',
  trophy: '<path d="M8 21h8M12 17v4M7 4h10v5a5 5 0 0 1-10 0V4Z"/><path d="M7 6H4a3 3 0 0 0 3 3M17 6h3a3 3 0 0 1-3 3"/>',
  scale: '<path d="M12 3v18M7 21h10M3 7h18M6 7l-3 7a3 3 0 0 0 6 0L6 7ZM18 7l-3 7a3 3 0 0 0 6 0l-3-7Z"/>',
};

export function icon(name, cls = "") {
  const markup = ICONS[name] || ICONS.info;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.7");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", `ic ${cls}`.trim());
  svg.innerHTML = markup;
  return svg;
}

export function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((node) => {
    const name = node.getAttribute("data-icon");
    if (!name || node.querySelector("svg")) return;
    node.appendChild(icon(name));
  });
}
