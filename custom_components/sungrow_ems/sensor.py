"""Sensor platform for Sungrow EMS: raw register sensors, poll status, derived sensors,
and (only when the PVOutput uploader is enabled) PVOutput last upload and
PVOutput donation status."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import DOMAIN
from .controller import SungrowController
from .coordinator import SungrowEmsConfigEntry, SungrowEmsCoordinator
from .derived import async_build_derived_sensors
from .entity import SungrowEmsEntity, SungrowNamedEntity, make_unique_id
from .force_charge import ForceChargeState
from .profiles import THREE_PHASE_KEYS
from .pvoutput import PVOutputUploader
from .registers import Device
from .sensor_types import (
    METER_DEPENDENT_KEYS,
    SENSOR_DESCRIPTIONS,
    SungrowSensorEntityDescription,
)

PARALLEL_UPDATES = 0

PVOUTPUT_LAST_UPLOAD_KEY = "pvoutput_last_upload"
PVOUTPUT_DONATION_STATUS_KEY = "pvoutput_donation_status"
PVOUTPUT_SENSOR_KEYS = (PVOUTPUT_LAST_UPLOAD_KEY, PVOUTPUT_DONATION_STATUS_KEY)

#: Home Assistant state strings are limited to 255 characters.
_MAX_STATE_LENGTH = 255


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SungrowEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up raw, status and derived sensors."""
    runtime = entry.runtime_data
    entities: list[SensorEntity] = []
    has_ihm = runtime.ihm is not None
    three_phase = runtime.inverter.profile.three_phase

    for description in SENSOR_DESCRIPTIONS:
        coordinator = runtime.coordinator_for(description.device)
        if coordinator is None:
            continue
        if has_ihm and description.key in METER_DEPENDENT_KEYS:
            # The grid meter is on the iHM: these inverter registers read 0.
            description = replace(description, entity_registry_enabled_default=False)
        elif three_phase and description.key in THREE_PHASE_KEYS:
            # Three-phase model (SHxxT): phase B/C carry data.
            description = replace(description, entity_registry_enabled_default=True)
        entities.append(SungrowRegisterSensor(coordinator, description))

    for coordinator in runtime.coordinators:
        entities.append(SungrowPollStatusSensor(coordinator))

    entities.extend(async_build_derived_sensors(hass, entry))

    if runtime.controller is not None:
        entities.append(
            ForceChargeStateSensor(runtime.ihm or runtime.inverter, runtime.controller)
        )

    if runtime.pvoutput is not None:
        entities.append(PVOutputLastUploadSensor(runtime.inverter, runtime.pvoutput))
        entities.append(
            PVOutputDonationStatusSensor(runtime.inverter, runtime.pvoutput)
        )
    else:
        # PVOutput switched off: drop the entities instead of leaving them
        # orphaned.
        registry = er.async_get(hass)
        for key in PVOUTPUT_SENSOR_KEYS:
            stale = registry.async_get_entity_id(
                "sensor", DOMAIN, make_unique_id(entry, key)
            )
            if stale is not None:
                registry.async_remove(stale)

    async_add_entities(entities)


