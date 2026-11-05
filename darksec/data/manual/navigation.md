# Navigation

> **A map and compass do not need batteries.** Learn them before you need them. The console's MGRS, UTM and declination tools are backups, not replacements for a paper map and a baseplate compass.

## The navigation kit
- Local topographic map(s), current edition, sealed in plastic.
- Baseplate compass with adjustable declination and a mirror.
- Protractor/romer for grid references, pencil, notebook.
- GPS/phone as a backup, with offline maps and spare power.
- Pace-count beads, altimeter/watch, headlamp.
- Route card: start, checkpoints, bearings, distances, timings, escape routes, water.

## Map basics
- **Scale:** 1:50,000 means 1 cm = 500 m. 1:25,000 is more detail.
- **Contours:** each line joins points of equal elevation. Index contours are labelled; count intervals between them.
- **Grid:** UTM/MGRS grid squares are 1 km at the standard scales. Read **right, then up** (eastings then northings).
- **Grid reference:** e.g. MGRS `18T WL 83959 07350` = 5-digit precision (1 m). Truncate to the precision you need; do not round.
- **Magnetic vs true north:** grid north, true north and magnetic north differ. Set declination once and check it.
- **Terrain association:** identify ridges, saddles, spurs, valleys, streams and roads. Contours tell a story; the compass tells direction.

## Compass work
1. Set the declination on the compass (or apply it mathematically).
2. Take a bearing: align the compass with the target, read the magnetic bearing, convert to grid/true as needed.
3. Follow the bearing, aiming off obstacles. Use a "handrail" (stream, ridge, road) when possible.
4. Resection: take bearings to two identifiable features; the intersection is your position. Three lines give a confidence triangle.
5. Back bearing: reverse the direction 180° to check you are on line.

**Formula:** true bearing = magnetic bearing + east declination (subtract west). The console computes WMM2025 declination for your location and date.

## Pace count
Measure 100 m on flat ground. Walk naturally and count every second step (or both steps). Repeat three times and average. Recalibrate on slopes, sand, snow, mud and with a pack. Use beads: 9 beads for hundreds, plus a marker for thousands. Typical adult: 60–70 double-paces per 100 m; a loaded pack adds 5–15%.

## Dead reckoning
Distance and bearing from a known point. Errors accumulate; update with terrain features and resection. Record each leg with time, bearing, distance and elevation. The console's Nav module calculates destination, midpoint, cross-track, resection and slope distance.

## Sun and stars
- **Northern hemisphere:** the sun is due south at solar noon; shadows point north. At night, Polaris is due north and its altitude equals your latitude.
- **Southern hemisphere:** use the Southern Cross: extend the long axis 4.5× its length, drop to the horizon for south. Use the two Pointer stars for confirmation.
- **Watch method (northern):** point the hour hand at the sun; bisect the angle between the hour hand and 12 o'clock for south (adjust for daylight saving). Approximate.
- **Equal-length shadow method:** mark the tip of a shadow, wait 15–20 minutes, mark again; the line between marks runs roughly east–west.
- **Moon:** not reliable for precise bearing. Use it for rough direction only.

## GPS and phones
- Download offline maps and the area of operations before leaving.
- Carry a power bank and cable; cold drains batteries.
- GPS accuracy degrades under canopy, in canyons and near buildings. Do not blind-trust it.
- Record waypoints in MGRS as well as lat/lon. A grid reference is easier to read on paper.
- If using the browser-based phone GPS bridge, put both devices on a private network, start the five-minute HTTPS session in Settings → Position, scan the QR with the phone camera, and explicitly accept the certificate warning only when its IP matches the selected computer interface. Grant precise location, tap START GPS, then verify the reported accuracy and map point before accepting it. Stop the session afterward. Do not use this on public/shared Wi-Fi; keep a paper map and compass.
- The phone bridge is temporary position input, not live tracking, an SOS system, or a message to an off-site contact. Keep the phone page open; location permission and battery use are required.
- If captured/lost, assume the device holds sensitive locations. Use the vault and a duress plan.

## Lost procedures
1. **Stop.** Mark your last known point.
2. **Protect:** shelter, water, fire, signal.
3. **Orient:** map, compass, terrain, sun. Identify at least two features.
4. **Decide:** stay or move. Moving without a known position often makes things worse.
5. **Signal:** three of anything; bright marker; whistle; mirror.
6. **Conserve:** battery, food, energy. Stay found.

## Night and bad weather
- Slow down; use a headlamp with a red filter to preserve night vision.
- Navigate by bearing and distance, not by sight.
- Use handrails and catching features (streams, ridges, roads) to stop you overshooting.
- In whiteout/fog, use a compass and rope/team link; do not trust visual slopes.

## The console
**Map** displays offline tiles, vector fallback, waypoints, tracks and coordinate conversion. It also overlays cached points intelligence — earthquakes, disaster/alert events and geolocated conflict diamonds (orange = UCDP verified, amber = GDELT live media-derived). Every overlay is age-stamped; cached layers never render as if current, and stale sources are excluded. **Nav** computes celestial data, sun/moon rise/set, twilight, planets, declination, resection, pace and dead reckoning. The **Library** contains FM 3-25.26 and map-reading manuals.

**Further reading:** *FM 3-25.26 Map Reading and Land Navigation*, *FM 21-76*, *Bowditch American Practical Navigator*, and the **maps** folder.
