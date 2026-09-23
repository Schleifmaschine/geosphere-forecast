"""Binärsensoren mit fertiger Logik für Automatisierungen."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    FROST_THRESHOLD,
    FROST_UNTIL_HOUR,
    IRRIGATION_BALANCE,
    IRRIGATION_MAX_RAIN_24H,
    PRECIP_THRESHOLD,
    THUNDER_CAPE,
    THUNDER_CAPE_PRECIP,
    THUNDER_HOURS,
    WARNING_LEVELS,
)
from .coordinator import GeoSphereConfigEntry, GeoSphereCoordinator, GeoSphereData, _sum
from .entity import GeoSphereEntity
from .sensor import _water_balance

Data = GeoSphereData


@dataclass(frozen=True, kw_only=True)
class GeoSphereBinaryDescription(BinarySensorEntityDescription):
    """Binärsensor mit Zustands-, Attribut- und Verfügbarkeitsfunktion."""

    is_on_fn: Callable[[Data], bool | None]
    attr_fn: Callable[[Data], dict[str, Any]] | None = None
    enabled_fn: Callable[[GeoSphereCoordinator], bool] = lambda c: True


def _hours_between(d: Data, start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [
        h for h in d.hourly
        if start <= dt_util.parse_datetime(h["datetime"]) + timedelta(hours=1) and
        dt_util.parse_datetime(h["datetime"]) < end
    ]


# --- Regen in der nächsten Stunde -------------------------------------------

def _rain_next_hour(d: Data) -> bool | None:
    window = d.current.get("rain_window")
    if window is not None:
        start = window["start"]
        return start is not None and start <= dt_util.utcnow() + timedelta(hours=1)
    # ohne Nowcast: nächste Stunde aus NWP/Ensemble
    if not d.hourly:
        return None
    h = d.hourly[0]
    prob = h.get("precipitation_probability")
    if prob is not None:
        return prob >= 50
    return (h.get("native_precipitation") or 0) >= PRECIP_THRESHOLD


def _rain_next_hour_attrs(d: Data) -> dict[str, Any]:
    window = d.current.get("rain_window")
    return {
        "source": "nowcast" if window is not None else "nwp",
        "expected_precipitation": d.current.get("precipitation_next_hour"),
        "rain_start": window["start"].isoformat() if window and window["start"] else None,
    }


# --- Frost ------------------------------------------------------------------

def _frost_window(d: Data) -> list[dict[str, Any]]:
    now = dt_util.now()
    end = now.replace(hour=FROST_UNTIL_HOUR, minute=0, second=0, microsecond=0)
    if now >= end:
        end += timedelta(days=1)
    return _hours_between(d, dt_util.as_utc(now), dt_util.as_utc(end))


def _frost_min(d: Data) -> tuple[float | None, float | None]:
    hours = _frost_window(d)
    temps = [h["native_temperature"] for h in hours if h["native_temperature"] is not None]
    p10 = [h["temp_p10"] for h in hours if h.get("temp_p10") is not None]
    tmin, tmin_p10 = min(temps, default=None), min(p10, default=None)
    return (
        round(tmin, 1) if tmin is not None else None,
        round(tmin_p10, 1) if tmin_p10 is not None else None,
    )


def _frost(d: Data) -> bool | None:
    tmin, p10 = _frost_min(d)
    if tmin is None:
        return None
    # auch warnen, wenn nur das kühle Ensemble-Szenario (10. Perzentil) darunter liegt
    return tmin < FROST_THRESHOLD or (p10 is not None and p10 < FROST_THRESHOLD)


def _frost_attrs(d: Data) -> dict[str, Any]:
    tmin, p10 = _frost_min(d)
    return {"min_temperature": tmin, "min_temperature_p10": p10, "threshold": FROST_THRESHOLD}


# --- Gewitter ---------------------------------------------------------------

def _thunder_hours(d: Data) -> list[dict[str, Any]]:
    return [
        h for h in d.hourly[:THUNDER_HOURS]
        if h["condition"] == "lightning-rainy"
        or (
            (h.get("cape") or 0) >= THUNDER_CAPE
            and (h.get("native_precipitation") or 0) >= THUNDER_CAPE_PRECIP
        )
    ]


def _thunder(d: Data) -> bool | None:
    return bool(_thunder_hours(d)) if d.hourly else None


def _thunder_attrs(d: Data) -> dict[str, Any]:
    hits = _thunder_hours(d)
    capes = [h["cape"] for h in d.hourly[:THUNDER_HOURS] if h.get("cape") is not None]
    return {
        "first_time": hits[0]["datetime"] if hits else None,
        "max_cape": max(capes, default=None),
        "hours": THUNDER_HOURS,
    }


# --- Bewässerung ------------------------------------------------------------

def _rain_24h(d: Data) -> float | None:
    return _sum(h["native_precipitation"] for h in d.hourly[:24])


def _irrigation(d: Data) -> bool | None:
    balance = _water_balance(d)
    if balance is None:
        return None
    return balance <= IRRIGATION_BALANCE and (_rain_24h(d) or 0) < IRRIGATION_MAX_RAIN_24H


def _irrigation_attrs(d: Data) -> dict[str, Any]:
    return {
        "water_balance_7d": _water_balance(d),
        "expected_rain_24h": _rain_24h(d),
        "balance_threshold": IRRIGATION_BALANCE,
        "rain_threshold": IRRIGATION_MAX_RAIN_24H,
    }


# --- Warnungen --------------------------------------------------------------

def _active_warnings(d: Data, type_id: int | None = None) -> list[dict[str, Any]]:
    now = dt_util.utcnow()
    return [
        w for w in d.warnings or []
        if (type_id is None or w["type_id"] == type_id)
        and w["level"] > 0
        and (w["start"] is None or w["start"] <= now)
        and (w["end"] is None or now < w["end"])
    ]


def _warning_on(type_id: int | None) -> Callable[[Data], bool | None]:
    def fn(d: Data) -> bool | None:
        if d.warnings is None:
            return None
        return bool(_active_warnings(d, type_id))

    return fn


def _warning_attrs(type_id: int | None) -> Callable[[Data], dict[str, Any]]:
    def fn(d: Data) -> dict[str, Any]:
        active = _active_warnings(d, type_id)
        now = dt_util.utcnow()
        upcoming = [
            w for w in d.warnings or []
            if (type_id is None or w["type_id"] == type_id) and w["start"] and w["start"] > now
        ]
        level = max((w["level"] for w in active), default=0)
        nxt = min(upcoming, key=lambda w: w["start"], default=None)
        until = max((w["end"] for w in active if w["end"]), default=None)
        return {
            "level": level,
            "level_name": WARNING_LEVELS.get(level),
            "until": until.isoformat() if until else None,
            "next_start": nxt["start"].isoformat() if nxt else None,
            "next_level": nxt["level"] if nxt else None,
            "text": active[0]["text"] if active else None,
        }

    return fn


def _climate(c: GeoSphereCoordinator) -> bool:
    return c.use_climate


def _warn(c: GeoSphereCoordinator) -> bool:
    return c.use_warnings


BINARY_SENSORS: tuple[GeoSphereBinaryDescription, ...] = (
    GeoSphereBinaryDescription(
        key="rain_next_hour", translation_key="rain_next_hour",
        device_class=BinarySensorDeviceClass.MOISTURE,
        is_on_fn=_rain_next_hour, attr_fn=_rain_next_hour_attrs,
    ),
    GeoSphereBinaryDescription(
        key="frost_tonight", translation_key="frost_tonight",
        device_class=BinarySensorDeviceClass.COLD,
        is_on_fn=_frost, attr_fn=_frost_attrs,
    ),
    GeoSphereBinaryDescription(
        key="thunderstorm_risk", translation_key="thunderstorm_risk",
        device_class=BinarySensorDeviceClass.SAFETY, icon="mdi:weather-lightning",
        is_on_fn=_thunder, attr_fn=_thunder_attrs,
    ),
    GeoSphereBinaryDescription(
        key="irrigation_needed", translation_key="irrigation_needed",
        icon="mdi:sprinkler-variant",
        is_on_fn=_irrigation, attr_fn=_irrigation_attrs, enabled_fn=_climate,
    ),
    GeoSphereBinaryDescription(
        key="warning_active", translation_key="warning_active",
        device_class=BinarySensorDeviceClass.SAFETY,
        is_on_fn=_warning_on(None), attr_fn=_warning_attrs(None), enabled_fn=_warn,
    ),
    *(
        GeoSphereBinaryDescription(
            key=f"warning_{key}", translation_key=f"warning_{key}",
            device_class=BinarySensorDeviceClass.SAFETY, icon=icon,
            is_on_fn=_warning_on(type_id), attr_fn=_warning_attrs(type_id), enabled_fn=_warn,
        )
        for type_id, key, icon in (
            (1, "storm", "mdi:weather-windy"),
            (2, "rain", "mdi:weather-pouring"),
            (3, "snow", "mdi:weather-snowy-heavy"),
            (4, "ice", "mdi:snowflake-alert"),
            (5, "thunderstorm", "mdi:weather-lightning-rainy"),
            (6, "heat", "mdi:thermometer-high"),
            (7, "cold", "mdi:thermometer-low"),
        )
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GeoSphereConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        GeoSphereBinarySensor(coordinator, desc)
        for desc in BINARY_SENSORS
        if desc.enabled_fn(coordinator)
    )


class GeoSphereBinarySensor(GeoSphereEntity, BinarySensorEntity):
    """Ein GeoSphere-Binärsensor."""

    entity_description: GeoSphereBinaryDescription

    def __init__(
        self, coordinator: GeoSphereCoordinator, description: GeoSphereBinaryDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attr_fn is None:
            return None
        return self.entity_description.attr_fn(self.coordinator.data)
