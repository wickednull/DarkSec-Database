# Legal, Ethical & Safety Boundaries

**Read this before you transmit, treat, defend, or enter someone else's property.**
DarkSec is a reference and record-keeping tool. It is not a license, a medical
license, a legal opinion, or an authorization to operate.

## 1. Radio transmission

- **Receive first.** Listening is generally lawful; transmitting is not
  automatically lawful. Every country, and often every band and power level, has
  its own rules.
- In the United States, unlicensed operation is limited to specific services
  (for example CB, FRS, MURS, and amateur radio under a valid license). The
  HackRF is **not** a type-accepted consumer radio. Connecting it to an antenna
  and transmitting can violate equipment-authorization, spurious-emission, and
  licensing rules even on an otherwise permitted frequency.
- Never transmit on aviation, marine distress, public-safety, cellular, military,
  or satellite bands without explicit authorization. Never jam, spoof, or
  interfere with any signal. Interference with safety-of-life communications is
  a serious crime in most jurisdictions.
- The built-in transmit path is deliberately gated behind a settings unlock, a
  legal attestation, and a typed confirmation. Those gates are there to slow you
  down, not to make transmission legal.
- Check your current national regulator before transmitting: FCC (US), Ofcom
  (UK), ISED (Canada), ACMA (Australia), BNetzA (Germany), or your local
  equivalent. Rules and band plans change.
- Amateur radio operators must identify with their assigned call sign and follow
  the band plan. Emergency operation may have special provisions, but do not
  assume them.
- Antennas, amplifiers, and batteries can create shock, burn, fire, RF-exposure,
  and lightning hazards. Disconnect antennas during storms. Do not touch live RF
  conductors. Respect RF exposure limits, especially near high-gain antennas.

## 2. Medical care

- The medical content is **reference material for trained responders and for
  improvisation when no clinician is available**. It is not a diagnosis or a
  prescription.
- Drug doses, especially for children, must be verified against a current
  formulary or a qualified clinician. A wrong dose can kill. When in doubt, give
  oxygen, keep the patient warm, stop bleeding, and evacuate.
- Do not perform procedures outside your training. Do not rely on an app to
  identify a plant, a mushroom, a snake, or a chemical.
- Emergency services may be unavailable or delayed. Plan evacuation before you
  need it. Tell someone where you are going.
- Record allergies, medications, pregnancy, and weight before treatment when
  possible. The patient log is encrypted only if you configure the vault; the
  database itself is not encrypted at rest. Use full-disk encryption and physical
  control of the device.

## 3. Use of force and security

- Security guidance is about avoidance, detection, de-escalation, and
  preservation of life. It is not tactical or weapons instruction.
- Self-defense law varies enormously. Proportionality, imminent threat, duty to
  retreat, and defense of property are not universal. Know your jurisdiction and
  consult a lawyer.
- Never booby-trap, poison, or set man-traps. They are indiscriminate, often
  illegal, and frequently injure the person who set them.
- Do not use this tool to surveil, stalk, harass, or target another person. Do
  not intercept communications you are not authorized to receive.
- In a collapse scenario, the greatest risk is often other people. OPSEC means
  not advertising supplies, movement, or weaknesses. It does not mean attacking
  first.

## 4. Hazardous materials, fire, and power

- CBRN content is protective and recognition-focused. Do not manufacture,
  weaponize, or disperse chemical, biological, radiological, or nuclear material.
- Radiation readings from consumer devices are indicative only. KI is not a
  general radiation antidote and can harm people with certain thyroid conditions.
- Fuel, generators, batteries, and solar systems can cause fire, electrocution,
  and carbon-monoxide poisoning. Never run a generator indoors, in a garage, or
  near a window. Use CO alarms. Fuse every circuit. Follow electrical codes.
- Fire is a tool and a threat. Check burn bans, never leave a fire unattended,
  and fully extinguish it. Do not burn treated wood, plastics, or unknown
  materials.
- Water treatment guidance assumes clear, non-chemical, non-radiological water.
  Boiling does not remove chemicals or radiation. Distillation and certified
  filtration do not remove every contaminant.

## 5. Foraging, hunting, and fishing

- Never eat a plant, mushroom, or animal you cannot identify with certainty.
  "Looks like" is not identification. Use multiple reliable sources and, where
  possible, a local expert.
- Some safe-looking plants have deadly look-alikes. Some animals are protected,
  dangerous, or carry disease. Follow hunting, fishing, and land-access law.
- Wild food is a supplement, not a plan. Calorie deficits, poisoning, and
  parasites are real.

## 6. Privacy and data

- The app binds to localhost and uses a random per-launch token. Do not expose it
  to a network or the internet. Do not disable the token unless you understand
  the risk.
- Operator data lives in `fielddata/`. It is not encrypted at rest unless you use
  the encrypted notes vault and full-disk encryption. Anyone with the device or a
  backup can read it.
- The panic purge deletes operator data and recordings. It does **not** securely
  erase flash storage; remnants may persist. Full-disk encryption is the real
  defense.
- Backups contain sensitive data. Store them accordingly. Test restore before you
  need it.

## 7. Online feeds and accuracy

- The optional feeds (USGS, NOAA, NWS, GDACS, WHO, ReliefWeb) are public and may
  be delayed, incomplete, or wrong. Cached data is clearly aged; do not mistake a
  cache for a live warning.
- Propagation, weather, tide, fallout, and astronomical outputs are estimates or
  nowcasts unless explicitly sourced. Verify with official broadcasts and
  instruments.
- The document library is a snapshot of third-party material. Its presence does
  not imply endorsement, accuracy, or currency. Check publication dates.

## 8. No warranty

This software is provided as-is, without warranty of any kind. The authors and
contributors are not liable for injury, death, property damage, legal
consequences, or data loss arising from its use. You are responsible for your
decisions, your equipment, and your compliance with the law.

**When the stakes are life and death, use the most reliable source available,
not the most convenient one.**
