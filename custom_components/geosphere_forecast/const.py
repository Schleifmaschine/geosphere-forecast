"""Konstanten für die GeoSphere Forecast Integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "geosphere_forecast"
ATTRIBUTION = "Daten: GeoSphere Austria (CC BY 4.0)"

API_BASE = "https://dataset.api.hub.geosphere.at/v1/timeseries"
WARNINGS_URL = "https://warnungen.zamg.at/wsapp/api/getWarningsForCoords"

# Datensätze
NWP_RESOURCE = "nwp-v2-1h-1km"
NOWCAST_RESOURCE = "nowcast-v1-15min-1km"
CHEM_RESOURCE = "chem-v2-1h-3km"
AQI_RESOURCE = "chem_aqi-v1-1d-3km"
DUST_RESOURCE = "chem_dust-v1-1h-0p2deg"
ENSEMBLE_RESOURCE = "ensemble-v2-1h-1km"
INCA_RESOURCE = "inca-v1-1h-1km"  # Analyse (historical), ca. 1 h verzögert

NWP_PARAMS = [
    "2t", "2r", "10u", "10v", "10fg", "msl", "tcc", "tp", "rain", "sf",
    "sy", "sund", "ssrd", "snowlmt", "cape",
]
NOWCAST_PARAMS = ["t2m", "td", "rh2m", "ff", "fx", "dd", "rr", "pt"]
CHEM_PARAMS = ["no2surf", "o3surf", "pm10surf", "pm25surf"]
ENSEMBLE_PARAMS = ["2t_p10", "2t_p90", "tp_p10", "tp_p50", "tp_p90", "10fg_p90"]
INCA_PARAMS = ["RR", "T2M", "GL"]
INCA_HOURS = 24
# Schwelle für "es regnet" bei der Wahrscheinlichkeitsschätzung (mm/h)
PRECIP_THRESHOLD = 0.1

# Abdeckung (lat_min, lon_min, lat_max, lon_max) laut /metadata
NWP_BBOX = (43.002, 5.0317, 51.498, 22.568)
NOWCAST_BBOX = (45.503, 8.098, 49.478, 17.742)

CONF_NOWCAST = "nowcast"
CONF_AIR_QUALITY = "air_quality"
CONF_WARNINGS = "warnings"
CONF_DUST = "dust"
CONF_ENSEMBLE = "ensemble"
CONF_INCA = "inca"

UPDATE_INTERVAL = timedelta(minutes=15)
# NWP/Chemie werden nur alle 3 h bzw. 1x täglich neu gerechnet
SLOW_REFRESH = timedelta(minutes=60)

# GeoSphere Wettersymbol (sy, 1–32) -> HA condition
SYMBOL_CONDITION: dict[int, str] = {
    1: "sunny", 2: "partlycloudy", 3: "partlycloudy", 4: "cloudy", 5: "cloudy",
    6: "fog", 7: "fog",
    8: "rainy", 9: "rainy", 10: "pouring",
    11: "snowy-rainy", 12: "snowy-rainy", 13: "snowy-rainy",
    14: "snowy", 15: "snowy", 16: "snowy",
    17: "rainy", 18: "rainy", 19: "pouring",
    20: "snowy-rainy", 21: "snowy-rainy", 22: "snowy-rainy",
    23: "snowy", 24: "snowy", 25: "snowy",
    26: "lightning-rainy", 27: "lightning-rainy", 28: "lightning-rainy",
    29: "lightning-rainy", 30: "lightning-rainy", 31: "lightning-rainy",
    32: "lightning-rainy",
}

SYMBOL_TEXT: dict[int, str] = {
    1: "sonnig", 2: "heiter", 3: "wolkig", 4: "stark bewölkt", 5: "bedeckt",
    6: "Nebel", 7: "Hochnebel",
    8: "leichter Regen", 9: "Regen", 10: "starker Regen",
    11: "leichter Schneeregen", 12: "Schneeregen", 13: "starker Schneeregen",
    14: "leichter Schneefall", 15: "Schneefall", 16: "starker Schneefall",
    17: "Regenschauer", 18: "Regenschauer", 19: "starke Regenschauer",
    20: "Schneeregenschauer", 21: "Schneeregenschauer", 22: "starke Schneeregenschauer",
    23: "Schneeschauer", 24: "Schneeschauer", 25: "starke Schneeschauer",
    26: "Gewitter", 27: "Gewitter mit Regen", 28: "Gewitter mit starkem Regen",
    29: "Gewitter mit Schneeregen", 30: "Gewitter mit Schnee",
    31: "Gewitter mit Hagel", 32: "schweres Gewitter",
}

# Für die Tagesvorhersage gewinnt die "ungünstigste" Bedingung
CONDITION_SEVERITY = [
    "sunny", "partlycloudy", "cloudy", "fog", "rainy",
    "snowy-rainy", "snowy", "pouring", "lightning-rainy",
]

WARNING_TYPES: dict[int, str] = {
    1: "Sturm", 2: "Regen", 3: "Schnee", 4: "Glatteis",
    5: "Gewitter", 6: "Hitze", 7: "Kälte",
}
WARNING_LEVELS: dict[int, str] = {0: "keine", 1: "gelb", 2: "orange", 3: "rot"}
