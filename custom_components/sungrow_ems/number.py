"""Number entities (control, opt-in): Backup reserve and AC22 charge current.

Only created when "Enable control" is on in the options. Every change goes
through the controller's SafeWriter (bounds, delta, cooldown, daily budget,
read-back).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .controller import SungrowController
from .coordinator import SungrowEmsConfigEntry, SungrowEmsCoordinator
from .entity import SungrowEmsEntity
from .force_charge import Method

PARALLEL_UPDATES = 1
#: The AC22 setpoint is a holding register behind the WiNet-S: read it
#: rarely ([INV] 1.2 rule 6).
SCAN_INTERVAL = timedelta(seconds=300)

BACKUP_RESERVE_KEY = "backup_reserve_soc_control"
AC22_CURRENT_KEY = "ac22_charge_current_control"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SungrowEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    controller = entry.runtime_data.controller
    if controller is None:
        return
    runtime = entry.runtime_data
    entities: list[NumberEntity] = [BackupReserveNumber(runtime.inverter, controller)]
    if controller.has_ac22:
        host = runtime.ihm or runtime.inverter
        entities.append(Ac22CurrentNumber(host, controller))
    async_add_entities(entities)


class BackupReserveNumber(SungrowEmsEntity, NumberEntity):
    """Backup reserve SoC (inverter HR 13100), Min SoC .. 100 %."""

    _attr_icon = "mdi:battery-lock"
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: SungrowEmsCoordinator, controller: SungrowController
    ) -> None:
        super().__init__(coordinator, BACKUP_RESERVE_KEY, "Backup reserve")
        self._controller = controller

    @property
    def native_min_value(self) -> float:
        return self._controller.reserve_bounds()[0]

    @property
    def native_max_value(self) -> float:
        return 100.0

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return data.get_float("mb_backup_reserve_soc") if data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"min_soc": self._controller.value("mb_min_soc")}

    async def async_set_native_value(self, value: float) -> None:
        fc = self._controller.force_charge
        if fc.active and fc.session is not None and fc.session.method == Method.RESERVE:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="force_charge_invalid",
                translation_placeholders={
                    "error": "a reserve-based forced charge is running; cancel it first"
                },
            )
        await self._controller.async_write(
            "backup_reserve_soc", round(value), bounds=self._controller.reserve_bounds()
        )


class Ac22CurrentNumber(SungrowEmsEntity, NumberEntity):
    """AC22 output current setpoint (HR 21203 x0.1 A behind the WiNet-S).

    Unverified register (community map): only writable with "Allow
    unverified writes". Polled directly every 5 minutes.
    """

    _attr_icon = "mdi:ev-station"
    _attr_mode = NumberMode.BOX
    _attr_native_step = 0.1
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_should_poll = True

    def __init__(
        self, coordinator: SungrowEmsCoordinator, controller: SungrowController
    ) -> None:
        super().__init__(coordinator, AC22_CURRENT_KEY, "EV charge current")
        self._controller = controller
        self._setpoint: float | None = None
        self._rated: float | None = None

    @property
    def available(self) -> bool:
        return self._setpoint is not None

    @property
    def native_min_value(self) -> float:
        return 6.0

    @property
    def native_max_value(self) -> float:
        return self._rated or 32.0

    @property
    def native_value(self) -> float | None:
        return self._setpoint

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        voltage = self._controller.value("ihm_meter_voltage_a") or 230.0
        phases = 3 if self._controller.value("ihm_meter_output_type") == 1 else 1
        return {
            "rated_current": self._rated,
            "unit_id": self._controller.ac22_unit,
            "approx_power_w": round(self._setpoint * voltage * phases)
            if self._setpoint is not None
            else None,
            "verified": False,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_schedule_update_ha_state(force_refresh=True)

    async def async_update(self) -> None:
        setpoint, rated = await self._controller.async_read_ac22()
        self._setpoint = setpoint
        if rated:
            self._rated = rated

    def _handle_coordinator_update(self) -> None:
        """Coordinator updates only refresh attributes; the value is polled."""
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        await self._controller.async_write(
            "ac22_charge_current", round(value, 1), bounds=(6.0, self._rated)
        )
        self._setpoint = round(value, 1)
        self.async_write_ha_state()
