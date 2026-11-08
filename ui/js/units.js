import { store } from "./store.js";

export function isImperial() {
  return String(store.settings?.units || "metric").toLowerCase() === "imperial";
}

function n(v) {
  if (v == null || v === "" || Number.isNaN(Number(v))) return null;
  return Number(v);
}

function fmtNum(v, digits = 1) {
  if (v == null || !Number.isFinite(v)) return "—";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function tempUnit() { return isImperial() ? "°F" : "°C"; }
export function speedUnit() { return isImperial() ? "mph" : "km/h"; }
export function distUnit() { return isImperial() ? "mi" : "km"; }
export function shortDistUnit() { return isImperial() ? "ft" : "m"; }
export function pressureUnit() { return isImperial() ? "inHg" : "hPa"; }
export function precipUnit() { return isImperial() ? "in" : "mm"; }
export function volumeUnit() { return isImperial() ? "gal" : "L"; }
export function weightUnit() { return isImperial() ? "lb" : "kg"; }

export function tempC(v, digits = 1) {
  const c = n(v); if (c == null) return "—";
  return isImperial() ? `${fmtNum(c * 9 / 5 + 32, digits)}°F` : `${fmtNum(c, digits)}°C`;
}
export function tempFromDisplay(v) { const x = n(v); if (x == null) return null; return isImperial() ? (x - 32) * 5 / 9 : x; }
export function tempToDisplay(v) { const c = n(v); if (c == null) return ""; return isImperial() ? (c * 9 / 5 + 32) : c; }

export function speedKmh(v, digits = 0) {
  const kmh = n(v); if (kmh == null) return "—";
  return isImperial() ? `${fmtNum(kmh * 0.621371, digits)} mph` : `${fmtNum(kmh, digits)} km/h`;
}
export function speedFromDisplay(v) { const x = n(v); if (x == null) return null; return isImperial() ? x / 0.621371 : x; }
export function speedToDisplay(v) { const kmh = n(v); if (kmh == null) return ""; return isImperial() ? kmh * 0.621371 : kmh; }

export function distKm(v, digits = 0) {
  const km = n(v); if (km == null) return "—";
  return isImperial() ? `${fmtNum(km * 0.621371, digits)} mi` : `${fmtNum(km, digits)} km`;
}
export function distFromDisplay(v) { const x = n(v); if (x == null) return null; return isImperial() ? x / 0.621371 : x; }
export function distToDisplay(v) { const km = n(v); if (km == null) return ""; return isImperial() ? km * 0.621371 : km; }

export function lengthM(v, digits = 0) {
  const m = n(v); if (m == null) return "—";
  return isImperial() ? `${fmtNum(m * 3.28084, digits)} ft` : `${fmtNum(m, digits)} m`;
}
// Human-readable distance from metres, switching to km/mi on long spans.
// Used by the map scale bar and route measurement readouts.
export function distM(v, digits = 0) {
  const m = n(v); if (m == null) return "—";
  if (isImperial()) {
    const ft = m * 3.28084;
    if (ft >= 5280) return `${fmtNum(ft / 5280, ft >= 52800 ? 0 : 1)} mi`;
    return `${fmtNum(ft, digits)} ft`;
  }
  if (m >= 1000) return `${fmtNum(m / 1000, m >= 10000 ? 0 : 1)} km`;
  return `${fmtNum(m, digits)} m`;
}
export function lengthFromDisplay(v) { const x = n(v); if (x == null) return null; return isImperial() ? x / 3.28084 : x; }
export function lengthToDisplay(v) { const m = n(v); if (m == null) return ""; return isImperial() ? m * 3.28084 : m; }

export function altM(v, digits = 0) { return lengthM(v, digits); }
export function altKm(v, digits = 0) {
  const km = n(v); if (km == null) return "—";
  return isImperial() ? `${fmtNum(km * 3280.84, 0)} ft` : `${fmtNum(km, digits)} km`;
}

export function pressureHpa(v, digits = 1) {
  const hpa = n(v); if (hpa == null) return "—";
  return isImperial() ? `${fmtNum(hpa * 0.0295299830714, 2)} inHg` : `${fmtNum(hpa, digits)} hPa`;
}
export function pressureFromDisplay(v) { const x = n(v); if (x == null) return null; return isImperial() ? x / 0.0295299830714 : x; }
export function pressureToDisplay(v) { const hpa = n(v); if (hpa == null) return ""; return isImperial() ? hpa * 0.0295299830714 : hpa; }

export function precipMm(v, digits = 1) {
  const mm = n(v); if (mm == null) return "—";
  return isImperial() ? `${fmtNum(mm / 25.4, 2)} in` : `${fmtNum(mm, digits)} mm`;
}
export function precipFromDisplay(v) { const x = n(v); if (x == null) return null; return isImperial() ? x * 25.4 : x; }
export function precipToDisplay(v) { const mm = n(v); if (mm == null) return ""; return isImperial() ? mm / 25.4 : mm; }

export function volumeL(v, digits = 1) {
  const l = n(v); if (l == null) return "—";
  return isImperial() ? `${fmtNum(l * 0.264172, digits)} gal` : `${fmtNum(l, digits)} L`;
}
export function volumeFromDisplay(v) { const x = n(v); if (x == null) return null; return isImperial() ? x / 0.264172 : x; }
export function volumeToDisplay(v) { const l = n(v); if (l == null) return ""; return isImperial() ? l * 0.264172 : l; }

export function weightKg(v, digits = 1) {
  const kg = n(v); if (kg == null) return "—";
  return isImperial() ? `${fmtNum(kg * 2.20462, digits)} lb` : `${fmtNum(kg, digits)} kg`;
}
export function weightFromDisplay(v) { const x = n(v); if (x == null) return null; return isImperial() ? x / 2.20462 : x; }
export function weightToDisplay(v) { const kg = n(v); if (kg == null) return ""; return isImperial() ? kg * 2.20462 : kg; }

// Generic by group key used in records/calculators
export const GROUP = {
  temp: { unit: tempUnit, fmt: tempC, from: tempFromDisplay, to: tempToDisplay },
  speed: { unit: speedUnit, fmt: speedKmh, from: speedFromDisplay, to: speedToDisplay },
  dist: { unit: distUnit, fmt: distKm, from: distFromDisplay, to: distToDisplay },
  length: { unit: shortDistUnit, fmt: lengthM, from: lengthFromDisplay, to: lengthToDisplay },
  pressure: { unit: pressureUnit, fmt: pressureHpa, from: pressureFromDisplay, to: pressureToDisplay },
  precip: { unit: precipUnit, fmt: precipMm, from: precipFromDisplay, to: precipToDisplay },
  volume: { unit: volumeUnit, fmt: volumeL, from: volumeFromDisplay, to: volumeToDisplay },
  weight: { unit: weightUnit, fmt: weightKg, from: weightFromDisplay, to: weightToDisplay },
};
