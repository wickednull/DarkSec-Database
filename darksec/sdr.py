"""SDR integration: HackRF, RTL-SDR, SoapySDR, network rtl_tcp and WebSDR.

Real hardware only: every function shells out to the installed SDR tools and
reports exactly what the device says. If no radio is present the console shows
NO DEVICE instead of inventing a spectrum.

RX: wideband sweep, IQ capture, AM/NFM/WFM/USB/LSB demodulation, live audio,
spectrum analysis and experimental NOAA APT weather-satellite image decoding.
NET: rtl_tcp client/server, so a Raspberry Pi or uConsole can feed another
DarkSec instance over the LAN.
TX: hard-gated behind an explicit settings unlock, legal attestation and a
confirmation phrase, and every attempt is audited.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import struct
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
_DETECT = {"at": 0.0, "data": None}
_DETECT_TTL = 20.0
_RTL_TCP_SERVER: dict = {"proc": None, "port": None, "bind": None, "started": None}


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


def _settings() -> dict:
    try:
        db = Database()
        try:
            return db.all_settings()
        finally:
            db.close()
    except Exception:
        return {}


def _read_first(paths: tuple[str, ...], default: str = "") -> str:
    for path in paths:
        try:
            value = Path(path).read_text(errors="replace").strip("\x00 \n\t")
            if value:
                return value
        except OSError:
            continue
    return default


def _decode_throttled(raw) -> list[str]:
    try:
        value = int(str(raw or "0"), 16) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return []
    bits = {
        0: "under-voltage detected",
        1: "ARM frequency capped",
        2: "currently throttled",
        3: "soft temperature limit active",
        16: "under-voltage has occurred",
        17: "ARM frequency capping has occurred",
        18: "throttling has occurred",
        19: "soft temperature limit has occurred",
    }
    return [label for bit, label in bits.items() if value & (1 << bit)]


def platform_info() -> dict:
    """Board and thermal state for uConsole, Raspberry Pi and desktops."""
    model = _read_first((
        "/proc/device-tree/model",
        "/sys/firmware/devicetree/base/model",
        "/sys/class/dmi/id/product_name",
    ), "unknown")
    board = _read_first(("/sys/class/dmi/id/board_name",
                          "/sys/class/dmi/id/product_name"), "")
    compatible = _read_first(("/proc/device-tree/compatible",
                              "/sys/firmware/devicetree/base/compatible"), "")
    if board and board.lower() not in model.lower():
        model = f"{model} / {board}"
    arch = os.uname().machine if hasattr(os, "uname") else "unknown"
    low = f"{model} {compatible}".lower()
    throttled_raw = ""
    if shutil.which("vcgencmd"):
        _rc, out = _run(["vcgencmd", "get_throttled"], 3)
        m = re.search(r"throttled=(0x[0-9a-fA-F]+|\d+)", out)
        if m:
            throttled_raw = m.group(1)
    thermal = None
    thermal_dir = Path("/sys/class/thermal")
    thermal_paths = sorted(thermal_dir.glob("thermal_zone*/temp")) if thermal_dir.exists() else []
    for path in thermal_paths:
        try:
            value = float(path.read_text().strip())
            thermal = round(value / 1000.0, 1) if value > 200 else round(value, 1)
            break
        except (OSError, ValueError):
            continue
    try:
        memory_mb = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        memory_mb = None
    return {
        "model": model,
        "arch": arch,
        "system": "Linux" if os.name == "posix" else os.name,
        "uconsole": "uconsole" in low or "clockwork" in low,
        "raspberry_pi": "raspberry pi" in low or "bcm27" in low,
        "thermal_c": thermal,
        "throttled": throttled_raw,
        "throttled_flags": _decode_throttled(throttled_raw),
        "memory_mb": memory_mb,
    }


def _platform_guidance(platform: dict, rtl: dict, hackrf: dict, tools: dict) -> list[dict]:
    notes: list[dict] = []

    def add(level: str, text: str) -> None:
        notes.append({"level": level, "text": text})

    if platform.get("uconsole"):
        add("info", "uConsole: the side USB-C port is power-only. Use the USB-A port for an SDR dongle.")
        add("info", "HackerGadgets AIO V2: the SDR port is off until GPIO 7 is high. Run `pinctrl 7 op; pinctrl 7 dh`, or enable SDR in aiov2_ctl and reboot.")
        add("info", "After unplugging an RTL-SDR, wait 15–30 seconds before reconnecting; the dongle needs a cool-down before it enumerates again.")
    if platform.get("raspberry_pi"):
        add("info", "Raspberry Pi: use a powered USB hub. SDR, Wi-Fi and the display together can exceed the board's USB current budget.")
        add("info", "Keep RTL-SDR at 2.4 Msps or lower on Pi 3/Zero. Pi 4/5 can use 2.4–3.2 Msps with a good power supply.")
    for flag in platform.get("throttled_flags") or []:
        add("warn", f"Raspberry Pi power/thermal flag: {flag}. Use a 5 V/3 A supply, improve cooling, and lower the SDR sample rate.")
    if platform.get("thermal_c") and platform["thermal_c"] >= 80:
        add("warn", f"CPU temperature is {platform['thermal_c']} °C. Reduce sample rate or improve airflow before long captures.")
    if not tools.get("rtl_test"):
        add("info", "RTL-SDR tools not installed. Debian/Ubuntu/Pi: `sudo apt install rtl-sdr`; Fedora: `sudo dnf install rtl-sdr`; Arch: `sudo pacman -S rtl-sdr`.")
    elif rtl.get("present") and not rtl.get("connected"):
        add("warn", "RTL-SDR tools are installed but no dongle was detected. Check the USB-A port, cable and `lsusb` (look for 0bda:2838).")
        add("info", "If the dongle is present but busy, stop gqrx/SDR++/rtl_tcp and add your user to `plugdev`. RTL-SDR udev rules come with the rtl-sdr package.")
        add("info", "If `rtl_test` reports a DVB driver claim error, blacklist `dvb_usb_rtl28xxu` (and optionally `rtl2832`/`rtl2830`) in /etc/modprobe.d/ and replug.")
    if not tools.get("hackrf_info"):
        add("info", "HackRF tools not installed. Debian/Ubuntu: `sudo apt install hackrf`; build from greatscottgadgets/hackrf for the newest firmware tools.")
    elif hackrf.get("present") and not hackrf.get("connected"):
        add("warn", "HackRF tools are installed but no HackRF responded. Check the USB cable, use a powered hub, and verify udev permissions.")
    if not tools.get("SoapySDRUtil"):
        add("info", "Optional: install SoapySDR (`soapysdr-tools` plus a device module) for SDRplay, Airspy, LimeSDR and PlutoSDR inventory.")
    add("info", "Network SDR: on a Pi/uConsole run `rtl_tcp -a 0.0.0.0 -p 1234`, then point another DarkSec instance at its IP and port.")
    add("info", "A remote WebSDR/KiwiSDR receiver works without any local radio; use the WebSDR panel and respect each operator's limits.")
    return notes


def _parse_rtl_test(out: str) -> dict:
    tuner = re.search(r"Found\s+(.+?)\s+tuner", out)
    gains = re.search(r"Supported gain values \(\d+\):\s*(.+)", out)
    rate = re.search(r"Sampling at\s+([\d.]+)", out)
    count = re.search(r"Found\s+(\d+)\s+device", out)
    error = ""
    for line in out.splitlines():
        low = line.lower()
        if ("usb_claim_interface" in low or "no supported devices" in low
                or "failed to open" in low or "open() failed" in low):
            error = line.strip()
            break
    gain_values = []
    if gains:
        try:
            gain_values = [float(v) for v in gains.group(1).split()]
        except ValueError:
            gain_values = []
    return {
        "tuner": tuner.group(1).strip() if tuner else "",
        "gains": gain_values,
        "sample_rate": float(rate.group(1)) if rate else None,
        "device_count": int(count.group(1)) if count else 0,
        "error": error,
    }


def detect(force: bool = False) -> dict:
    now = time.monotonic()
    if not force and _DETECT["data"] and now - _DETECT["at"] < _DETECT_TTL:
        return _DETECT["data"]
    hackrf_bin = shutil.which("hackrf_info")
    tool_names = (
        "hackrf_info", "hackrf_sweep", "hackrf_transfer", "rtl_test", "rtl_sdr",
        "rtl_power", "rtl_fm", "rtl_tcp", "rtl_eeprom", "SoapySDRUtil", "gqrx",
        "inspectrum", "sox", "ffmpeg", "gpspipe", "dump1090", "readsb",
        "rtl_433", "multimon-ng", "direwolf",
    )
    tools = {name: shutil.which(name) for name in tool_names}
    info: dict = {
        "tools": tools,
        "hackrf": {"present": bool(hackrf_bin), "connected": False, "raw": ""},
        "rtl": {"present": bool(tools.get("rtl_test")), "connected": False, "raw": ""},
        "soapy": {"present": bool(tools.get("SoapySDRUtil")), "connected": False, "devices": []},
    }
    if hackrf_bin:
        rc, out = _run([hackrf_bin], 8)
        info["hackrf"]["raw"] = out.strip()[:1200]
        info["hackrf"]["connected"] = "No HackRF boards found" not in out and rc == 0
        for key, pattern in (
            ("board_id", r"Board ID Number:\s*(.+)"), ("firmware", r"Firmware Version:\s*(.+)"),
            ("serial", r"Serial number:\s*(.+)"), ("part_id", r"Part ID Number:\s*(.+)"),
        ):
            m = re.search(pattern, out)
            if m:
                info["hackrf"][key] = m.group(1).strip()
    if tools.get("rtl_test"):
        _rc, out = _run([tools["rtl_test"], "-t"], 6)
        parsed = _parse_rtl_test(out)
        info["rtl"].update(parsed)
        info["rtl"]["raw"] = out.strip()[:1400]
        info["rtl"]["connected"] = bool(parsed["device_count"]) and not parsed["error"]
        if not parsed["device_count"] and "Found" in out and "No supported" not in out:
            info["rtl"]["connected"] = True
    if tools.get("SoapySDRUtil"):
        _rc, out = _run([tools["SoapySDRUtil"], "--find"], 8)
        devices = []
        current: dict = {}
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("Found device"):
                if current:
                    devices.append(current)
                current = {}
            elif "=" in line:
                key, value = line.split("=", 1)
                current[key.strip()] = value.strip()
        if current:
            devices.append(current)
        info["soapy"]["devices"] = devices
        info["soapy"]["connected"] = bool(devices)
        info["soapy"]["raw"] = out.strip()[:1200]
    settings = _settings()
    host = str(settings.get("sdr_rtl_tcp_host") or "").strip()
    try:
        port = int(settings.get("sdr_rtl_tcp_port") or 1234)
    except (TypeError, ValueError):
        port = 1234
    info["rtl_tcp"] = {"configured": bool(host), "host": host, "port": port,
                       "connected": False}
    platform = platform_info()
    info["platform"] = platform
    info["guidance"] = _platform_guidance(platform, info["rtl"], info["hackrf"], tools)
    backends = [
        {"id": "hackrf", "name": "HackRF", "connected": bool(info["hackrf"]["connected"]),
         "rx": True, "tx": True},
        {"id": "rtl", "name": "RTL-SDR", "connected": bool(info["rtl"]["connected"]),
         "rx": True, "tx": False},
        {"id": "soapy", "name": "SoapySDR", "connected": bool(info["soapy"]["connected"]),
         "rx": False, "tx": False, "note": "device inventory only; capture support pending"},
        {"id": "rtl_tcp", "name": "Network rtl_tcp", "connected": False,
         "configured": bool(host), "host": host, "port": port, "rx": True, "tx": False},
        {"id": "websdr", "name": "WebSDR / KiwiSDR", "connected": True,
         "rx": True, "tx": False, "note": "remote web receiver"},
    ]
    info["backends"] = backends
    info["active_backend"] = next(
        (b["id"] for b in backends if b["id"] in ("hackrf", "rtl") and b["connected"]),
        "rtl_tcp" if host else "none",
    )
    info["gps"] = {"gpspipe": bool(tools.get("gpspipe"))}
    info["adsb"] = {"dump1090": bool(tools.get("dump1090") or tools.get("readsb"))}
    with _LOCK:
        _DETECT.update({"at": now, "data": info})
    return info


def choose_backend(preferred: str = "auto") -> str:
    info = detect()
    preferred = str(preferred or "auto").lower()
    if preferred in ("hackrf", "rtl", "rtl_tcp"):
        return preferred
    if info["hackrf"].get("connected"):
        return "hackrf"
    if info["rtl"].get("connected"):
        return "rtl"
    if info.get("rtl_tcp", {}).get("configured"):
        return "rtl_tcp"
    return "none"


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


def _hackrf_sweep_stream(start_hz: float, end_hz: float, bin_hz: float = 100_000,
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


def _parse_rtl_power_line(line: str) -> dict | None:
    """Parse one rtl_power CSV row: date,time,low,high,step,samples,dB..."""
    line = line.strip()
    if not line or line.lower().startswith("date"):
        return None
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 7:
        return None
    try:
        low = float(parts[2]); high = float(parts[3])
        step = float(parts[4]); count = int(float(parts[5]))
        db = [float(x) for x in parts[6:6 + count] if x not in ("", "nan", "NaN")]
    except (ValueError, IndexError):
        return None
    if not db:
        return None
    return {"stamp": parts[0] + " " + parts[1], "low_hz": low, "high_hz": high,
            "bin_hz": step, "db": db}


def _rtl_sweep_stream(start_hz: float, end_hz: float, bin_hz: float = 100_000,
                      gain: int = 40, ppm: int = 0, device: int = 0):
    """Yield sweep frames from rtl_power, one CSV row per sweep interval."""
    if not shutil.which("rtl_power"):
        yield {"type": "error", "error": "rtl_power is not installed (package: rtl-sdr)"}
        return
    start_hz, end_hz = int(start_hz), int(end_hz)
    bin_hz = max(1_000, min(2_800_000, int(bin_hz)))
    cmd = ["rtl_power", "-f", f"{start_hz}:{end_hz}:{bin_hz}", "-i", "1",
           "-g", str(max(0, int(gain))), "-p", str(int(ppm)), "-d", str(int(device)), "-"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
    except Exception as exc:
        yield {"type": "error", "error": str(exc)}
        return
    frames = 0
    try:
        for line in proc.stdout:
            parsed = _parse_rtl_power_line(line)
            if not parsed:
                continue
            frames += 1
            yield _frame({"stamp": parsed["stamp"], "bins": parsed["db"],
                          "low_hz": parsed["low_hz"], "high_hz": parsed["high_hz"],
                          "bin_hz": parsed["bin_hz"]})
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
    if err and frames == 0:
        first = next((ln.strip() for ln in err.splitlines() if ln.strip()), err.strip())
        if "no supported" in first.lower() or "usb_claim" in first.lower():
            first = ("No RTL-SDR device available. On a uConsole use the USB-A port; "
                     "if it was just re-plugged wait 15-30 s. " + first)[:400]
        yield {"type": "error", "error": first[:400]}


def sweep_stream(start_hz: float, end_hz: float, bin_hz: float = 100_000,
                 lna: int = 32, vga: int = 40, amp: bool = False,
                 backend: str = "auto", gain: int = 40, ppm: int = 0,
                 device: int = 0):
    """Yield sweep frames from the best available local receiver."""
    backend = choose_backend(backend)
    if backend == "hackrf":
        yield from _hackrf_sweep_stream(start_hz, end_hz, bin_hz, lna, vga, amp)
        return
    if backend == "rtl":
        yield from _rtl_sweep_stream(start_hz, end_hz, bin_hz, gain, ppm, device)
        return
    info = detect()
    if backend == "rtl_tcp":
        yield {"type": "error", "error": "Wideband sweep over rtl_tcp is not supported; use Live audio or a local RTL-SDR."}
        return
    notes = " ".join(n["text"] for n in (info.get("guidance") or [])[:3])
    yield {"type": "error", "error": "No SDR receiver found. " + notes[:500]}


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


# --------------------------------------------------------------------------
# Network rtl_tcp
# --------------------------------------------------------------------------

class RTLTCPClient:
    """Minimal rtl_tcp client (big-endian 5-byte commands, uint8 IQ stream)."""

    def __init__(self, host: str, port: int = 1234, timeout: float = 5.0):
        self.host = str(host or "").strip()
        self.port = int(port)
        self.timeout = float(timeout)
        self.sock: socket.socket | None = None
        self.info: dict = {}

    def connect(self) -> dict:
        if not self.host:
            raise ValueError("rtl_tcp host is not configured")
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        header = self._read_exact(12)
        if len(header) < 12 or header[:4] != b"RTL0":
            raise RuntimeError("Not an rtl_tcp server (missing RTL0 header)")
        tuner, gains = struct.unpack(">II", header[4:12])
        self.info = {"host": self.host, "port": self.port, "magic": "RTL0",
                     "tuner_type": tuner, "gain_count": gains}
        return self.info

    def _read_exact(self, count: int) -> bytes:
        if self.sock is None:
            return b""
        out = b""
        while len(out) < count:
            chunk = self.sock.recv(count - len(out))
            if not chunk:
                break
            out += chunk
        return out

    def _cmd(self, command: int, value: int) -> None:
        if self.sock is None:
            raise RuntimeError("rtl_tcp is not connected")
        self.sock.sendall(struct.pack(">BI", command, int(value) & 0xFFFFFFFF))

    def set_frequency(self, hz: int) -> None:
        self._cmd(0x01, int(hz))

    def set_sample_rate(self, rate: int) -> None:
        self._cmd(0x02, int(rate))

    def set_gain_mode(self, manual: bool = True) -> None:
        self._cmd(0x03, 1 if manual else 0)

    def set_gain(self, db: float) -> None:
        self._cmd(0x04, int(round(float(db) * 10)))

    def set_ppm(self, ppm: int) -> None:
        self._cmd(0x05, int(ppm) & 0xFFFFFFFF)

    def set_bias_tee(self, enabled: bool) -> None:
        self._cmd(0x0E, 1 if enabled else 0)

    def read(self, count: int) -> bytes:
        if self.sock is None:
            return b""
        try:
            return self.sock.recv(count)
        except (socket.timeout, OSError):
            return b""

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


def probe_rtl_tcp(host: str, port: int = 1234) -> dict:
    client = RTLTCPClient(host, port, timeout=4.0)
    try:
        info = client.connect()
        info["ok"] = True
        return info
    except Exception as exc:
        return {"ok": False, "host": host, "port": port, "error": str(exc)[:240]}
    finally:
        client.close()


def start_capture(freq_hz: float, seconds: float = 5.0, rate: float = 2_400_000,
                  demod: str = "nfm", lna: int = 32, vga: int = 40, amp: bool = False,
                  keep_iq: bool = False, name: str = "", backend: str = "auto",
                  host: str = "", port: int = 0) -> dict:
    freq_hz = float(freq_hz)
    rate = float(rate)
    seconds = float(seconds)
    backend = choose_backend(backend)
    if backend == "none":
        info = detect()
        notes = " ".join(n["text"] for n in (info.get("guidance") or [])[:3])
        raise ValueError("No SDR receiver found. " + notes[:400])
    if backend == "rtl" and not detect().get("rtl", {}).get("connected"):
        raise ValueError("No RTL-SDR detected. On a uConsole use USB-A and wait 15-30 s after re-plugging.")
    if backend == "rtl_tcp" and not (host or _settings().get("sdr_rtl_tcp_host")):
        raise ValueError("rtl_tcp host is not configured")
    if not 1_000_000 <= freq_hz <= 6_000_000_000:
        raise ValueError("Frequency must be 1 MHz - 6 GHz")
    if backend == "hackrf":
        if not 2_000_000 <= rate <= 20_000_000:
            raise ValueError("HackRF sample rate must be 2-20 Msps")
    else:
        if not 900_000 <= rate <= 3_200_000:
            raise ValueError("RTL-SDR sample rate must be 0.9-3.2 Msps")
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
    settings = _settings()
    job = _new_job("capture", freq_hz=freq_hz, rate=rate, seconds=seconds, demod=demod,
                   name=name or f"{freq_hz/1e6:.4f} MHz {demod.upper()}", keep_iq=keep_iq,
                   samples=samples, lna=lna, vga=vga, amp=amp, backend=backend,
                   sample_format="i8" if backend == "hackrf" else "u8",
                   ppm=int(settings.get("sdr_ppm") or 0),
                   rtl_gain=int(settings.get("sdr_rtl_gain") or 40),
                   host=str(host or settings.get("sdr_rtl_tcp_host") or "").strip(),
                   port=int(port or settings.get("sdr_rtl_tcp_port") or 1234))
    thread = threading.Thread(target=_capture_worker, args=(job["id"],), daemon=True)
    thread.start()
    return get_job(job["id"])


def _capture_hackrf(job_id: str, job: dict, iq_path: Path, samples: int,
                    seconds: float) -> None:
    rate = job["rate"]; freq = job["freq_hz"]
    cmd = ["hackrf_transfer", "-r", str(iq_path), "-f", str(int(freq)), "-s", str(int(rate)),
           "-n", str(int(samples)), "-l", str(int(job.get("lna", 32))),
           "-g", str(int(job.get("vga", 40))), "-a", "1" if job.get("amp") else "0", "-p", "0"]
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
        if time.time() > deadline or (_JOBS.get(job_id) or {}).get("stop"):
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


def _capture_rtl(job_id: str, job: dict, iq_path: Path, samples: int,
                 seconds: float) -> None:
    rate = job["rate"]; freq = job["freq_hz"]
    cmd = ["rtl_sdr", "-f", str(int(freq)), "-s", str(int(rate)),
           "-n", str(int(samples)), "-g", str(int(job.get("rtl_gain") or 40)),
           "-d", "0"]
    if job.get("ppm"):
        cmd += ["-p", str(int(job["ppm"]))]
    cmd.append(str(iq_path))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    with _LOCK:
        _PROCS[job_id] = proc
    deadline = time.time() + seconds + 45
    while proc.poll() is None:
        try:
            size = iq_path.stat().st_size if iq_path.exists() else 0
            _update_job(job_id, progress=round(min(0.98, size / max(1, samples * 2)), 3))
        except Exception:
            pass
        if time.time() > deadline or (_JOBS.get(job_id) or {}).get("stop"):
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
        if "usb_claim" in err.lower() or "no supported" in err.lower():
            err = ("RTL-SDR is busy or not detected. Stop other SDR programs. On a "
                   "uConsole use USB-A and wait 15-30 s after re-plugging. " + err)
        raise RuntimeError(err or f"rtl_sdr exited {rc}; is the RTL-SDR connected?")


def _capture_rtl_tcp(job_id: str, job: dict, iq_path: Path, samples: int,
                     seconds: float) -> None:
    client = RTLTCPClient(job.get("host") or "", int(job.get("port") or 1234))
    client.connect()
    client.set_sample_rate(int(job["rate"]))
    client.set_frequency(int(job["freq_hz"]))
    client.set_gain_mode(True)
    client.set_gain(int(job.get("rtl_gain") or 40))
    if job.get("ppm"):
        client.set_ppm(int(job["ppm"]))
    remaining = samples * 2
    deadline = time.time() + seconds + 30
    with open(iq_path, "wb") as fh:
        while remaining > 0:
            if (_JOBS.get(job_id) or {}).get("stop") or time.time() > deadline:
                _update_job(job_id, status="stopped", finished=utcnow())
                return
            chunk = client.read(min(262144, remaining))
            if not chunk:
                break
            fh.write(chunk)
            remaining -= len(chunk)
            _update_job(job_id, progress=round(min(0.98, (samples * 2 - remaining) / max(1, samples * 2)), 3))
    client.close()
    if remaining > 0:
        raise RuntimeError("rtl_tcp closed before the requested samples arrived")


def _capture_worker(job_id: str) -> None:
    with _LOCK:
        job = dict(_JOBS[job_id])
    freq = job["freq_hz"]; rate = job["rate"]; seconds = job["seconds"]
    samples = job["samples"]; demod = job.get("demod") or "iq"
    backend = job.get("backend") or "hackrf"
    sample_format = job.get("sample_format") or ("i8" if backend == "hackrf" else "u8")
    keep_iq = bool(job.get("keep_iq"))
    iq_path = config.RECORDING_DIR / f"{job_id}.iq"
    audio_path = config.RECORDING_DIR / f"{job_id}.wav"
    image_path = config.RECORDING_DIR / f"{job_id}.png"
    _update_job(job_id, status="running", progress=0.0)
    try:
        if backend == "rtl":
            _capture_rtl(job_id, job, iq_path, samples, seconds)
        elif backend == "rtl_tcp":
            _capture_rtl_tcp(job_id, job, iq_path, samples, seconds)
        else:
            _capture_hackrf(job_id, job, iq_path, samples, seconds)
        if (_JOBS.get(job_id) or {}).get("status") == "stopped":
            return
        if not iq_path.exists() or iq_path.stat().st_size < 1024:
            raise RuntimeError("No IQ samples captured; check the radio and USB permissions")
        _update_job(job_id, progress=0.99, status="processing")
        spectrum = analyze_spectrum(iq_path, rate, sample_format=sample_format)
        _update_job(job_id, spectrum=json.dumps(spectrum))
        if demod in ("am", "nfm", "wfm", "usb", "lsb"):
            demod_to_wav(iq_path, audio_path, rate, demod, sample_format=sample_format)
        elif demod == "apt":
            apt_to_png(iq_path, image_path, rate, sample_format=sample_format)
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
            "size": record_size,
            "note": f"backend={backend};format={sample_format}",
        })
        db.close()
    except Exception as exc:
        with _LOCK:
            proc = _PROCS.pop(job_id, None)
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
        _update_job(job_id, status="error", error=str(exc)[:500], finished=utcnow())


# --------------------------------------------------------------------------
# DSP
# --------------------------------------------------------------------------

def _iter_iq(path: Path, block_samples: int = 2_000_000,
             sample_format: str = "i8"):
    """Yield complex IQ blocks from an 8-bit capture (HackRF int8 or RTL uint8)."""
    unsigned = str(sample_format or "i8").lower() in ("u8", "uint8", "rtl")
    with open(path, "rb") as fh:
        while True:
            raw = fh.read(block_samples * 2)
            if len(raw) < 4:
                break
            if len(raw) % 2:
                raw = raw[:-1]
            if unsigned:
                arr = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
                arr = (arr - 127.5) / 127.5
            else:
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
                 audio_rate: int = 48_000, sample_format: str = "i8") -> dict:
    mode = mode.lower()
    if mode == "nfm":
        audio_rate = 24_000
    prev_iq = None
    ssb_phase = 0.0
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(audio_rate)
        for iq in _iter_iq(iq_path, sample_format=sample_format):
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


def analyze_spectrum(iq_path: Path, rate: float, bins: int = 512,
                     sample_format: str = "i8") -> dict:
    try:
        raw = iq_path.open("rb").read(min(iq_path.stat().st_size, 8_000_000))
        if str(sample_format).lower() in ("u8", "uint8", "rtl"):
            arr = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
            arr = (arr - 127.5) / 127.5
        else:
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


def apt_to_png(iq_path: Path, out_path: Path, rate: float = 2_400_000,
               sample_format: str = "i8") -> dict:
    """Experimental NOAA APT decoder: AM envelope -> 4160 px/s image."""
    from PIL import Image
    target = 4160.0
    samples = []
    for iq in _iter_iq(iq_path, sample_format=sample_format):
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


# --------------------------------------------------------------------------
# Live audio (server-side DSP, browser plays PCM)
# --------------------------------------------------------------------------

class _LiveSpectrum:
    """FFT display frames from the same IQ blocks used for live audio."""

    def __init__(self, fft_size: int = 2048, bins: int = 512):
        self.fft_size = max(256, int(fft_size))
        self.bins = max(64, int(bins))
        self.window = np.hanning(self.fft_size).astype(np.float32)

    def process(self, iq: np.ndarray, center_hz: float, rate: float) -> dict | None:
        if iq.size < self.fft_size:
            return None
        segment = iq[-self.fft_size:]
        spec = np.fft.fftshift(np.fft.fft(segment * self.window))
        power = 20.0 * np.log10(np.maximum(np.abs(spec) / self.fft_size, 1e-9))
        step = max(1, power.size // self.bins)
        values = power[::step][:self.bins]
        peak_idx = int(np.argmax(values))
        span = float(rate)
        start = float(center_hz) - span / 2.0
        return {
            "type": "spectrum",
            "center_hz": float(center_hz),
            "rate": float(rate),
            "start_hz": start,
            "end_hz": start + span,
            "bins": [round(float(v), 1) for v in values],
            "peak_hz": round(start + (peak_idx / max(1, len(values) - 1)) * span, 1),
            "peak_db": round(float(values[peak_idx]), 1),
        }


def live_audio_rate(rate: float) -> int:
    rate = float(rate or 1_024_000)
    decim = max(1, int(round(rate / 48_000)))
    return int(round(rate / decim))


class _LiveDemod:
    """Block-continuous demodulator for live PCM audio.

    State is kept across blocks (filter, discriminator, AGC, de-emphasis) so
    tuning does not produce a click every 50 ms.
    """

    def __init__(self, mode: str, rate: float, out_rate: int = 48_000,
                 squelch: float = 0.0):
        from scipy.signal import butter

        self.mode = str(mode or "nfm").lower()
        self.rate = float(rate)
        self.decim = max(1, int(round(self.rate / out_rate)))
        self.out_rate = self.rate / self.decim
        cutoff = {"wfm": 15_000.0, "nfm": 4_000.0, "am": 5_000.0,
                  "usb": 3_000.0, "lsb": 3_000.0}.get(self.mode, 4_000.0)
        cutoff = min(cutoff, self.rate * 0.45)
        self.sos = butter(5, max(100.0, cutoff) / (self.rate / 2), btype="low", output="sos")
        self.zi = np.zeros((self.sos.shape[0], 2), dtype=np.float64)
        self.prev = None
        self.ssb_phase = 0.0
        self.de_state = 0.0
        self.agc = 0.05
        self.squelch = max(0.0, min(100.0, float(squelch or 0)))

    def _bytes_to_iq(self, data: bytes, sample_format: str) -> np.ndarray:
        if len(data) % 2:
            data = data[:-1]
        if str(sample_format).lower() in ("u8", "uint8", "rtl"):
            arr = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
            arr = (arr - 127.5) / 127.5
        else:
            arr = np.frombuffer(data, dtype=np.int8).astype(np.float32)
        iq = arr[0::2] + 1j * arr[1::2]
        if iq.size:
            iq -= np.mean(iq)
        return iq

    def process(self, iq: np.ndarray) -> bytes:
        if iq.size < 8:
            return b""
        if self.mode in ("nfm", "wfm"):
            if self.prev is not None:
                combined = np.concatenate(([self.prev], iq))
                x = np.angle(combined[1:] * np.conj(combined[:-1])).astype(np.float32)
            else:
                x = np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32)
                x = np.concatenate(([0.0], x))
            self.prev = iq[-1]
        elif self.mode == "am":
            x = np.abs(iq).astype(np.float32)
            x -= float(np.mean(x))
        elif self.mode in ("usb", "lsb"):
            offset = 1650.0 if self.mode == "usb" else -1650.0
            n = np.arange(iq.size, dtype=np.float32)
            phase = self.ssb_phase - 2.0 * np.pi * offset * n / self.rate
            mixed = iq * np.exp(1j * phase)
            self.ssb_phase = float(phase[-1] - 2.0 * np.pi * offset / self.rate) if iq.size else self.ssb_phase
            self.ssb_phase = (self.ssb_phase + np.pi) % (2.0 * np.pi) - np.pi
            x = mixed.real.astype(np.float32)
        else:
            return b""
        from scipy.signal import lfilter, sosfilt
        filtered, self.zi = sosfilt(self.sos, x, zi=self.zi)
        audio = filtered[::self.decim]
        if not audio.size:
            return b""
        if self.mode == "wfm":
            tau = 75e-6
            a = 1.0 / (self.out_rate * tau + 1.0)
            audio, zf = lfilter([a], [1.0, a - 1.0], audio,
                                zi=[float(self.de_state) * (1.0 - a)])
            self.de_state = float(zf[0]) if len(zf) else 0.0
        rms = float(np.sqrt(np.mean(np.square(audio))) or 0.0)
        peak = max(self.agc * 0.995, float(np.max(np.abs(audio))) + 1e-9)
        self.agc = peak
        audio = (audio / peak) * 0.8
        if self.squelch > 0:
            threshold = 0.004 + (self.squelch / 100.0) * 0.12
            if rms < threshold:
                audio = np.zeros_like(audio)
        audio = np.clip(audio, -1.0, 1.0)
        return (audio * 32767.0).astype("<i2").tobytes()


def _live_source(backend: str, freq_hz: float, rate: float, gain: float,
                 ppm: int, host: str, port: int, lna: int, vga: int,
                 amp: bool, device: int = 0, stop_event=None):
    """Yield (bytes, sample_format) IQ blocks from a live receiver."""
    backend = choose_backend(backend)
    block = max(4096, int(rate * 0.05)) * 2
    proc = None
    client = None
    try:
        if backend == "hackrf":
            cmd = ["hackrf_transfer", "-r", "-", "-f", str(int(freq_hz)),
                   "-s", str(int(rate)), "-l", str(int(lna)), "-g", str(int(vga)),
                   "-a", "1" if amp else "0", "-p", "0"]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        elif backend == "rtl":
            cmd = ["rtl_sdr", "-f", str(int(freq_hz)), "-s", str(int(rate)),
                   "-g", str(int(gain)), "-d", str(int(device))]
            if ppm:
                cmd += ["-p", str(int(ppm))]
            cmd.append("-")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        elif backend == "rtl_tcp":
            client = RTLTCPClient(host, int(port), timeout=5.0)
            client.connect()
            client.set_sample_rate(int(rate))
            client.set_frequency(int(freq_hz))
            client.set_gain_mode(True)
            client.set_gain(float(gain))
            if ppm:
                client.set_ppm(int(ppm))
        else:
            return
        sample_format = "i8" if backend == "hackrf" else "u8"
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if proc is not None:
                data = proc.stdout.read(block) if proc.stdout else b""
            elif client is not None:
                data = client.read(block)
            else:
                break
            if not data:
                break
            yield data, sample_format
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if client is not None:
            client.close()


def live_preflight(backend: str = "auto", host: str = "", port: int = 1234) -> dict:
    chosen = choose_backend(backend)
    if chosen == "none":
        info = detect()
        notes = " ".join(n["text"] for n in (info.get("guidance") or [])[:3])
        raise ValueError("No SDR receiver found. " + notes[:400])
    if chosen == "hackrf":
        if not shutil.which("hackrf_transfer"):
            raise ValueError("hackrf_transfer is not installed")
        if not detect().get("hackrf", {}).get("connected"):
            raise ValueError("No HackRF detected. Connect it, use a powered hub and check USB permissions.")
    elif chosen == "rtl":
        if not shutil.which("rtl_sdr"):
            raise ValueError("rtl_sdr is not installed (package: rtl-sdr)")
        if not detect().get("rtl", {}).get("connected"):
            raise ValueError("No RTL-SDR detected. On a uConsole use USB-A and wait 15-30 s after re-plugging.")
    elif chosen == "rtl_tcp":
        if not host:
            raise ValueError("rtl_tcp host is not configured")
        probe = probe_rtl_tcp(host, int(port))
        if not probe.get("ok"):
            raise ValueError(f"rtl_tcp {host}:{port} did not answer: {probe.get('error')}")
    return {"backend": chosen}


def live_audio_stream(freq_hz: float, mode: str = "nfm", backend: str = "auto",
                      rate: float = 1_024_000, gain: float = 40, ppm: int = 0,
                      squelch: float = 0, host: str = "", port: int = 1234,
                      lna: int = 32, vga: int = 40, amp: bool = False,
                      device: int = 0, stop_event=None, max_seconds: float = 900.0):
    """Yield signed 16-bit little-endian mono PCM in real time."""
    chosen = choose_backend(backend)
    deadline = time.monotonic() + max(10.0, float(max_seconds or 900.0))
    rate = max(2_000_000.0, min(20_000_000.0, rate)) if chosen == "hackrf" else \
        max(900_000.0, min(3_200_000.0, rate))
    demod = _LiveDemod(mode, rate, squelch=squelch)
    source = _live_source(chosen, freq_hz, rate, gain, ppm, host, port, lna, vga,
                          amp, device, stop_event=stop_event)
    try:
        for data, sample_format in source:
            if (stop_event is not None and stop_event.is_set()) or time.monotonic() > deadline:
                break
            iq = demod._bytes_to_iq(data, sample_format)
            pcm = demod.process(iq)
            if pcm:
                yield pcm
    finally:
        try:
            source.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# rtl_tcp server (share a local RTL-SDR with another DarkSec/phone)
# --------------------------------------------------------------------------

def lan_urls(port: int) -> list[str]:
    urls: list[str] = []
    try:
        import psutil

        for name, addrs in psutil.net_if_addrs().items():
            if name.startswith(("lo", "docker", "veth", "br-")):
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    if addr.address.startswith(("10.", "192.168.", "172.")) or addr.address.startswith("169.254."):
                        urls.append(f"rtl_tcp://{addr.address}:{port}")
    except Exception:
        pass
    return sorted(set(urls))


def rtl_tcp_server_status() -> dict:
    proc = _RTL_TCP_SERVER.get("proc")
    running = bool(proc is not None and proc.poll() is None)
    port = _RTL_TCP_SERVER.get("port")
    return {
        "running": running,
        "pid": proc.pid if running else None,
        "bind": _RTL_TCP_SERVER.get("bind"),
        "port": port,
        "started": _RTL_TCP_SERVER.get("started"),
        "urls": lan_urls(int(port)) if running and port else [],
        "note": ("rtl_tcp has no authentication. Use it only on a trusted private LAN."
                 if running else "Start rtl_tcp to share this RTL-SDR with another device."),
    }


def stop_rtl_tcp_server() -> dict:
    proc = _RTL_TCP_SERVER.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _RTL_TCP_SERVER.update({"proc": None, "port": None, "bind": None, "started": None})
    return rtl_tcp_server_status()


def start_rtl_tcp_server(bind: str = "0.0.0.0", port: int = 1234,
                         freq_hz: float = 100_000_000, rate: float = 2_400_000,
                         gain: float = 40, ppm: int = 0, bias: bool = False) -> dict:
    if not shutil.which("rtl_tcp"):
        raise ValueError("rtl_tcp is not installed (package: rtl-sdr)")
    info = detect()
    if not info.get("rtl", {}).get("connected"):
        raise ValueError("No RTL-SDR detected. Connect it (USB-A on uConsole) and rescan.")
    stop_rtl_tcp_server()
    cmd = ["rtl_tcp", "-a", str(bind or "0.0.0.0"), "-p", str(int(port)),
           "-f", str(int(freq_hz)), "-s", str(int(rate)), "-g", str(int(gain))]
    if ppm:
        cmd += ["-P", str(int(ppm))]
    if bias:
        cmd += ["-T"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.6)
    if proc.poll() is not None:
        raise ValueError(f"rtl_tcp exited with code {proc.returncode}; the port may be in use")
    _RTL_TCP_SERVER.update({"proc": proc, "port": int(port), "bind": str(bind),
                            "started": utcnow()})
    return rtl_tcp_server_status()


def enable_uconsole_sdr(gpio: int = 7) -> dict:
    """Raise the HackerGadgets AIO V2 SDR enable GPIO.

    The board leaves the SDR port off until GPIO 7 is driven high. This is a
    convenience for operators running DarkSec with the needed permission; it
    reports the exact command if it cannot run it.
    """
    tool = shutil.which("pinctrl")
    if not tool:
        raise ValueError("pinctrl is not installed. On uConsole run: pinctrl 7 op; pinctrl 7 dh")
    outputs = []
    for args in (["op"], ["dh"]):
        rc, out = _run([tool, str(int(gpio)), *args], 5)
        outputs.append(out.strip())
        if rc != 0:
            raise ValueError(
                f"pinctrl {gpio} {args[0]} failed: {out.strip()[:200]}. "
                f"Run manually with sudo: pinctrl {gpio} op; pinctrl {gpio} dh"
            )
    return {"ok": True, "gpio": int(gpio), "output": "\n".join(o for o in outputs if o),
            "note": "AIO V2 SDR rail enabled. Re-plug the RTL-SDR after 15-30 s if it does not appear."}

import atexit as _atexit
_atexit.register(lambda: stop_rtl_tcp_server())
