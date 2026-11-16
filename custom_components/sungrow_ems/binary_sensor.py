"""Binary sensors for Sungrow EMS.

* Off-grid (backup) mode: inverter running state 13000 == 0x1000 exactly.
  (The old ``& 4096`` test also matched standby, shutdown, fault and warn
  states 0x1200-0x1900, 0x5500, 0x9100.)
* EV charger fault: iHM charger status 8552 outside the documented codes
  1, 2, 3, 6 (seen in the field: 8, 9 and 10).
* Inverter fault: any bit set in the inverter's fault registers (13052-13067,
  13072-13077). Alarm-only registers are reported as attributes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SungrowEmsConfigEntry, SungrowEmsCoordinator, SungrowEmsData
from .entity import SungrowEmsEntity
from .registers import (
    FAULT_BITS,
    IHM_CHARGER_STATUS,
    REGISTERS_BY_KEY,
    RUNNING_STATE_OFF_GRID,
    Device,
    active_bits,
)

PARALLEL_UPDATES = 0

#: Registers whose set bits mean the inverter or battery is in fault.
FAULT_KEYS: tuple[str, ...] = (
    "mb_grid_side_fault",
    "mb_system_fault_1",
    "mb_system_fault_2",
    "mb_dc_side_fault",
    "mb_permanent_fault",
    "mb_bdc_side_fault",
    "mb_bdc_side_permanent_fault",
    "mb_battery_fault",
    "mb_bms_protection",
    "mb_bms_fault_1",
    "mb_bms_fault_2",
)
#: Warning-level registers, reported as attributes only.
ALARM_KEYS: tuple[str, ...] = (
    "mb_inverter_alarm",
    "mb_battery_alarm",
    "mb_bms_alarm",
    "mb_bms_alarm_2",
)


@dataclass(frozen=True, kw_only=True)
class SungrowBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Binary sensor computed from one device's snapshot."""

    device: Device
    keys: tuple[str, ...]
    is_on_fn: Callable[[SungrowEmsData], bool | None]
    attrs_fn: Callable[[SungrowEmsData], dict[str, Any]] | None = None
    icon_on: str | None = None
    icon_off: str | None = None


def _off_grid(data: SungrowEmsData) -> bool | None:
    code = data.raw.get("mb_running_state")
    return None if code is None else code == RUNNING_STATE_OFF_GRID


def _off_grid_attrs(data: SungrowEmsData) -> dict[str, Any]:
    code = data.raw.get("mb_running_state")
    if code is None:
        return {}
    return {
        "running_state": REGISTERS_BY_KEY["mb_running_state"].label(code),
        "running_state_code": f"0x{code:04X}",
        "backup_power": data.values.get("mb_backup_total_power"),
    }


def _charger_fault(data: SungrowEmsData) -> bool | None:
    code = data.raw.get("ihm_charger_status")
    return None if code is None else code not in IHM_CHARGER_STATUS


def _charger_attrs(data: SungrowEmsData) -> dict[str, Any]:
    code = data.raw.get("ihm_charger_status")
    if code is None:
        return {}
    return {
        "status": REGISTERS_BY_KEY["ihm_charger_status"].label(code),
        "status_code": code,
        "fault_code": None if code in IHM_CHARGER_STATUS else code,
    }


def _inverter_fault(data: SungrowEmsData) -> bool | None:
    present = [data.raw.get(k) for k in FAULT_KEYS if k in data.raw]
    if not present:
        return None
    return any(present)


def _inverter_fault_attrs(data: SungrowEmsData) -> dict[str, Any]:
    faults = {
        key.removeprefix("mb_"): active_bits(data.raw[key], FAULT_BITS[key])
        for key in FAULT_KEYS
        if data.raw.get(key)
    }
    alarms = {
        key.removeprefix("mb_"): active_bits(data.raw[key], FAULT_BITS[key])
        for key in ALARM_KEYS
        if data.raw.get(key)
    }
    return {"faults": faults, "alarms": alarms}


BINARY_SENSORS: tuple[SungrowBinarySensorEntityDescription, ...] = (
    SungrowBinarySensorEntityDescription(
        key="off_grid",
        name="Off-grid (backup) mode",
        device=Device.INVERTER,
        keys=("mb_running_state",),
        is_on_fn=_off_grid,
        attrs_fn=_off_grid_attrs,
        icon_on="mdi:transmission-tower-off",
        icon_off="mdi:transmission-tower",
    ),
    SungrowBinarySensorEntityDescription(
        key="inverter_fault",
        name="Inverter fault",
        device=Device.INVERTER,
        device_class=BinarySensorDeviceClass.PROBLEM,
        keys=FAULT_KEYS,
        is_on_fn=_inverter_fault,
        attrs_fn=_inverter_fault_attrs,
    ),
    SungrowBinarySensorEntityDescription(
        key="ev_charger_fault",
        name="EV charger fault",
        device=Device.IHM,
        device_class=BinarySensorDeviceClass.PROBLEM,
        keys=("ihm_charger_status",),
        is_on_fn=_charger_fault,
        attrs_fn=_charger_attrs,
        icon="mdi:ev-station",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SungrowEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    runtime = entry.runtime_data
    entities: list[SungrowBinarySensor] = []
    for description in BINARY_SENSORS:
        coordinator = runtime.coordinator_for(description.device)
        if coordinator is not None:
            entities.append(SungrowBinarySensor(coordinator, description))
    async_add_entities(entities)


class SungrowBinarySensor(SungrowEmsEntity, BinarySensorEntity):
    """Binary sensor driven by a coordinator snapshot."""

    entity_description: SungrowBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: SungrowEmsCoordinator,
        description: SungrowBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, description.key, str(description.name))
        self.entity_description = description

    @property
    def available(self) -> bool:
        data = self.coordinator.data
        return (
            super().available
            and data is not None
            and any(key in data.raw for key in self.entity_description.keys)
        )

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        return None if data is None else self.entity_description.is_on_fn(data)

    @property
    def icon(self) -> str | None:
        description = self.entity_description
        if description.icon_on is None:
            return super().icon
        return description.icon_on if self.is_on else description.icon_off

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        if data is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(data) or None