class SungrowRegisterSensor(SungrowEmsEntity, SensorEntity):
    """One register, decoded and sentinel-filtered by the coordinator."""

    entity_description: SungrowSensorEntityDescription

    def __init__(
        self,
        coordinator: SungrowEmsCoordinator,
        description: SungrowSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, description.key, str(description.name))
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
    been since the last good read. Disabled by default: it changes on every
    poll, which fills the Activity log and the recorder. The Connection binary
    sensor is the everyday health indicator.
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
        name = "Last successful poll"
        super().__init__(coordinator, f"{coordinator.device}_last_poll", name)
        self.entity_description = SensorEntityDescription(
            key=f"{coordinator.device}_last_poll",
            name=name,
            entity_registry_enabled_default=False,
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


class PVOutputLastUploadSensor(SungrowNamedEntity, RestoreSensor):
    """Time of the last successful PVOutput upload (diagnostic).

    Only exists while the PVOutput uploader is enabled. The state changes only
    when an upload succeeds; the attributes describe the latest attempt, so a
    failing upload is visible without the state moving. The value survives a
    restart. Lives on the inverter device.
    """

    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-upload"
    # Change on every attempt: shown live, kept out of the recorder.
    _unrecorded_attributes = frozenset(
        {
            "last_attempt",
            "last_payload",
            "last_response",
            "uploads_ok",
            "uploads_failed",
            "rate_limit_remaining",
        }
    )

    def __init__(
        self, coordinator: SungrowEmsCoordinator, uploader: PVOutputUploader
    ) -> None:
        self._uploader = uploader
        self._attr_unique_id = make_unique_id(
            coordinator.config_entry, PVOUTPUT_LAST_UPLOAD_KEY
        )
        self._attr_device_info = coordinator.device_info
        self._set_names(coordinator.device, "PVOutput last upload")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if (
            self._uploader.last_success is None
            and last is not None
            and isinstance(last.native_value, datetime)
        ):
            self._uploader.last_success = last.native_value
        self.async_on_remove(self._uploader.add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> datetime | None:
        return self._uploader.last_success

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        uploader = self._uploader
        result = uploader.last_result
        settings = uploader.settings
        return {
            "upload_mode": settings.upload_mode,
            "interval_minutes": uploader.interval if uploader.live else None,
            "daily_time": None if uploader.live else settings.daily_time.isoformat(),
            "tier": settings.tier,
            "last_status": result.status if result else None,
            "last_ok": result.ok if result else None,
            "last_attempt": result.attempted.isoformat() if result else None,
            "last_response": result.message if result else None,
            "last_payload": result.payload if result else None,
            "uploads_ok": uploader.uploads_ok,
            "uploads_failed": uploader.uploads_failed,
            "rate_limit_remaining": uploader.rate_remaining,
            "rate_limited_until": uploader.blocked_until.isoformat()
            if uploader.blocked_until
            else None,
        }


class PVOutputDonationStatusSensor(SungrowNamedEntity, SensorEntity):
    """Whether the PVOutput account is in donation mode (diagnostic).

    State: "Active" or "Not active", from the hourly request limit PVOutput
    returns with every answer (300 = donation, 60 = free). When PVOutput
    refuses donation-only values (v7-v12, b1-b5), for example after a
    donation has expired, the state is PVOutput's own error message until
    donation mode is active again. Unknown until the first answer.

    While donation is configured but not active, the uploader keeps sending
    the standard values without v7-v12 / b1-b5, so no data is lost.
    """

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: SungrowEmsCoordinator, uploader: PVOutputUploader
    ) -> None:
        self._uploader = uploader
        self._attr_unique_id = make_unique_id(
            coordinator.config_entry, PVOUTPUT_DONATION_STATUS_KEY
        )
        self._attr_device_info = coordinator.device_info
        self._set_names(coordinator.device, "PVOutput donation status")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._uploader.add_listener(self._handle_update))

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str | None:
        status = self._uploader.donation_status
        if status is None:
            return None
        return status[:_MAX_STATE_LENGTH]

    @property
    def icon(self) -> str:
        uploader = self._uploader
        if uploader.donation_error:
            return "mdi:alert-circle-outline"
        if uploader.donation_active:
            return "mdi:hand-heart"
        return "mdi:hand-heart-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        uploader = self._uploader
        settings = uploader.settings
        if not settings.donation:
            extended = "not configured"
        elif uploader.withhold_donation_params:
            extended = "withheld"
        else:
            extended = "sent"
        return {
            "configured_tier": settings.tier,
            "hourly_request_limit": uploader.rate_limit,
            "donation_values": extended,
            "error": uploader.donation_error,
            "last_checked": uploader.donation_checked.isoformat()
            if uploader.donation_checked
            else None,
        }


class ForceChargeStateSensor(SungrowEmsEntity, SensorEntity):
    """State of the force-charge state machine (control only).

    idle / charging / target_reached / aborted, with the session (method,
    target, power, previous settings, deadline) and the reason it ended in
    the attributes.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:battery-charging-high"

    def __init__(
        self, coordinator: SungrowEmsCoordinator, controller: SungrowController
    ) -> None:
        super().__init__(coordinator, "force_charge_state", "Force charge state")
        self._controller = controller
        self._attr_options = [str(s) for s in ForceChargeState]

    @property
    def available(self) -> bool:
        """Always available: the state is local, not read from a device."""
        return True

    @property
    def native_value(self) -> str:
        return str(self._controller.force_charge.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._controller.force_charge.attributes()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._controller.async_add_listener(self.async_write_ha_state)
        )
