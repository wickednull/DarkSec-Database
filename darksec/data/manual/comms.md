# Communications

> **Receive first. Transmit only when licensed, authorised, or in a genuine emergency permitted by law.** A radio that transmits on the wrong frequency can interfere with life-saving communications — or reveal your position.

## The comms plan
Write it down and give every team member a copy:
- Primary and alternate frequencies/channels, mode, power and antenna.
- Call signs and unit identifiers.
- Check-in times and missed-check-in procedure.
- Encryption keys (if used) and key-change schedule.
- Emergency distress frequency and procedure.
- Relay stations and fallback methods (phone, text, courier, signal).
- Emission security: when to go silent, how long to transmit, antenna placement.

## Radio fundamentals
- **VHF/UHF** is mostly line-of-sight. Height and antenna gain matter more than power.
- **HF** can reach beyond the horizon via ionospheric propagation; it is time-of-day, season and space-weather dependent.
- **FM** is clear but captures the strongest signal; **SSB** is efficient for weak-signal HF; **AM** is used in aviation.
- **Squelch** suppresses noise but can hide weak signals. **CTCSS/DCS** tones reduce nuisance audio, not interference.
- **Power** increases range roughly with the square root of height; doubling power gives only a small range increase. Antenna first.
- **Batteries:** carry spare packs, a power bank, and a way to charge from solar/vehicle. Cold reduces capacity.

## Antennas
- **Half-wave dipole:** 468/f(MHz) feet total, each leg 234/f feet. Feed with coax; orient broadside to the target.
- **Quarter-wave vertical:** 234/f feet; needs a ground plane or radials. Good for local/omni.
- **EFHW:** end-fed half-wave with a 49:1 transformer; convenient for portable HF.
- **Loop:** 1005/f feet circumference; quieter on receive.
- **Yagi:** directional gain; point at the target; useful for weak-signal or interference rejection.
- Keep antennas away from metal, power lines and people. Height beats power. Use a choke/balun to reduce common-mode noise.
- Never transmit into a disconnected/mismatched antenna without an ATU/SWR check; you can damage the radio.

## HF propagation
- **Day:** higher bands (20/17/15/12/10 m) with higher solar flux.
- **Night:** lower bands (80/40/30 m).
- **Greyline:** dawn/dusk paths can be exceptionally good.
- **Space weather:** solar flares cause shortwave fadeouts; geomagnetic storms (Kp ≥5) degrade high-latitude paths. Monitor WWV/WWVH at 10/15/20 MHz or the SWPC feed when online.
- The console's HF outlook is a **planning heuristic**, not a prediction. Use beacons, WSPR, or a real propagation engine for operations.

## Nets and procedures
- Listen before transmitting. Identify with your call sign.
- Use **prowords**: ROGER (received), WILCO (will comply), OVER (reply expected), OUT (end), SAY AGAIN, STANDBY, BREAK, MAYDAY, PAN PAN, SECURITE.
- **Radio check:** "This is [call], radio check, over." Reply with readability/signal strength.
- **MAYDAY:** MAYDAY x3, this is [call], position, nature of distress, persons, assistance required, over.
- Keep transmissions short. Use plain language. Write messages before sending.
- **Emission security:** every transmission is a beacon. Use lowest power, shortest duration, directional antennas, and pre-arranged schedules. Avoid patterns.

## Emergency and calling frequencies
| Service | Frequency | Mode | Notes |
|---|---|---|---|
| Aviation distress | 121.5 MHz | AM | Guarded; false alarms are illegal |
| Military aviation distress | 243.0 MHz | AM | Guarded |
| Marine distress | 156.8 MHz (Ch 16) | FM | MAYDAY; Ch 70 DSC |
| CB emergency | 27.065 MHz (Ch 9) | AM | US |
| GMRS emergency | 462.675 MHz | FM | US; licence required |
| 2 m calling | 146.520 MHz | FM | US; Wilderness Protocol |
| APRS | 144.390 MHz | AFSK | US |
| NOAA weather | 162.400–162.550 | FM | Continuous broadcast |
| WWV time | 2.5/5/10/15/20 MHz | AM | Time and propagation |
| SATERN | 14.265 MHz | USB | Disaster net |
| Maritime Mobile Net | 14.300 MHz | USB | Daytime maritime traffic |

Frequencies and channel plans change. Verify with the current national regulator and local emergency plan before transmitting.

## Distress signalling without a radio
- Three of anything: whistle blasts, fires, flashes, marks.
- Signal mirror: sweep the horizon; flash the target.
- Ground-to-air: large contrasting symbols (V = need help, X = need medical help, arrow = going this way).
- Bright clothing/tarp, strobe at night, whistle in fog.
- Phone: text uses less power and can queue when voice fails. Keep it warm and conserve battery.

## The console
**Comms** includes the frequency database, HF outlook, antenna/horizon calculators, Morse trainer, phonetic alphabet, prowords and Q-codes. **SDR** provides real spectrum and capture. The **radio log** records frequencies, modes, callsigns and summaries for after-action review.

**Further reading:** *ARRL Handbook*, *ARRL ARES Field Resources Manual*, *FM 24-19 Radio Operator's Handbook*, *FM 6-02 Signal Support*, and the **comms** folder.
