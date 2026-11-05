# Power Systems

> **Electricity is optional; safety is not.** Carbon monoxide, backfeeding, arc flash and lithium fires kill. Fuse everything, ventilate combustion, and respect local electrical code.

## Load audit
List every load with watts and hours/day. Multiply, sum, divide by 1,000 = kWh/day.

| Load | Watts | Hours/day | kWh/day |
|---|---:|---:|---:|
| LED lights ×4 | 40 | 5 | 0.20 |
| Phone/laptop charging | 65 | 3 | 0.20 |
| Fridge (efficient) | 150 | 8 (duty) | 1.20 |
| Radio (RX) | 5 | 12 | 0.06 |
| Water pump | 500 | 0.5 | 0.25 |
| Medical device | 60 | 24 | 1.44 |

Start with the smallest viable system. Efficiency beats generation: insulation, LED, DC appliances, timers, and turning things off.

## Solar
- **Array watts** ≈ daily kWh × 1,000 ÷ (peak-sun-hours × derate). Derate 0.7–0.8 for heat, dust, wiring, battery and age.
- Peak sun hours vary by latitude/season/weather. The console estimates clear-sky insolation; real output is lower.
- Tilt: roughly latitude for year-round; latitude −15° for summer; latitude +15° for winter. Face the equator (south in the north, north in the south).
- Avoid shade; even partial shade can collapse a string. Use a charge controller rated for array Voc and current.
- **MPPT** is better in cold/low-light; **PWM** is cheaper for small systems.
- Fuse between panel, controller, battery and load. Use UV-rated cable and MC4 connectors.

## Batteries
- **Lead-acid (flooded/AGM/GEL):** keep depth of discharge ≤50%; vent hydrogen; avoid freezing; check water (flooded).
- **LiFePO4:** 80–90% usable with BMS; lighter, longer life; needs low-temperature charge protection. Do not charge below 0 °C.
- **Lithium-ion (NMC):** high energy density, fire risk; use certified BMS and charger.
- **Bank Ah** = daily kWh × 1,000 × autonomy days ÷ (DoD × voltage).
- **Runtime** = Ah × V × DoD × inverter efficiency ÷ load W.
- Wire batteries with equal-length cables; fuse at the battery positive; use a disconnect.
- Do not mix chemistries, ages or capacities. Torque terminals; keep terminals covered.

## Inverters
- Size for surge (motors, pumps, fridges) not just continuous watts.
- Pure sine wave for electronics/motors; modified sine can overheat.
- Idle draw matters: a 1,000 W inverter may draw 10–20 W with no load.
- Keep inverter ventilated and dry. Do not parallel cheap inverters.
- **Never backfeed** a house through a socket. Use a transfer switch or critical-load subpanel.

## Generators
- **Never indoors, in a garage, crawlspace, or near windows/vents.** CO kills. Place outdoors, exhaust away, 5 m+ from openings.
- Use a CO alarm. Let it cool before refuelling. Store fuel in approved containers, away from ignition.
- Exercise monthly under load; change oil per manual; keep spare filters/plugs.
- Do not overload; use a breaker panel or rated cords.
- Fuel stabiliser and rotation: gasoline degrades in months; diesel stores longer with treatment; propane stores indefinitely.

## Wire sizing
Voltage drop = 2 × current × one-way length × resistance ÷ 1,000 (for metres and Ω/km). Keep drop ≤3% for critical DC, ≤5% for general. The console selects an AWG from a conservative table. Ampacity must also be satisfied. Fuse to protect the wire, not just the device.

## Safety
- DC arcs are dangerous; disconnect and verify before working.
- Use insulated tools, remove jewellery, work one hand where possible.
- Lock out/tag out. Label feeds.
- Ground and bond per code; use GFCI/RCD where required.
- Keep batteries away from metal, water and children. Have a Class D extinguisher for lithium, or water for cooling if safe.
- Solar panels produce voltage in light; cover them before working.

## The console
**Power** computes load, solar array, battery bank, runtime, wire size and solar insolation by latitude/date. Log battery %, fuel, generator hours and solar yield to spot degradation.

**Further reading:** *Photovoltaic Systems Handbook*, *NEC Article 690/706*, *FM 3-34.50*, and the **engineering** library folder.
