"""Weather-Entity mit stündlicher und täglicher Vorhersage."""

from __future__ import annotations

from typing import Any

from homeassistant.components.weather import (
    Forecast,
    SingleCoordinatorWeatherEntity,
    WeatherEntityFeature,
)
from homeassistant.const import (
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SYMBOL_TEXT
from .coordinator import GeoSphereConfigEntry, GeoSphereCoordinator
from .entity import GeoSphereEntity

# Nur diese Keys kennt HA im Forecast; Zusatzwerte bleiben in den Sensoren
_FORECAST_KEYS = {
    "datetime", "condition", "is_daytime", "native_temperature", "native_templow",
    "humidity", "cloud_coverage", "native_precipitation", "native_pressure",
    "native_wind_speed", "native_wind_gust_speed", "wind_bearing",
    "precipitation_probability",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GeoSphereConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([GeoSphereWeather(entry.runtime_data)])


class GeoSphereWeather(GeoSphereEntity, SingleCoordinatorWeatherEntity[GeoSphereCoordinator]):
    """GeoSphere Wetter (NWP 1 km + Nowcast)."""

    _attr_name = None
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.METERS_PER_SECOND
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_HOURLY | WeatherEntityFeature.FORECAST_DAILY
    )

    def __init__(self, coordinator: GeoSphereCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_weather"

    @property
    def _cur(self) -> dict[str, Any]:
        return self.coordinator.data.current

    @property
    def condition(self) -> str | None:
        return self._cur.get("condition")

    @property
    def native_temperature(self) -> float | None:
        return self._cur.get("temperature")

    @property
    def native_dew_point(self) -> float | None:
        return self._cur.get("dew_point")

    @property
    def humidity(self) -> float | None:
        return self._cur.get("humidity")

    @property
    def native_pressure(self) -> float | None:
        return self._cur.get("pressure")

    @property
    def native_wind_speed(self) -> float | None:
        return self._cur.get("wind_speed")

    @property
    def native_wind_gust_speed(self) -> float | None:
        return self._cur.get("wind_gust")

    @property
    def wind_bearing(self) -> float | None:
        return self._cur.get("wind_bearing")

    @property
    def cloud_coverage(self) -> float | None:
        return self._cur.get("cloud_coverage")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sy = self._cur.get("symbol")
        ref = self.coordinator.data.nwp.reference_time
        return {
            "weather_symbol": int(sy) if sy is not None else None,
            "weather_text": SYMBOL_TEXT.get(int(sy)) if sy is not None else None,
            "model_run": ref.isoformat() if ref else None,
        }

    @callback
    def _async_forecast_hourly(self) -> list[Forecast] | None:
        return _clean(self.coordinator.data.hourly)

    @callback
    def _async_forecast_daily(self) -> list[Forecast] | None:
        return _clean(self.coordinator.data.daily)


def _clean(items: list[dict[str, Any]]) -> list[Forecast]:
    return [
        Forecast(**{k: v for k, v in item.items() if k in _FORECAST_KEYS and v is not None})
        for item in items
    ]
