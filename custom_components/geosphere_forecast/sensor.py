"""Sensoren: aktuelle Werte, Tageswerte, Luftqualität und Warnungen."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    DEGREE,
    PERCENTAGE,
    UnitOfIrradiance,
    UnitOfLength,
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import SYMBOL_TEXT, WARNING_LEVELS, WARNING_TYPES
from .coordinator import GeoSphereConfigEntry, GeoSphereCoordinator, GeoSphereData, _sum
from .entity import GeoSphereEntity

try:  # HA >= 2026.x
    from homeassistant.const import UnitOfDensity

    MICROGRAMS_PER_M3 = UnitOfDensity.MICROGRAMS_PER_CUBIC_METER
except ImportError:  # ältere Versionen
    from homeassistant.const import CONCENTRATION_MICROGRAMS_PER_CUBIC_METER as MICROGRAMS_PER_M3

Data = GeoSphereData


@dataclass(frozen=True, kw_only=True)
class GeoSphereSensorDescription(SensorEntityDescription):
    """Sensorbeschreibung mit Wert-, Attribut- und Verfügbarkeitsfunktion."""

    value_fn: Callable[[Data], Any]
    # Liefert eine Zeitreihe [{datetime, value}] als Attribut "forecast"
    forecast_fn: Callable[[Data], list[dict[str, Any]]] | None = None
    attr_fn: Callable[[Data], dict[str, Any]] | None = None
    enabled_fn: Callable[[GeoSphereCoordinator], bool] = lambda c: True


def _hourly(key: str) -> Callable[[Data], list[dict[str, Any]]]:
    return lambda d: [
        {"datetime": h["datetime"], "value": h[key]} for h in d.hourly if h.get(key) is not None
    ]


def _daily(day: int, key: str) -> Callable[[Data], Any]:
    return lambda d: d.daily[day].get(key) if len(d.daily) > day else None


def _hourly_sum(key: str, hours: int) -> Callable[[Data], Any]:
    return lambda d: _sum(h[key] for h in d.hourly[:hours])


def _rest_of_today(key: str) -> Callable[[Data], Any]:
    def fn(d: Data) -> Any:
        today = dt_util.now().date()
        return _sum(
            h[key]
            for h in d.hourly
            if dt_util.as_local(dt_util.parse_datetime(h["datetime"])).date() == today
        )

    return fn


def _rest_of_today_max(key: str) -> Callable[[Data], Any]:
    def fn(d: Data) -> Any:
        today = dt_util.now().date()
        vals = [
            h[key]
            for h in d.hourly
            if h.get(key) is not None
            and dt_util.as_local(dt_util.parse_datetime(h["datetime"])).date() == today
        ]
        return max(vals) if vals else None

    return fn


def _daily_range(day: int, lo: str, hi: str) -> Callable[[Data], dict[str, Any]]:
    """Ensemble-Bandbreite eines Tageswerts als Attribute."""

    def fn(d: Data) -> dict[str, Any]:
        if len(d.daily) <= day or d.daily[day].get(lo) is None:
            return {}
        return {"p10": d.daily[day][lo], "p90": d.daily[day][hi]}

    return fn


def _inca_latest(param: str) -> Callable[[Data], Any]:
    def fn(d: Data) -> Any:
        if d.inca is None or not d.inca.timestamps:
            return None
        return d.inca.get(param, len(d.inca.timestamps) - 1)

    return fn


def _inca_sum(param: str, hours: int) -> Callable[[Data], Any]:
    def fn(d: Data) -> Any:
        if d.inca is None or not d.inca.timestamps:
            return None
        n = len(d.inca.timestamps)
        return _sum(d.inca.get(param, i) for i in range(max(0, n - hours), n))

    return fn


def _inca_attrs(d: Data) -> dict[str, Any]:
    if d.inca is None or not d.inca.timestamps:
        return {}
    return {"measured_at": d.inca.timestamps[-1].isoformat()}


def _chem_now(param: str) -> Callable[[Data], Any]:
    def fn(d: Data) -> Any:
        if d.chem is None:
            return None
        return d.chem.get(param, d.chem.index_at(dt_util.utcnow()))

    return fn


def _chem_forecast(param: str) -> Callable[[Data], list[dict[str, Any]]]:
    def fn(d: Data) -> list[dict[str, Any]]:
        if d.chem is None:
            return []
        start = d.chem.index_at(dt_util.utcnow())
        return [
            {"datetime": ts.isoformat(), "value": d.chem.get(param, i)}
            for i, ts in enumerate(d.chem.timestamps)
            if i >= start
        ]

    return fn


def _aqi(day: int) -> Callable[[Data], Any]:
    def fn(d: Data) -> Any:
        if d.aqi is None:
            return None
        today = dt_util.utcnow().date()
        for i, ts in enumerate(d.aqi.timestamps):
            if (ts.date() - today).days == day:
                value = d.aqi.get("aqi", i)
                return int(value) if value is not None else None
        return None

    return fn


def _dust_now(d: Data) -> Any:
    if d.dust is None:
        return None
    return d.dust.get("dust", d.dust.index_at(dt_util.utcnow()))


def _dust_upcoming(d: Data, hours: int | None = None) -> list[tuple[Any, float]]:
    if d.dust is None:
        return []
    start = d.dust.index_at(dt_util.utcnow())
    end = len(d.dust.timestamps) if hours is None else start + hours
    return [
        (d.dust.timestamps[i], v)
        for i in range(start, min(end, len(d.dust.timestamps)))
        if (v := d.dust.get("dust", i)) is not None
    ]


def _dust_max(hours: int | None) -> Callable[[Data], Any]:
    def fn(d: Data) -> Any:
        values = _dust_upcoming(d, hours)
        return max(v for _, v in values) if values else None

    return fn


def _dust_peak_attrs(d: Data) -> dict[str, Any]:
    values = _dust_upcoming(d)
    if not values:
        return {}
    ts, _ = max(values, key=lambda x: x[1])
    return {"peak_time": ts.isoformat(), "model_run": d.dust.reference_time.isoformat() if d.dust.reference_time else None}


def _dust_forecast(d: Data) -> list[dict[str, Any]]:
    return [{"datetime": ts.isoformat(), "value": v} for ts, v in _dust_upcoming(d)]


def _warn_level(d: Data) -> int | None:
    if d.warnings is None:
        return None
    return max((w["level"] for w in d.warnings), default=0)


def _warn_attrs(d: Data) -> dict[str, Any]:
    warnings = sorted(d.warnings or [], key=lambda w: -w["level"])
    return {
        "level_name": WARNING_LEVELS.get(_warn_level(d) or 0),
        "count": len(warnings),
        "warnings": [
            {
                "type": WARNING_TYPES.get(w["type_id"], w["type_id"]),
                "level": w["level"],
                "level_name": WARNING_LEVELS.get(w["level"]),
                "start": w["start"].isoformat() if w["start"] else None,
                "end": w["end"].isoformat() if w["end"] else None,
                "text": w["text"],
                "effects": w["effects"],
                "recommendations": w["recommendations"],
            }
            for w in warnings
        ],
    }


def _symbol_text(d: Data) -> str | None:
    sy = d.current.get("symbol")
    return SYMBOL_TEXT.get(int(sy)) if sy is not None else None


def _nowcast(c: GeoSphereCoordinator) -> bool:
    return c.use_nowcast


def _air(c: GeoSphereCoordinator) -> bool:
    return c.use_air_quality


def _dust(c: GeoSphereCoordinator) -> bool:
    return c.use_dust


def _ens(c: GeoSphereCoordinator) -> bool:
    return c.use_ensemble


def _inca(c: GeoSphereCoordinator) -> bool:
    return c.use_inca


def _warn(c: GeoSphereCoordinator) -> bool:
    return c.use_warnings


TEMP = {
    "device_class": SensorDeviceClass.TEMPERATURE,
    "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
}
PRECIP = {
    "device_class": SensorDeviceClass.PRECIPITATION,
    "native_unit_of_measurement": UnitOfPrecipitationDepth.MILLIMETERS,
}
WIND = {
    "device_class": SensorDeviceClass.WIND_SPEED,
    "native_unit_of_measurement": UnitOfSpeed.METERS_PER_SECOND,
    "suggested_unit_of_measurement": UnitOfSpeed.KILOMETERS_PER_HOUR,
    "state_class": SensorStateClass.MEASUREMENT,
}
DUST_UNIT = "mg/m²"
SUN = {
    "device_class": SensorDeviceClass.DURATION,
    "native_unit_of_measurement": UnitOfTime.HOURS,
    "icon": "mdi:weather-sunny",
}

SENSORS: tuple[GeoSphereSensorDescription, ...] = (
    # --- aktuelle Werte (Nowcast bzw. NWP) ---
    GeoSphereSensorDescription(
        key="temperature", translation_key="temperature", **TEMP,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.current.get("temperature"),
        forecast_fn=_hourly("native_temperature"),
    ),
    GeoSphereSensorDescription(
        key="dew_point", translation_key="dew_point", **TEMP,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.current.get("dew_point"), enabled_fn=_nowcast,
    ),
    GeoSphereSensorDescription(
        key="humidity", translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY, native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.current.get("humidity"), forecast_fn=_hourly("humidity"),
    ),
    GeoSphereSensorDescription(
        key="wind_speed", translation_key="wind_speed", **WIND,
        value_fn=lambda d: d.current.get("wind_speed"),
        forecast_fn=_hourly("native_wind_speed"),
    ),
    GeoSphereSensorDescription(
        key="wind_gust", translation_key="wind_gust", **WIND,
        value_fn=lambda d: d.current.get("wind_gust"),
        forecast_fn=_hourly("native_wind_gust_speed"),
    ),
    GeoSphereSensorDescription(
        key="wind_bearing", translation_key="wind_bearing",
        native_unit_of_measurement=DEGREE, icon="mdi:compass-outline",
        value_fn=lambda d: d.current.get("wind_bearing"),
    ),
    GeoSphereSensorDescription(
        key="pressure", translation_key="pressure",
        device_class=SensorDeviceClass.ATMOSPHERIC_PRESSURE,
        native_unit_of_measurement=UnitOfPressure.HPA,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.current.get("pressure"), forecast_fn=_hourly("native_pressure"),
    ),
    GeoSphereSensorDescription(
        key="cloud_coverage", translation_key="cloud_coverage",
        native_unit_of_measurement=PERCENTAGE, icon="mdi:weather-cloudy",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.current.get("cloud_coverage"),
        forecast_fn=_hourly("cloud_coverage"),
    ),
    GeoSphereSensorDescription(
        key="global_radiation", translation_key="global_radiation",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.current.get("global_radiation"),
        forecast_fn=_hourly("global_radiation"),
    ),
    GeoSphereSensorDescription(
        key="snow_limit", translation_key="snow_limit",
        device_class=SensorDeviceClass.DISTANCE, native_unit_of_measurement=UnitOfLength.METERS,
        icon="mdi:snowflake-alert", state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.current.get("snow_limit"), forecast_fn=_hourly("snow_limit"),
    ),
    GeoSphereSensorDescription(
        key="cape", translation_key="cape", native_unit_of_measurement="J/kg",
        icon="mdi:weather-lightning", state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.current.get("cape"), forecast_fn=_hourly("cape"),
    ),
    GeoSphereSensorDescription(
        key="weather_text", translation_key="weather_text", icon="mdi:weather-partly-cloudy",
        value_fn=_symbol_text,
    ),
    # --- Niederschlag ---
    GeoSphereSensorDescription(
        key="precipitation_next_hour", translation_key="precipitation_next_hour", **PRECIP,
        value_fn=lambda d: d.current.get("precipitation_next_hour"), enabled_fn=_nowcast,
    ),
    GeoSphereSensorDescription(
        key="precipitation_today", translation_key="precipitation_today", **PRECIP,
        value_fn=_rest_of_today("native_precipitation"),
        forecast_fn=_hourly("native_precipitation"),
    ),
    GeoSphereSensorDescription(
        key="precipitation_24h", translation_key="precipitation_24h", **PRECIP,
        value_fn=_hourly_sum("native_precipitation", 24),
    ),
    GeoSphereSensorDescription(
        key="snowfall_24h", translation_key="snowfall_24h", **PRECIP, icon="mdi:weather-snowy",
        value_fn=_hourly_sum("snow", 24),
    ),
    GeoSphereSensorDescription(
        key="precipitation_tomorrow", translation_key="precipitation_tomorrow", **PRECIP,
        value_fn=_daily(1, "native_precipitation"),
        attr_fn=_daily_range(1, "precip_p10", "precip_p90"),
    ),
    GeoSphereSensorDescription(
        key="precipitation_probability_today", translation_key="precipitation_probability_today",
        native_unit_of_measurement=PERCENTAGE, icon="mdi:weather-rainy",
        value_fn=_rest_of_today_max("precipitation_probability"),
        forecast_fn=_hourly("precipitation_probability"), enabled_fn=_ens,
    ),
    GeoSphereSensorDescription(
        key="precipitation_probability_tomorrow", translation_key="precipitation_probability_tomorrow",
        native_unit_of_measurement=PERCENTAGE, icon="mdi:weather-rainy",
        value_fn=_daily(1, "precipitation_probability"), enabled_fn=_ens,
    ),
    # --- Tageswerte ---
    GeoSphereSensorDescription(
        key="temp_max_today", translation_key="temp_max_today", **TEMP,
        value_fn=_daily(0, "native_temperature"),
        attr_fn=_daily_range(0, "temp_max_p10", "temp_max_p90"),
    ),
    GeoSphereSensorDescription(
        key="temp_min_today", translation_key="temp_min_today", **TEMP,
        value_fn=_daily(0, "native_templow"),
        attr_fn=_daily_range(0, "templow_p10", "templow_p90"),
    ),
    GeoSphereSensorDescription(
        key="temp_max_tomorrow", translation_key="temp_max_tomorrow", **TEMP,
        value_fn=_daily(1, "native_temperature"),
        attr_fn=_daily_range(1, "temp_max_p10", "temp_max_p90"),
    ),
    GeoSphereSensorDescription(
        key="temp_min_tomorrow", translation_key="temp_min_tomorrow", **TEMP,
        value_fn=_daily(1, "native_templow"),
        attr_fn=_daily_range(1, "templow_p10", "templow_p90"),
    ),
    GeoSphereSensorDescription(
        key="sunshine_today", translation_key="sunshine_today", **SUN,
        value_fn=_daily(0, "sunshine_hours"),
    ),
    GeoSphereSensorDescription(
        key="sunshine_tomorrow", translation_key="sunshine_tomorrow", **SUN,
        value_fn=_daily(1, "sunshine_hours"),
    ),
    # --- INCA-Analyse: "gemessene" Werte am Standort (ca. 1 h verzögert) ---
    GeoSphereSensorDescription(
        key="inca_precipitation_1h", translation_key="inca_precipitation_1h", **PRECIP,
        value_fn=_inca_latest("RR"), attr_fn=_inca_attrs, enabled_fn=_inca,
    ),
    GeoSphereSensorDescription(
        key="inca_precipitation_24h", translation_key="inca_precipitation_24h", **PRECIP,
        value_fn=_inca_sum("RR", 24), attr_fn=_inca_attrs, enabled_fn=_inca,
    ),
    GeoSphereSensorDescription(
        key="inca_temperature", translation_key="inca_temperature", **TEMP,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_inca_latest("T2M"), attr_fn=_inca_attrs, enabled_fn=_inca,
    ),
    GeoSphereSensorDescription(
        key="inca_global_radiation", translation_key="inca_global_radiation",
        device_class=SensorDeviceClass.IRRADIANCE,
        native_unit_of_measurement=UnitOfIrradiance.WATTS_PER_SQUARE_METER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_inca_latest("GL"), attr_fn=_inca_attrs, enabled_fn=_inca,
    ),
    # --- Luftqualität ---
    GeoSphereSensorDescription(
        key="aqi_today", translation_key="aqi_today", device_class=SensorDeviceClass.AQI,
        value_fn=_aqi(0), enabled_fn=_air,
    ),
    GeoSphereSensorDescription(
        key="aqi_tomorrow", translation_key="aqi_tomorrow", device_class=SensorDeviceClass.AQI,
        value_fn=_aqi(1), enabled_fn=_air,
    ),
    *(
        GeoSphereSensorDescription(
            key=param, translation_key=param, device_class=dc,
            native_unit_of_measurement=MICROGRAMS_PER_M3,
            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1,
            value_fn=_chem_now(param), forecast_fn=_chem_forecast(param), enabled_fn=_air,
        )
        for param, dc in (
            ("no2surf", SensorDeviceClass.NITROGEN_DIOXIDE),
            ("o3surf", SensorDeviceClass.OZONE),
            ("pm10surf", SensorDeviceClass.PM10),
            ("pm25surf", SensorDeviceClass.PM25),
        )
    ),
    # --- Wüstenstaub (Saharastaub), Säulenmenge über dem Standort ---
    GeoSphereSensorDescription(
        key="dust_load", translation_key="dust_load", native_unit_of_measurement=DUST_UNIT,
        icon="mdi:weather-dust", state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_dust_now, forecast_fn=_dust_forecast, enabled_fn=_dust,
    ),
    GeoSphereSensorDescription(
        key="dust_max_24h", translation_key="dust_max_24h", native_unit_of_measurement=DUST_UNIT,
        icon="mdi:weather-dust", suggested_display_precision=1,
        value_fn=_dust_max(24), enabled_fn=_dust,
    ),
    GeoSphereSensorDescription(
        key="dust_max_5d", translation_key="dust_max_5d", native_unit_of_measurement=DUST_UNIT,
        icon="mdi:weather-dust", suggested_display_precision=1,
        value_fn=_dust_max(None), attr_fn=_dust_peak_attrs, enabled_fn=_dust,
    ),
    # --- Warnungen ---
    GeoSphereSensorDescription(
        key="warning_level", translation_key="warning_level", icon="mdi:alert",
        value_fn=_warn_level, attr_fn=_warn_attrs, enabled_fn=_warn,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GeoSphereConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        GeoSphereSensor(coordinator, desc) for desc in SENSORS if desc.enabled_fn(coordinator)
    )


class GeoSphereSensor(GeoSphereEntity, SensorEntity):
    """Ein GeoSphere-Sensor."""

    entity_description: GeoSphereSensorDescription
    # Zeitreihen nicht in die Recorder-DB schreiben
    _unrecorded_attributes = frozenset({"forecast", "warnings"})

    def __init__(
        self, coordinator: GeoSphereCoordinator, description: GeoSphereSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        desc = self.entity_description
        attrs: dict[str, Any] = {}
        if desc.forecast_fn:
            attrs["forecast"] = desc.forecast_fn(self.coordinator.data)
        if desc.attr_fn:
            attrs.update(desc.attr_fn(self.coordinator.data))
        return attrs or None
