#!/usr/bin/env bash
# Install the DarkSec runtime, document tools and Python dependencies.
#
# Supported: Debian/Ubuntu (apt), Fedora (dnf), Arch (pacman), openSUSE
# (zypper), Alpine (apk) and macOS (Homebrew). Run from the repository root:
#
#   ./install.sh                 core app + document/OCR tools
#   ./install.sh --with-sdr      also install optional radio/GPS tools
#   ./install.sh --no-venv       use the system Python instead of .venv
#
set -euo pipefail
cd "$(dirname "$0")"

WITH_SDR=0
MAKE_VENV=1
for arg in "$@"; do
  case "$arg" in
    --with-sdr) WITH_SDR=1 ;;
    --no-venv) MAKE_VENV=0 ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    echo "This installer needs root or sudo for system packages." >&2
    exit 1
  fi
fi

PM=""
if command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v pacman >/dev/null 2>&1; then PM=pacman
elif command -v zypper >/dev/null 2>&1; then PM=zypper
elif command -v apk >/dev/null 2>&1; then PM=apk
elif command -v brew >/dev/null 2>&1; then PM=brew
fi

echo "DarkSec installer"
echo "Package manager: ${PM:-not detected}"
echo

install_one() {
  local package="$1"
  case "$PM" in
    apt)
      DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y --no-install-recommends "$package" >/dev/null
      ;;
    dnf)
      $SUDO dnf install -y "$package" >/dev/null
      ;;
    pacman)
      $SUDO pacman -S --needed --noconfirm "$package" >/dev/null
      ;;
    zypper)
      $SUDO zypper --non-interactive install "$package" >/dev/null
      ;;
    apk)
      $SUDO apk add --no-cache "$package" >/dev/null
      ;;
    brew)
      brew install "$package" >/dev/null
      ;;
    *)
      return 1
      ;;
  esac
}

install_group() {
  local label="$1"; shift
  local failed=()
  for package in "$@"; do
    printf '  %-28s ' "$package"
    if install_one "$package"; then
      echo "ok"
    else
      echo "skipped/unavailable"
      failed+=("$package")
    fi
  done
  if [ "${#failed[@]}" -gt 0 ]; then
    echo "  Note: could not install: ${failed[*]}"
  fi
}

if [ -n "$PM" ]; then
  if [ "$PM" = "apt" ]; then
    $SUDO apt-get update -y >/dev/null
  elif [ "$PM" = "brew" ]; then
    brew update >/dev/null 2>&1 || true
  fi

  case "$PM" in
    apt)
      BASE_PKGS=(python3 python3-venv python3-pip poppler-utils tesseract-ocr
                 tesseract-ocr-eng djvulibre-bin ffmpeg sox git nodejs npm)
      SDR_PKGS=(rtl-sdr hackrf gpsd-clients multimon-ng direwolf)
      ;;
    dnf)
      BASE_PKGS=(python3 python3-pip poppler-utils tesseract tesseract-langpack-eng
                 djvulibre ffmpeg sox git nodejs npm)
      SDR_PKGS=(rtl-sdr hackrf gpsd-clients multimon-ng direwolf)
      ;;
    pacman)
      BASE_PKGS=(python python-pip poppler tesseract tesseract-data-eng djvulibre
                 ffmpeg sox git nodejs npm)
      SDR_PKGS=(rtl-sdr hackrf gpsd multimon-ng direwolf)
      ;;
    zypper)
      BASE_PKGS=(python3 python3-pip poppler-tools tesseract-ocr
                 tesseract-ocr-traineddata-english djvulibre ffmpeg sox git nodejs npm)
      SDR_PKGS=(rtl-sdr hackrf gpsd multimon-ng direwolf)
      ;;
    apk)
      BASE_PKGS=(python3 py3-pip poppler-utils tesseract-ocr tesseract-ocr-data-eng
                 djvulibre ffmpeg sox git nodejs npm)
      SDR_PKGS=(rtl-sdr hackrf gpsd-clients multimon-ng direwolf)
      ;;
    brew)
      BASE_PKGS=(python3 poppler tesseract djvulibre ffmpeg sox git node npm)
      SDR_PKGS=(rtl-sdr hackrf gpsd multimon-ng direwolf)
      ;;
  esac

  echo "Installing document, OCR and runtime tools:"
  install_group "base" "${BASE_PKGS[@]}"
  echo

  if [ "$WITH_SDR" -eq 1 ]; then
    echo "Installing optional SDR / GPS tools:"
    install_group "sdr" "${SDR_PKGS[@]}"
    echo
    echo "Radio hardware may need a replug (or logout/login) for udev rules."
    echo
  fi
else
  echo "No supported package manager found. Install these packages manually:"
  echo "  poppler-utils, tesseract-ocr (+ English data), djvulibre-bin, ffmpeg, sox, git"
  echo "  Python 3.11+ with pip and venv"
  echo
fi

PYTHON=python3
if ! command -v python3 >/dev/null 2>&1; then
  if command -v python >/dev/null 2>&1; then
    PYTHON=python
  else
    echo "Python 3 is not installed." >&2
    exit 1
  fi
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo
  echo "Python 3.11+ is required; found $("$PYTHON" -V 2>&1)." >&2
  echo "Install a newer Python, then rerun ./install.sh." >&2
  exit 1
fi

if [ "$MAKE_VENV" -eq 1 ]; then
  if [ ! -x ".venv/bin/python" ] && [ ! -x ".venv/Scripts/python.exe" ]; then
    echo "Creating Python virtual environment in .venv"
    if ! "$PYTHON" -m venv .venv; then
      echo "Could not create .venv. Install the Python venv package (python3-venv) and retry." >&2
      exit 1
    fi
  fi
  if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
  else
    PYTHON=".venv/Scripts/python.exe"
  fi
fi

echo
echo "Installing Python requirements with $PYTHON"
"$PYTHON" -m pip install --upgrade pip >/dev/null
"$PYTHON" -m pip install -r requirements.txt

if [ -f package.json ] && command -v npm >/dev/null 2>&1; then
  echo
  echo "Installing desktop shell packages (npm)"
  if npm install --no-audit --no-fund; then
    echo "Desktop shell packages installed."
  else
    echo "npm install failed; the web launcher still works." >&2
  fi
fi

echo
echo "Verifying installation"
if [ "$WITH_SDR" -eq 1 ]; then
  DOCTOR_OK=0
  "$PYTHON" tools/doctor.py --strict --sdr || DOCTOR_OK=$?
else
  DOCTOR_OK=0
  "$PYTHON" tools/doctor.py --strict || DOCTOR_OK=$?
fi
if [ "$DOCTOR_OK" -eq 0 ]; then
  echo
  echo "Install complete."
  echo "  Web:      ./launch-web.sh"
  echo "  Desktop:  ./launch-desktop.sh"
else
  echo
  echo "Install finished, but some required tools are still missing (see above)." >&2
  exit 1
fi
