"""HackRF / RTL-SDR integration.

Real hardware only: every function shells out to the installed HackRF tools
and reports exactly what the device says. If no radio is present the console
shows NO DEVICE instead of inventing a spectrum.

RX: wideband sweep, IQ capture, AM/NFM/WFM/USB/LSB demodulation, spectrum
analysis and experimental NOAA APT weather-satellite image decoding.
TX: hard-gated behind an explicit settings unlock, legal attestation and a
confirmation phrase, and every attempt is audited.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import threading
import time
import uuid
import wave
from pathlib import Path

import numpy as np

from . import config
from .db import Database, utcnow

MAX_CAPTURE_BYTES = 750 * 1024 * 1024
MAX_SWEEP_SPAN_HZ = 6_000_000_000

_JOBS: dict[str, dict] = {}
_PROCS: dict[str, subprocess.Popen] = {}
_LOCK = threading.RLock()


def _run(cmd: list[str], timeout: float = 8) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              errors="replace")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, "not installed"
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as exc:
        return 1, str(exc)


def _quantize_gain(value, maximum: int, step: int) -> int:
    """Clamp and snap a HackRF gain to a value the hardware accepts."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = 0
    return max(0, min(maximum, (v // step) * step))


def detect() -> dict:
    hackrf = shutil.which("hackrf_info")
    info = {"tools": {}, "hackrf": {"present": bool(hackrf), "connected": False, "raw": ""}}
    for tool in ("hackrf_info", "hackrf_sweep", "hackrf_transfer", "rtl_test", "rtl_fm",
                 "dump1090", "rtl_433", "multimon-ng", "direwolf", "gqrx", "inspectrum",
                 "sox", "ffmpeg", "gpspipe"):
        info["tools"][tool] = shutil.which(tool)
    if hackrf:
        rc, out = _run([hackrf], 8)
        info["hackrf"]["raw"] = out.strip()
        info["hackrf"]["connected"] = "No HackRF boards found" not in out and rc == 0
        for key, pattern in (
            ("board_id", r"Board ID Number:\s*(.+)"), ("firmware", r"Firmware Version:\s*(.+)"),
            ("serial", r"Serial number:\s*(.+)"), ("part_id", r"Part ID Number:\s*(.+)"),
        ):
            m = re.search(pattern, out)
            if m:
                info["hackrf"][key] = m.group(1).strip()
    if shutil.which("rtl_test"):
        rc, out = _run(["rtl_test", "-t"], 6)
        info["rtl"] = {"present": True, "connected": "Found" in out and "No supported" not in out,
                       "raw": out.strip()[:800]}
    else:
        info["rtl"] = {"present": False, "connected": False, "raw": "not installed"}
    info["gps"] = {"gpspipe": shutil.which("gpspipe") is not None}
    info["adsb"] = {"dump1090": shutil.which("dump1090") is not None}
    return info


# --------------------------------------------------------------------------
# Spectrum sweep
# --------------------------------------------------------------------------

def _parse_sweep_line(line: str) -> dict | None:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 7:
        return None
    try:
        low = float(parts[2]); high = float(parts[3])
        bin_hz = float(parts[4]); count = int(float(parts[5]))
        db = [float(x) for x in parts[6:6 + count]]
    except (ValueError, IndexError):
        return None
    if not db:
        return None
    return {"stamp": parts[0] + " " + parts[1], "low_hz": low, "high_hz": high,
            "bin_hz": bin_hz, "db": db}


def _downsample(values: list[float], target: int = 1024) -> list[float]:
    if len(values) <= target:
        return [round(v, 1) for v in values]
    out = []
    size = len(values) / target
    for i in range(target):
        a = int(i * size); b = max(a + 1, int((i + 1) * size))
        chunk = values[a:b]
        out.append(round(max(chunk), 1))
    return out


def sweep_stream(start_hz: float, end_hz: float, bin_hz: float = 100_000,
                 lna: int = 32, vga: int = 40, amp: bool = False):
    """Yield NDJSON-ready sweep frames from hackrf_sweep."""
    start_hz, end_hz = int(start_hz), int(end_hz)
    if not (1_000_000 <= start_hz < end_hz <= 6_000_000_000):
        yield {"type": "error", "error": "Frequency must be within 1 MHz - 6 GHz and start < end"}
        return
    if end_hz - start_hz > MAX_SWEEP_SPAN_HZ:
        yield {"type": "error", "error": "Span too large"}
        return
    if not shutil.which("hackrf_sweep"):
        yield {"type": "error", "error": "hackrf_sweep is not installed"}
        return
    bin_hz = max(10_000, min(5_000_000, int(bin_hz)))
    # hackrf_sweep accepts the -f range in whole MHz, unlike hackrf_transfer (Hz).
    fmin_mhz = int(math.floor(start_hz / 1e6))
    fmax_mhz = int(math.ceil(end_hz / 1e6))
    if fmax_mhz <= fmin_mhz:
        fmax_mhz = fmin_mhz + 1
    cmd = ["hackrf_sweep", "-f", f"{fmin_mhz}:{fmax_mhz}", "-w", str(bin_hz),
           "-l", str(_quantize_gain(lna, 40, 8)), "-g", str(_quantize_gain(vga, 62, 2)),
           "-a", "1" if amp else "0"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
    except Exception as exc:
        yield {"type": "error", "error": str(exc)}
        return
    current = None
    frames = 0
    try:
        for line in proc.stdout:
            parsed = _parse_sweep_line(line)
            if not parsed:
                continue
            if current and parsed["stamp"] != current["stamp"]:
                yield _frame(current)
                frames += 1
                current = None
            if current is None:
                current = {"stamp": parsed["stamp"], "bins": [], "low_hz": parsed["low_hz"],
                           "high_hz": parsed["high_hz"], "bin_hz": parsed["bin_hz"]}
            else:
                current["low_hz"] = min(current["low_hz"], parsed["low_hz"])
                current["high_hz"] = max(current["high_hz"], parsed["high_hz"])
            current["bins"].extend(parsed["db"])
            current["bin_hz"] = parsed["bin_hz"]
        if current:
            yield _frame(current)
            frames += 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    err = ""
    try:
        err = (proc.stderr.read() or "").strip()
    except Exception:
        pass
    if err and (frames == 0 or "No HackRF boards found" in err):
        first = next((ln.strip() for ln in err.splitlines() if ln.strip()), err.strip())
        if "not found" in first.lower() or "no hackrf" in first.lower():
            first = f"No HackRF board found ({first[:160]}). Connect the radio and check USB permissions."
        yield {"type": "error", "error": first[:400]}


def _frame(current: dict) -> dict:
    bins = current["bins"]
    return {
        "type": "sweep", "stamp": current["stamp"],
        "start_hz": current["low_hz"], "end_hz": current["high_hz"],
        "bin_hz": current["bin_hz"], "count": len(bins),
        "db": _downsample(bins), "peak_db": round(max(bins), 1) if bins else None,
    }


# --------------------------------------------------------------------------
# Capture and demodulation
# --------------------------------------------------------------------------

def _new_job(kind: str, **kwargs) -> dict:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "kind": kind, "status": "queued", "progress": 0.0,
           "error": None, "created": utcnow(), **kwargs}
    with _LOCK:
        _JOBS[job_id] = job
    try:
        db = Database()
        db.execute(
            "INSERT INTO sdr_jobs(id,kind,freq_hz,rate,seconds,status,progress,created) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (job_id, kind, kwargs.get("freq_hz"), kwargs.get("rate"), kwargs.get("seconds"),
             "queued", 0, utcnow()),
        )
        db.close()
    except Exception:
        pass
    return job


def _update_job(job_id: str, **kwargs) -> None:
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id].update(kwargs)
    try:
        db = Database()
        allowed = {k: v for k, v in kwargs.items() if k in
                   ("status", "progress", "iq_path", "audio_path", "image_path", "spectrum", "error", "finished")}
        if allowed:
            keys = list(allowed)
            db.execute(
                f"UPDATE sdr_jobs SET {','.join(f'{k}=?' for k in keys)} WHERE id=?",
                [allowed[k] for k in keys] + [job_id],
            )
        db.close()
    except Exception:
        pass


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        if job_id in _JOBS:
            return dict(_JOBS[job_id])
    db = Database()
    row = db.one("SELECT * FROM sdr_jobs WHERE id=?", (job_id,))
    db.close()
    return dict(row) if row else None


def list_jobs(limit: int = 20) -> list[dict]:
    db = Database()
    rows = db.query("SELECT * FROM sdr_jobs ORDER BY created DESC LIMIT ?", (limit,))
    db.close()
    return [dict(r) for r in rows]


def stop_job(job_id: str) -> dict:
    with _LOCK:
        job = _JOBS.get(job_id)
        proc = _PROCS.get(job_id)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    if job:
        job["stop"] = True
        job["status"] = "stopping"
    return {"id": job_id, "status": "stopping"}


def start_capture(freq_hz: float, seconds: float = 5.0, rate: float = 2_400_000,
                  demod: str = "nfm", lna: int = 32, vga: int = 40, amp: bool = False,
                  keep_iq: bool = False, name: str = "") -> dict:
    freq_hz = float(freq_hz)
    rate = float(rate)
    seconds = float(seconds)
    if not 1_000_000 <= freq_hz <= 6_000_000_000:
        raise ValueError("Frequency must be 1 MHz - 6 GHz")
    if not 2_000_000 <= rate <= 20_000_000:
        raise ValueError("Sample rate must be 2-20 Msps")
    if not 0.2 <= seconds <= 180:
        raise ValueError("Capture duration must be 0.2-180 s")
    demod = str(demod).lower()
    if demod not in ("iq", "am", "nfm", "wfm", "usb", "lsb", "apt"):
        raise ValueError("Unsupported demodulation mode")
    lna = _quantize_gain(lna, 40, 8)
    vga = _quantize_gain(vga, 62, 2)
    samples = int(rate * seconds)
    expected = samples * 2
    if expected > MAX_CAPTURE_BYTES:
        raise ValueError(f"Capture would be {expected/1e6:.0f} MB; limit is {MAX_CAPTURE_BYTES/1e6:.0f} MB. Shorten duration or lower rate.")
    if demod == "iq":
        keep_iq = True
    free = shutil.disk_usage(config.RECORDING_DIR).free
    if free < expected + 50_000_000:
        raise ValueError("Not enough free disk space for this capture")
    job = _new_job("capture", freq_hz=freq_hz, rate=rate, seconds=seconds, demod=demod,
                   name=name or f"{freq_hz/1e6:.4f} MHz {demod.upper()}", keep_iq=keep_iq,
                   samples=samples, lna=lna, vga=vga, amp=amp)
    thread = threading.Thread(target=_capture_worker, args=(job["id"],), daemon=True)
    thread.start()
    return get_job(job["id"])


def _capture_worker(job_id: str) -> None:
    with _LOCK:
        job = dict(_JOBS[job_id])
    freq = job["freq_hz"]; rate = job["rate"]; seconds = job["seconds"]
    samples = job["samples"]; demod = job.get("demod") or "iq"
    keep_iq = bool(job.get("keep_iq"))
    iq_path = config.RECORDING_DIR / f"{job_id}.iq"
    audio_path = config.RECORDING_DIR / f"{job_id}.wav"
    image_path = config.RECORDING_DIR / f"{job_id}.png"
    _update_job(job_id, status="running", progress=0.0)
    cmd = ["hackrf_transfer", "-r", str(iq_path), "-f", str(int(freq)), "-s", str(int(rate)),
           "-n", str(int(samples)), "-l", str(int(job.get("lna", 32))),
           "-g", str(int(job.get("vga", 40))), "-a", "1" if job.get("amp") else "0", "-p", "0"]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with _LOCK:
            _PROCS[job_id] = proc
        deadline = time.time() + seconds + 45
        while proc.poll() is None:
            try:
                size = iq_path.stat().st_size if iq_path.exists() else 0
                _update_job(job_id, progress=round(min(0.98, size / max(1, samples * 2)), 3))
            except Exception:
                pass
            if time.time() > deadline:
                proc.terminate()
                break
            if (_JOBS.get(job_id) or {}).get("stop"):
                proc.terminate()
                break
            time.sleep(0.25)
        rc = proc.wait(timeout=10)
        _PROCS.pop(job_id, None)
        if (_JOBS.get(job_id) or {}).get("stop"):
            _update_job(job_id, status="stopped", finished=utcnow())
            return
        if rc != 0:
            err = ""
            try:
                err = (proc.stderr.read() or "").strip()
            except Exception:
                pass
            raise RuntimeError(err or f"hackrf_transfer exited {rc}; is the HackRF connected?")
        if not iq_path.exists() or iq_path.stat().st_size < 1024:
            raise RuntimeError("No IQ samples captured; check the radio and USB permissions")
        _update_job(job_id, progress=0.99, status="processing")
        spectrum = analyze_spectrum(iq_path, rate)
        _update_job(job_id, spectrum=json.dumps(spectrum))
        if demod in ("am", "nfm", "wfm", "usb", "lsb"):
            demod_to_wav(iq_path, audio_path, rate, demod)
        elif demod == "apt":
            apt_to_png(iq_path, image_path, rate)
        size = iq_path.stat().st_size if iq_path.exists() else 0
        if not keep_iq:
            try:
                iq_path.unlink(missing_ok=True)
            except Exception:
                pass
        _update_job(job_id, status="complete", progress=1.0,
                    audio_path=str(audio_path) if audio_path.exists() else None,
                    image_path=str(image_path) if image_path.exists() else None,
                    finished=utcnow())
        db = Database()
        record_size = (audio_path.stat().st_size if audio_path.exists() else 0) + \
                      (image_path.stat().st_size if image_path.exists() else 0) + \
                      (iq_path.stat().st_size if iq_path.exists() else 0)
        db.insert("sdr_recordings", {
            "ts": utcnow(), "name": job.get("name") or f"{freq/1e6:.4f} MHz",
            "freq_hz": freq, "rate": rate, "kind": demod,
            "iq_path": str(iq_path) if iq_path.exists() else None,
            "audio_path": str(audio_path) if audio_path.exists() else None,
            "image_path": str(image_path) if image_path.exists() else None,
            "size": record_size, "note": "",
        })
        db.close()
    except Exception as exc:
        try:
            proc.terminate()
        except Exception:
            pass
        _PROCS.pop(job_id, None)
        _update_job(job_id, status="error", error=str(exc)[:500], finished=utcnow())


# --------------------------------------------------------------------------
# DSP
# --------------------------------------------------------------------------

def _iter_iq(path: Path, block_samples: int = 2_000_000):
    with open(path, "rb") as fh:
        while True:
            raw = fh.read(block_samples * 2)
            if len(raw) < 4:
                break
            if len(raw) % 2:
                raw = raw[:-1]
            arr = np.frombuffer(raw, dtype=np.int8).astype(np.float32)
            iq = arr[0::2] + 1j * arr[1::2]
            if iq.size == 0:
                break
            iq -= np.mean(iq)
            yield iq


def _resample(x: np.ndarray, rate: float, out_rate: float) -> np.ndarray:
    from math import gcd
    from scipy.signal import resample_poly
    r = int(rate); o = int(out_rate)
    g = gcd(r, o)
    return resample_poly(x, o // g, r // g).astype(np.float32)


def _lowpass(x: np.ndarray, rate: float, cutoff: float) -> np.ndarray:
    from scipy.signal import butter, sosfilt
    sos = butter(5, min(0.99, cutoff / (rate / 2)), btype="low", output="sos")
    return sosfilt(sos, x).astype(np.float32)


def _deemphasis(x: np.ndarray, rate: float, tau: float = 50e-6) -> np.ndarray:
    from scipy.signal import lfilter
    a = 1 / (rate * tau + 1)
    return lfilter([a], [1, a - 1], x).astype(np.float32)


def _write_wav_chunk(wf: wave.Wave_write, x: np.ndarray) -> None:
    peak = float(np.percentile(np.abs(x), 99.5)) if x.size else 0.0
    if peak > 0:
        x = x / peak * 0.8
    x = np.clip(x, -1, 1)
    wf.writeframes((x * 32767).astype(np.int16).tobytes())


def demod_to_wav(iq_path: Path, out_path: Path, rate: float, mode: str,
                 audio_rate: int = 48_000) -> dict:
    mode = mode.lower()
    if mode == "nfm":
        audio_rate = 24_000
    prev_iq = None
    ssb_phase = 0.0
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(audio_rate)
        for iq in _iter_iq(iq_path):
            if mode in ("nfm", "wfm"):
                if prev_iq is not None and prev_iq.size:
                    iq_d = np.concatenate(([prev_iq[-1]], iq))
                    disc = np.angle(iq_d[1:] * np.conj(iq_d[:-1])).astype(np.float32)
                else:
                    disc = np.angle(iq[1:] * np.conj(iq[:-1]))
                    disc = np.concatenate(([0.0], disc)).astype(np.float32)
                prev_iq = iq
                if mode == "wfm":
                    disc = _deemphasis(_resample(disc, rate, audio_rate), audio_rate)
                else:
                    disc = _resample(disc, rate, audio_rate)
                    disc = _lowpass(disc, audio_rate, 4_000)
                _write_wav_chunk(wf, disc)
            elif mode == "am":
                env = np.abs(iq)
                env = _lowpass(env, rate, 5_000)
                env = _resample(env, rate, audio_rate)
                env = env - np.mean(env)
                _write_wav_chunk(wf, env)
            elif mode in ("usb", "lsb"):
                # Bring the 300-3000 Hz audio passband to baseband, then lowpass.
                # Keep a continuous oscillator phase across blocks to avoid clicks.
                offset = 1650.0 if mode == "usb" else -1650.0
                n = np.arange(iq.size, dtype=np.float32)
                phase = ssb_phase - 2.0 * np.pi * offset * n / rate
                mixed = iq * np.exp(1j * phase)
                ssb_phase = float(phase[-1] - 2.0 * np.pi * offset / rate) if iq.size else ssb_phase
                ssb_phase = (ssb_phase + np.pi) % (2.0 * np.pi) - np.pi
                base = mixed.real.astype(np.float32)
                base = _lowpass(base, rate, 3_200)
                base = _resample(base, rate, audio_rate)
                _write_wav_chunk(wf, base)
    return {"path": str(out_path), "mode": mode, "audio_rate": audio_rate}


def analyze_spectrum(iq_path: Path, rate: float, bins: int = 512) -> dict:
    try:
        raw = iq_path.open("rb").read(min(iq_path.stat().st_size, 8_000_000))
        arr = np.frombuffer(raw, dtype=np.int8).astype(np.float32)
        iq = arr[0::2] + 1j * arr[1::2]
        n = 8192
        segs = min(32, max(1, iq.size // n))
        if segs < 1:
            return {}
        window = np.hanning(n).astype(np.float32)
        psd = np.zeros(n, dtype=np.float64)
        for i in range(segs):
            seg = iq[i * n:(i + 1) * n]
            if seg.size < n:
                break
            spec = np.fft.fftshift(np.fft.fft(seg * window))
            psd += np.abs(spec) ** 2
        psd = 10 * np.log10(np.maximum(psd / max(1, segs), 1e-12))
        psd = psd - np.max(psd)
        step = max(1, n // bins)
        psd = psd[::step][:bins]
        return {"start_hz": -rate / 2, "end_hz": rate / 2, "bins": [round(float(v), 1) for v in psd],
                "sample_rate": rate}
    except Exception:
        return {}


def apt_to_png(iq_path: Path, out_path: Path, rate: float = 2_400_000) -> dict:
    """Experimental NOAA APT decoder: AM envelope -> 4160 px/s image."""
    from PIL import Image
    target = 4160.0
    samples = []
    for iq in _iter_iq(iq_path):
        env = np.abs(iq)
        env = _lowpass(env, rate, 2_000)
        samples.append(_resample(env, rate, target))
    if not samples:
        raise RuntimeError("No samples for APT decode")
    data = np.concatenate(samples)
    rows = data.size // 2080
    if rows < 8:
        raise RuntimeError("Capture too short for an APT frame (need >4 s at 2.08 kpx/line)")
    img = data[:rows * 2080].reshape(rows, 2080)
    lo, hi = np.percentile(img, (2, 98))
    if hi > lo:
        img = (img - lo) / (hi - lo)
    img = np.clip(img, 0, 1) * 255
    Image.fromarray(img.astype(np.uint8), mode="L").save(out_path)
    return {"path": str(out_path), "rows": int(rows), "width": 2080,
            "note": "Experimental APT decode. Image may be skewed; use a dedicated decoder for operational weather imagery."}


# --------------------------------------------------------------------------
# Transmit (gated)
# --------------------------------------------------------------------------

def transmit(iq_path: str, freq_hz: float, rate: float, vga: int, amp: bool,
             confirm: str, attest: bool) -> dict:
    db = Database()
    settings = db.all_settings()
    if not settings.get("sdr_tx_enabled"):
        db.close()
        raise PermissionError("Transmit is disabled in settings")
    if not settings.get("sdr_tx_attested"):
        db.close()
        raise PermissionError("Legal attestation is not recorded")
    if not attest or confirm.strip().upper() != "TRANSMIT":
        db.close()
        raise PermissionError('Type TRANSMIT and attest that you are licensed and authorised for this frequency')
    path = Path(iq_path)
    if not path.exists() or path.suffix.lower() not in (".iq", ".bin", ".complex", ".raw"):
        db.close()
        raise ValueError("A local IQ file is required")
    if not 1_000_000 <= freq_hz <= 6_000_000_000:
        db.close()
        raise ValueError("Frequency out of range")
    cmd = ["hackrf_transfer", "-t", str(path), "-f", str(int(freq_hz)), "-s", str(int(rate)),
           "-x", str(_quantize_gain(vga, 47, 1)), "-a", "1" if amp else "0", "-p", "0"]
    db.audit("SDR_TX", f"freq={freq_hz} rate={rate} file={path.name}")
    db.close()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(((proc.stdout or "") + (proc.stderr or ""))[:500] or "Transmit failed")
    return {"ok": True, "note": "Transmission complete. Monitor your signal and comply with local law."}


# --------------------------------------------------------------------------
# Recordings
# --------------------------------------------------------------------------

def list_recordings() -> list[dict]:
    db = Database()
    rows = db.query("SELECT * FROM sdr_recordings ORDER BY ts DESC LIMIT 100")
    db.close()
    return [dict(r) for r in rows]


def delete_recording(rec_id: int) -> dict:
    db = Database()
    row = db.one("SELECT * FROM sdr_recordings WHERE id=?", (rec_id,))
    if not row:
        db.close()
        raise ValueError("Recording not found")
    for key in ("iq_path", "audio_path", "image_path"):
        p = row[key]
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
    db.delete("sdr_recordings", rec_id)
    db.close()
    return {"deleted": rec_id}


def adsb_snapshot() -> dict:
    """Read a local dump1090 JSON feed if one is running."""
    import requests
    try:
        r = requests.get("http://127.0.0.1:8080/data/aircraft.json", timeout=1.5)
        r.raise_for_status()
        data = r.json()
        aircraft = []
        for a in data.get("aircraft", [])[:100]:
            aircraft.append({"hex": a.get("hex"), "flight": (a.get("flight") or "").strip(),
                             "alt_baro": a.get("alt_baro"), "gs": a.get("gs"),
                             "track": a.get("track"), "lat": a.get("lat"), "lon": a.get("lon")})
        return {"available": True, "aircraft": aircraft, "source": "dump1090"}
    except Exception:
        return {"available": False, "aircraft": [],
                "note": "Start dump1090 (RTL-SDR) or feed a HackRF IQ file to dump1090 --ifile, then reload."}
