"""Shared entity base classes for Sungrow EMS."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import ENTITY_ID_PREFIX_IHM, ENTITY_ID_PREFIX_INVERTER, UNIQUE_ID_SUFFIX
from .coordinator import SungrowEmsCoordinator, SungrowEmsData
from .registers import Device


def make_unique_id(entry: ConfigEntry, key: str) -> str:
    """Unique ID for an entity of this integration: ``{entry_id}_{key}_ems``."""
    return f"{entry.entry_id}_{key}{UNIQUE_ID_SUFFIX}"


def default_entity_id(entity: Entity, device: Device, name: str) -> str:
    """Entity ID for a new install, e.g. ``sensor.sungrow_ems_inverter_battery_soc``.

    Fixed per device instead of derived from the device name, so renaming the
    devices ("SH10RS", "iHM") does not change the IDs a new install gets.
    """
    domain = (
        Platform.BINARY_SENSOR
        if isinstance(entity, BinarySensorEntity)
        else Platform.SENSOR
    )
    prefix = ENTITY_ID_PREFIX_IHM if device is Device.IHM else ENTITY_ID_PREFIX_INVERTER
    return f"{domain}.{prefix}_{slugify(name)}"


class SungrowNamedEntity(Entity):
    """Naming shared by every entity of this integration.

    The entity name is just the data point ("Battery SoC"); Home Assistant
    shows it with the device name in front ("SH10RS Battery SoC",
    "iHM Battery SoC"). The entity ID a new install gets is fixed by
    :func:`default_entity_id`; an entity that is already registered keeps
    its stored entity ID (looked up by unique ID), so history is untouched.
    """

    _attr_has_entity_name = True

    def _set_names(self, device: Device, name: str) -> None:
        self._attr_name = name
        self.entity_id = default_entity_id(self, device, name)


class SungrowEmsEntity(SungrowNamedEntity, CoordinatorEntity[SungrowEmsCoordinator]):
    """Base for entities fed by one device coordinator.

    Entities attach to the inverter device (named after its model, e.g.
    "SH10RS") or the "iHM" device. Entity IDs stay
    ``sensor.sungrow_ems_inverter_*`` / ``sensor.sungrow_ems_ihomemanager_*``
    so they never collide with the original integration.
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
