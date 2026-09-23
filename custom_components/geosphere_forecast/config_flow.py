"""Config Flow für GeoSphere Forecast."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_LATITUDE, CONF_LOCATION, CONF_LONGITUDE, CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    LocationSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .api import GeoSphereClient, GeoSphereError
from .const import (
    CONF_AIR_QUALITY,
    CONF_CLIMATE,
    CONF_DUST,
    CONF_EXTENDED,
    CONF_EXTENDED_DAYS,
    CONF_ENSEMBLE,
    CONF_INCA,
    CONF_NOWCAST,
    CONF_SNOW,
    CONF_STATION,
    CONF_WARNINGS,
    DEFAULT_EXTENDED_DAYS,
    DOMAIN,
    MAX_EXTENDED_DAYS,
    NWP_BBOX,
    NWP_RESOURCE,
    STATION_AUTO,
    STATION_NONE,
)
from .coordinator import in_bbox, nearest_stations


class GeoSphereConfigFlow(ConfigFlow, domain=DOMAIN):
    """Standort auswählen und prüfen."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            lat = round(user_input[CONF_LOCATION][CONF_LATITUDE], 4)
            lon = round(user_input[CONF_LOCATION][CONF_LONGITUDE], 4)
            await self.async_set_unique_id(f"{lat:.3f}_{lon:.3f}")
            self._abort_if_unique_id_configured()

            if not in_bbox(lat, lon, NWP_BBOX):
                errors["base"] = "outside_area"
            else:
                client = GeoSphereClient(async_get_clientsession(self.hass))
                try:
                    await client.forecast(NWP_RESOURCE, ["2t"], lat, lon)
                except GeoSphereError:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=user_input[CONF_NAME],
                        data={CONF_LATITUDE: lat, CONF_LONGITUDE: lon},
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=self.hass.config.location_name): TextSelector(),
                vol.Required(
                    CONF_LOCATION,
                    default={
                        CONF_LATITUDE: self.hass.config.latitude,
                        CONF_LONGITUDE: self.hass.config.longitude,
                    },
                ): LocationSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GeoSphereOptionsFlow()


class GeoSphereOptionsFlow(OptionsFlow):
    """Optionale Datenquellen ein-/ausschalten."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        opts = self.config_entry.options
        station_options = [
            SelectOptionDict(value=STATION_AUTO, label="Automatisch (nächste Station)"),
            SelectOptionDict(value=STATION_NONE, label="Keine Station"),
        ]
        client = GeoSphereClient(async_get_clientsession(self.hass))
        try:
            stations = await client.stations()
        except GeoSphereError:
            stations = []
        data = self.config_entry.data
        for station, dist in nearest_stations(stations, data[CONF_LATITUDE], data[CONF_LONGITUDE]):
            station_options.append(
                SelectOptionDict(
                    value=str(station["id"]),
                    label=f"{station['name'].title()} ({dist:.1f} km, {station.get('altitude', 0):.0f} m)",
                )
            )
        schema = vol.Schema(
            {
                vol.Required(CONF_STATION, default=opts.get(CONF_STATION, STATION_AUTO)): SelectSelector(
                    SelectSelectorConfig(options=station_options, mode=SelectSelectorMode.DROPDOWN)
                ),
                vol.Required(CONF_NOWCAST, default=opts.get(CONF_NOWCAST, True)): bool,
                vol.Required(CONF_EXTENDED, default=opts.get(CONF_EXTENDED, False)): bool,
                vol.Required(
                    CONF_EXTENDED_DAYS,
                    default=opts.get(CONF_EXTENDED_DAYS, DEFAULT_EXTENDED_DAYS),
                ): NumberSelector(
                    NumberSelectorConfig(min=3, max=MAX_EXTENDED_DAYS, step=1, mode=NumberSelectorMode.SLIDER)
                ),
                vol.Required(CONF_ENSEMBLE, default=opts.get(CONF_ENSEMBLE, True)): bool,
                vol.Required(CONF_INCA, default=opts.get(CONF_INCA, True)): bool,
                vol.Required(CONF_AIR_QUALITY, default=opts.get(CONF_AIR_QUALITY, True)): bool,
                vol.Required(CONF_CLIMATE, default=opts.get(CONF_CLIMATE, True)): bool,
                vol.Required(CONF_SNOW, default=opts.get(CONF_SNOW, True)): bool,
                vol.Required(CONF_DUST, default=opts.get(CONF_DUST, True)): bool,
                vol.Required(CONF_WARNINGS, default=opts.get(CONF_WARNINGS, True)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
