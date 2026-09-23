"""Gemeinsame Basis für alle Entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import GeoSphereCoordinator


class GeoSphereEntity(CoordinatorEntity[GeoSphereCoordinator]):
    """Basis-Entity mit Device-Info (ein Service-Device pro Standort)."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(self, coordinator: GeoSphereCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_device_info = DeviceInfo(
            entry_type=DeviceEntryType.SERVICE,
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="GeoSphere Austria",
            model="Data Hub Forecast",
            name=entry.title,
            configuration_url="https://data.hub.geosphere.at/group/wettervorhersagen",
        )
