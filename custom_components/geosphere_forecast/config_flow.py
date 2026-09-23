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
from homeassistant.helpers.selector import LocationSelector, TextSelector

from .api import GeoSphereClient, GeoSphereError
from .const import (
    CONF_AIR_QUALITY,
    CONF_DUST,
    CONF_ENSEMBLE,
    CONF_INCA,
    CONF_NOWCAST,
    CONF_WARNINGS,
    DOMAIN,
    NWP_BBOX,
    NWP_RESOURCE,
)
from .coordinator import in_bbox


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
        schema = vol.Schema(
            {
                vol.Required(CONF_NOWCAST, default=opts.get(CONF_NOWCAST, True)): bool,
                vol.Required(CONF_ENSEMBLE, default=opts.get(CONF_ENSEMBLE, True)): bool,
                vol.Required(CONF_INCA, default=opts.get(CONF_INCA, True)): bool,
                vol.Required(CONF_AIR_QUALITY, default=opts.get(CONF_AIR_QUALITY, True)): bool,
                vol.Required(CONF_DUST, default=opts.get(CONF_DUST, True)): bool,
                vol.Required(CONF_WARNINGS, default=opts.get(CONF_WARNINGS, True)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
