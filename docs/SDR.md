# SDR Field Guide (HackRF, RTL-SDR, uConsole, Raspberry Pi, WebSDR)

DarkSec's SDR console has four receive paths:

| Path | Hardware | What it is for |
| --- | --- | --- |
| `hackrf` | HackRF One / Jawbreaker | Wideband sweep, IQ capture, live audio, gated TX |
| `rtl` | RTL-SDR (RTL2832U: R820T/R828D/E4000/FC001x) | Cheap HF/VHF/UHF receive, sweep, capture, live audio |
| `rtl_tcp` | Any `rtl_tcp` server on the LAN | Use a Pi/uConsole/desktop as a remote receiver |
| WebSDR | Public KiwiSDR/WebSDR sites | Listen with **no local radio** |

SoapySDR devices (SDRplay, Airspy, LimeSDR, PlutoSDR) are inventoried so the
hardware panel can tell you what is attached, but capture support is not yet
exposed in the console.

## Quick start (Debian / Ubuntu / Raspberry Pi OS / uConsole)

```bash
sudo apt update
sudo apt install rtl-sdr hackrf soapysdr-tools \
  soapysdr-module-rtlsdr soapysdr-module-hackrf
sudo usermod -aG plugdev "$USER"      # log out/in afterwards
rtl_test -t                           # should list the dongle
hackrf_info                           # HackRF only
```

Then open **SDR / Spectrum** and press **RESCAN**. The hardware panel shows
the detected tuner, gain steps, board model, CPU temperature and any fixes
that apply.

If you installed DarkSec with `bash install.sh --with-sdr`, these tools were
already installed.

## ClockworkPi uConsole

The uConsole is a Raspberry Pi CM4/CM5 handheld. Important hardware details:

* The **USB-C port on the side is power/charging only**. The CM4 USB host
  lanes go to the **USB-A port**. Plug the SDR dongle into USB-A.
* The **HackerGadgets AIO V1/V2** expansion board has an internal RTL-SDR.
  On **AIO V2** the SDR, GPS, LoRa and internal USB rails are off by default
  and controlled by GPIOs. SDR is **GPIO 7**:
  ```bash
  pinctrl 7 op
  pinctrl 7 dh
  ```
  With the `aiov2_ctl` service, enable SDR there and reboot. The SDR button in
  DarkSec's hardware panel runs the same `pinctrl` commands when it has
  permission (usually root); otherwise it shows the exact command to run.
* Use the IPEX/SMA **SDR** antenna connector on the expansion board, not the
  Wi-Fi antenna.
* After unplugging an RTL-SDR, wait **15–30 seconds** before reconnecting.
  The dongle has an initialization cool-down and will not enumerate if you
  re-plug immediately.
* A directly inserted USB-A dongle can mechanically block the adjacent USB-C
  port. A short USB-A extension cable avoids this.
* The RTL2832U/R860 on the AIO board gets warm. A thermal pad to the rear
  panel is recommended for long captures.
* Keep the live sample rate at **1.024–2.4 Msps** on the uConsole. Higher
  rates increase USB and CPU load.

Recommended uConsole images with SDR support: Rex's Bookworm/Trixie ports
(`forum.clockworkpi.com`), which include SDR++ and the RTL-SDR drivers. DarkSec
does not require SDR++; it uses `rtl_sdr`, `rtl_power` and `rtl_tcp` directly.

## Raspberry Pi

* Use a **powered USB hub** or a good 5 V/3 A supply. RTL-SDR plus Wi-Fi plus
  a display can trip under-voltage detection.
* Pi 3 / Zero: keep RTL-SDR at **2.4 Msps or lower**. Pi 4/5 can use
  2.4–3.2 Msps with a good supply.
* The hardware panel reads `vcgencmd get_throttled` and the thermal zone. If
  it reports under-voltage or throttling, fix power/cooling before blaming the
  SDR.
* RTL-SDR tuning below ~24 MHz needs direct sampling; the R820T tuner does not
  cover HF. Use an upconverter or an RTL-SDR Blog V4 (which has an HF path) for
  shortwave. `rtl_power`/`rtl_sdr` direct-sampling modes are hardware-specific.
* If `rtl_test` reports `usb_claim_interface error`, another program (gqrx,
  SDR++, rtl_tcp, dump1090) already owns the dongle. Stop it first.
* If the DVB-T driver claims the device, blacklist it:
  ```bash
  echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
  sudo modprobe -r dvb_usb_rtl28xxu
  ```
  Replug the dongle afterwards.

## RTL-SDR HF direct sampling and the HackerGadgets AIO V2

The AIO V2 uses an RTL2832U + R820T with an HF path wired to the **Q** direct-sampling input. Leave **HF MODE** on **AUTO**: DarkSec selects **HF Q** below 24 MHz and the **R820T tuner** at 24 MHz and above, including while tuning across the boundary. The same selector exists in Scan, Capture and Share this SDR. An explicit **TUNER** or **HF Q** choice is corrected when it cannot work at the tuned frequency, and the receiver panel shows a warning explaining the switch.

`rtl_test` reporting `No E4000 tuner found, aborting.` is normal for an R820T; it is not a fault. The useful lines are the device name, `R820T tuner`, the gain list and `Sampling at ...`.

