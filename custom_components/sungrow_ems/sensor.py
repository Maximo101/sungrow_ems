"""Sensor platform for Sungrow EMS: raw register sensors, poll status, derived sensors."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .coordinator import SungrowEmsConfigEntry, SungrowEmsCoordinator
from .derived import async_build_derived_sensors
from .entity import SungrowEmsEntity
from .registers import Device
from .sensor_types import SENSOR_DESCRIPTIONS, SungrowSensorEntityDescription

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SungrowEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up raw, status and derived sensors."""
    runtime = entry.runtime_data
    entities: list[SensorEntity] = []

    for description in SENSOR_DESCRIPTIONS:
        coordinator = runtime.coordinator_for(description.device)
        if coordinator is None:
            continue
        entities.append(SungrowRegisterSensor(coordinator, description))

    for coordinator in runtime.coordinators:
        entities.append(SungrowPollStatusSensor(coordinator))

    entities.extend(async_build_derived_sensors(hass, entry))
    async_add_entities(entities)


class SungrowRegisterSensor(SungrowEmsEntity, SensorEntity):
    """One register, decoded and sentinel-filtered by the coordinator."""

    entity_description: SungrowSensorEntityDescription

    def __init__(
        self,
        coordinator: SungrowEmsCoordinator,
        description: SungrowSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Unavailable only when the coordinator is down or the key was dropped
        (rejected by the device, or invalid for ``failure_threshold`` polls)."""
        data = self.coordinator.data
        return super().available and data is not None and self._key in data.values

    @property
    def native_value(self) -> StateType:
        data = self.coordinator.data
        if data is None:
            return None
        value = data.values.get(self._key)
        if value is None:
            return None
        if self.entity_description.value_fn is not None:
            return self.entity_description.value_fn(value, data.raw.get(self._key))
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        data = self.coordinator.data
        if fn is None or data is None or self._key not in data.values:
            return None
        return fn(data.values[self._key], data.raw.get(self._key)) or None


class SungrowPollStatusSensor(SungrowEmsEntity, SensorEntity):
    """Timestamp of the last successful poll, with failure counters.

    Stays available while the device is down so you can see how long it has
    been since the last good read.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Counters that change every poll: shown live, kept out of the recorder.
    _unrecorded_attributes = frozenset(
        {
            "seconds_since_value_change",
            "last_poll_duration",
            "last_poll_requests",
            "requests_total",
            "timeouts_total",
            "reconnects_total",
        }
    )

    def __init__(self, coordinator: SungrowEmsCoordinator) -> None:
        super().__init__(coordinator, f"{coordinator.device}_last_poll")
        self.entity_description = SensorEntityDescription(
            key=f"{coordinator.device}_last_poll",
            name="Last successful poll",
            icon="mdi:lan-connect"
            if coordinator.device is Device.INVERTER
            else "mdi:router-network",
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> datetime | None:
        data = self.coordinator.data
        return data.last_success if data is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coordinator = self.coordinator
        data = coordinator.data
        last = coordinator.last_read
        stats = coordinator.hub.stats
        last_change = data.last_change if data is not None else None
        return {
            "last_value_change": last_change.isoformat() if last_change else None,
            "seconds_since_value_change": round(data.age_of(last_change))
            if data is not None and last_change is not None
            else None,
            "consecutive_failures": coordinator.consecutive_failures,
            "failure_threshold": coordinator.failure_threshold,
            "last_error": data.last_error if data is not None else None,
            "stale_blocks": list(data.stale_blocks) if data is not None else [],
            "connected": coordinator.hub.connected,
            "last_poll_duration": round(last.duration, 2) if last is not None else None,
            "last_poll_requests": last.requests if last is not None else None,
            "requests_total": stats.requests,
            "timeouts_total": stats.timeouts,
            "reconnects_total": stats.connects,
        }
