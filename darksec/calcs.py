"""Field calculations.

Every function returns the inputs it used and an explicit note where a value
is an estimate. Nothing here replaces a trained medic, a current map, a tide
table, a propagation prediction or a radiation instrument.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

# --------------------------------------------------------------------------
# Weather
# --------------------------------------------------------------------------

def wind_chill_c(temp_c: float, wind_kmh: float) -> dict:
    if temp_c > 10 or wind_kmh < 4.8:
        return {"value_c": temp_c, "applicable": False,
                "note": "Wind-chill formula applies at <=10 C and wind >=4.8 km/h."}
    v = wind_kmh ** 0.16
    value = 13.12 + 0.6215 * temp_c - 11.37 * v + 0.3965 * temp_c * v
    return {"value_c": round(value, 1), "applicable": True, "note": "Environment Canada / NWS wind chill."}


def heat_index_c(temp_c: float, rh: float) -> dict:
    t = temp_c * 9 / 5 + 32
    if t < 80:
        return {"value_c": temp_c, "applicable": False, "note": "Heat index applies above 27 C."}
    hi = -42.379 + 2.04901523 * t + 10.14333127 * rh - 0.22475541 * t * rh
    hi -= 0.00683783 * t * t + 0.05481717 * rh * rh
    hi += 0.00122874 * t * t * rh + 0.00085282 * t * rh * rh - 0.00000199 * t * t * rh * rh
    if rh < 13 and 80 <= t <= 112:
        hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
    if rh > 85 and 80 <= t <= 87:
        hi += ((rh - 85) / 10) * ((87 - t) / 5)
    return {"value_c": round((hi - 32) * 5 / 9, 1), "applicable": True, "note": "Rothfusz regression (NWS)."}


def dew_point_c(temp_c: float, rh: float) -> float:
    rh = max(0.1, min(100.0, rh))
    a, b = 17.27, 237.7
    gamma = (a * temp_c) / (b + temp_c) + math.log(rh / 100.0)
    return round((b * gamma) / (a - gamma), 1)


def wet_bulb_c(temp_c: float, rh: float) -> float:
    rh = max(1.0, min(100.0, rh))
    return round(
        temp_c * math.atan(0.151977 * math.sqrt(rh + 8.313659))
        + math.atan(temp_c + rh)
        - math.atan(rh - 1.676331)
        + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh)
        - 4.686035,
        1,
    )


def humidex(temp_c: float, dew_c: float) -> float:
    e = 6.112 * 10 ** (7.5 * dew_c / (237.7 + dew_c))
    return round(temp_c + 0.5555 * (e - 10), 1)


def pressure_altitude_ft(pressure_hpa: float) -> float:
    return round(145442.16 * (1 - (pressure_hpa / 1013.25) ** 0.190263), 0)


def density_altitude_ft(pressure_alt_ft: float, temp_c: float) -> float:
    isa_temp = 15 - 1.98 * (pressure_alt_ft / 1000.0)
    return round(pressure_alt_ft + 118.8 * (temp_c - isa_temp), 0)


def lightning_distance_km(seconds_to_thunder: float) -> dict:
    return {"km": round(seconds_to_thunder * 0.343, 2), "miles": round(seconds_to_thunder * 0.213, 2),
            "note": "Sound speed ~343 m/s. If thunder follows flash within 30 s, take shelter."}


BEAUFORT = [
    (1, "CALM", "Smoke rises vertically"), (5, "LIGHT AIR", "Smoke drift"),
    (11, "LIGHT BREEZE", "Leaves rustle"), (19, "GENTLE BREEZE", "Leaves and twigs move"),
    (28, "MODERATE", "Dust and paper rise"), (38, "FRESH", "Small trees sway"),
    (49, "STRONG", "Large branches move"), (61, "NEAR GALE", "Whole trees move"),
    (74, "GALE", "Twigs break off"), (88, "STRONG GALE", "Slight structural damage"),
    (102, "STORM", "Trees uprooted"), (117, "VIOLENT STORM", "Widespread damage"),
    (999, "HURRICANE", "Devastation"),
]


def beaufort(kmh: float) -> dict:
    for limit, name, effect in BEAUFORT:
        if kmh < limit:
            return {"force": BEAUFORT.index((limit, name, effect)), "name": name,
                    "effect": effect, "kmh": kmh}
    return {"force": 12, "name": "HURRICANE", "effect": "Devastation", "kmh": kmh}


def pressure_trend(hpa_now: float, hpa_3h_ago: float) -> dict:
    change = hpa_now - hpa_3h_ago
    if change >= 3.5:
        text = "RISING RAPIDLY - improving, expect stronger wind"
    elif change >= 1.0:
        text = "RISING - improving"
    elif change <= -3.5:
        text = "FALLING RAPIDLY - storm likely, secure position"
    elif change <= -1.0:
        text = "FALLING - deterioration likely"
    else:
        text = "STEADY"
    return {"change_hpa": round(change, 2), "tendency": text,
            "note": "3-hour pressure tendency is a nowcast, not a forecast."}


def solar_insolation(lat: float, day_of_year: int | None = None, tilt: float | None = None,
                     clear_sky: float = 0.75) -> dict:
    """Clear-sky daily plane-of-array insolation in kWh/m2/day (offline model)."""
    n = day_of_year or datetime.now(timezone.utc).timetuple().tm_yday
    decl = math.radians(23.45 * math.sin(math.radians(360 * (284 + n) / 365)))
    phi = math.radians(lat)
    tilt = abs(lat) if tilt is None else tilt
    beta = math.radians(max(0.0, min(90.0, tilt)))
    hemi = 1.0 if lat >= 0 else -1.0
    # A tilt toward the equator: south-facing in the north, north-facing in the south.
    phi_eff = phi - hemi * beta
    ws = math.acos(max(-1.0, min(1.0, -math.tan(phi_eff) * math.tan(decl))))
    eccentricity = 1 + 0.033 * math.cos(math.radians(360 * n / 365))
    total = 0.0
    steps = 96
    for i in range(steps):
        omega = -ws + (2 * ws) * (i + 0.5) / steps
        cos_theta = (math.sin(decl) * math.sin(phi_eff)
                     + math.cos(decl) * math.cos(phi_eff) * math.cos(omega))
        if cos_theta > 0:
            total += cos_theta * (2 * ws / steps)
    # total is the integral of cos(theta) over hour angle (radians). Convert to
    # daily energy with dt = 12/pi d(omega); without this factor the result is
    # low by a factor of 12.
    kwh = 1367 * eccentricity * total * (12.0 / math.pi) * clear_sky / 1000
    return {
        "lat": lat, "day_of_year": n, "tilt_deg": tilt,
        "kwh_m2_day": round(kwh, 2),
        "daily_peak_sun_hours": round(kwh, 2),
        "note": "Clear-sky estimate. Apply local weather derating; real output is lower.",
    }


# --------------------------------------------------------------------------
# Medical (reference calculators; not a prescription)
# --------------------------------------------------------------------------

def bmi(weight_kg: float, height_cm: float) -> dict:
    b = weight_kg / (height_cm / 100) ** 2
    if b < 18.5:
        cat = "UNDERWEIGHT"
    elif b < 25:
        cat = "NORMAL"
    elif b < 30:
        cat = "OVERWEIGHT"
    elif b < 35:
        cat = "OBESE I"
    elif b < 40:
        cat = "OBESE II"
    else:
        cat = "OBESE III"
    return {"bmi": round(b, 1), "category": cat}


def bsa_mosteller(weight_kg: float, height_cm: float) -> float:
    return round(math.sqrt(height_cm * weight_kg / 3600), 2)


def ibw_devine(sex: str, height_cm: float) -> dict:
    inches = height_cm / 2.54
    base = 50.0 if sex.lower().startswith("m") else 45.5
    ibw = base + 2.3 * max(0.0, inches - 60)
    return {"ibw_kg": round(ibw, 1), "note": "Devine formula; not valid for children."}


def mean_arterial_pressure(sys: int, dia: int) -> float:
    return round((sys + 2 * dia) / 3, 1)


def shock_index(hr: int, sys: int) -> dict:
    si = hr / sys if sys else 0
    risk = "NORMAL" if si < 0.7 else "ELEVATED" if si < 0.9 else "HIGH" if si < 1.0 else "CRITICAL"
    return {"index": round(si, 2), "risk": risk, "note": "Shock index >0.9 warrants urgent assessment."}


def qsofa(rr: int, sys: int, altered_mental_status: bool) -> dict:
    score = int(rr >= 22) + int(sys <= 100) + int(bool(altered_mental_status))
    return {"score": score, "high_risk": score >= 2,
            "note": "qSOFA is a screening prompt, not a diagnosis."}


def gcs(eye: int, verbal: int, motor: int) -> dict:
    if not (1 <= eye <= 4 and 1 <= verbal <= 5 and 1 <= motor <= 6):
        raise ValueError("GCS components out of range")
    total = eye + verbal + motor
    severity = "SEVERE" if total <= 8 else "MODERATE" if total <= 12 else "MILD"
    return {"total": total, "severity": severity, "intubate_consider": total <= 8}


def news2(rr: int, spo2: int, sys: int, hr: int, temp_c: float, avpu: str = "A",
          supplemental_o2: bool = False, spo2_scale1: bool = True) -> dict:
    rr_score = 3 if rr <= 8 else 1 if rr <= 11 else 0 if rr <= 20 else 2 if rr <= 24 else 3
    if spo2_scale1:
        spo2_score = 3 if spo2 <= 91 else 2 if spo2 <= 93 else 1 if spo2 <= 95 else 0
    else:
        spo2_score = (3 if spo2 <= 83 else 2 if spo2 <= 85 else 1 if spo2 <= 87
                      else 0 if spo2 <= 92 else 1 if spo2 <= 94 else 2 if spo2 <= 96 else 3)
    sys_score = 3 if sys <= 90 else 2 if sys <= 100 else 1 if sys <= 110 else 0 if sys <= 219 else 3
    hr_score = 3 if hr <= 40 else 1 if hr <= 50 else 0 if hr <= 90 else 1 if hr <= 110 else 2 if hr <= 130 else 3
    temp_score = (3 if temp_c <= 35.0 else 1 if temp_c <= 36.0 else 0 if temp_c <= 38.0
                  else 1 if temp_c <= 39.0 else 2)
    avpu_score = 3 if avpu.upper() != "A" else 0
    o2_score = 2 if supplemental_o2 else 0
    components = {"respiratory": rr_score, "spo2": spo2_score, "systolic": sys_score,
                  "heart_rate": hr_score, "temperature": temp_score,
                  "consciousness": avpu_score, "supplemental_o2": o2_score}
    score = sum(components.values())
    risk = "LOW" if score <= 4 else "MEDIUM" if score <= 6 else "HIGH"
    # NEWS2 escalation: 5-6 is urgent, >=7 is an emergency, and any single
    # parameter scoring 3 needs urgent clinical review even at a low total.
    urgent = score >= 5 or max(components.values()) >= 3
    emergency = score >= 7
    return {"score": score, "risk": risk, "urgent": urgent, "emergency": emergency,
            "single_parameter_3": max(components.values()) >= 3,
            "components": components,
            "note": "NEWS2 track-and-trigger. Total 5-6 or any single parameter of 3 needs urgent "
                    "clinical review; total >=7 is an emergency. Follow local protocol."}


def iv_drip_rate(volume_ml: float, minutes: float, drop_factor: int = 20) -> dict:
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    rate = volume_ml * drop_factor / minutes
    return {"drops_per_min": round(rate, 1), "ml_per_hr": round(volume_ml / minutes * 60, 1),
            "drop_factor": drop_factor}


def maintenance_fluid_ml_hr(weight_kg: float) -> dict:
    if weight_kg <= 10:
        ml_hr = 4 * weight_kg
    elif weight_kg <= 20:
        ml_hr = 40 + 2 * (weight_kg - 10)
    else:
        ml_hr = 60 + 1 * (weight_kg - 20)
    return {"ml_day": round(ml_hr * 24, 0), "ml_hr": round(ml_hr, 1),
            "note": "4-2-1 rule (4 mL/kg/h first 10 kg, 2 next 10, 1 thereafter). "
                    "Maintenance only; replace losses separately and follow local protocol."}


def parkland_burn(weight_kg: float, tbsa_pct: float) -> dict:
    total = 4 * weight_kg * tbsa_pct
    return {"total_ml_24h": round(total, 0), "first_8h_ml": round(total / 2, 0),
            "next_16h_ml": round(total / 2, 0),
            "note": "Parkland formula for crystalloid: half of the 24 h total in the first 8 h "
                    "from the time of burn (not from assessment), remainder over 16 h. Confirm with "
                    "a burn protocol; over-resuscitation harms."}


def rule_of_nines(age_years: float = 30) -> dict:
    if age_years < 1:
        parts = {"head": 18, "trunk_front": 18, "trunk_back": 18, "each_arm": 9,
                 "each_leg": 14, "perineum": 0}
        note = "Infant proportions. Lund-Browder is more accurate for burns in small children."
    elif age_years < 10:
        parts = {"head": 15, "trunk_front": 18, "trunk_back": 18, "each_arm": 9,
                 "each_leg": 15.5, "perineum": 0}
        note = "Child proportions; each leg is 15.5% so the regions total 100%."
    else:
        parts = {"head": 9, "trunk_front": 18, "trunk_back": 18, "each_arm": 9,
                 "each_leg": 18, "perineum": 1}
        note = "Adult proportions; the perineum adds the final 1% to the classic 99% estimate."
    total = (parts["head"] + parts["trunk_front"] + parts["trunk_back"]
             + 2 * parts["each_arm"] + 2 * parts["each_leg"] + parts["perineum"])
    return {**parts, "total_pct": total, "note": note}


def pediatric_weight_estimate(age_years: float) -> dict:
    if 0 < age_years < 1:
        months = age_years * 12
        kg = (months + 9) / 2
        note = "Infant estimate: (age in months + 9) / 2 kg. Use a measured weight whenever possible."
    elif 1 <= age_years <= 10:
        kg = 2 * (age_years + 4)
        note = "APLS 1-10 year estimate: 2 x (age + 4) kg. Use a measured weight whenever possible."
    else:
        return {"weight_kg": None,
                "note": "No validated simple formula above 10 years. Use a measured weight; do not "
                        "extrapolate paediatric dosing from this tool."}
    return {"weight_kg": round(kg, 1), "note": note}


def epinephrine_anaphylaxis(weight_kg: float) -> dict:
    if weight_kg <= 0:
        raise ValueError("weight must be positive")
    dose_mg = min(0.5, max(0.01 * weight_kg, 0.01))
    if weight_kg >= 30:
        auto = "0.3 mg adult autoinjector (0.5 mg acceptable per protocol)"
    elif weight_kg >= 15:
        auto = "0.15 mg junior autoinjector"
    else:
        auto = ("no standard autoinjector; draw 0.01 mg/kg IM (max 0.15 mg) from a "
                "1 mg/mL vial with a 1 mL syringe")
    return {"mg": round(dose_mg, 2), "ml_1mg_per_ml": round(dose_mg, 2),
            "auto_injector": auto,
            "note": "Anaphylaxis is a clinical emergency. Give IM epinephrine into the outer thigh, "
                    "call for help, and repeat after 5-15 min if needed. Use the 1 mg/mL (1:1,000) "
                    "IM formulation, never the dilute IV preparation. Fixed-dose autoinjectors may "
                    "differ from the calculated weight-based dose; follow the device label and local "
                    "protocol. This is reference information, not a prescription."}


def naloxone_dose() -> dict:
    return {"adult_im_iv_mg": 0.4, "repeat": "every 2-3 minutes, titrating to breathing; higher total doses may be needed for fentanyl/carfentanil",
            "intranasal_mg": 4.0, "note": "Naloxone may precipitate withdrawal; support airway and breathing and call emergency services. Repeat until breathing is adequate; the opioid may outlast naloxone."}


# --------------------------------------------------------------------------
# Water / food
# --------------------------------------------------------------------------

def water_requirement(people: int, days: int, climate: str = "temperate") -> dict:
    base = {"temperate": 4.0, "hot": 6.0, "desert": 8.0, "cold": 5.0, "high_altitude": 6.0}.get(climate, 4.0)
    liters = people * days * base
    return {"liters": round(liters, 0), "gallons": round(liters / 3.785, 1),
            "liters_per_person_day": base,
            "note": "4 L/person/day is a minimum planning figure (drinking + basic hygiene). Increase for heat, illness, pregnancy and work."}


def bleach_dose(liters: float, bleach_pct: float = 6.0, cloudy: bool = False) -> dict:
    """CDC-style household bleach disinfection guidance.

    The drop figure follows the CDC clear/cloudy table for 5.25-6% and 8.25%
    bleach. Always use unscented bleach and verify with a test kit where possible.
    """
    if bleach_pct <= 0:
        raise ValueError("bleach concentration must be positive")
    if bleach_pct <= 6.5:
        drops_per_l = 4.0 if cloudy else 2.0
    elif bleach_pct <= 8.5:
        drops_per_l = 2.0 if cloudy else 1.0
    else:
        drops_per_l = 1.0 if cloudy else 0.5
    drops = drops_per_l * liters
    ml = drops * 0.05
    return {
        "drops": round(drops, 1), "ml": round(ml, 2), "drops_per_liter": drops_per_l,
        "contact_minutes": 30 if not cloudy else 60,
        "target_free_chlorine_ppm": 2.0 if not cloudy else 4.0,
        "note": "Use plain unscented household bleach (5.25-8.25% sodium hypochlorite). "
                "Do not use bleach with additives. If water is cloudy, filter first. "
                "For 5.25-6%: 2 drops/L clear, 4 drops/L cloudy. For 8.25%: 1 drop/L clear, 2 drops/L cloudy. "
                "Boiling or certified filtration is preferred.",
    }


def boil_water(minutes_at_altitude_m: float = 0) -> dict:
    elevation_ft = minutes_at_altitude_m * 3.28084
    minutes = 3 if elevation_ft >= 6500 else 1
    return {"rolling_boil_minutes": minutes, "note": "CDC: 1 minute rolling boil below 6,500 ft; 3 minutes above. Cooling does not re-contaminate."}


def ors_recipe(liters: float = 1.0) -> dict:
    return {
        "water_l": liters,
        "salt_g": round(2.6 * liters, 2),
        "glucose_g": round(13.5 * liters, 2),
        "table_sugar_g": round(25.7 * liters, 1),
        "potassium_chloride_g": round(1.5 * liters, 2),
        "household_measure": f"{round(0.5 * liters, 2)} level tsp salt + "
                             f"{round(6 * liters, 1)} level tsp table sugar per liter; "
                             "potassium chloride optional (commercial ORS preferred)",
        "note": "WHO low-osmolarity ORS formula. 13.5 g glucose equals about 25.7 g table sugar "
                "(sucrose); 6 level tsp is the household equivalent. Household teaspoons are "
                "approximate and can seriously over- or under-dose: use a scale or commercial ORS "
                "sachet when possible, never add extra salt, and do not use in infants without "
                "medical advice. Seek care for severe dehydration, persistent vomiting or blood in stool.",
    }


def ration_days(calories: float, people: int, kcal_per_person_day: float = 2000) -> dict:
    if people <= 0:
        raise ValueError("people must be positive")
    days = calories / (people * kcal_per_person_day)
    return {"days": round(days, 1), "kcal_per_person_day": kcal_per_person_day,
            "note": "2000 kcal is a planning baseline; hard work, cold and pregnancy increase needs."}


# --------------------------------------------------------------------------
# Power
# --------------------------------------------------------------------------

def solar_array_size(load_kwh_day: float, peak_sun_hours: float, derate: float = 0.75) -> dict:
    if peak_sun_hours <= 0:
        raise ValueError("peak sun hours must be positive")
    watts = load_kwh_day * 1000 / (peak_sun_hours * derate)
    return {"array_watts": round(watts, 0), "derate": derate,
            "note": "Add 20-30% margin for age, heat, wiring and weather."}


def battery_bank(load_kwh_day: float, autonomy_days: float, dod: float = 0.5, voltage: float = 12.0) -> dict:
    wh = load_kwh_day * 1000 * autonomy_days / dod
    ah = wh / voltage
    return {"bank_wh": round(wh, 0), "bank_ah": round(ah, 1), "voltage": voltage,
            "depth_of_discharge": dod,
            "note": "Lead-acid: keep DoD <=50%. LiFePO4: 80-90% usable with BMS. Round up."}


def battery_runtime(ah: float, voltage: float, load_w: float, dod: float = 0.5,
                    inverter_eff: float = 0.85) -> dict:
    if load_w <= 0:
        raise ValueError("load must be positive")
    hours = ah * voltage * dod * inverter_eff / load_w
    return {"hours": round(hours, 2), "usable_wh": round(ah * voltage * dod * inverter_eff, 0),
            "note": "Derate for temperature, inverter idle draw and battery age."}


AWG_TABLE = [
    ("4/0", 0.1608, 230), ("3/0", 0.2028, 200), ("2/0", 0.2558, 175), ("1/0", 0.3224, 150),
    ("1", 0.4066, 130), ("2", 0.5127, 115), ("4", 0.8154, 85), ("6", 1.296, 65),
    ("8", 2.061, 50), ("10", 3.277, 35), ("12", 5.211, 25), ("14", 8.286, 20),
    ("16", 13.17, 15), ("18", 20.95, 10),
]


def _awg_index(name: str) -> int:
    for i, (n, _, _) in enumerate(AWG_TABLE):
        if n == name:
            return i
    raise ValueError("unknown AWG")


def wire_size(amps: float, one_way_m: float, voltage: float, max_drop_pct: float = 3.0) -> dict:
    allowed = voltage * max_drop_pct / 100.0
    for name, ohm_per_km, ampacity in AWG_TABLE:
        drop = 2 * amps * one_way_m * ohm_per_km / 1000.0
        if drop <= allowed and amps <= ampacity:
            return {"awg": name, "voltage_drop_v": round(drop, 2),
                    "drop_pct": round(drop / voltage * 100, 2),
                    "ampacity_a": ampacity,
                    "note": "Conservative free-air copper ampacity. Follow local electrical code and fuse every run."}
    raise ValueError("Load exceeds table; parallel conductors or higher voltage required")


# --------------------------------------------------------------------------
# CBRN
# --------------------------------------------------------------------------

def fallout_rate(rate0_usv_h: float, t0_h: float, t_h: float, decay: float = 1.2) -> dict:
    if t0_h <= 0 or t_h <= 0:
        raise ValueError("times must be positive")
    rate = rate0_usv_h * (t_h / t0_h) ** (-decay)
    return {"dose_rate_usv_h": round(rate, 3), "decay_exponent": decay,
            "note": "Way-Wigner approximation. Actual fallout is not a single isotope; measure with an instrument."}


def fallout_dose(rate0_usv_h: float, t0_h: float, t1_h: float, decay: float = 1.2) -> dict:
    if t1_h <= t0_h:
        raise ValueError("end time must exceed start time")
    k = rate0_usv_h * t0_h ** decay
    dose = k * (t1_h ** (1 - decay) - t0_h ** (1 - decay)) / (1 - decay)
    return {"dose_usv": round(dose, 2), "dose_msv": round(dose / 1000, 3),
            "note": "Integrated Way-Wigner dose assuming no sheltering. Shelter reduces the rate by the protection factor."}


def shelter_dose(dose_usv: float, protection_factor: float) -> dict:
    if protection_factor <= 0:
        raise ValueError("protection factor must be positive")
    return {"sheltered_usv": round(dose_usv / protection_factor, 2),
            "protection_factor": protection_factor,
            "note": "Basement/interior protection factors vary widely (often 10-100+). Use measured values."}


def ki_dose(age_years: float) -> dict:
    if age_years < 1 / 12:
        mg = 16
    elif age_years < 3:
        mg = 32
    elif age_years <= 18:
        mg = 65
    else:
        mg = 130
    return {"mg": mg, "repeat": "every 24 hours while exposure continues, only when directed by authorities",
            "note": "FDA age-based KI guidance for radioactive iodine. KI protects the thyroid only and is not a general radiation antidote. "
                    "Adults over 40 generally should not take KI unless instructed by health authorities because the risk of "
                    "thyroid adverse effects rises with age. Do not use in iodine allergy or dermatitis herpetiformis. "
                    "Evacuation, sheltering and avoiding contaminated food/water matter more than tablets."}


def radiation_convert(value: float, unit: str) -> dict:
    units = {"sv": 1.0, "msv": 1e-3, "usv": 1e-6, "rem": 0.01, "mrem": 1e-5, "rad": 0.01, "gy": 1.0}
    u = unit.lower()
    if u not in units:
        raise ValueError("unknown unit")
    sv = value * units[u]
    return {"sv": sv, "msv": sv * 1000, "usv": sv * 1e6, "rem": sv * 100, "mrem": sv * 1000,
            "note": "1 Gy = 1 Sv for gamma/beta (quality factor 1). Neutron and alpha doses need weighting factors."}


def decay_7_10(rate_now_usv_h: float, hours_elapsed: float) -> dict:
    """7-10 rule: at seven times the elapsed time, dose rate is about 1/10.

    ``hours_elapsed`` is time since the detonation, not time since the
    measurement. The forecast is relative (t^-1.2, Way-Wigner), so it does
    not require assuming a dose rate at H+1.
    """
    if hours_elapsed <= 0:
        raise ValueError("hours since detonation must be positive")
    if rate_now_usv_h < 0:
        raise ValueError("dose rate cannot be negative")
    factor = 7.0 ** -1.2
    rate_7x = rate_now_usv_h * factor
    return {"hours_now": round(hours_elapsed, 2),
            "hours_at_7x": round(hours_elapsed * 7, 2),
            "dose_rate_now_usv_h": round(rate_now_usv_h, 3),
            "dose_rate_at_7x_usv_h": round(rate_7x, 4),
            "reduction_factor": round(factor, 4),
            "note": "Relative t^-1.2 decay at the same location, assuming no new fallout. "
                    "The 7-10 rule is a planning approximation; measure with a calibrated instrument."}


# --------------------------------------------------------------------------
# Communications
# --------------------------------------------------------------------------

def radio_horizon_km(h1_m: float, h2_m: float) -> dict:
    # Radio horizon with standard 4/3 Earth refraction: d_km = 4.12(sqrt(h1)+sqrt(h2)).
    km = 4.12 * (math.sqrt(max(0, h1_m)) + math.sqrt(max(0, h2_m)))
    return {"km": round(km, 1), "miles": round(km * 0.621371, 1),
            "note": "4/3 Earth refraction approximation. Terrain, buildings and foliage reduce "
                    "range; the radio horizon is a best case, not a guarantee."}


def dipole_length(freq_mhz: float) -> dict:
    total_ft = 468 / freq_mhz
    return {"total_ft": round(total_ft, 2), "each_leg_ft": round(total_ft / 2, 2),
            "total_m": round(total_ft * 0.3048, 2), "each_leg_m": round(total_ft * 0.3048 / 2, 2),
            "note": "Half-wave dipole in free space; trim for resonance at height."}


def vertical_length(freq_mhz: float) -> dict:
    ft = 234 / freq_mhz
    return {"ft": round(ft, 2), "m": round(ft * 0.3048, 2),
            "radials": "4+ quarter-wave radials at 45 degrees",
            "note": "Quarter-wave vertical; ground loss dominates performance."}


def loop_length(freq_mhz: float) -> dict:
    ft = 1005 / freq_mhz
    return {"circumference_ft": round(ft, 2), "circumference_m": round(ft * 0.3048, 2),
            "note": "Full-wave loop; feed with a 1:1 or 4:1 balun as needed."}


def free_space_loss_db(freq_mhz: float, km: float) -> float:
    if freq_mhz <= 0 or km <= 0:
        raise ValueError("frequency and distance must be positive")
    return round(32.44 + 20 * math.log10(freq_mhz) + 20 * math.log10(km), 1)


def link_budget(tx_w: float, tx_gain_dbi: float, rx_gain_dbi: float, freq_mhz: float,
                km: float, rx_sensitivity_dbm: float = -120, cable_loss_db: float = 2.0) -> dict:
    if tx_w <= 0:
        raise ValueError("transmit power must be positive")
    tx_dbm = 10 * math.log10(tx_w * 1000)
    fspl = free_space_loss_db(freq_mhz, km)
    received = tx_dbm + tx_gain_dbi + rx_gain_dbi - cable_loss_db - fspl
    return {"tx_dbm": round(tx_dbm, 1), "path_loss_db": fspl,
            "received_dbm": round(received, 1), "margin_db": round(received - rx_sensitivity_dbm, 1),
            "viable": received >= rx_sensitivity_dbm,
            "note": "Free-space estimate. Add 10-30 dB for terrain/foliage/urban clutter."}


MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
    "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
    "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
    "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..", "0": "-----",
    "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....", "6": "-....",
    "7": "--...", "8": "---..", "9": "----.", ".": ".-.-.-", ",": "--..--", "?": "..--..",
    "/": "-..-.", "-": "-....-", "(": "-.--.", ")": "-.--.-", ":": "---...", "'": ".----.",
    "=": "-...-", "+": ".-.-.", "@": ".--.-.", "!": "-.-.--",
}


def morse(text: str) -> dict:
    parts = []
    for ch in text.upper():
        if ch == " ":
            parts.append("/")
        elif ch in MORSE:
            parts.append(MORSE[ch])
        else:
            parts.append("?")
    return {"morse": " ".join(parts), "note": "Use the trainer to practise at 5-20 WPM."}


PHONETIC = {
    "A": "ALFA", "B": "BRAVO", "C": "CHARLIE", "D": "DELTA", "E": "ECHO", "F": "FOXTROT",
    "G": "GOLF", "H": "HOTEL", "I": "INDIA", "J": "JULIETT", "K": "KILO", "L": "LIMA",
    "M": "MIKE", "N": "NOVEMBER", "O": "OSCAR", "P": "PAPA", "Q": "QUEBEC", "R": "ROMEO",
    "S": "SIERRA", "T": "TANGO", "U": "UNIFORM", "V": "VICTOR", "W": "WHISKEY",
    "X": "XRAY", "Y": "YANKEE", "Z": "ZULU", "0": "ZERO", "1": "ONE", "2": "TWO",
    "3": "THREE", "4": "FOUR", "5": "FIVE", "6": "SIX", "7": "SEVEN", "8": "EIGHT",
    "9": "NINER",
}


def phonetic(text: str) -> str:
    return " ".join(PHONETIC.get(c, c) for c in text.upper() if not c.isspace())


def hf_outlook(sfi: float, kp: float, sun_altitude_deg: float) -> dict:
    day = sun_altitude_deg > 0
    if sfi >= 150:
        day_bands = ["10m", "12m", "15m", "17m", "20m"]
        night_bands = ["20m", "30m", "40m", "80m"]
    elif sfi >= 110:
        day_bands = ["15m", "17m", "20m", "30m"]
        night_bands = ["30m", "40m", "80m"]
    elif sfi >= 90:
        day_bands = ["17m", "20m", "30m"]
        night_bands = ["40m", "80m", "160m"]
    else:
        day_bands = ["20m", "30m", "40m"]
        night_bands = ["40m", "80m", "160m"]
    bands = [f"{b} {'DAY' if day else 'NIGHT'}" for b in (day_bands if day else night_bands)]
    quality = "GOOD" if sfi >= 120 and kp <= 3 else "FAIR" if sfi >= 90 and kp <= 4 else "POOR"
    return {
        "daytime": day, "sfi": sfi, "kp": kp, "quality": quality,
        "bands": bands,
        "note": "Planning heuristic only. Use beacons, WSPR, or an actual propagation program; "
                "solar and geomagnetic conditions change hourly. Kp >=5 degrades polar and high-latitude paths.",
    }


def path_heading(lat1: float, lon1: float, lat2: float, lon2: float) -> dict:
    from .geo import haversine, initial_bearing
    return {"distance_km": round(haversine(lat1, lon1, lat2, lon2) / 1000, 1),
            "bearing_deg": round(initial_bearing(lat1, lon1, lat2, lon2), 1),
            "short_path": True}


# --------------------------------------------------------------------------
# Misc field math
# --------------------------------------------------------------------------

def pace_count(distance_m: float, paces_per_100m: float) -> dict:
    paces = distance_m / 100 * paces_per_100m
    return {"paces": round(paces, 0), "paces_per_100m": paces_per_100m,
            "note": "Calibrate on flat ground and again on slope; pace length changes with terrain and load."}


def dead_reckoning(lat: float, lon: float, bearing_deg: float, distance_m: float) -> dict:
    from .geo import destination
    out = destination(lat, lon, bearing_deg, distance_m)
    return {"lat": round(out[0], 6), "lon": round(out[1], 6), "bearing_deg": bearing_deg,
            "distance_m": distance_m, "note": "Assumes constant bearing (great-circle leg)."}


def triangulate(p1: tuple[float, float], b1: float, p2: tuple[float, float], b2: float) -> dict:
    from .geo import resection
    r = resection(p1, b1, p2, b2)
    return {**r, "note": "Two-bearing resection; error grows when the angle between bearings is small."}


def slope_distance(horizontal_m: float, slope_deg: float) -> dict:
    return {"slope_distance_m": round(horizontal_m / math.cos(math.radians(slope_deg)), 2),
            "vertical_m": round(horizontal_m * math.tan(math.radians(slope_deg)), 2)}
