"""Data update coordinators for Sungrow EMS.

One coordinator per physical device (inverter, iHomeManager), each driving
its own ``SungrowModbusHub``. They poll independently, so a slow or offline
iHM never delays inverter data and vice versa.

Reliability model:
  * Per-key last-known-good. A register that comes back as a sentinel or out
    of range keeps its previous value. It is only dropped (entity goes
    unavailable) after ``failure_threshold`` consecutive invalid readings.
  * Per-block staleness. A block that fails or is skipped keeps its values
    until it has failed ``failure_threshold`` cycles in a row.
  * Whole-device failure. A cycle where nothing could be read returns the
    previous snapshot. ``UpdateFailed`` (all entities unavailable) is raised
    only after ``failure_threshold`` consecutive failed cycles, or on the very
    first refresh when there is nothing to fall back to.
  * Slow blocks (identity, firmware, settings readback) are read according to
    their ``refresh_interval`` instead of every poll.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_FAILURE_THRESHOLD,
    DEVICE_NAME_IHM,
    DEVICE_NAME_INVERTER,
    DOMAIN,
    MANUFACTURER,
)
from .modbus import ReadResult, SungrowModbusError, SungrowModbusHub
from .registers import (
    IHM_DEVICE_TYPE_CODE,
    REGISTERS_BY_KEY,
    Block,
    Device,
    Value,
    blocks_for,
    format_ihm_protocol_version,
)

if TYPE_CHECKING:
    from .derived import DerivedEngine

_LOGGER = logging.getLogger(__name__)

#: Keys whose change should refresh the device registry entry.
_IDENTITY_KEYS: dict[Device, tuple[str, ...]] = {
    Device.INVERTER: (
        "mb_serial_number",
        "mb_device_type_code",
        "mb_arm_version",
        "mb_dsp_version",
    ),
    Device.IHM: (
        "ihm_software_version",
        "ihm_protocol_version",
        "ihm_device_type_code",
    ),
}


def get_option(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Option value, falling back to entry data, then to the default."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


@dataclass(slots=True, frozen=True)
class SungrowEmsData:
    """Immutable snapshot published to entities after every poll."""

    values: Mapping[str, Value]
    raw: Mapping[str, int]
    updated: Mapping[str, datetime]
    last_success: datetime | None
    consecutive_failures: int
    last_error: str | None
    stale_blocks: tuple[str, ...] = ()
    last_change: datetime | None = None

    def get(self, key: str) -> Value | None:
        """Value for a register key, or None if not available."""
        return self.values.get(key)

    def get_float(self, key: str) -> float | None:
        """Numeric value as float, or None."""
        value = self.values.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    @staticmethod
    def age_of(stamp: datetime) -> float:
        """Seconds since ``stamp``."""
        return (dt_util.utcnow() - stamp).total_seconds()

    def age(self, key: str | None = None) -> float | None:
        """Seconds since this key (or the last successful poll) was refreshed."""
        stamp = self.updated.get(key) if key else self.last_success
        if stamp is None:
            return None
        return (dt_util.utcnow() - stamp).total_seconds()


class SungrowEmsCoordinator(DataUpdateCoordinator[SungrowEmsData]):
    """Polls one Sungrow device through its hub."""

    device: Device
    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        hub: SungrowModbusHub,
        *,
        device: Device,
        scan_interval: float,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {device}",
            update_interval=timedelta(seconds=float(scan_interval)),
            always_update=True,
        )
        self.hub = hub
        self.device = device
        self.failure_threshold = max(1, int(failure_threshold))
        self.blocks: tuple[Block, ...] = blocks_for(device)
        self._block_keys: dict[str, tuple[str, ...]] = {
            b.name: tuple(r.key for r in b.registers) for b in self.blocks
        }

        self._values: dict[str, Value] = {}
        self._raw: dict[str, int] = {}
        self._updated: dict[str, datetime] = {}
        self._invalid_streak: dict[str, int] = {}
        self._block_last_read: dict[str, float] = {}
        self._block_fail_streak: dict[str, int] = {}

        self._failures = 0
        self._last_success: datetime | None = None
        self._last_error: str | None = None
        self._identity_seen: dict[str, Value] = {}
        self._last_change: datetime | None = None
        self.last_read: ReadResult | None = None

    # -- public helpers ----------------------------------------------------

    @property
    def consecutive_failures(self) -> int:
        return self._failures

    @property
    def device_identifier(self) -> tuple[str, str]:
        """Device registry identifier for this coordinator's device."""
        base = self.config_entry.unique_id or self.config_entry.entry_id
        return (DOMAIN, f"{base}_{self.device}")

    @property
    def device_info(self) -> DeviceInfo:
        """DeviceInfo built from whatever identity registers have been read."""
        values = self._values
        if self.device is Device.INVERTER:
            model_reg = REGISTERS_BY_KEY["mb_device_type_code"]
            code = self._raw.get("mb_device_type_code")
            model = model_reg.label(code) if code is not None else None
            return DeviceInfo(
                identifiers={self.device_identifier},
                name=DEVICE_NAME_INVERTER,
                manufacturer=MANUFACTURER,
                model=model or "Hybrid inverter",
                model_id=f"0x{code:04X}" if code is not None else None,
                serial_number=_as_str(values.get("mb_serial_number"))
                or self.config_entry.unique_id,
                sw_version=_as_str(values.get("mb_arm_version")),
                hw_version=_as_str(values.get("mb_dsp_version")),
            )
        code = self._raw.get("ihm_device_type_code")
        return DeviceInfo(
            identifiers={self.device_identifier},
            name=DEVICE_NAME_IHM,
            manufacturer=MANUFACTURER,
            model="iHomeManager",
            model_id=f"0x{code:04X}"
            if code is not None
            else f"0x{IHM_DEVICE_TYPE_CODE:04X}",
            sw_version=_as_str(values.get("ihm_software_version")),
            hw_version=format_ihm_protocol_version(
                self._raw.get("ihm_protocol_version")  # type: ignore[arg-type]
            ),
        )

    def diagnostics(self) -> dict[str, Any]:
        """Snapshot for diagnostics."""
        last = self.last_read
        return {
            "device": str(self.device),
            "update_interval": self.update_interval.total_seconds()
            if self.update_interval
            else None,
            "failure_threshold": self.failure_threshold,
            "consecutive_failures": self._failures,
            "last_success": self._last_success.isoformat()
            if self._last_success
            else None,
            "last_error": self._last_error,
            "block_fail_streak": dict(self._block_fail_streak),
            "invalid_streak": dict(self._invalid_streak),
            "last_read": None
            if last is None
            else {
                "blocks_ok": list(last.blocks_ok),
                "blocks_failed": dict(last.blocks_failed),
                "blocks_skipped": list(last.blocks_skipped),
                "unsupported_spans": list(last.unsupported_spans),
                "requests": last.requests,
                "duration": round(last.duration, 3),
            },
            "hub": self.hub.diagnostics(),
        }

    # -- polling -----------------------------------------------------------

    def _due_blocks(self, now: float) -> list[Block]:
        due: list[Block] = []
        for block in self.blocks:
            last = self._block_last_read.get(block.name)
            if (
                last is None
                or block.refresh_interval is None
                or now - last >= block.refresh_interval
            ):
                due.append(block)
        return due

    async def _async_update_data(self) -> SungrowEmsData:
        now = time.monotonic()
        due = self._due_blocks(now)
        try:
            result = await self.hub.async_read_blocks(due)
        except SungrowModbusError as err:
            return self._handle_failure(err)

        self.last_read = result
        decoded = result.decode(due)
        stamp = dt_util.utcnow()

        if any(self._values.get(k) != v for k, v in decoded.values.items()):
            # Shows how often the device itself refreshes what it serves
            # (a successful poll can return the same cached values).
            self._last_change = stamp
        for key, value in decoded.values.items():
            self._values[key] = value
            self._updated[key] = stamp
            self._invalid_streak.pop(key, None)
        self._raw.update(decoded.raw)

        for key in decoded.invalid:
            streak = self._invalid_streak.get(key, 0) + 1
            self._invalid_streak[key] = streak
            if streak >= self.failure_threshold and key in self._values:
                _LOGGER.debug(
                    "%s: %s invalid for %d polls, marking unavailable",
                    self.name,
                    key,
                    streak,
                )
                self._drop_key(key)

        for name in result.blocks_ok:
            self._block_last_read[name] = now
            self._block_fail_streak.pop(name, None)
        for name in (*result.blocks_failed, *result.blocks_skipped):
            streak = self._block_fail_streak.get(name, 0) + 1
            self._block_fail_streak[name] = streak
            if streak == self.failure_threshold:
                _LOGGER.warning(
                    "%s: block %s failed %d polls in a row, its values are now unavailable",
                    self.name,
                    name,
                    streak,
                )
            if streak >= self.failure_threshold:
                for key in self._block_keys.get(name, ()):
                    self._drop_key(key)

        if self._failures:
            _LOGGER.info(
                "%s: recovered after %d failed polls", self.name, self._failures
            )
        self._failures = 0
        self._last_success = stamp
        self._last_error = (
            "; ".join(f"{k}: {v}" for k, v in result.blocks_failed.items()) or None
        )
        self._async_maybe_update_device_registry()
        return self._snapshot()

    def _handle_failure(self, err: Exception) -> SungrowEmsData:
        self._failures += 1
        self._last_error = str(err)
        if self.data is None or self._failures >= self.failure_threshold:
            raise UpdateFailed(
                f"{self.name}: {self._failures} consecutive failed polls: {err}"
            ) from err
        if self._failures == 1:
            _LOGGER.info(
                "%s: poll failed, keeping last known values (%s)", self.name, err
            )
        else:
            _LOGGER.debug(
                "%s: poll failed %d/%d: %s",
                self.name,
                self._failures,
                self.failure_threshold,
                err,
            )
        return self._snapshot()

    def _drop_key(self, key: str) -> None:
        self._values.pop(key, None)
        self._raw.pop(key, None)
        self._updated.pop(key, None)

    def _snapshot(self) -> SungrowEmsData:
        return SungrowEmsData(
            values=dict(self._values),
            raw=dict(self._raw),
            updated=dict(self._updated),
            last_success=self._last_success,
            consecutive_failures=self._failures,
            last_error=self._last_error,
            stale_blocks=tuple(
                name for name, streak in self._block_fail_streak.items() if streak
            ),
            last_change=self._last_change,
        )

    @callback
    def _async_maybe_update_device_registry(self) -> None:
        """Push model / serial / firmware into the device registry when they change."""
        current = {
            k: self._values[k] for k in _IDENTITY_KEYS[self.device] if k in self._values
        }
        if not current or current == self._identity_seen:
            return
        self._identity_seen = current
        registry = dr.async_get(self.hass)
        lookup = getattr(registry, "async_get_device_by_identifier", None)
        if lookup is not None:
            device = lookup(self.device_identifier, self.config_entry.entry_id)
        else:  # Home Assistant releases before the per-entry lookup existed
            device = registry.async_get_device(identifiers={self.device_identifier})
        if device is None:
            return
        info = self.device_info
        registry.async_update_device(
            device.id,
            model=info.get("model"),
            model_id=info.get("model_id"),
            serial_number=info.get("serial_number"),
            sw_version=info.get("sw_version"),
            hw_version=info.get("hw_version"),
        )


