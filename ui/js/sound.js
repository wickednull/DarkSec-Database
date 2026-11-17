/* Local UI sound cues. Generated with Web Audio; no audio files or network. */

let enabled = false;
let ctx = null;

const CUES = {
  ok: [
    { f: 880, d: 0.055, t: 0 },
    { f: 1320, d: 0.075, t: 0.075 },
  ],
  warn: [
    { f: 520, d: 0.075, t: 0 },
    { f: 390, d: 0.11, t: 0.09 },
  ],
  error: [
    { f: 220, d: 0.11, t: 0 },
    { f: 180, d: 0.17, t: 0.12 },
  ],
  notify: [
    { f: 660, d: 0.05, t: 0 },
    { f: 990, d: 0.08, t: 0.07 },
  ],
};

function audioContext() {
  if (ctx) return ctx;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  try { ctx = new Ctor(); } catch { ctx = null; }
  return ctx;
}

function play(kind, force = false) {
  if (!enabled && !force) return;
  const audio = audioContext();
  if (!audio) return;
  try {
    // Browsers block audio until the page has seen a real user gesture. Do not
    // queue cues fired during boot; play them from the next interaction on.
    const active = navigator.userActivation ? navigator.userActivation.hasBeenActive : true;
    if (audio.state !== "running") {
      audio.resume().catch(() => {});
      if (!active) return;
    }
    const steps = CUES[kind] || CUES.ok;
    const now = audio.currentTime;
    for (const step of steps) {
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      const start = now + step.t;
      const end = start + step.d;
      osc.type = "sine";
      osc.frequency.setValueAtTime(step.f, start);
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.exponentialRampToValueAtTime(0.08, start + 0.008);
      gain.gain.exponentialRampToValueAtTime(0.0001, end);
      osc.connect(gain);
      gain.connect(audio.destination);
      osc.start(start);
      osc.stop(end + 0.02);
    }
  } catch { /* audio is optional */ }
}

export function setSoundEnabled(value) {
  enabled = !!value;
  if (enabled) audioContext();
}

export function soundCue(kind = "ok") {
  play(kind, false);
}

export function testSound() {
  play("ok", true);
}

// Browsers require a user gesture before audio starts. Resume on the first
// interaction so later notification sounds are not silently dropped.
try {
  window.addEventListener("pointerdown", () => {
    const audio = audioContext();
    if (audio && audio.state === "suspended") audio.resume().catch(() => {});
  }, { once: true });
} catch {}
