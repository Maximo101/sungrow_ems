"""Opt-in control for Sungrow EMS: the Home Assistant side of the write path.

Created only when "Enable control" is on in the options. It owns:

  * one ``writer.SafeWriter`` (every write goes through it), wired to the
    entry's existing hubs: inverter (WiNet-S, unit from the config), iHM,
    and the AC22 behind the WiNet-S on its own unit ID (same socket);
  * the force-charge state machine (``force_charge.ForceChargeManager``).

After a verified write the value is pushed into the matching coordinator,
so entities update immediately instead of at the next settings read (the
inverter's holding registers are only read every 300 s, [INV] 1.2 rule 6).
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN
from .coordinator import (
    SungrowEmsConfigEntry,
    SungrowEmsCoordinator,
    SungrowEmsRuntimeData,
)
from .force_charge import ForceChargeManager
from .modbus import SungrowModbusError
from .profiles import (
    AC22_RATED_CURRENT_ADDRESS,
    AC22_RATED_CURRENT_SCALE,
    WriteDevice,
    WriteTarget,
)
from .registers import Table
from .writer import Endpoint, SafeWriter, WriteError, WriteRefused, WriteResult

_LOGGER = logging.getLogger(__name__)


class SungrowController:
    """Write access for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SungrowEmsConfigEntry,
        runtime: SungrowEmsRuntimeData,
        *,
        allow_unverified: bool,
        ac22_unit: int,
        force_charge_timeout: int,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.runtime = runtime
        self.ac22_unit = int(ac22_unit)
        inverter = runtime.inverter
        endpoints: dict[WriteDevice, Endpoint] = {
            WriteDevice.INVERTER: Endpoint(inverter.hub, inverter.hub.device_id),
        }
        if runtime.ihm is not None:
            endpoints[WriteDevice.IHM] = Endpoint(
                runtime.ihm.hub, runtime.ihm.hub.device_id
            )
        if self.ac22_unit > 0:
            # Same WiNet-S socket as the inverter, different unit ID: never a
            # second TCP connection.
            endpoints[WriteDevice.AC22] = Endpoint(inverter.hub, self.ac22_unit)
        self.writer = SafeWriter(
            endpoints,
            profile=lambda: inverter.profile,
            healthy=self._healthy,
            allow_unverified=allow_unverified,
        )
        self.writer.on_written = self._on_written
        self.force_charge = ForceChargeManager(
            hass, entry, self, default_timeout_minutes=force_charge_timeout
        )
        self._listeners: list[Callable[[], None]] = []

    # -- lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        """Load the force-charge store; restores settings after a crash."""
        await self.force_charge.async_load()

    async def async_shutdown(self) -> None:
        """Stop an active forced charge and restore its settings (HA stop, unload)."""
        await self.force_charge.async_shutdown()

    # -- helpers for entities and services ------------------------------------

    @property
    def allow_unverified(self) -> bool:
        return self.writer.allow_unverified

    @property
    def has_ihm(self) -> bool:
        return self.runtime.ihm is not None

    @property
    def has_ac22(self) -> bool:
        return self.ac22_unit > 0

    def coordinator_for(self, device: WriteDevice) -> SungrowEmsCoordinator | None:
        if device is WriteDevice.IHM:
            return self.runtime.ihm
        return self.runtime.inverter  # inverter and AC22 share the WiNet-S

    def value(self, key: str) -> float | None:
        """Latest polled value of a register key from either coordinator."""
        for coordinator in self.runtime.coordinators:
            data = coordinator.data
            if data is not None and (v := data.get_float(key)) is not None:
                return v
        return None

    def battery_soc(self) -> float | None:
        """Battery SoC: inverter 13023, else iHM 8163."""
        soc = (
            self.runtime.inverter.data.get_float("mb_battery_soc")
            if self.runtime.inverter.data
            else None
        )
        if (
            soc is None
            and self.runtime.ihm is not None
            and self.runtime.ihm.data is not None
        ):
            soc = self.runtime.ihm.data.get_float("ihm_battery_soc")
        return soc

    def reserve_bounds(self) -> tuple[float, float]:
        """Backup reserve: from Min SoC (13059, when known) to 100 %."""
        min_soc = self.value("mb_min_soc")
        return (float(min_soc) if min_soc is not None else 0.0, 100.0)

    def ihm_power_bounds(self) -> tuple[float, float | None]:
        """iHM 8026: 0 .. battery charge/discharge upper limit (IR 8149)."""
        return (0.0, self.value("ihm_battery_power_limit"))

    def inverter_power_bounds(self) -> tuple[float, float | None]:
        """Inverter 13052: 0 .. BDC rated power (IR 5628), W."""
        return (0.0, self.value("mb_bdc_rated_power"))

    async def async_write(
        self,
        key: str,
        value: float | int,
        *,
        bounds: tuple[float | None, float | None] | None = None,
        priority: bool = False,
    ) -> WriteResult:
        """Write through the SafeWriter, raising Home Assistant errors."""
        try:
            return await self.writer.async_write(
                key, value, bounds=bounds, priority=priority
            )
        except WriteRefused as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="write_refused",
                translation_placeholders={"error": str(err)},
            ) from err
        except WriteError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    async def async_read_ac22(self) -> tuple[float | None, float | None]:
        """(current setpoint A, rated current A) of the AC22, read directly."""
        if not self.has_ac22:
            return None, None
        hub = self.runtime.inverter.hub
        setpoint: float | None = None
        rated: float | None = None
        try:
            setpoint = float(await self.writer.async_read("ac22_charge_current"))
        except (WriteError, SungrowModbusError) as err:
            _LOGGER.debug("AC22 setpoint read failed: %s", err)
        try:
            words = await hub.async_read_raw(
                Table.INPUT, AC22_RATED_CURRENT_ADDRESS, 1, device_id=self.ac22_unit
            )
            if words and words[0] not in (0, 0xFFFF):
                rated = round(words[0] * AC22_RATED_CURRENT_SCALE, 1)
        except SungrowModbusError as err:
            _LOGGER.debug("AC22 rated current read failed: %s", err)
        return setpoint, rated

    def diagnostics(self) -> dict[str, Any]:
        return {
            "ac22_unit": self.ac22_unit,
            "profile": {
                "family": str(self.runtime.inverter.profile.family),
                "model": self.runtime.inverter.profile.model,
                "phases": self.runtime.inverter.profile.phases,
                "write_targets": sorted(self.runtime.inverter.profile.write_targets),
            },
            "writer": self.writer.diagnostics(),
            "force_charge": self.force_charge.diagnostics(),
        }

    @callback
    def async_add_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        """Listen for controller state changes (force-charge state)."""
        self._listeners.append(update)

        def _remove() -> None:
            self._listeners.remove(update)

        return _remove

    @callback
    def async_notify(self) -> None:
        for update in list(self._listeners):
            update()

    # -- internals -----------------------------------------------------------

    def _healthy(self, device: WriteDevice) -> bool:
        coordinator = self.coordinator_for(device)
        return (
            coordinator is not None
            and coordinator.last_update_success
            and coordinator.consecutive_failures == 0
        )

    def _on_written(self, target: WriteTarget, _value: float | int, raw: int) -> None:
        if target.read_key is None:
            return
        coordinator = self.coordinator_for(target.device)
        if coordinator is not None:
            coordinator.async_apply_write(target.read_key, raw & 0xFFFFFFFF)