class SungrowInverterCoordinator(SungrowEmsCoordinator):
    """Coordinator for the SH-series hybrid inverter."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        hub: SungrowModbusHub,
        *,
        scan_interval: float,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    ) -> None:
        super().__init__(
            hass,
            entry,
            hub,
            device=Device.INVERTER,
            scan_interval=scan_interval,
            failure_threshold=failure_threshold,
        )


class SungrowIhmCoordinator(SungrowEmsCoordinator):
    """Coordinator for the iHomeManager (system master, grid meter, EV charger)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        hub: SungrowModbusHub,
        *,
        scan_interval: float,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    ) -> None:
        super().__init__(
            hass,
            entry,
            hub,
            device=Device.IHM,
            scan_interval=scan_interval,
            failure_threshold=failure_threshold,
        )


@dataclass(slots=True)
class SungrowEmsRuntimeData:
    """Everything a config entry owns while loaded (``entry.runtime_data``)."""

    inverter: SungrowInverterCoordinator
    ihm: SungrowIhmCoordinator | None
    hubs: list[SungrowModbusHub] = field(default_factory=list)
    derived: DerivedEngine | None = None

    @property
    def coordinators(self) -> list[SungrowEmsCoordinator]:
        out: list[SungrowEmsCoordinator] = [self.inverter]
        if self.ihm is not None:
            out.append(self.ihm)
        return out

    def coordinator_for(self, device: Device) -> SungrowEmsCoordinator | None:
        return self.inverter if device is Device.INVERTER else self.ihm


type SungrowEmsConfigEntry = ConfigEntry[SungrowEmsRuntimeData]


def _as_str(value: Value | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
