"""Shared entity base classes for Sungrow EMS."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import UNIQUE_ID_SUFFIX
from .coordinator import SungrowEmsCoordinator, SungrowEmsData


def make_unique_id(entry: ConfigEntry, key: str) -> str:
    """Unique ID for an entity of this integration: ``{entry_id}_{key}_ems``."""
    return f"{entry.entry_id}_{key}{UNIQUE_ID_SUFFIX}"


class SungrowEmsEntity(CoordinatorEntity[SungrowEmsCoordinator]):
    """Base for entities fed by one device coordinator.

    Device naming: entities attach to "Sungrow EMS Inverter" or
    "Sungrow EMS iHomeManager", so entity IDs come out as
    ``sensor.sungrow_ems_inverter_*`` / ``sensor.sungrow_ems_ihomemanager_*``
    and never collide with the original integration.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: SungrowEmsCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = make_unique_id(coordinator.config_entry, key)
        self._attr_device_info = coordinator.device_info

    @property
    def snapshot(self) -> SungrowEmsData | None:
        """Latest coordinator snapshot (None before the first good poll)."""
        return self.coordinator.data

    @property
    def available(self) -> bool:
        """Available while the coordinator is healthy and has published data."""
        return super().available and self.coordinator.data is not None
