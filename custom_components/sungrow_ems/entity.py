"""Shared entity base classes for Sungrow EMS."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEVICE_NAME_IHM,
    DEVICE_NAME_INVERTER,
    IHM_NAME_SUFFIX,
    UNIQUE_ID_SUFFIX,
)
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

    The shown name is the entity's own name only ("Battery SoC"), not
    prefixed with the device name; iHomeManager entities carry the " (iHM)"
    tag ("Battery SoC (iHM)"). ``has_entity_name`` is therefore off.

    The entity ID is still built as "<device name> <plain name>", so a new
    install gets ``sensor.sungrow_ems_inverter_battery_soc`` /
    ``sensor.sungrow_ems_ihomemanager_battery_soc`` exactly as before.
    Existing installs keep the entity IDs stored in the entity registry
    (looked up by unique ID), so history and statistics are untouched.
    """

    _attr_has_entity_name = False
    _plain_name: str | None = None
    _device_name: str | None = None

    def _set_names(self, device: Device, name: str) -> None:
        self._plain_name = name
        self._device_name = (
            DEVICE_NAME_IHM if device is Device.IHM else DEVICE_NAME_INVERTER
        )
        self._attr_name = display_name(device, name)

    @property
    def suggested_object_id(self) -> str | None:
        """Entity ID base: device name + plain name, without the (iHM) tag."""
        if self._plain_name is not None and self._device_name is not None:
            return f"{self._device_name} {self._plain_name}"
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
