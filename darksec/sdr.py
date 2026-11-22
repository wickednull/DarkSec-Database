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
import sys
import threading
import time
import uuid
import wave
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pwd
    import grp
except Exception:  # Windows has no pwd/grp; SDR still works elsewhere.
    pwd = grp = None

import numpy as np

from . import config
from .db import Database, utcnow

MAX_CAPTURE_BYTES = 750 * 1024 * 1024
MAX_SWEEP_SPAN_HZ = 6_000_000_000

_JOBS: dict[str, dict] = {}
_PROCS: dict[str, subprocess.Popen] = {}
_SWEEP: dict = {"proc": None, "started": None}
_LOCK = threading.RLock()
_DETECT = {"at": 0.0, "data": None}
_DETECT_TTL = 20.0
HF_TUNER_MAX_HZ = 24_000_000
_RTL_TCP_SERVER: dict = {"proc": None, "port": None, "bind": None, "started": None}
_ADSB: dict = {"proc": None, "binary": None, "port": None, "started": None,
               "started_at": None, "log": None, "warning": "", "dir": None,
               "last_error": ""}
_ADSB_FEED_CACHE: dict = {"at": 0.0, "port": None, "data": None}


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


class LiveSourceError(RuntimeError):
    """A receiver process failed before or during a live stream."""

    def __init__(self, message: str, fatal: bool = True, detail: str = ""):
        super().__init__(message)
        self.fatal = fatal
        self.detail = detail


def _direct_requested(value) -> str:
    """Normalise AUTO / tuner / HF-Q values coming from settings or the UI."""
    if value is None:
        return "auto"
    text = str(value).strip().lower()
    if text in ("", "auto", "automatic", "-1"):
        return "auto"
    if text in ("2", "q", "hf", "hfq", "hf q", "direct", "true", "yes", "on"):
        return "2"
    return "0"


def resolve_direct_sampling(backend: str, freq_hz: float, requested="auto") -> dict:
    """Pick tuner vs RTL direct-sampling Q for the frequency actually tuned.

    The R820T tuner used by most RTL-SDRs (including the HackerGadgets AIO V2)
    cannot tune below 24 MHz. AUTO therefore selects the Q branch for HF and
    the tuner at/above 24 MHz. An impossible explicit choice is corrected with
    a warning instead of silently delivering an empty spectrum.
    """
    backend = str(backend or "auto").lower()
    req = _direct_requested(requested)
    if backend not in ("rtl", "rtl_tcp"):
        return {"mode": 0, "requested": req, "auto": req == "auto",
                "warning": "", "hf": float(freq_hz) < HF_TUNER_MAX_HZ}
    hf = float(freq_hz) < HF_TUNER_MAX_HZ
    desired = 2 if hf else 0
    mode = desired if req == "auto" else int(req)
    warning = ""
    if mode != desired:
        if hf:
            warning = ("TUNER cannot receive below 24 MHz on an R820T. Using HF direct-sampling Q "
                       "for this frequency; connect an HF antenna to the Q branch.")
        else:
            warning = ("HF direct-sampling Q does not cover VHF/UHF. Using the R820T tuner "
                       "for this frequency.")
        mode = desired
    return {"mode": mode, "requested": req, "auto": req == "auto",
            "warning": warning, "hf": hf}


def _friendly_receiver_error(text: str) -> str:
    low = (text or "").lower()
    if "usb_claim_interface" in low or "device is busy" in low or "resource busy" in low:
        return ("The RTL-SDR is busy: another program (live receiver, rtl_tcp, dump1090, gqrx or SDR++) "
                "already owns it. Stop the other receiver and retry.")
    if "no supported devices" in low or "no matching devices" in low or "no devices found" in low:
        return ("No RTL-SDR was found. Re-seat it in the USB-A port, wait 15-30 s after re-plugging, "
                "and check USB permissions (plugdev/udev).")
    if "failed to open" in low or "open() failed" in low or "permission denied" in low:
        return ("Cannot open the SDR device (permissions or another program). Check the udev rules, "
                "add your user to plugdev, and stop other SDR software.")
    if "no hackrf boards found" in low:
        return "No HackRF found. Check the USB cable, use a powered hub and verify udev permissions."
    if "address already in use" in low:
        return "The rtl_tcp port is already in use by another process."
    return ""


