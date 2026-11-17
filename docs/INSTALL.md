# Installing DarkSec NIGHTWATCH

DarkSec needs Python 3.11+ and a few local command-line tools. The installer
sets up all of them, including the OCR tools Delamain uses to read scanned PDFs.

## One command

From the repository root:

```bash
./install.sh
```

Add optional radio/GPS tools:

```bash
./install.sh --with-sdr
```

The installer detects apt, dnf, pacman, zypper, apk or Homebrew, installs the
packages below, creates `.venv`, installs `requirements.txt` and the Electron
packages, then runs `tools/doctor.py --strict`.

## What gets installed

| Package | Why it is needed |
| --- | --- |
| Python 3.11+ | Backend runtime |
| python3-venv / pip | Isolated `.venv` and Python packages |
| poppler-utils | `pdftotext` text extraction, `pdftoppm` page rendering, `pdfinfo` page counts |
| tesseract-ocr + English data | OCR for scanned/image-only PDFs |
| djvulibre-bin | `djvutxt` text extraction for DjVu documents |
| ffmpeg | SDR audio conversion and playback |
| sox | SDR audio processing |
| git | Program updates from the configured remote |
| Node.js + npm | Electron desktop shell and optional map tooling |

The Python requirements include FastAPI/Uvicorn, NumPy/SciPy, Pillow, sgp4,
ephem, pypdf, qrcode, cryptography, psutil and charset-normalizer. Run
`.venv/bin/python -m pip install -r requirements.txt` after editing them.

Optional radio/GPS packages (`--with-sdr`): `rtl-sdr`, `hackrf`,
`gpsd-clients`, `multimon-ng`, `direwolf`, and `dump1090` where the distro
provides it. Radio hardware usually needs a replug or a new login session after
the udev rules are installed.

## Manual install

### Debian / Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip poppler-utils \
  tesseract-ocr tesseract-ocr-eng djvulibre-bin ffmpeg sox git nodejs npm
```

Optional SDR:

```bash
sudo apt install -y rtl-sdr hackrf gpsd-clients multimon-ng direwolf
```

### Fedora

```bash
sudo dnf install -y python3 python3-pip poppler-utils tesseract \
  tesseract-langpack-eng djvulibre ffmpeg sox git nodejs npm
```

### Arch

```bash
sudo pacman -S --needed python python-pip poppler tesseract \
  tesseract-data-eng djvulibre ffmpeg sox git nodejs npm
```

### openSUSE

```bash
sudo zypper install python3 python3-pip poppler-tools tesseract-ocr \
  tesseract-ocr-traineddata-english djvulibre ffmpeg sox git nodejs npm
```

### Alpine

```bash
sudo apk add python3 py3-pip poppler-utils tesseract-ocr \
  tesseract-ocr-data-eng djvulibre ffmpeg sox git nodejs npm
```

### macOS (Homebrew)

```bash
brew install python3 poppler tesseract djvulibre ffmpeg sox git node npm
```

## Python packages

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

`launch-web.sh`, `launch-desktop.sh` and `reindex.sh` prefer `.venv`
automatically. The Electron shell also checks `.venv` before system Python.

## Verify

```bash
.venv/bin/python tools/doctor.py
```

The doctor reports every Python package and external tool, marks required items
and prints an install hint for anything missing. `--strict` fails if a required
or recommended document tool is absent. Settings → Document library shows the
same status: pdf text, pypdf, OCR and DjVu.

## OCR notes

- OCR is used when a PDF has no text layer: Delamain calls
  `get_library_document`, which renders pages with `pdftoppm` and reads them with
  `tesseract`.
- Up to 12 pages are OCR'd per request by default. Ask for a specific page to
  read a later part of a large scanned manual.
- The OCR language defaults to `eng`. Set `DARKSEC_OCR_LANG` to another
  installed tesseract language (for example `deu` or `spa`) before launching.
- OCR text is written back into the library index, so the next search or
  question is fast.
- OCR is CPU-intensive. A dozen pages can take tens of seconds on older
  hardware; the assistant timeout defaults to 300 seconds.

## Troubleshooting

**Delamain says a PDF has no text layer.** Run `tools/doctor.py`. If `tesseract`
or `pdftoppm` is missing, run `./install.sh` and restart DarkSec. Then ask again
with the document title or call `get_library_document`.

**Text PDFs are not searchable.** Check `pdftotext` and `pypdf` in the doctor.
Re-run **Settings → Document library → START INDEX** after installing missing
tools.

**OCR language is wrong.** Install the matching tesseract language package and
set `DARKSEC_OCR_LANG`, then restart.

**Satellite view is empty or reports an import error.** The satellite
propagation needs the `sgp4` Python package. Run `./install.sh` or
`.venv/bin/python -m pip install -r requirements.txt`, then restart. The doctor
lists `sgp4 (satellite propagation)`.

**Electron opens but the backend does not start.** Run
`./install.sh` so `.venv` exists, or launch with
`DARKSEC_PYTHON=$PWD/.venv/bin/python ./launch-desktop.sh`. The error dialog
also links back to this file.

**No radio hardware appears.** Install the SDR packages with
`./install.sh --with-sdr`, replug the device, and check `rtl_test` or
`hackrf_info` in a terminal.
