# HackRF / SDR Operations

> **Receive is not automatically legal, and transmit definitely is not.** Interception and privacy laws vary by country. The console is receive-first. Transmit is disabled until you explicitly enable it and attest that you are licensed/authorised. In an emergency, act within the law that applies to you.

## Hardware
**HackRF One:** 1 MHz–6 GHz, half-duplex, 8-bit IQ, up to 20 Msps. It is a wideband scanner and experimental radio, not a laboratory receiver. Dynamic range is limited; strong FM broadcast signals can desensitise the front end.

- Antenna: start with a discone or a resonant dipole for the band of interest. A random wire works but overloads easily.
- Connector: SMA. Use quality coax; keep runs short.
- Power: USB bus power. Use a powered hub if the device resets.
- Bias tee: `hackrf_biast -b 1` powers an external LNA; **never** enable it into a DC-shorted antenna.
- Static: ground yourself before connecting. Do not transmit without an antenna or dummy load.

## Setup checks
```bash
hackrf_info                 # firmware, serial, board
hackrf_sweep -f 88:108 -w 100000   # quick FM broadcast check
lsusb | grep -i great       # device visible?
```
If you get "No HackRF boards found", check USB cable, permissions (udev rules), and that no other program (gqrx, SDR++) has the device open. Only one process can use it at a time.

## Receive workflow in the console
1. **SDR → Device** — confirm the radio is detected. The console runs `hackrf_info` live.
2. **Sweep** — set start/end (e.g. 144–148 MHz), bin width 100 kHz, LNA 24–32, VGA 32–40, amp off. Watch the waterfall. Reduce gain if the noise floor is flat-topped or the spectrum is full of IMD.
3. **Capture** — select a frequency, 2.4 Msps, 5–10 s, demod NFM/WFM/AM/USB/LSB. The console records IQ, demodulates to WAV, and computes a spectrum. Keep IQ only when you need to re-analyse.
4. **Recordings** — play audio, keep IQ, or delete. Storage fills fast: 2.4 Msps ≈ 4.8 MB/s (≈17 GB/hour).
5. **Scanner** — step through a list of frequencies, dwell 2–5 s each. Log hits.

## Common signals
| Signal | Frequency | Mode / notes |
|---|---|---|
| FM broadcast | 87.5–108 MHz | WFM, 200 kHz |
| Airband | 118–137 MHz | AM, 25/8.33 kHz channels |
| NOAA APT | 137.1/137.62/137.9125 | AM, 2.4 Msps, experimental image decode |
| Public safety | 150–174 / 450–470 | NFM, often trunked/encrypted |
| 2 m ham | 144–148 | NFM/SSB |
| 70 cm ham | 420–450 | NFM/SSB |
| FRS/GMRS | 462/467 | NFM |
| ADS-B | 1090 | PPM; use dump1090 |
| AIS | 161.975/162.025 | GMSK; use AIS decoder |
| GPS L1 | 1575.42 | BPSK; weak, needs active antenna |
| Weather satellite | 137 MHz | APT/LRPT |
| Wi-Fi/Bluetooth | 2.4 GHz | digital, wideband |

## Digital decoders on the same machine
The console detects these tools and links to the right workflow:
- **dump1090** — ADS-B aircraft. With an RTL-SDR: `dump1090 --device-index 0 --net --quiet`. With HackRF: capture 2.4 Msps IQ at 1090 MHz, then `dump1090 --ifile capture.iq --net` (raw IQ support depends on build).
- **rtl_433** — ISM sensors (433/868/915 MHz). Use an RTL-SDR or HackRF-to-file.
- **multimon-ng** — POCSAG/FLEX paging, DTMF, AFSK, Morse from audio.
- **direwolf** — AX.25/APRS TNC.
- **gqrx / inspectrum** — interactive tuning and signal analysis.
- **gpspipe/gpsd** — GPS position from a USB receiver.

The console does not fake decodes. If a decoder is not installed or the radio is absent, it says so.

## NOAA APT weather images
1. Set frequency to 137.1 MHz (NOAA 19), 137.62 (NOAA 15) or 137.9125 (NOAA 18).
2. Sample rate 2.4 Msps, duration 120 s, demod APT.
3. Start the capture **when the satellite passes**. Use a pass-prediction tool or listen for the characteristic ticking.
4. The console produces a 2080-px-wide image. It is experimental: skew, sync and contrast may need a dedicated decoder.
5. Better antenna: a V-dipole or turnstile at 137 MHz, oriented for RHCP, with a clear horizon.

## Transmit — hard rules
The console requires all of the following:
1. `sdr_tx_enabled` in Settings.
2. `sdr_tx_attested` — you have recorded that you hold the required licence/authorisation.
3. Typing **TRANSMIT** in the confirmation box for every transmission.
Every attempt is written to the audit log.

**Do not transmit:**
- On public-safety, aviation, marine distress, military, or satellite frequencies without explicit authority.
- Without a valid licence for the band and mode.
- With more power than necessary; use the lowest power that works.
- Near explosives, medical devices, or in a way that interferes with life-safety communications.
- To jam, spoof, or disrupt any signal. That is a crime and can kill people.

In a genuine life-threatening emergency, many jurisdictions permit distress transmissions on designated frequencies. Know your local law **before** you need it.

## Emission security
A transmitter is a beacon. Direction-finding is cheap. Use the minimum power, shortest duration, directional antennas, and pre-arranged schedules. Do not transmit from a fixed shelter if you can avoid it. Turn off devices that beacon (phones, Wi-Fi, satellite messengers) when silence matters.

## Maintenance
- Keep firmware and tools current when online; record versions.
- Label antennas and coax with band and connector type.
- Carry adapters (SMA-BNC-N), a dummy load, spare USB cable, and a ferrite choke.
- Test the full chain before deployment: radio → cable → antenna → decoder.
- Log every capture and every transmission.

**Further reading:** *ARRL Handbook*, *HackRF documentation*, *GNU Radio docs*, *ITU Radio Regulations*, and your national regulator's licence conditions.