def _spawn_stream(cmd: list[str]) -> tuple[subprocess.Popen, deque]:
    """Start a streaming receiver and drain its stderr without blocking.

    rtl_sdr and hackrf_transfer explain real failures (busy dongle, permissions,
    missing device) on stderr. Dropping that text is why the UI could only say
    "reconnecting", so keep the last lines and surface them on failure.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL)
    lines: deque = deque(maxlen=60)

    def drain():
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    lines.append(line)
        except Exception:
            pass

    threading.Thread(target=drain, daemon=True).start()
    return proc, lines


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
    device_name = str(rtl.get("device_name") or "").lower()
    if "hackergadgets" in device_name or "aio_v2" in device_name or "aio v2" in device_name:
        add("info", "HackerGadgets AIO V2 RTL-SDR detected. Leave DIRECT on TUNER for 24 MHz and above; select HF Q for shortwave below 24 MHz. HF I is the alternate input if Q is noisy.")
    if not tools.get("rtl_test"):
        add("info", "RTL-SDR tools not installed. Debian/Ubuntu/Pi: `sudo apt install rtl-sdr`; Fedora: `sudo dnf install rtl-sdr`; Arch: `sudo pacman -S rtl-sdr`.")
    elif rtl.get("present") and not rtl.get("connected"):
        if hackrf.get("connected"):
            add("info", "No RTL-SDR detected; the connected HackRF is used instead. RTL-SDR support stays available if one is plugged in.")
        else:
            add("warn", "RTL-SDR tools are installed but no dongle was detected. Check the USB-A port, cable and `lsusb` (look for 0bda:2838).")
            add("info", "If the dongle is present but busy, stop gqrx/SDR++/rtl_tcp and add your user to `plugdev`. RTL-SDR udev rules come with the rtl-sdr package.")
            add("info", "If `rtl_test` reports a DVB driver claim error, blacklist `dvb_usb_rtl28xxu` (and optionally `rtl2832`/`rtl2830`) in /etc/modprobe.d/ and replug.")
    if not tools.get("hackrf_info"):
        add("info", "HackRF tools not installed. Debian/Ubuntu: `sudo apt install hackrf`; build from greatscottgadgets/hackrf for the newest firmware tools.")
    elif hackrf.get("present") and not hackrf.get("connected"):
        if rtl.get("connected"):
            add("info", "No HackRF detected; the connected RTL-SDR is used instead.")
        else:
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
    device = re.search(r"^\s*0:\s+(.+?)(?:,?\s+SN:.*)?$", out, re.M)
    direct = re.search(r"Enabled direct sampling mode,\s*input\s*(\d+)", out, re.I)
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
        "device_name": device.group(1).strip() if device else "",
        "direct_sampling": int(direct.group(1)) if direct else 0,
        "error": error,
    }


_SDR_USB_VIDPIDS = ("0bda:283", "1d50:60", "1d50:cc15")


def _is_sdr_usb_id(vidpid: str) -> bool:
    return any((vidpid or "").lower().startswith(prefix) for prefix in _SDR_USB_VIDPIDS)


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


def _track_sweep(proc: subprocess.Popen) -> None:
    """Remember the running sweep so it can be stopped from another request."""
    with _LOCK:
        _SWEEP["proc"] = proc
        _SWEEP["started"] = utcnow()


def _untrack_sweep(proc: subprocess.Popen | None = None) -> None:
    with _LOCK:
        if proc is None or _SWEEP.get("proc") is proc:
            _SWEEP["proc"] = None
            _SWEEP["started"] = None


def sweep_running() -> bool:
    with _LOCK:
        proc = _SWEEP.get("proc")
    return bool(proc and proc.poll() is None)


def stop_sweep() -> dict:
    """Terminate the wideband sweep receiver, if one is running.

    Closing the browser stream is not reliable enough on its own: a proxy can
    keep the response open, and rtl_power keeps the dongle claimed until the
    process is actually gone.
    """
    with _LOCK:
        proc = _SWEEP.get("proc")
    if not proc or proc.poll() is not None:
        _untrack_sweep()
        return {"stopped": False, "note": "No sweep is running"}
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    _untrack_sweep(proc)
    return {"stopped": True, "note": "Sweep stopped"}


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
    _track_sweep(proc)
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
        _untrack_sweep(proc)
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
                      gain: int = 40, ppm: int = 0, device: int = 0,
                      direct: bool = False):
    """Yield sweep frames from rtl_power, one CSV row per sweep interval."""
    if not shutil.which("rtl_power"):
        yield {"type": "error", "error": "rtl_power is not installed (package: rtl-sdr)"}
        return
    start_hz, end_hz = int(start_hz), int(end_hz)
    bin_hz = max(1_000, min(2_800_000, int(bin_hz)))
    cmd = ["rtl_power", "-f", f"{start_hz}:{end_hz}:{bin_hz}", "-i", "1",
           "-g", str(max(0, int(gain))), "-p", str(int(ppm)), "-d", str(int(device))]
    if direct:
        cmd.append("-D")
    cmd.append("-")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
    except Exception as exc:
        yield {"type": "error", "error": str(exc)}
        return
    _track_sweep(proc)
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
        _untrack_sweep(proc)
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
                 device: int = 0, direct="auto"):
    """Yield sweep frames from the best available local receiver."""
    backend = choose_backend(backend)
    if backend == "hackrf":
        yield from _hackrf_sweep_stream(start_hz, end_hz, bin_hz, lna, vga, amp)
        return
    if backend == "rtl":
        req = _direct_requested(direct)
        if req == "auto" and start_hz < HF_TUNER_MAX_HZ < end_hz:
            yield {"type": "error", "error": (
                "This sweep crosses 24 MHz. RTL-SDR needs HF Q below 24 MHz and the tuner above it, "
                "so scan 0.1-24 MHz and 24-1766 MHz as two separate sweeps (or choose a mode explicitly).")}
            return
        resolved = resolve_direct_sampling("rtl", (start_hz + end_hz) / 2.0, direct)
        if resolved["warning"]:
            yield {"type": "notice", "message": resolved["warning"]}
        yield from _rtl_sweep_stream(start_hz, end_hz, bin_hz, gain, ppm, device,
                                     bool(resolved["mode"]))
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

    def set_direct_sampling(self, mode: int = 2) -> None:
        # 0 = tuner, 1 = I-ADC, 2 = Q-ADC (HF direct sampling)
        self._cmd(0x09, int(mode))

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
                  host: str = "", port: int = 0, direct="auto") -> dict:
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
                   port=int(port or settings.get("sdr_rtl_tcp_port") or 1234),
                   direct=bool(resolve_direct_sampling(backend, freq_hz, direct)["mode"]))
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
    if job.get("direct"):
        cmd.append("-D")
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
    if job.get("direct"):
        client.set_direct_sampling(2)
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


def _recording_path(row: dict, kind: str) -> Path | None:
    p = row.get(f"{kind}_path")
    if not p:
        return None
    path = Path(str(p))
    try:
        path.resolve().relative_to(Path(config.RECORDING_DIR).resolve())
    except Exception:
        return None
    return path if path.exists() and path.is_file() else None


def update_recording(rec_id: int, *, name: str | None = None, note: str | None = None) -> dict:
    db = Database()
    row = db.one("SELECT * FROM sdr_recordings WHERE id=?", (rec_id,))
    if not row:
        db.close()
        raise ValueError("Recording not found")
    updates = {}
    if name is not None:
        name = str(name).strip()[:120]
        if name:
            updates["name"] = name
    if note is not None:
        updates["note"] = str(note).strip()[:2000]
    if updates:
        keys = list(updates)
        db.execute(
            f"UPDATE sdr_recordings SET {', '.join(f'{k}=?' for k in keys)} WHERE id=?",
            [updates[k] for k in keys] + [rec_id],
        )
    row = db.one("SELECT * FROM sdr_recordings WHERE id=?", (rec_id,))
    db.close()
    return dict(row) if row else {"id": rec_id}


def analyze_recording(rec_id: int) -> dict:
    db = Database()
    row = db.one("SELECT * FROM sdr_recordings WHERE id=?", (rec_id,))
    db.close()
    if not row:
        raise ValueError("Recording not found")
    row = dict(row)
    out = {
        "id": rec_id,
        "name": row.get("name"),
        "freq_hz": row.get("freq_hz"),
        "rate": row.get("rate"),
        "kind": row.get("kind"),
        "files": {},
        "spectrum": None,
        "audio": None,
    }
    for kind in ("iq", "audio", "image"):
        path = _recording_path(row, kind)
        if path:
            try:
                st = path.stat()
                out["files"][kind] = {"path": str(path), "bytes": st.st_size, "name": path.name}
            except OSError:
                pass
    iq_path = _recording_path(row, "iq")
    if iq_path:
        sample_format = "u8" if str(row.get("kind") or "").lower() in ("rtl", "u8") else "i8"
        # Heuristic: rtl captures are stored as u8 in this codebase; hackrf as i8.
        # If backend info is not stored, try to infer from name.
        backend_hint = str(row.get("name") or "").lower()
        if "rtl" in backend_hint:
            sample_format = "u8"
        spec = analyze_spectrum(iq_path, float(row.get("rate") or 2_400_000), sample_format=sample_format)
        if spec and spec.get("bins"):
            bins = spec["bins"]
            peak_db = max(bins)
            noise = float(np.percentile(bins, 20)) if bins else peak_db
            out["spectrum"] = {
                **spec,
                "peak_db": round(float(peak_db), 2),
                "noise_floor_db": round(noise, 2),
                "snr_est_db": round(float(peak_db - noise), 2),
            }
    audio_path = _recording_path(row, "audio")
    if audio_path and audio_path.suffix.lower() == ".wav":
        try:
            with wave.open(str(audio_path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate() or 1
                sampwidth = wf.getsampwidth()
                channels = wf.getnchannels()
                duration = frames / float(rate)
                # Read a limited sample for RMS/peak.
                wf.rewind()
                chunk = wf.readframes(min(frames, rate * 30))
            if sampwidth == 2:
                arr = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / 32768.0
            elif sampwidth == 1:
                arr = (np.frombuffer(chunk, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
            else:
                arr = np.array([], dtype=np.float32)
            if channels > 1 and arr.size:
                arr = arr.reshape(-1, channels).mean(axis=1)
            rms = float(np.sqrt(np.mean(np.square(arr)))) if arr.size else 0.0
            peak = float(np.max(np.abs(arr))) if arr.size else 0.0
            out["audio"] = {
                "duration_s": round(duration, 3),
                "sample_rate": rate,
                "channels": channels,
                "sample_width": sampwidth,
                "rms": round(rms, 6),
                "peak": round(peak, 6),
                "peak_dbfs": round(20 * math.log10(peak + 1e-12), 2),
            }
        except Exception as exc:
            out["audio"] = {"error": str(exc)[:200]}
    return out


def decode_tools() -> dict:
    tools = {}
    for name, binary, note in [
        ("multimon-ng", "multimon-ng", "POCSAG/FLEX/EAS/AFSK decoders (if supported by build)"),
        ("direwolf", "direwolf", "APRS/AX.25 packet decoder"),
        ("rtl_433", "rtl_433", "ISM/OOK sensor decoder"),
        ("sox", "sox", "Audio conversion utility"),
    ]:
        path = shutil.which(binary)
        tools[name] = {"available": bool(path), "path": path or "", "note": note}
    return {"tools": tools}


def decode_recording(rec_id: int, decoder: str) -> dict:
    db = Database()
    row = db.one("SELECT * FROM sdr_recordings WHERE id=?", (rec_id,))
    db.close()
    if not row:
        raise ValueError("Recording not found")
    row = dict(row)
    decoder = str(decoder or "").strip().lower()
    audio_path = _recording_path(row, "audio")
    if not audio_path:
        raise ValueError("This recording has no audio file to decode")
    if decoder == "multimon-ng":
        binary = shutil.which("multimon-ng")
        if not binary:
            raise ValueError("multimon-ng is not installed")
        # Safe, fixed arg list; decode common pager/AFSK modes from WAV.
        cmd = [binary, "-t", "wav", "-a", "POCSAG512", "-a", "POCSAG1200", "-a", "POCSAG2400",
               "-a", "AFSK1200", "-a", "AFSK2400", str(audio_path)]
        timeout = 90
    elif decoder == "direwolf":
        binary = shutil.which("direwolf")
        if not binary:
            raise ValueError("direwolf is not installed")
        cmd = [binary, "-q", "d", "-t", "0", "-r", "48000", "-a", "10", str(audio_path)]
        timeout = 120
    elif decoder == "rtl_433":
        binary = shutil.which("rtl_433")
        if not binary:
            raise ValueError("rtl_433 is not installed")
        cmd = [binary, "-r", str(audio_path), "-F", "json"]
        timeout = 120
    else:
        raise ValueError("Unsupported decoder")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "decoder": decoder, "error": "Decoder timed out", "output": "", "lines": []}
    except Exception as exc:
        return {"ok": False, "decoder": decoder, "error": str(exc)[:300], "output": "", "lines": []}
    raw = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    # Limit output size returned to UI.
    trimmed = lines[-400:]
    return {
        "ok": proc.returncode == 0 or bool(trimmed),
        "decoder": decoder,
        "returncode": proc.returncode,
        "output": "\n".join(trimmed)[-20000:],
        "lines": trimmed[-200:],
        "note": "External decoder output is unverified. Confirm before acting on it.",
    }


def _adsb_device_type() -> str:
    """Pick the ADS-B receiver: RTL-SDR first, then HackRF (readsb only)."""
    info = detect()
    if (info.get("rtl") or {}).get("connected"):
        return "rtlsdr"
    if (info.get("hackrf") or {}).get("connected"):
        return "hackrf"
    return ""


def _adsb_binary(device_type: str = "") -> str | None:
    if device_type == "hackrf":
        # dump1090 has no HackRF backend; recent readsb builds do.
        return shutil.which("readsb")
    for name in ("dump1090-fa", "dump1090-mutability", "dump1090", "readsb"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _adsb_settings() -> dict:
    s = _settings()
    try:
        port = int(s.get("adsb_port") or 8081)
    except (TypeError, ValueError):
        port = 8081
    try:
        web_port = int(s.get("lan_port") or config.default_settings().get("lan_port") or 8080)
    except (TypeError, ValueError):
        web_port = 8080
    if port == web_port:
        # An external-feed port equal to the DarkSec web port would probe our
        # own server, so fall back to the documented default.
        port = 8081
    try:
        gain = int(float(s.get("adsb_gain") or 40))
    except (TypeError, ValueError):
        gain = 40
    try:
        device = int(s.get("adsb_device") or 0)
    except (TypeError, ValueError):
        device = 0
    return {"host": str(s.get("adsb_host") or "127.0.0.1").strip(), "port": port,
            "gain": gain, "device": device}


def _adsb_url(port: int | None = None) -> str:
    cfg = _adsb_settings()
    return f"http://{cfg['host']}:{int(port or cfg['port'])}/data/aircraft.json"


def _adsb_json_dir() -> Path:
    return Path(config.DATA_DIR) / "adsb"


def _adsb_feed(port: int | None = None) -> dict | None:
    import requests
    cfg = _adsb_settings()
    target = int(port or cfg["port"])
    managed_dir = _ADSB.get("dir")
    if managed_dir:
        path = Path(managed_dir) / "aircraft.json"
        try:
            if path.exists() and time.time() - path.stat().st_mtime <= 15:
                data = json.loads(path.read_text(errors="replace"))
                if isinstance(data, dict) and isinstance(data.get("aircraft"), list):
                    return data
        except Exception:
            pass
    now = time.monotonic()
    if (_ADSB_FEED_CACHE["port"] == target and now - _ADSB_FEED_CACHE["at"] < 1.5):
        return _ADSB_FEED_CACHE["data"]
    try:
        r = requests.get(_adsb_url(target), timeout=1.5)
        r.raise_for_status()
        data = r.json()
    except Exception:
        data = None
    _ADSB_FEED_CACHE.update({"at": now, "port": target, "data": data})
    return data


def adsb_status() -> dict:
    proc = _ADSB.get("proc")
    ours = bool(proc is not None and proc.poll() is None)
    cfg = _adsb_settings()
    port = int(_ADSB.get("port") or cfg["port"])
    data = _adsb_feed(port)
    external = bool(data is not None and not ours)
    if data and ours:
        _remember_spotted(data.get("aircraft") or [])
    exited = ""
    if proc is not None and proc.poll() is not None:
        tail = ""
        try:
            tail = " | ".join((_ADSB.get("log") and Path(_ADSB["log"]).read_text(errors="replace").splitlines()[-3:]) or [])
        except Exception:
            pass
        exited = (f"The ADS-B receiver exited with code {proc.returncode}."
                  + (f" {tail[:400]}" if tail else " Check the ADS-B log."))
    elif proc is None and not external:
        # The process object is gone after a restart, but the log still shows
        # the last failure. Surface a recent crash instead of pretending idle.
        log_path = Path(_ADSB.get("log") or (Path(config.LOG_DIR) / "dump1090.log"))
        try:
            if log_path.exists() and time.time() - log_path.stat().st_mtime < 3600:
                tail_lines = log_path.read_text(errors="replace").splitlines()[-4:]
                if any(("wedged" in ln.lower()) or ("abnormal exit" in ln.lower())
                       for ln in tail_lines):
                    exited = ("The last ADS-B receiver run stopped unexpectedly: "
                              + " | ".join(tail_lines)[-400:])
        except OSError:
            pass

    hint = ""
    messages = int((data or {}).get("messages") or 0)
    aircraft = len((data or {}).get("aircraft") or [])
    if ours and data is not None and messages == 0 and not external:
        started_at = _ADSB.get("started_at")
        if started_at and time.monotonic() - started_at > 120:
            hint = ("No ADS-B messages after two minutes. 1090 MHz needs a dedicated "
                    "1090 MHz antenna with a clear view of the sky; a VHF/UHF whip or a "
                    "disconnected antenna hears almost nothing. Keep the HackRF away "
                    "from strong FM/TV transmitters and check that readsb reports "
                    "samples without 'Lost packets' in the log.")
    return {
        "available": data is not None,
        "running": bool(ours or external),
        "managed": ours,
        "external": external,
        "binary": _ADSB.get("binary") or _adsb_binary(_adsb_device_type()),
        "port": port,
        "host": cfg["host"],
        "gain": cfg["gain"],
        "device": cfg["device"],
        "started": _ADSB.get("started"),
        "log": str(_ADSB.get("log") or ""),
        "warning": _ADSB.get("warning") or "",
        "json_dir": str(_ADSB.get("dir") or ""),
        "feed": (str(Path(_ADSB.get("dir")) / "aircraft.json") if ours and _ADSB.get("dir")
                 else _adsb_url(port)),
        "aircraft_count": aircraft,
        "messages": messages,
        "hint": hint,
        "note": (exited or _ADSB.get("last_error") or _ADSB.get("warning")
                 or ("A feed is already running outside DarkSec; showing it." if external
                     else "Start the ADS-B receiver (dump1090 for an RTL-SDR, readsb for a HackRF)." if not ours
                     else "The ADS-B receiver is running under DarkSec; reading its local JSON output.")),
    }


def start_adsb(port: int | None = None, gain: int | None = None,
               device: int | None = None) -> dict:
    cfg = _adsb_settings()
    port = int(port or cfg["port"])
    gain = int(cfg["gain"] if gain is None else gain)
    device = int(cfg["device"] if device is None else device)
    if _adsb_feed(port) is not None:
        return adsb_status()
    device_type = _adsb_device_type()
    if not device_type:
        message = ("No SDR radio detected for ADS-B. Connect an RTL-SDR (dump1090/readsb) "
                   "or a HackRF (readsb only; dump1090 cannot drive a HackRF).")
        _ADSB.update({"last_error": message})
        raise ValueError(message)
    binary = _adsb_binary(device_type)
    if not binary:
        message = ("ADS-B with a HackRF needs readsb; readsb is not installed. "
                   "Install readsb, or connect an RTL-SDR for dump1090."
                   if device_type == "hackrf" else
                   "dump1090/readsb is not installed. Install dump1090-mutability, "
                   "dump1090-fa or readsb.")
        _ADSB.update({"last_error": message})
        raise ValueError(message)
    stop_adsb()
    json_dir = _adsb_json_dir()
    try:
        json_dir.mkdir(parents=True, exist_ok=True)
        (json_dir / "aircraft.json").unlink(missing_ok=True)
    except OSError:
        pass
    if not os.access(json_dir, os.W_OK):
        raise ValueError(f"ADS-B data directory is not writable: {json_dir}")
    base = Path(binary).name.lower()
    if device_type == "hackrf":
        serial = str((detect().get("hackrf") or {}).get("serial") or "").strip()
        cmd = [binary, "--device-type=hackrf"]
        if serial:
            cmd.append(f"--device={serial}")
        # libhackrf hands over 256 KiB transfers; readsb's default 128 KiB SDR
        # buffer makes it throw away half of every block (and report -500000 ppm
        # "Lost N packets" while decoding almost nothing).
        cmd += [f"--hackrf-vgagain={_quantize_gain(gain, 62, 2)}",
                "--sdr-buffer-size=256",
                "--write-json", str(json_dir), "--quiet"]
    elif "readsb" in base:
        cmd = [binary, "--device-type", "rtlsdr", "--device", str(device),
               "--gain", str(gain), "--write-json", str(json_dir), "--quiet"]
    else:
        cmd = [binary, "--device-index", str(device), "--gain", str(gain),
               "--write-json", str(json_dir), "--quiet"]
    log_path = Path(config.LOG_DIR) / "dump1090.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "wb")
    except OSError:
        log = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    finally:
        if log is not subprocess.DEVNULL:
            try:
                log.close()
            except Exception:
                pass
    time.sleep(1.2)
    if proc.poll() is not None:
        tail = ""
        try:
            tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-8:])
        except OSError:
            pass
        message = f"The ADS-B receiver exited with code {proc.returncode}. {tail[:500]}".strip()
        _ADSB.update({"proc": None, "binary": binary, "port": port, "started": None,
                      "started_at": None, "log": str(log_path), "warning": "",
                      "dir": None, "last_error": message})
        raise ValueError(message)
    _ADSB.update({"proc": proc, "binary": binary, "port": port,
                  "started": utcnow(), "started_at": time.monotonic(),
                  "log": str(log_path), "warning": "",
                  "dir": str(json_dir), "last_error": ""})
    return adsb_status()


def stop_adsb() -> dict:
    proc = _ADSB.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _ADSB.update({"proc": None, "binary": None, "port": None, "started": None,
                  "started_at": None, "log": None, "warning": "", "dir": None,
                  "last_error": ""})
    return adsb_status()


ADSB_SPOTTED_KEEP_DAYS = 7
ADSB_SPOTTED_MAX = 2000
_last_spotted_write = 0.0
_last_spotted_cleanup = 0.0


def _remember_spotted(aircraft: list[dict]) -> None:
    """Persist every aircraft seen so the table survives stopping the receiver."""
    global _last_spotted_write
    if not aircraft:
        return
    now = time.monotonic()
    if now - _last_spotted_write < 3.0:
        return
    _last_spotted_write = now
    stamp = utcnow()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ADSB_SPOTTED_KEEP_DAYS))
    cutoff = cutoff.replace(microsecond=0).isoformat()
    try:
        db = Database()
        try:
            for a in aircraft:
                hexid = str(a.get("hex") or "").strip().upper()
                if not hexid:
                    continue
                alt = a.get("alt_baro")
                if not isinstance(alt, (int, float)):
                    alt = str(alt) if alt == "ground" else None
                db.execute(
                    "INSERT INTO adsb_spotted(hex,flight,first_seen,last_seen,alt_baro,gs,track,lat,lon,messages) "
                    "VALUES(?,?,?,?,?,?,?,?,?,1) "
                    "ON CONFLICT(hex) DO UPDATE SET "
                    "flight=COALESCE(NULLIF(excluded.flight,''),adsb_spotted.flight),"
                    "last_seen=excluded.last_seen,"
                    "alt_baro=COALESCE(excluded.alt_baro,adsb_spotted.alt_baro),"
                    "gs=COALESCE(excluded.gs,adsb_spotted.gs),"
                    "track=COALESCE(excluded.track,adsb_spotted.track),"
                    "lat=COALESCE(excluded.lat,adsb_spotted.lat),"
                    "lon=COALESCE(excluded.lon,adsb_spotted.lon),"
                    "messages=adsb_spotted.messages+1",
                    (hexid, str(a.get("flight") or "").strip(), stamp, stamp, alt,
                     a.get("gs"), a.get("track"), a.get("lat"), a.get("lon")),
                )
            global _last_spotted_cleanup
            if now - _last_spotted_cleanup > 60.0:
                _last_spotted_cleanup = now
                db.execute("DELETE FROM adsb_spotted WHERE last_seen < ?", (cutoff,))
                db.execute(
                    "DELETE FROM adsb_spotted WHERE hex IN ("
                    "SELECT hex FROM adsb_spotted ORDER BY last_seen DESC "
                    "LIMIT -1 OFFSET ?)", (ADSB_SPOTTED_MAX,))
        finally:
            db.close()
    except Exception:
        # A spot history is a convenience; never break the live feed for it.
        pass


def spotted_aircraft(limit: int = 300) -> list[dict]:
    """Aircraft seen recently, newest last-seen first (survives a stop/restart)."""
    try:
        db = Database()
        try:
            rows = db.query(
                "SELECT hex,flight,first_seen,last_seen,alt_baro,gs,track,lat,lon,messages "
                "FROM adsb_spotted ORDER BY last_seen DESC LIMIT ?", (int(limit),))
        finally:
            db.close()
    except Exception:
        return []
    return [dict(r) for r in rows]


def clear_spotted() -> dict:
    """Delete the saved ADS-B spot history (vault notes are untouched)."""
    try:
        db = Database()
        try:
            with db.transaction() as conn:
                before = conn.execute("SELECT COUNT(*) FROM adsb_spotted").fetchone()[0]
                conn.execute("DELETE FROM adsb_spotted")
        finally:
            db.close()
    except Exception:
        return {"deleted": 0}
    return {"deleted": int(before or 0)}


def spotted_markdown(rows: list[dict], title: str = "", saved_at: str | None = None) -> str:
    """Render the spot history as a vault note body."""
    saved = saved_at or utcnow()
    lines = [f"# {title or 'ADS-B spotted aircraft'}", "",
             f"Saved: {saved}", f"Aircraft: {len(rows)}"]
    if rows:
        lines.append(f"Last spot: {rows[0].get('last_seen') or '—'}")
    lines += ["", "| ICAO | Flight | Altitude | GS | Track | Position | Last seen |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        alt = r.get("alt_baro")
        alt_text = f"{alt} ft" if isinstance(alt, (int, float)) else (str(alt) if alt else "—")
        gs = r.get("gs")
        gs_text = f"{round(float(gs))} kt" if isinstance(gs, (int, float)) else "—"
        trk = r.get("track")
        trk_text = f"{round(float(trk))}°" if isinstance(trk, (int, float)) else "—"
        lat, lon = r.get("lat"), r.get("lon")
        pos = (f"{float(lat):.4f}, {float(lon):.4f}"
               if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) else "—")
        lines.append(f"| {r.get('hex') or '—'} | {(r.get('flight') or '—')} | {alt_text} | "
                     f"{gs_text} | {trk_text} | {pos} | {r.get('last_seen') or '—'} |")
    return "\n".join(lines)


def adsb_snapshot() -> dict:
    """Read the live dump1090/readsb feed, or the last saved spot history."""
    status = adsb_status()
    data = _adsb_feed(status.get("port"))
    if data:
        aircraft = []
        for a in data.get("aircraft", [])[:200]:
            aircraft.append({"hex": a.get("hex"), "flight": (a.get("flight") or "").strip(),
                             "alt_baro": a.get("alt_baro"), "gs": a.get("gs"),
                             "track": a.get("track"), "lat": a.get("lat"), "lon": a.get("lon")})
        return {"available": True, "live": True, "aircraft": aircraft,
                "source": "dump1090", "status": status}
    spotted = spotted_aircraft()
    if spotted:
        return {"available": True, "live": False, "aircraft": spotted,
                "source": "saved", "spotted_at": spotted[0].get("last_seen") or "",
                "note": (f"Receiver stopped. Showing {len(spotted)} aircraft last spotted "
                         "while it was running."),
                "status": status}
    return {"available": False, "live": False, "aircraft": [], "status": status,
            "note": "Start the ADS-B receiver from the ADS-B tab "
                    "(dump1090 for an RTL-SDR, readsb for a HackRF)."}


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
                 amp: bool, device: int = 0, stop_event=None, direct=False):
    """Yield (bytes, sample_format) IQ blocks from a live receiver.

    A process that dies without producing samples raises LiveSourceError with
    the receiver's own stderr, so the UI can show the real fault instead of an
    endless "reconnecting" loop.
    """
    backend = choose_backend(backend)
    block = max(4096, int(rate * 0.05)) * 2
    proc = None
    client = None
    errors: deque = deque(maxlen=60)
    produced = False
    ended = False
    stopped = False
    direct_mode = resolve_direct_sampling(backend, freq_hz, direct)["mode"]
    try:
        if backend == "hackrf":
            cmd = ["hackrf_transfer", "-r", "-", "-f", str(int(freq_hz)),
                   "-s", str(int(rate)), "-l", str(int(lna)), "-g", str(int(vga)),
                   "-a", "1" if amp else "0", "-p", "0"]
            proc, errors = _spawn_stream(cmd)
        elif backend == "rtl":
            cmd = ["rtl_sdr", "-f", str(int(freq_hz)), "-s", str(int(rate)),
                   "-g", str(int(gain)), "-d", str(int(device))]
            if direct_mode:
                cmd.append("-D")
            if ppm:
                cmd += ["-p", str(int(ppm))]
            cmd.append("-")
            proc, errors = _spawn_stream(cmd)
        elif backend == "rtl_tcp":
            client = RTLTCPClient(host, int(port), timeout=5.0)
            try:
                client.connect()
            except Exception as exc:
                raise LiveSourceError(
                    f"rtl_tcp {host or '(unset)'}:{int(port)} did not answer: {exc}",
                    fatal=True) from exc
            client.set_sample_rate(int(rate))
            client.set_frequency(int(freq_hz))
            client.set_gain_mode(True)
            client.set_gain(float(gain))
            if direct_mode:
                client.set_direct_sampling(2)
            if ppm:
                client.set_ppm(int(ppm))
        else:
            raise LiveSourceError("No SDR receiver is available. Connect an RTL-SDR, "
                                  "HackRF or rtl_tcp server.", fatal=True)
        sample_format = "i8" if backend == "hackrf" else "u8"
        while True:
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            if proc is not None:
                data = proc.stdout.read(block) if proc.stdout else b""
            elif client is not None:
                data = client.read(block)
            else:
                break
            if not data:
                ended = True
                break
            produced = True
            yield data, sample_format
    finally:
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if client is not None:
            client.close()
        if stop_event is not None and stop_event.is_set():
            stopped = True
        if proc is not None and ended and not stopped:
            rc = proc.returncode
            detail = " | ".join(list(errors)[-5:])[:900]
            friendly = _friendly_receiver_error(detail)
            if friendly or (not produced and rc not in (0, None)):
                reason = friendly or f"Receiver exited with code {rc}."
                raise LiveSourceError(
                    f"{reason}" + (f" {detail}" if detail else ""),
                    fatal=True, detail=detail)
            if not produced:
                raise LiveSourceError(
                    "The receiver started but produced no IQ samples. " +
                    (detail or "Check the antenna, gain and USB connection."),
                    fatal=True, detail=detail)
            if detail and rc not in (0, -15, 143, None):
                raise LiveSourceError(
                    f"Receiver stopped unexpectedly (exit {rc}). {detail}",
                    fatal=False, detail=detail)


def live_preflight(backend: str = "auto", host: str = "", port: int = 1234) -> dict:
    chosen = choose_backend(backend)
    info = detect()
    rtl_raw = str((info.get("rtl") or {}).get("raw") or "").lower()
    rtl_busy = ("usb_claim_interface" in rtl_raw or "failed to open" in rtl_raw
                or "resource busy" in rtl_raw)
    if chosen == "none":
        if rtl_busy:
            raise ValueError("The RTL-SDR is busy: another program (rtl_tcp, dump1090, gqrx, SDR++ or "
                             "a second DarkSec receiver) already owns it. Stop it and retry.")
        notes = " ".join(n["text"] for n in (info.get("guidance") or [])[:3])
        raise ValueError("No SDR receiver found. " + notes[:400])
    if chosen == "hackrf":
        if not shutil.which("hackrf_transfer"):
            raise ValueError("hackrf_transfer is not installed")
        if not info.get("hackrf", {}).get("connected"):
            raise ValueError("No HackRF detected. Connect it, use a powered hub and check USB permissions.")
    elif chosen == "rtl":
        if not shutil.which("rtl_sdr"):
            raise ValueError("rtl_sdr is not installed (package: rtl-sdr)")
        if not info.get("rtl", {}).get("connected"):
            if rtl_busy:
                raise ValueError("The RTL-SDR is busy: another program already owns it. "
                                 "Stop rtl_tcp, dump1090, gqrx or SDR++ and retry.")
            raise ValueError("No RTL-SDR detected. On a uConsole use USB-A and wait 15-30 s after re-plugging.")
    elif chosen == "rtl_tcp":
        if not host:
            raise ValueError("rtl_tcp host is not configured")
        probe = probe_rtl_tcp(host, int(port))
        if not probe.get("ok"):
            raise ValueError(f"rtl_tcp {host}:{port} did not answer: {probe.get('error')}")
    return {"backend": chosen}


def diagnose(freq_hz: float = 100_000_000.0, backend: str = "auto",
             direct="auto") -> dict:
    """Build a copyable report explaining why a receiver is or is not working."""
    info = detect(force=True)
    settings = _settings()
    chosen = choose_backend(backend)
    resolved = resolve_direct_sampling(chosen, freq_hz, direct)
    sections: list[dict] = []

    def section(title: str, status: str = "info", lines=None) -> None:
        clean = [str(line) for line in (lines or []) if str(line).strip()]
        sections.append({"title": title, "status": status, "lines": clean})

    try:
        uid = os.getuid()
    except Exception:
        uid = 0
    try:
        user = pwd.getpwuid(uid).pw_name if pwd else os.environ.get("USER", "?")
    except Exception:
        user = os.environ.get("USER", "?")
    try:
        groups = ", ".join(sorted({grp.getgrgid(g).gr_name for g in os.getgroups()})) if grp else "?"
    except Exception:
        groups = "?"
    section("Process", "info", [
        f"User: {user} (uid {uid})",
        f"Groups: {groups}",
        f"Working directory: {os.getcwd()}",
        f"Python: {sys.version.split()[0]}",
    ])
    if "plugdev" not in groups.split(", ") and uid != 0:
        sections[-1]["status"] = "warn"
        sections[-1]["lines"].append("WARN: user is not in plugdev; RTL-SDR access may fail.")

    # USB inventory and permissions.
    usb_map: dict[tuple[int, int], tuple[str, str]] = {}
    lsusb = shutil.which("lsusb")
    if lsusb:
        _rc, usb_out = _run([lsusb], 6)
        for line in usb_out.splitlines():
            m = re.match(r"Bus (\d+) Device (\d+): ID ([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)",
                         line.strip())
            if m:
                usb_map[(int(m.group(1)), int(m.group(2)))] = (
                    f"{m.group(3).lower()}:{m.group(4).lower()}", m.group(5).strip())
        sdr_lines = [ln for ln in usb_out.splitlines()
                     if any(k in ln.lower() for k in ("realtek", "0bda:283", "hackrf", "1d50:"))]
        section("USB devices", "ok" if sdr_lines else "warn",
                sdr_lines or ["No RTL-SDR/HackRF USB entry found in lsusb."])
    else:
        section("USB devices", "warn", ["lsusb is not installed (package: usbutils)."])
    usb_root = Path("/dev/bus/usb")
    if usb_root.exists():
        radio_rows = []
        denied = 0
        try:
            buses = sorted(usb_root.iterdir())[:12]
        except OSError:
            buses = []
        for bus in buses:
            if not bus.is_dir():
                continue
            try:
                devices = sorted(bus.iterdir())[:32]
            except OSError:
                continue
            for dev in devices:
                # usbfs entries are character devices, not regular files.
                try:
                    if dev.is_dir():
                        continue
                    dev.stat()
                    busno, devno = int(bus.name), int(dev.name)
                except (OSError, ValueError):
                    continue
                readable = os.access(dev, os.R_OK | os.W_OK)
                if usb_map:
                    vidpid, desc = usb_map.get((busno, devno), ("", ""))
                    if not _is_sdr_usb_id(vidpid):
                        # Root hubs, webcams, Bluetooth, fingerprint readers and
                        # other root-only nodes are normal; listing them as
                        # permission failures just buries the radio.
                        continue
                    radio_rows.append(
                        f"{dev} {vidpid} {desc or 'SDR'} [{'rw' if readable else 'NO ACCESS'}]")
                else:
                    if devno == 1:
                        continue
                    radio_rows.append(f"{dev} [{'rw' if readable else 'NO ACCESS'}]")
                if not readable:
                    denied += 1
        if radio_rows:
            section("USB device nodes", "warn" if denied else "ok", radio_rows)
        else:
            section("USB device nodes", "warn", [
                "No RTL-SDR/HackRF device node: the radio is not enumerated.",
                "Built-in devices and root hubs are root-only by design and are omitted here.",
            ])
        if denied:
            section("USB permissions", "warn", [
                f"{denied} SDR USB node(s) are not writable by this user.",
                "Install the rtl-sdr udev rules (RTL-SDR) or the libhackrf udev rules (HackRF),",
                "add your user to plugdev, then re-plug the radio.",
            ])
    else:
        section("/dev/bus/usb", "warn", ["Not present; USB devices cannot be opened from this environment."])

    mods = shutil.which("lsmod")
    if mods:
        _rc, out = _run([mods], 6)
        dvb = [ln.strip() for ln in out.splitlines() if "dvb_usb_rtl28xxu" in ln or ln.startswith("rtl283")]
        if dvb:
            section("Kernel drivers", "warn", dvb + [
                "A DVB kernel driver owns the dongle. Blacklist dvb_usb_rtl28xxu and re-plug."])

    # Installed tools.
    tools = info.get("tools") or {}
    tool_lines = []
    for name in ("rtl_test", "rtl_sdr", "rtl_power", "rtl_tcp", "hackrf_info",
                 "hackrf_transfer", "SoapySDRUtil", "dump1090", "readsb"):
        tool_lines.append(f"{name}: {tools.get(name) or 'NOT INSTALLED'}")
    section("SDR tools", "ok" if tools.get("rtl_test") or tools.get("hackrf_info") else "warn", tool_lines)

    # Full rtl_test output.
    rtl = info.get("rtl") or {}
    if tools.get("rtl_test"):
        rc, out = _run([tools["rtl_test"], "-t"], 10)
        ok = bool(rtl.get("connected"))
        section("rtl_test", "ok" if ok else ("error" if chosen == "rtl" else "info"),
                [f"exit code: {rc}",
                 f"device: {rtl.get('device_name') or 'not reported'}",
                 f"tuner: {rtl.get('tuner') or 'not reported'}",
                 f"direct sampling seen in output: {rtl.get('direct_sampling') or 0}",
                 "note: -t is a tuner benchmark; it never streams samples",
                 *([] if ok else ["No RTL-SDR enumerated; informational when the chosen backend is not rtl."]),
                 *out.strip().splitlines()[-14:]])
    else:
        section("rtl_test", "error", ["rtl_test is not installed (package: rtl-sdr)."])

    # Short real capture at the requested frequency.
    if chosen == "rtl" and tools.get("rtl_sdr"):
        cmd = [tools["rtl_sdr"], "-f", str(int(freq_hz)), "-s", "1024000",
               "-g", str(int(float(settings.get("sdr_rtl_gain") or 40))),
               "-d", "0", "-n", "262144"]
        if resolved["mode"]:
            cmd.append("-D")
        ppm = int(settings.get("sdr_ppm") or 0)
        if ppm:
            cmd += ["-p", str(ppm)]
        cmd.append("-")
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=15)
            err = proc.stderr.decode("utf-8", "replace").strip()
            ok = proc.returncode == 0 and len(proc.stdout or b"") > 4096
            friendly = _friendly_receiver_error(err)
            section("Short RTL capture", "ok" if ok else "error", [
                f"command: {' '.join(cmd)}",
                f"exit code: {proc.returncode}; bytes: {len(proc.stdout or b'')}",
                *(friendly or "").splitlines(),
                *err.splitlines()[-8:],
            ])
        except subprocess.TimeoutExpired:
            section("Short RTL capture", "error", [
                f"Timed out after 15 s: {' '.join(cmd)}",
                "The device opened but the USB bulk transfer never delivered samples.",
                "rtl_test -t passing is not proof of streaming: -t only benchmarks the tuner.",
                "Check, in this order:",
                "  1. Nothing else owns it: rtl_tcp, rtl_power, dump1090, gqrx, SDR++, or a sweep left running.",
                "  2. Power and cabling: USB-A on the uConsole, powered hub on a Pi, short cable.",
                "  3. Re-plug and wait 15-30 s; the dongle will not re-initialise immediately.",
                "  4. Streaming test by hand: rtl_test -s 2400000 (Ctrl-C after a few seconds).",
                "     'lost at least N bytes' means data flows; silence with no error means a stalled endpoint.",
                "  5. uConsole AIO V2: confirm the SDR rail (pinctrl 7 op; pinctrl 7 dh).",
            ])
        except Exception as exc:
            section("Short RTL capture", "error", [str(exc)])
    elif chosen == "hackrf":
        hk = info.get("hackrf") or {}
        section("HackRF info", "ok" if hk.get("connected") else "error",
                (hk.get("raw") or "No hackrf_info output").splitlines()[-12:])
        if hk.get("connected") and tools.get("hackrf_transfer"):
            rate = 2_000_000
            cmd = [tools["hackrf_transfer"], "-r", os.devnull, "-f", str(int(freq_hz)),
                   "-s", str(rate), "-n", str(rate * 2),
                   "-l", str(int(float(settings.get("sdr_lna_gain") or 32))),
                   "-g", str(int(float(settings.get("sdr_rx_gain") or 40))), "-a", "0"]
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=12)
                err = (proc.stderr or b"").decode("utf-8", "replace").strip()
                ok = proc.returncode == 0 and "MB/second" in err
                friendly = _friendly_receiver_error(err)
                section("Short HackRF capture", "ok" if ok else "error", [
                    f"command: {' '.join(cmd)}",
                    f"exit code: {proc.returncode}",
                    *(friendly.splitlines() if friendly else []),
                    *err.splitlines()[-6:],
                ])
            except subprocess.TimeoutExpired:
                section("Short HackRF capture", "error", [
                    f"Timed out after 12 s: {' '.join(cmd)}",
                    "hackrf_info listed the board but no samples arrived.",
                    "Stop any live receiver or ADS-B process, check the USB cable,",
                    "and prefer a port on a different USB controller (hackrf_info",
                    "warns when other devices share the bus).",
                ])
            except Exception as exc:
                section("Short HackRF capture", "error", [str(exc)])
    elif chosen == "rtl_tcp":
        host = str(settings.get("sdr_rtl_tcp_host") or "").strip()
        port = int(settings.get("sdr_rtl_tcp_port") or 1234)
        probe = probe_rtl_tcp(host, port) if host else {"ok": False, "error": "host not configured"}
        detail = probe.get("error") or (
            f"RTL0 handshake · tuner type {probe.get('tuner_type')} · "
            f"{probe.get('gain_count')} gain steps")
        section("rtl_tcp", "ok" if probe.get("ok") else "error",
                [f"{host or '(unset)'}:{port}", str(detail)])

    # Busy processes and receivers owned outside DarkSec.
    pg = shutil.which("pgrep")
    busy = []
    if pg:
        for name in ("rtl_sdr", "rtl_tcp", "rtl_power", "rtl_fm", "dump1090",
                     "dump1090-fa", "readsb", "gqrx", "hackrf_transfer", "SDR++"):
            _rc, out = _run([pg, "-x", "-a", name], 4)
            for line in out.splitlines():
                line = line.strip()
                if line and line not in busy:
                    busy.append(line)
    section("Other SDR processes", "warn" if busy else "ok",
            busy[:10] or ["None found. Only one program can own the dongle."])

    effective = {
        "requested backend": backend,
        "chosen backend": chosen,
        "frequency_hz": int(freq_hz),
        "direct requested": resolved["requested"],
        "direct effective": "Q (2)" if resolved["mode"] else "tuner (0)",
        "hf": resolved["hf"],
        "warning": resolved["warning"] or "none",
        "rtl gain": settings.get("sdr_rtl_gain"),
        "ppm": settings.get("sdr_ppm"),
        "live rate Msps": settings.get("sdr_live_rate"),
        "rtl_tcp": f"{settings.get('sdr_rtl_tcp_host') or ''}:{settings.get('sdr_rtl_tcp_port') or 1234}",
    }
    section("Effective settings", "warn" if resolved["warning"] else "ok",
            [f"{k}: {v}" for k, v in effective.items()])

    guidance = info.get("guidance") or []
    if guidance:
        has_warn = any(note.get("level") == "warn" for note in guidance)
        section("Platform guidance", "warn" if has_warn else "info",
                [note.get("text", "") for note in guidance[:10]])

    text_lines = []
    for item in sections:
        text_lines.append(f"[{item['status'].upper()}] {item['title']}")
        for line in item["lines"]:
            text_lines.append(f"  {line}")
        text_lines.append("")
    return {
        "generated": utcnow(),
        "platform": info.get("platform") or {},
        "backend": chosen,
        "frequency_hz": int(freq_hz),
        "direct": resolved,
        "sections": sections,
        "text": "\n".join(text_lines)[:16000],
    }


def live_audio_stream(freq_hz: float, mode: str = "nfm", backend: str = "auto",
                      rate: float = 1_024_000, gain: float = 40, ppm: int = 0,
                      squelch: float = 0, host: str = "", port: int = 1234,
                      lna: int = 32, vga: int = 40, amp: bool = False,
                      device: int = 0, stop_event=None, max_seconds: float = 900.0,
                      direct: bool = False):
    """Yield signed 16-bit little-endian mono PCM in real time."""
    chosen = choose_backend(backend)
    deadline = time.monotonic() + max(10.0, float(max_seconds or 900.0))
    rate = max(2_000_000.0, min(20_000_000.0, rate)) if chosen == "hackrf" else \
        max(900_000.0, min(3_200_000.0, rate))
    demod = _LiveDemod(mode, rate, squelch=squelch)
    source = _live_source(chosen, freq_hz, rate, gain, ppm, host, port, lna, vga,
                          amp, device, stop_event=stop_event, direct=direct)
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
                         gain: float = 40, ppm: int = 0, bias: bool = False,
                         direct="auto") -> dict:
    if not shutil.which("rtl_tcp"):
        raise ValueError("rtl_tcp is not installed (package: rtl-sdr)")
    info = detect()
    if not info.get("rtl", {}).get("connected"):
        raise ValueError("No RTL-SDR detected. Connect it (USB-A on uConsole) and rescan.")
    stop_rtl_tcp_server()
    resolved = resolve_direct_sampling("rtl", float(freq_hz), direct)
    cmd = ["rtl_tcp", "-a", str(bind or "0.0.0.0"), "-p", str(int(port)),
           "-f", str(int(freq_hz)), "-s", str(int(rate)), "-g", str(int(gain))]
    if ppm:
        cmd += ["-P", str(int(ppm))]
    if bias:
        cmd += ["-T"]
    if resolved["mode"]:
        cmd += ["-D"]
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
_atexit.register(lambda: stop_adsb())
