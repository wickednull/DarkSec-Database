"""Static calculator catalog and dispatch table.

The catalog drives the UI form fields; CALC_FUNCS maps each name to the pure
implementation in :mod:`darksec.calcs`.
"""
from __future__ import annotations

from typing import Any

from . import calcs


# Calculator catalog
# --------------------------------------------------------------------------

def _f(name: str, label: str, kind: str = "number", default: Any = None, unit: str = "",
       options: list | None = None, minimum: float | None = None, maximum: float | None = None) -> dict:
    out = {"name": name, "label": label, "type": kind, "unit": unit}
    if default is not None:
        out["default"] = default
    if options:
        out["options"] = options
    if minimum is not None:
        out["min"] = minimum
    if maximum is not None:
        out["max"] = maximum
    return out


CALC_CATALOG = [
    # Weather
    {"name": "wind_chill_c", "group": "WEATHER", "label": "Wind chill", "fields": [
        _f("temp_c", "Air temperature", "number", 0, "°C"),
        _f("wind_kmh", "Wind speed", "number", 20, "km/h")]},
    {"name": "heat_index_c", "group": "WEATHER", "label": "Heat index", "fields": [
        _f("temp_c", "Air temperature", "number", 32, "°C"),
        _f("rh", "Relative humidity", "number", 60, "%", minimum=1, maximum=100)]},
    {"name": "dew_point_c", "group": "WEATHER", "label": "Dew point", "fields": [
        _f("temp_c", "Air temperature", "number", 20, "°C"),
        _f("rh", "Relative humidity", "number", 60, "%", minimum=1, maximum=100)]},
    {"name": "wet_bulb_c", "group": "WEATHER", "label": "Wet-bulb temperature", "fields": [
        _f("temp_c", "Air temperature", "number", 30, "°C"),
        _f("rh", "Relative humidity", "number", 50, "%", minimum=1, maximum=100)]},
    {"name": "humidex", "group": "WEATHER", "label": "Humidex", "fields": [
        _f("temp_c", "Air temperature", "number", 28, "°C"),
        _f("dew_c", "Dew point", "number", 20, "°C")]},
    {"name": "pressure_altitude_ft", "group": "WEATHER", "label": "Pressure altitude", "fields": [
        _f("pressure_hpa", "Station pressure", "number", 1013.25, "hPa")]},
    {"name": "density_altitude_ft", "group": "WEATHER", "label": "Density altitude", "fields": [
        _f("pressure_alt_ft", "Pressure altitude", "number", 0, "ft"),
        _f("temp_c", "Outside air temperature", "number", 15, "°C")]},
    {"name": "lightning_distance_km", "group": "WEATHER", "label": "Lightning distance", "fields": [
        _f("seconds_to_thunder", "Seconds from flash to thunder", "number", 10, "s", minimum=0)]},
    {"name": "beaufort", "group": "WEATHER", "label": "Beaufort scale", "fields": [
        _f("kmh", "Wind speed", "number", 30, "km/h")]},
    {"name": "pressure_trend", "group": "WEATHER", "label": "3-hour pressure tendency", "fields": [
        _f("hpa_now", "Pressure now", "number", 1013, "hPa"),
        _f("hpa_3h_ago", "Pressure 3 h ago", "number", 1015, "hPa")]},
    {"name": "solar_insolation", "group": "WEATHER", "label": "Clear-sky solar insolation", "fields": [
        _f("lat", "Latitude", "number", 40, "°", minimum=-89, maximum=89),
        _f("day_of_year", "Day of year (blank = today)", "number", None),
        _f("tilt", "Array tilt (blank = latitude)", "number", None, "°"),
        _f("clear_sky", "Clear-sky factor", "number", 0.75)]},
    # Medical
    {"name": "bmi", "group": "MEDICAL", "label": "Body mass index", "fields": [
        _f("weight_kg", "Weight", "number", 75, "kg", minimum=1),
        _f("height_cm", "Height", "number", 175, "cm", minimum=30)]},
    {"name": "bsa_mosteller", "group": "MEDICAL", "label": "Body surface area (Mosteller)", "fields": [
        _f("weight_kg", "Weight", "number", 75, "kg", minimum=1),
        _f("height_cm", "Height", "number", 175, "cm", minimum=30)]},
    {"name": "ibw_devine", "group": "MEDICAL", "label": "Ideal body weight (Devine)", "fields": [
        _f("sex", "Sex", "select", "male", options=["male", "female"]),
        _f("height_cm", "Height", "number", 175, "cm", minimum=120)]},
    {"name": "mean_arterial_pressure", "group": "MEDICAL", "label": "Mean arterial pressure", "fields": [
        _f("sys", "Systolic", "number", 120, "mmHg", minimum=40, maximum=300),
        _f("dia", "Diastolic", "number", 80, "mmHg", minimum=20, maximum=200)]},
    {"name": "shock_index", "group": "MEDICAL", "label": "Shock index", "fields": [
        _f("hr", "Heart rate", "number", 80, "bpm", minimum=20, maximum=300),
        _f("sys", "Systolic", "number", 120, "mmHg", minimum=40, maximum=300)]},
    {"name": "qsofa", "group": "MEDICAL", "label": "qSOFA screen", "fields": [
        _f("rr", "Respiratory rate", "number", 16, "/min", minimum=1, maximum=80),
        _f("sys", "Systolic", "number", 120, "mmHg", minimum=40, maximum=300),
        _f("altered_mental_status", "Altered mental status", "boolean", False)]},
    {"name": "gcs", "group": "MEDICAL", "label": "Glasgow Coma Scale", "fields": [
        _f("eye", "Eye (1-4)", "number", 4, minimum=1, maximum=4),
        _f("verbal", "Verbal (1-5)", "number", 5, minimum=1, maximum=5),
        _f("motor", "Motor (1-6)", "number", 6, minimum=1, maximum=6)]},
    {"name": "news2", "group": "MEDICAL", "label": "NEWS2", "fields": [
        _f("rr", "Respiratory rate", "number", 16, "/min"),
        _f("spo2", "SpO₂", "number", 98, "%"),
        _f("sys", "Systolic", "number", 120, "mmHg"),
        _f("hr", "Heart rate", "number", 80, "bpm"),
        _f("temp_c", "Temperature", "number", 37, "°C"),
        _f("avpu", "AVPU", "select", "A", options=["A", "V", "P", "U"]),
        _f("supplemental_o2", "Supplemental oxygen", "boolean", False),
        _f("spo2_scale1", "SpO₂ scale 1", "boolean", True)]},
    {"name": "iv_drip_rate", "group": "MEDICAL", "label": "IV drip rate", "fields": [
        _f("volume_ml", "Volume", "number", 500, "mL", minimum=1),
        _f("minutes", "Time", "number", 60, "min", minimum=1),
        _f("drop_factor", "Drop factor", "number", 20, "gtt/mL", minimum=1)]},
    {"name": "maintenance_fluid_ml_hr", "group": "MEDICAL", "label": "Maintenance fluids (4-2-1)", "fields": [
        _f("weight_kg", "Weight", "number", 70, "kg", minimum=1)]},
    {"name": "parkland_burn", "group": "MEDICAL", "label": "Parkland burn estimate", "fields": [
        _f("weight_kg", "Weight", "number", 70, "kg", minimum=1),
        _f("tbsa_pct", "TBSA burned", "number", 20, "%", minimum=1, maximum=100)]},
    {"name": "rule_of_nines", "group": "MEDICAL", "label": "Rule of nines", "fields": [
        _f("age_years", "Age", "number", 30, "years", minimum=0, maximum=120)]},
    {"name": "pediatric_weight_estimate", "group": "MEDICAL", "label": "Paediatric weight estimate", "fields": [
        _f("age_years", "Age", "number", 5, "years", minimum=0.1, maximum=18)]},
    {"name": "epinephrine_anaphylaxis", "group": "MEDICAL", "label": "Epinephrine (anaphylaxis) reference", "fields": [
        _f("weight_kg", "Weight", "number", 70, "kg", minimum=1)]},
    {"name": "naloxone_dose", "group": "MEDICAL", "label": "Naloxone reference", "fields": []},
    # Water / food
    {"name": "water_requirement", "group": "WATER", "label": "Water requirement", "fields": [
        _f("people", "People", "number", 2, minimum=1),
        _f("days", "Days", "number", 3, minimum=1),
        _f("climate", "Climate", "select", "temperate",
           options=["temperate", "hot", "desert", "cold", "high_altitude"])]},
    {"name": "bleach_dose", "group": "WATER", "label": "Bleach disinfection", "fields": [
        _f("liters", "Water volume", "number", 1, "L", minimum=0.1),
        _f("bleach_pct", "Sodium hypochlorite", "number", 6, "%", minimum=0.1, maximum=12),
        _f("cloudy", "Cloudy water", "boolean", False)]},
    {"name": "boil_water", "group": "WATER", "label": "Boil water time", "fields": [
        _f("minutes_at_altitude_m", "Elevation", "number", 0, "m", minimum=0)]},
    {"name": "ors_recipe", "group": "WATER", "label": "ORS recipe", "fields": [
        _f("liters", "Volume", "number", 1, "L", minimum=0.1)]},
    {"name": "ration_days", "group": "FOOD", "label": "Ration days", "fields": [
        _f("calories", "Total calories available", "number", 20000, "kcal", minimum=1),
        _f("people", "People", "number", 2, minimum=1),
        _f("kcal_per_person_day", "Calories per person/day", "number", 2000, "kcal", minimum=500)]},
    # Power
    {"name": "solar_array_size", "group": "POWER", "label": "Solar array size", "fields": [
        _f("load_kwh_day", "Daily load", "number", 3, "kWh", minimum=0.01),
        _f("peak_sun_hours", "Peak sun hours", "number", 4, "h", minimum=0.1),
        _f("derate", "System derate", "number", 0.75)]},
    {"name": "battery_bank", "group": "POWER", "label": "Battery bank size", "fields": [
        _f("load_kwh_day", "Daily load", "number", 3, "kWh", minimum=0.01),
        _f("autonomy_days", "Autonomy", "number", 2, "days", minimum=0.5),
        _f("dod", "Depth of discharge", "number", 0.5),
        _f("voltage", "Bank voltage", "number", 12, "V")]},
    {"name": "battery_runtime", "group": "POWER", "label": "Battery runtime", "fields": [
        _f("ah", "Battery capacity", "number", 100, "Ah", minimum=1),
        _f("voltage", "Voltage", "number", 12, "V", minimum=1),
        _f("load_w", "Load", "number", 100, "W", minimum=1),
        _f("dod", "Usable depth of discharge", "number", 0.5),
        _f("inverter_eff", "Inverter efficiency", "number", 0.85)]},
    {"name": "wire_size", "group": "POWER", "label": "Copper wire size", "fields": [
        _f("amps", "Current", "number", 20, "A", minimum=0.1),
        _f("one_way_m", "One-way run", "number", 5, "m", minimum=0.1),
        _f("voltage", "System voltage", "number", 12, "V", minimum=1),
        _f("max_drop_pct", "Maximum voltage drop", "number", 3, "%")]},
    # CBRN
    {"name": "fallout_rate", "group": "CBRN", "label": "Fallout dose rate", "fields": [
        _f("rate0_usv_h", "Reference dose rate", "number", 1000, "µSv/h"),
        _f("t0_h", "Reference time H+", "number", 1, "h", minimum=0.1),
        _f("t_h", "Time of interest H+", "number", 7, "h", minimum=0.1),
        _f("decay", "Decay exponent", "number", 1.2)]},
    {"name": "fallout_dose", "group": "CBRN", "label": "Accumulated fallout dose", "fields": [
        _f("rate0_usv_h", "Reference dose rate", "number", 1000, "µSv/h"),
        _f("t0_h", "Start H+", "number", 1, "h", minimum=0.1),
        _f("t1_h", "End H+", "number", 24, "h", minimum=0.1),
        _f("decay", "Decay exponent", "number", 1.2)]},
    {"name": "shelter_dose", "group": "CBRN", "label": "Sheltered dose", "fields": [
        _f("dose_usv", "Unshielded dose", "number", 10000, "µSv"),
        _f("protection_factor", "Protection factor", "number", 20, minimum=0.1)]},
    {"name": "ki_dose", "group": "CBRN", "label": "KI thyroid blocking (reference)", "fields": [
        _f("age_years", "Age", "number", 30, "years", minimum=0)]},
    {"name": "radiation_convert", "group": "CBRN", "label": "Radiation unit conversion", "fields": [
        _f("value", "Value", "number", 1),
        _f("unit", "Unit", "select", "usv", options=["sv", "msv", "usv", "rem", "mrem", "rad", "gy"])]},
    {"name": "decay_7_10", "group": "CBRN", "label": "7-10 decay rule", "fields": [
        _f("rate_now_usv_h", "Dose rate now", "number", 1000, "µSv/h"),
        _f("hours_elapsed", "Hours since detonation", "number", 7, "h", minimum=0.1)]},
    # Comms
    {"name": "radio_horizon_km", "group": "COMMS", "label": "Radio horizon", "fields": [
        _f("h1_m", "Antenna 1 height", "number", 2, "m", minimum=0),
        _f("h2_m", "Antenna 2 height", "number", 2, "m", minimum=0)]},
    {"name": "dipole_length", "group": "COMMS", "label": "Half-wave dipole", "fields": [
        _f("freq_mhz", "Frequency", "number", 146.52, "MHz", minimum=0.1)]},
    {"name": "vertical_length", "group": "COMMS", "label": "Quarter-wave vertical", "fields": [
        _f("freq_mhz", "Frequency", "number", 146.52, "MHz", minimum=0.1)]},
    {"name": "loop_length", "group": "COMMS", "label": "Full-wave loop", "fields": [
        _f("freq_mhz", "Frequency", "number", 14.2, "MHz", minimum=0.1)]},
    {"name": "free_space_loss_db", "group": "COMMS", "label": "Free-space path loss", "fields": [
        _f("freq_mhz", "Frequency", "number", 146.52, "MHz", minimum=0.1),
        _f("km", "Distance", "number", 10, "km", minimum=0.01)]},
    {"name": "link_budget", "group": "COMMS", "label": "Link budget", "fields": [
        _f("tx_w", "Transmit power", "number", 5, "W", minimum=0.001),
        _f("tx_gain_dbi", "TX antenna gain", "number", 2, "dBi"),
        _f("rx_gain_dbi", "RX antenna gain", "number", 2, "dBi"),
        _f("freq_mhz", "Frequency", "number", 146.52, "MHz", minimum=0.1),
        _f("km", "Distance", "number", 10, "km", minimum=0.01),
        _f("rx_sensitivity_dbm", "Receiver sensitivity", "number", -120, "dBm"),
        _f("cable_loss_db", "Cable/connector loss", "number", 2, "dB")]},
    {"name": "morse", "group": "COMMS", "label": "Morse encode", "fields": [
        _f("text", "Text", "text", "SOS")]},
    {"name": "phonetic", "group": "COMMS", "label": "Phonetic alphabet", "fields": [
        _f("text", "Text", "text", "NIGHTWATCH")]},
    {"name": "hf_outlook", "group": "COMMS", "label": "HF band outlook (heuristic)", "fields": [
        _f("sfi", "Solar flux index", "number", 120),
        _f("kp", "Kp index", "number", 2),
        _f("sun_altitude_deg", "Sun altitude", "number", 30, "°")]},
    {"name": "path_heading", "group": "COMMS", "label": "Path distance / bearing", "fields": [
        _f("lat1", "From latitude", "number", 40, "°"),
        _f("lon1", "From longitude", "number", -100, "°"),
        _f("lat2", "To latitude", "number", 38, "°"),
        _f("lon2", "To longitude", "number", -90, "°")]},
    # Nav / misc
    {"name": "pace_count", "group": "NAV", "label": "Pace count", "fields": [
        _f("distance_m", "Distance", "number", 1000, "m", minimum=0),
        _f("paces_per_100m", "Paces per 100 m", "number", 62, minimum=1)]},
    {"name": "dead_reckoning", "group": "NAV", "label": "Dead reckoning", "fields": [
        _f("lat", "Latitude", "number", 40, "°"),
        _f("lon", "Longitude", "number", -100, "°"),
        _f("bearing_deg", "Bearing", "number", 90, "°"),
        _f("distance_m", "Distance", "number", 1000, "m", minimum=0)]},
    {"name": "slope_distance", "group": "NAV", "label": "Slope distance", "fields": [
        _f("horizontal_m", "Horizontal distance", "number", 100, "m", minimum=0),
        _f("slope_deg", "Slope angle", "number", 15, "°", minimum=0, maximum=89)]},
]

CALC_FUNCS = {
    name: getattr(calcs, name) for name in [
        "wind_chill_c", "heat_index_c", "dew_point_c", "wet_bulb_c", "humidex",
        "pressure_altitude_ft", "density_altitude_ft", "lightning_distance_km", "beaufort",
        "pressure_trend", "solar_insolation", "bmi", "bsa_mosteller", "ibw_devine",
        "mean_arterial_pressure", "shock_index", "qsofa", "gcs", "news2", "iv_drip_rate",
        "maintenance_fluid_ml_hr", "parkland_burn", "rule_of_nines",
        "pediatric_weight_estimate", "epinephrine_anaphylaxis", "naloxone_dose",
        "water_requirement", "bleach_dose", "boil_water", "ors_recipe", "ration_days",
        "solar_array_size", "battery_bank", "battery_runtime", "wire_size", "fallout_rate",
        "fallout_dose", "shelter_dose", "ki_dose", "radiation_convert", "decay_7_10",
        "radio_horizon_km", "dipole_length", "vertical_length", "loop_length",
        "free_space_loss_db", "link_budget", "morse", "phonetic", "hf_outlook",
        "path_heading", "pace_count", "dead_reckoning", "slope_distance",
    ]
}