If the receiver reports an error instead of audio, open **Diagnose receiver** (SYSTEM tab or **DIAGNOSE** in the page header). It reports the process user and groups, USB nodes, `lsusb`, the full `rtl_test`/`hackrf_info` output, a short real capture at the tuned frequency (RTL-SDR or HackRF), other programs that own the dongle, and the effective HF/tuner mode. **COPY** or **SAVE TXT** produces a report you can send without guessing.

`rtl_test -t` only benchmarks the tuner and never streams samples, so a passing `-t` does not prove the USB data path works. Test streaming on its own:

```bash
rtl_test -s 2400000        # Ctrl-C after a few seconds
```

`lost at least N bytes` means samples are flowing; silence plus no error means the endpoint is stalled (re-plug, wait 15–30 s, check power/cable).

## ADS-B / dump1090

Open **SDR → ADS-B** to start and stop a managed `dump1090`/`readsb` receiver, set tuner gain and device index, and watch the aircraft table. Managed mode uses dump1090's `--write-json` output under `fielddata/adsb/` and reads it directly, so it does **not** open an HTTP port and cannot collide with DarkSec's LAN server on 8080. The **EXTERNAL PORT** setting (default **8081**) is used only to detect and reuse a feed that another program already runs over HTTP. Stop the live receiver or rtl_tcp first — one RTL-SDR cannot feed two programs at once. Logs are written to `fielddata/logs/dump1090.log`.

## HackRF

```bash
sudo apt install hackrf
hackrf_info
```

HackRF is not a calibrated instrument. Use modest LNA/VGA gain; excessive gain
creates false images and overload. The TX path is hard-locked in DarkSec and
requires a settings unlock, legal attestation and the `TRANSMIT` confirmation.

## RTL-SDR gain and PPM

RTL-SDR gain is a discrete table (the hardware panel lists the steps). `0`
selects automatic gain. A PPM correction compensates for crystal error; start
at 0 and only adjust after comparing against a known carrier or WWV/CHU.

## Network SDR (rtl_tcp)

Any machine with an RTL-SDR can share it:

```bash
rtl_tcp -a 0.0.0.0 -p 1234 -f 100000000 -s 2400000 -g 40
```

On the other DarkSec instance open **SDR / Spectrum → Network SDR**, enter the
host and port, press **PROBE**, then select **Network rtl_tcp** in Live audio
or Capture. The **Share this SDR** panel can start/stop `rtl_tcp` for you and
shows copyable LAN URLs.

`rtl_tcp` has **no authentication**. Bind it to a trusted private LAN only
(or `127.0.0.1` and use an SSH tunnel). Never expose port 1234 to the internet.

## WebSDR / KiwiSDR (no radio required)

The **WebSDR / remote receivers** panel lists public KiwiSDR receivers from the
KiwiSDR community mirror (`cdn.aethersdr.com/kiwi.json`) plus a small curated
fallback. Choose a frequency and mode, then **LISTEN**. DarkSec creates a tuned
URL such as `?f=7100lsb` and opens the receiver inside a DarkSec modal, so
playback and waterfall stay in the app. **OPEN EXTERNALLY** is available if a
receiver blocks embedding or if DarkSec is HTTPS and the receiver is HTTP.

Notes:

* DarkSec does not proxy, record or automate public receivers. One click = one
  browser session, exactly like visiting the site.
* Public receivers are volunteer-run. Respect their user limits, terms and
  local laws. Do not hammer or scrape them.
* Receivers with `ext_api = 0` are marked **web only**. Their operators have
  disabled native API access; DarkSec only opens their web UI.
* If the directory mirror is unreachable, the curated receivers and the last
  cached list remain available.
* On an HTTPS DarkSec page, a plain-HTTP receiver may be blocked by the browser
  as mixed content. Open it in a new tab or run DarkSec over HTTP on the LAN.

## Live audio

Live audio is demodulated on the server and streamed to the browser as 16-bit
little-endian mono PCM. It works with HackRF, RTL-SDR and rtl_tcp. The browser
schedules the PCM into an `AudioContext`; the level bar shows signal activity.

Modes: NFM, WFM (mono), AM, USB, LSB. Squelch is a 0–100 threshold (0 = off).
For weak signals, lower the sample rate, reduce gain if the spectrum is
saturated, and use NFM/AM rather than WFM.

## Troubleshooting commands

```bash
lsusb | grep -Ei '0bda:2838|1d50:60'   # RTL-SDR / HackRF
rtl_test -t
rtl_test -t -T                          # bias tee test on supported dongles
rtl_power -f 88M:108M:125k -i 1 -1 -    # one sweep to stdout
rtl_sdr -f 100M -s 2.4M -n 2400000 /tmp/test.iq
hackrf_info
hackrf_sweep -f 88:108 -w 100000
SoapySDRUtil --find
pinctrl 7 op && pinctrl 7 dh           # uConsole AIO V2 SDR rail
vcgencmd get_throttled                 # Raspberry Pi power/thermal flags
```

Use **Settings → Offline maps → DIAGNOSE** for map issues; SDR issues are shown
in the SDR hardware panel and the device output expanders.

## Legal and RF safety

Receiving is generally permitted, but some frequencies are restricted by local
law. Transmitting requires a licence or an applicable exemption. Never
transmit into an antenna indoors, near sensitive electronics, or without
checking the frequency and power. DarkSec records every TX attempt.
