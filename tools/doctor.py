#!/usr/bin/env python3
"""Check the local DarkSec runtime and document-tool dependencies.

Run from the repository root with the same interpreter the app uses:

    python3 tools/doctor.py
    python3 tools/doctor.py --strict
    python3 tools/doctor.py --sdr

Exit status is non-zero when a required item is missing. ``./install.sh``
installs the system packages and Python requirements, then runs this check in
strict mode.
"""
from __future__ import annotations

import argparse
import importlib
import json
import platform
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PYTHON_PACKAGES = [
    ("fastapi", "FastAPI"),
    ("uvicorn", "Uvicorn"),
    ("psutil", "psutil"),
    ("cryptography", "cryptography"),
    ("ephem", "ephem"),
    ("numpy", "NumPy"),
    ("scipy", "SciPy (SDR DSP)"),
    ("PIL", "Pillow (SDR imagery)"),
    ("requests", "requests"),
    ("charset_normalizer", "charset-normalizer"),
    ("qrcode", "qrcode"),
    ("pypdf", "pypdf (PDF text fallback)"),
    ("sgp4", "sgp4 (satellite propagation)"),
]

REQUIRED_BINS = {
    "pdftotext": ("poppler-utils", "PDF text extraction"),
    "pdftoppm": ("poppler-utils", "PDF page rendering for OCR"),
    "tesseract": ("tesseract-ocr", "OCR for scanned/image PDFs"),
}

RECOMMENDED_BINS = {
    "pdfinfo": ("poppler-utils", "PDF page counts"),
    "djvutxt": ("djvulibre-bin", "DjVu text extraction"),
    "ffmpeg": ("ffmpeg", "SDR audio conversion/playback"),
    "sox": ("sox", "SDR audio processing"),
    "git": ("git", "program updates"),
}

SDR_BINS = {
    "rtl_test": ("rtl-sdr", "RTL-SDR receiver"),
    "hackrf_info": ("hackrf", "HackRF receiver"),
    "gpspipe": ("gpsd-clients", "GPS position source"),
    "dump1090": ("dump1090-mutability", "ADS-B aircraft feed"),
    "rtl_433": ("rtl-433", "433 MHz sensors"),
    "multimon-ng": ("multimon-ng", "POCSAG/FLEX decoding"),
    "direwolf": ("direwolf", "APRS"),
}

APT_HINTS = {
    "poppler-utils": "poppler-utils",
    "tesseract-ocr": "tesseract-ocr tesseract-ocr-eng",
    "djvulibre-bin": "djvulibre-bin",
    "ffmpeg": "ffmpeg",
    "sox": "sox",
    "git": "git",
    "rtl-sdr": "rtl-sdr",
    "hackrf": "hackrf",
    "gpsd-clients": "gpsd-clients",
    "dump1090-mutability": "dump1090-mutability",
    "rtl-433": "rtl-433",
    "multimon-ng": "multimon-ng",
    "direwolf": "direwolf",
}


def _mark(ok: bool) -> str:
    return "OK " if ok else "MISS"


def _python_version_ok() -> bool:
    return sys.version_info >= (3, 11)


def _import_ok(name: str) -> tuple[bool, str]:
    try:
        importlib.import_module(name)
        return True, ""
    except Exception as exc:  # noqa: BLE001 - report the real import failure
        return False, f"{type(exc).__name__}: {exc}"[:160]


def collect(check_sdr: bool) -> dict:
    rows = []
    ok_all = True

    py_ok = _python_version_ok()
    rows.append({
        "group": "python", "name": "Python 3.11+", "required": True,
        "ok": py_ok, "detail": platform.python_version(),
        "hint": "install Python 3.11 or newer",
    })
    if not py_ok:
        ok_all = False

    for module, label in PYTHON_PACKAGES:
        ok, detail = _import_ok(module)
        rows.append({
            "group": "python", "name": label, "required": True,
            "ok": ok, "detail": detail,
            "hint": "python3 -m pip install -r requirements.txt",
        })
        if not ok:
            ok_all = False

    for binary, (package, purpose) in REQUIRED_BINS.items():
        path = shutil.which(binary)
        rows.append({
            "group": "document", "name": binary, "required": True,
            "ok": bool(path), "detail": path or purpose,
            "hint": f"install {package}",
        })
        if not path:
            ok_all = False

    for binary, (package, purpose) in RECOMMENDED_BINS.items():
        path = shutil.which(binary)
        rows.append({
            "group": "recommended", "name": binary, "required": False,
            "ok": bool(path), "detail": path or purpose,
            "hint": f"install {package}",
        })

    for binary, (package, purpose) in SDR_BINS.items():
        path = shutil.which(binary)
        rows.append({
            "group": "sdr", "name": binary, "required": check_sdr,
            "ok": bool(path), "detail": path or purpose,
            "hint": f"install {package}",
        })
        if check_sdr and not path:
            ok_all = False

    for binary, package in (("node", "nodejs"), ("npm", "npm")):
        path = shutil.which(binary)
        rows.append({
            "group": "desktop", "name": binary, "required": False,
            "ok": bool(path), "detail": path or "needed for the Electron desktop shell",
            "hint": f"install {package}",
        })

    return {"ok": ok_all, "rows": rows, "platform": platform.platform()}


def apt_hint(rows: list[dict]) -> str:
    packages: list[str] = []
    for row in rows:
        if row["ok"] or row["group"] not in ("document", "recommended", "sdr"):
            continue
        package = row["hint"].replace("install ", "").strip()
        if package and package not in packages:
            packages.append(package)
    if not packages:
        return ""
    expanded = []
    for package in packages:
        for name in APT_HINTS.get(package, package).split():
            if name not in expanded:
                expanded.append(name)
    return "sudo apt install -y " + " ".join(expanded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true",
                        help="fail if any required item is missing")
    parser.add_argument("--sdr", action="store_true",
                        help="treat SDR hardware tools as required")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    report = collect(args.sdr)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    print(f"DarkSec dependency check — {report['platform']}")
    print(f"Repository: {ROOT}")
    print()
    groups = [
        ("python", "Python packages"),
        ("document", "Document tools (required)"),
        ("recommended", "Recommended tools"),
        ("sdr", "SDR / hardware tools" + ("" if args.sdr else " (optional)")),
        ("desktop", "Desktop shell"),
    ]
    for key, title in groups:
        rows = [row for row in report["rows"] if row["group"] == key]
        if not rows:
            continue
        print(title)
        for row in rows:
            flag = _mark(row["ok"])
            required = "required" if row["required"] else "optional"
            detail = f" — {row['detail']}" if row["detail"] else ""
            print(f"  [{flag}] {row['name']:<22} {required:<8}{detail}")
        print()

    missing_required = [row for row in report["rows"] if row["required"] and not row["ok"]]
    missing_recommended = [row for row in report["rows"]
                           if not row["required"] and not row["ok"]
                           and row["group"] in ("recommended", "document")]
    if missing_required:
        print("Missing required items. Run ./install.sh or install them manually.")
        hint = apt_hint(report["rows"])
        if hint:
            print(hint)
        return 1
    if missing_recommended and args.strict:
        print("Required items are present, but recommended document tools are missing.")
        hint = apt_hint(report["rows"])
        if hint:
            print(hint)
        return 1
    print("Core runtime and document tools are ready.")
    if missing_recommended:
        print("Optional tools missing; OCR and some document formats may be unavailable.")
        hint = apt_hint(report["rows"])
        if hint:
            print(hint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
