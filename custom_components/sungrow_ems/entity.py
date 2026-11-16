"""Shared entity base classes for Sungrow EMS."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import IHM_NAME_SUFFIX, UNIQUE_ID_SUFFIX
from .coordinator import SungrowEmsCoordinator, SungrowEmsData
from .registers import Device


def make_unique_id(entry: ConfigEntry, key: str) -> str:
    """Unique ID for an entity of this integration: ``{entry_id}_{key}_ems``."""
    return f"{entry.entry_id}_{key}{UNIQUE_ID_SUFFIX}"


def display_name(device: Device, name: str) -> str:
    """Entity name as shown in the UI: iHomeManager entities end in " (iHM)"."""
    return f"{name}{IHM_NAME_SUFFIX}" if device is Device.IHM else name


class SungrowNamedEntity(Entity):
    """Naming shared by every entity of this integration.

    The shown name carries the " (iHM)" tag for iHomeManager entities. The
    entity ID is still built from the plain name, so it stays
    ``sensor.sungrow_ems_ihomemanager_battery_soc`` (the device name already
    says which device it is) and IDs are the same as in earlier versions.
    """

    _attr_has_entity_name = True
    _plain_name: str | None = None

    def _set_names(self, device: Device, name: str) -> None:
        self._plain_name = name
        self._attr_name = display_name(device, name)

    @property
    def suggested_object_id(self) -> str | None:
        """Entity ID base: the name without the device tag."""
        if self._plain_name is not None:
            return self._plain_name
        return super().suggested_object_id


class SungrowEmsEntity(SungrowNamedEntity, CoordinatorEntity[SungrowEmsCoordinator]):
    """Base for entities fed by one device coordinator.

    Device naming: entities attach to "Sungrow EMS Inverter" or
    "Sungrow EMS iHomeManager", so entity IDs come out as
    ``sensor.sungrow_ems_inverter_*`` / ``sensor.sungrow_ems_ihomemanager_*``
    and never collide with the original integration.
    """

    def __init__(self, coordinator: SungrowEmsCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = make_unique_id(coordinator.config_entry, key)
        self._attr_device_info = coordinator.device_info
        self._set_names(coordinator.device, name)

    @property
    def snapshot(self) -> SungrowEmsData | None:
        """Latest coordinator snapshot (None before the first good poll)."""
        return self.coordinator.data

    @property
    def available(self) -> bool:
        """Available while the coordinator is healthy and has published data."""
        return super().available and self.coordinator.data is not None
