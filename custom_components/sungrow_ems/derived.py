"""Native ports of the configuration.yaml energy templates and utility meters.

Replaces (see configuration.yaml):
  template sensors  Grid Import Safe, Grid Export Safe, Daily Consumption,
                    Daily Consumption from PV, Daily Export to Grid %,
                    Battery Charged from PV %, Daily Consumption from PV %,
                    Self Sufficient, Self Sufficient %, Purchased %,
                    System Status, Active Export to Grid, Active Import from
                    Grid, Battery Charging Power, Battery Discharging Power,
                    Daily Battery Charge from Grid
  integration       Total Battery Charge Cumulative (Riemann, left method)
  utility_meter     Daily Import (iHM), Daily Export (iHM),
                    Daily Battery Charge Total

Changes from the YAML, all deliberate:
  * Grid Import/Export Safe no longer latch forever. An out-of-range reading
    is held back, but after ``spike_rebaseline_count`` consecutive readings
    that agree with each other the new level is accepted.
  * Small backwards steps on the Safe meters are ignored (monotonic), so
    Home Assistant never sees a false meter reset.
  * Every percentage is zero-guarded (Battery Charged from PV % and Daily
    Consumption from PV % divided by PV without a guard).
  * Inputs that are unavailable make the result unavailable instead of being
    treated as 0 (``| float(0)``), which caused dips to 0 in the history.
  * System Status decodes register 13001 as the bitfield it is. The existing
    labels are kept for the codes the YAML listed (matched on bits 0-3);
    other combinations get a composed label instead of "Unknown".
  * Daily sensors that can go down during the day use state_class TOTAL with
    last_reset at local midnight (the YAML used measurement or
    total_increasing, which corrupts long-term statistics).
  * Daily Battery Charge from Grid uses the inverter's own daily charge
    counter (13040) when available, and the integrated meter otherwise.

  * PV split. Battery Charged from PV %, Daily Consumption from PV % and
    Daily Export to Grid % are the shares of today's PV that went to the
    battery, the home and the grid. They add up to exactly 100 (largest-
    remainder rounding). PV to grid prefers the inverter's PV-only export
    counter (13005) over the iHM grid export, and is capped at the PV left
    after battery charging; PV to home is the remainder.
  * Self Sufficient = PV to home + battery to home (discharge x efficiency,
    minus battery energy exported), capped at Daily Consumption. Self
    Sufficient % and Purchased % are shares of Daily Consumption and add up
    to 100.
  * Battery capacity has no hard-coded fallback: options override, then iHM
    8147, inverter 13039, inverter 5639, otherwise unavailable.

Kept as in the YAML (flag for a later decision): Daily Consumption subtracts
only the PV share of battery charging and applies the discharge efficiency.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
import logging
import time
from typing import Any, Literal

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_SPIKE_MAX_DAILY,
    CONF_SPIKE_MAX_JUMP,
    CONF_SPIKE_REBASELINE_COUNT,
    DEFAULT_BATTERY_CAPACITY_OPTION,
    DEFAULT_DISCHARGE_EFFICIENCY,
    DEFAULT_SPIKE_MAX_DAILY,
    DEFAULT_SPIKE_MAX_JUMP,
    DEFAULT_SPIKE_REBASELINE_COUNT,
    DOMAIN,
    INTEGRATION_MAX_GAP_SECONDS,
    MAX_BATTERY_CAPACITY,
)
from .coordinator import (
    SungrowEmsConfigEntry,
    SungrowEmsCoordinator,
    SungrowIhmCoordinator,
    SungrowInverterCoordinator,
    get_option,
)
from .entity import make_unique_id
from .registers import POWER_FLOW_BITS, Device, decode_bits

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stateful calculators (pure Python)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SpikeFilter:
    """Grid Import/Export Safe: a cumulative meter that ignores corrupt jumps.

    * A reading at or below ``min_valid`` is ignored (YAML availability: > 1).
    * The first valid reading becomes the baseline.
    * A step forward of 0..max_jump is accepted.
    * A small step backwards (up to max_jump) is ignored: the value stays put.
    * Anything else is an outlier. Outliers are collected; once
      ``rebaseline_count`` consecutive outliers agree with each other (each
      0..max_jump above the previous one), the newest becomes the baseline.
      Any normal reading in between clears the collection.
    """

    max_jump: float
    rebaseline_count: int
    min_valid: float = 1.0
    value: float | None = None
    candidates: list[float] = field(default_factory=list)
    last_raw: float | None = None
    rejected: int = 0
    rebaselines: int = 0

    def update(self, raw: float | None) -> bool:
        """Feed a raw reading. Returns True if ``value`` changed."""
        if raw is None:
            return False
        raw = float(raw)
        self.last_raw = raw
        if raw <= self.min_valid:
            return False
        if self.value is None:
            self.value = raw
            self.candidates.clear()
            return True

        delta = raw - self.value
        if 0 <= delta <= self.max_jump:
            self.candidates.clear()
            if delta == 0:
                return False
            self.value = raw
            return True
        if -self.max_jump <= delta < 0:
            self.candidates.clear()
            return False

        self.rejected += 1
        if self.candidates and 0 <= raw - self.candidates[-1] <= self.max_jump:
            self.candidates.append(raw)
        else:
            self.candidates = [raw]
        if len(self.candidates) >= self.rebaseline_count:
            _LOGGER.warning(
                "Accepting new meter baseline %.3f kWh (was %.3f) after %d consistent readings",
                raw,
                self.value,
                len(self.candidates),
            )
            self.value = raw
            self.candidates.clear()
            self.rebaselines += 1
            return True
        _LOGGER.debug(
            "Rejected meter reading %.3f (current %.3f, %d/%d towards re-baseline)",
            raw,
            self.value,
            len(self.candidates),
            self.rebaseline_count,
        )
        return False


@dataclass(slots=True)
class DailyMeter:
    """utility_meter with ``cycle: daily`` over a cumulative source.

    Adds positive source deltas to today's total. A source decrease is treated
    as a source reset (re-based, nothing added). A single delta above
    ``max_delta`` is skipped, so a re-baselined Safe meter cannot dump a
    multi-day jump into today.
    """

    max_delta: float
    value: float = 0.0
    last_source: float | None = None
    period: date | None = None
    skipped: int = 0

    def roll(self, today: date) -> bool:
        """Start a new day if needed. Returns True on reset."""
        if self.period == today:
            return False
        self.period = today
        self.value = 0.0
        return True

    def update(self, source: float | None, today: date) -> bool:
        changed = self.roll(today)
        if source is None:
            return changed
        source = float(source)
        if self.last_source is None:
            self.last_source = source
            return changed
        delta = source - self.last_source
        self.last_source = source
        if delta <= 0:
            return changed
        if delta > self.max_delta:
            self.skipped += 1
            _LOGGER.warning(
                "Daily meter skipped an implausible step of %.3f kWh (limit %.1f)",
                delta,
                self.max_delta,
            )
            return changed
        self.value += delta
        return True


@dataclass(slots=True)
class LeftRiemann:
    """``integration`` sensor, method: left, unit_prefix: k. Power in W, energy in kWh.

    Intervals longer than ``max_gap`` seconds are skipped (not integrated), so
    downtime never contributes "last power x outage length".
    """

    max_gap: float
    value: float = 0.0
    last_power: float | None = None
    last_time: float | None = None

    def update(self, power_w: float | None, now: float) -> bool:
        changed = False
        if self.last_time is not None and self.last_power is not None:
            elapsed = now - self.last_time
            if 0 < elapsed <= self.max_gap and self.last_power > 0:
                self.value += self.last_power * elapsed / 3_600_000
                changed = True
        if power_w is None:
            self.last_power = None
            self.last_time = None
        else:
            self.last_power = float(power_w)
            self.last_time = now
        return changed


# ---------------------------------------------------------------------------
# Formulas (pure Python)
# ---------------------------------------------------------------------------

#: Labels from the YAML System Status template, keyed by bits 0-3 of 13001.
SYSTEM_STATUS_LABELS: Mapping[int, str] = {
    0: "Standby",
    2: "Battery Charging (Grid)",
    3: "PV & Battery Charging",
    4: "Battery Discharging",
    8: "Grid Import (Active Load)",
    9: "Excess Solar / Load Active",
    11: "PV & Battery Charging (Active Load)",
    12: "Battery Discharging to Load",
    13: "Grid/Battery Hybrid Mode",
}
SYSTEM_STATUS_OFFLINE = "Offline / Unavailable"
_COMPOSED_PARTS = (
    (0, "PV Generating"),
    (1, "Battery Charging"),
    (2, "Battery Discharging"),
    (3, "Load Active"),
)


def system_status(code: int | None) -> tuple[str, str, dict[str, Any]]:
    """(label, icon, attributes) for the System Status sensor."""
    if code is None:
        return SYSTEM_STATUS_OFFLINE, "mdi:lan-disconnect", {}
    code = int(code)
    core = code & 0x0F
    flags = decode_bits(code, POWER_FLOW_BITS)
    label = SYSTEM_STATUS_LABELS.get(core) or " / ".join(
        text for bit, text in _COMPOSED_PARTS if (core >> bit) & 1
    )
    if core == 0:
        icon = "mdi:power-sleep"
    elif core in (2, 3, 11):
        icon = "mdi:battery-charging"
    elif core in (4, 12, 13):
        icon = "mdi:battery-arrow-down"
    elif core in (1, 9):
        icon = "mdi:solar-power"
    elif core == 8:
        icon = "mdi:transmission-tower-import"
    else:
        icon = "mdi:power-plug"
    return label, icon, {"code": code, "core_code": core, **flags}


def _f(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _split_pct(parts: Sequence[float], total: float) -> list[int]:
    """Whole percentages of ``total`` that always add up to exactly 100.

    Largest-remainder rounding: floor every share, then hand the missing
    points to the shares with the biggest fractional parts. Plain rounding of
    28.2 / 70.4 / 1.4 would give 28 + 70 + 1 = 99.
    """
    if total <= 0:
        return [0] * len(parts)
    raw = [max(p, 0.0) / total * 100 for p in parts]
    floors = [int(r) for r in raw]
    missing = 100 - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - floors[i], reverse=True)
    for i in order[: max(missing, 0)]:
        floors[i] += 1
    return floors


def compute_derived(
    inv: Mapping[str, Any],
    ihm: Mapping[str, Any],
    meters: Mapping[str, float | None],
    *,
    efficiency: float,
    max_daily: float,
    capacity: float | None,
) -> dict[str, Any]:
    """All non-stateful derived values. A value is None when an input is missing."""
    out: dict[str, Any] = {}

    grid_kw = _f(ihm.get("ihm_grid_active_power"))  # + import, - export
    out["active_import_power"] = (
        None if grid_kw is None else (round(grid_kw * 1000) if grid_kw > 0 else 0)
    )
    out["active_export_power"] = (
        None if grid_kw is None else (round(-grid_kw * 1000) if grid_kw < 0 else 0)
    )

    battery_w = _f(inv.get("mb_battery_power"))  # + discharge, - charge
    out["battery_discharging_power"] = (
        None if battery_w is None else max(battery_w, 0.0)
    )
    out["battery_charging_power"] = None if battery_w is None else max(-battery_w, 0.0)

    pv = _f(inv.get("mb_daily_pv_generation"))
    charge_pv = _f(inv.get("mb_daily_battery_charge_from_pv"))
    discharge = _f(inv.get("mb_daily_battery_discharge"))
    imp = meters.get("daily_import")
    exp = meters.get("daily_export")

    charge_total = _f(inv.get("mb_daily_battery_charge"))
    if charge_total is None:
        charge_total = meters.get("daily_battery_charge_total")
    out["daily_battery_charge_from_grid"] = (
        None
        if charge_total is None or charge_pv is None
        else round(max(charge_total - charge_pv, 0.0), 2)
    )

    imp_c = None if imp is None else (imp if imp < max_daily else 0.0)
    exp_c = None if exp is None else (exp if exp < max_daily else 0.0)

    # --- Where today's PV went: battery / home / grid (adds up to 100 %) ----
    # PV to grid prefers the inverter's own PV-only export counter (13005),
    # which excludes battery energy sent to the grid. The iHM grid export is
    # the fallback. Either way it is capped at what PV had left after
    # charging the battery, and PV to home is the remainder, so the three
    # shares always add up to the day's PV generation.
    pv_export_src = _f(inv.get("mb_daily_export_from_pv"))
    if pv_export_src is None:
        pv_export_src = exp_c
    pv_to_battery = pv_to_grid = pv_to_home = None
    if pv is not None and charge_pv is not None and pv_export_src is not None:
        pv_to_battery = min(max(charge_pv, 0.0), max(pv, 0.0))
        pv_left = max(pv - pv_to_battery, 0.0)
        pv_to_grid = min(max(pv_export_src, 0.0), pv_left)
        pv_to_home = pv_left - pv_to_grid
    out["daily_consumption_from_pv"] = (
        None if pv_to_home is None else round(pv_to_home, 2)
    )
    if pv_to_battery is None or pv_to_home is None or pv_to_grid is None or pv is None:
        out["battery_charged_from_pv_pct"] = None
        out["daily_consumption_from_pv_pct"] = None
        out["daily_export_pct"] = None
    else:
        battery_pct, home_pct, grid_pct = _split_pct(
            (pv_to_battery, pv_to_home, pv_to_grid), pv
        )
        out["battery_charged_from_pv_pct"] = battery_pct
        out["daily_consumption_from_pv_pct"] = home_pct
        out["daily_export_pct"] = grid_pct

    # --- Daily consumption (energy balance, as in the YAML) -----------------
    if None in (pv, imp_c, exp_c, charge_pv, discharge):
        consumption = None
    else:
        assert pv is not None and imp_c is not None and exp_c is not None
        assert charge_pv is not None and discharge is not None
        net = pv + imp_c - exp_c - charge_pv + discharge * efficiency
        consumption = max(net, 0.0)
    out["daily_consumption"] = None if consumption is None else round(consumption, 2)

    # --- Self sufficiency: consumption covered by PV and battery ------------
    # Battery to home = discharge (after efficiency) minus any battery energy
    # that went to the grid instead (grid export beyond the PV share). With
    # that, PV to home + battery to home + import = daily consumption, so
    # Self sufficient never exceeds consumption and Self sufficient % plus
    # Purchased % is always 100.
    self_kwh = None
    if (
        consumption is not None
        and pv_to_home is not None
        and pv_to_grid is not None
        and discharge is not None
        and exp_c is not None
    ):
        battery_export = max(exp_c - pv_to_grid, 0.0)
        battery_to_home = max(discharge * efficiency - battery_export, 0.0)
        self_kwh = min(pv_to_home + battery_to_home, consumption)
    out["self_sufficient"] = None if self_kwh is None else round(self_kwh, 2)
    if self_kwh is None or consumption is None:
        out["self_sufficient_pct"] = None
        out["purchased_pct"] = None
    elif consumption <= 0:
        out["self_sufficient_pct"] = 0
        out["purchased_pct"] = 0
    else:
        self_pct, purchased_pct = _split_pct(
            (self_kwh, consumption - self_kwh), consumption
        )
        out["self_sufficient_pct"] = self_pct
        out["purchased_pct"] = purchased_pct

    soc = _f(inv.get("mb_battery_soc"))
    if soc is None:
        soc = _f(ihm.get("ihm_battery_soc"))
    out["battery_stored_energy"] = (
        None if soc is None or capacity is None else round(soc / 100 * capacity, 2)
    )

    code = inv.get("mb_power_flow_status")
    out["system_status"] = system_status(None if code is None else int(code))
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

#: Stateful component keys (also entity keys).
GRID_IMPORT_SAFE = "grid_import_safe"
GRID_EXPORT_SAFE = "grid_export_safe"
DAILY_IMPORT = "daily_import"
DAILY_EXPORT = "daily_export"
BATTERY_CHARGE_CUMULATIVE = "battery_charge_cumulative"
DAILY_BATTERY_CHARGE_TOTAL = "daily_battery_charge_total"


class DerivedEngine:
    """Runs the stateful calculators and formulas on every coordinator update.

    Stateful components start "not ready" and ignore updates until their
    entity has restored its previous state (or the entity is disabled in the
    registry, in which case they start fresh). That way a restart never feeds
    a reading into an empty meter before the saved value is back.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SungrowEmsConfigEntry,
        inverter: SungrowInverterCoordinator,
        ihm: SungrowIhmCoordinator | None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.inverter = inverter
        self.ihm = ihm

        max_jump = float(get_option(entry, CONF_SPIKE_MAX_JUMP, DEFAULT_SPIKE_MAX_JUMP))
        rebaseline = int(
            get_option(
                entry, CONF_SPIKE_REBASELINE_COUNT, DEFAULT_SPIKE_REBASELINE_COUNT
            )
        )
        self.max_daily = float(
            get_option(entry, CONF_SPIKE_MAX_DAILY, DEFAULT_SPIKE_MAX_DAILY)
        )
        self.efficiency = float(
            get_option(entry, CONF_DISCHARGE_EFFICIENCY, DEFAULT_DISCHARGE_EFFICIENCY)
        )
        self.capacity_option = float(
            get_option(entry, CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY_OPTION)
        )

        self.import_safe = SpikeFilter(max_jump=max_jump, rebaseline_count=rebaseline)
        self.export_safe = SpikeFilter(max_jump=max_jump, rebaseline_count=rebaseline)
        self.daily_import = DailyMeter(max_delta=max_jump)
        self.daily_export = DailyMeter(max_delta=max_jump)
        self.charge_cumulative = LeftRiemann(max_gap=INTEGRATION_MAX_GAP_SECONDS)
        self.daily_charge_total = DailyMeter(max_delta=max_jump)

        self._ready: set[str] = set()
        self._listeners: list[Callable[[], None]] = []
        self.values: dict[str, Any] = {}
        self.day_start: datetime = dt_util.start_of_local_day()

    # -- lifecycle -----------------------------------------------------------

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Subscribe to both coordinators and local midnight. Returns the unsubscriber."""
        unsubs: list[CALLBACK_TYPE] = [
            self.inverter.async_add_listener(self._async_handle_update)
        ]
        if self.ihm is not None:
            unsubs.append(self.ihm.async_add_listener(self._async_handle_update))
        unsubs.append(
            async_track_time_change(
                self.hass, self._async_midnight, hour=0, minute=0, second=0
            )
        )
        self._update()

        @callback
        def _stop() -> None:
            for unsub in unsubs:
                unsub()
            self._listeners.clear()

        return _stop

    @callback
    def add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def is_ready(self, key: str) -> bool:
        return key in self._ready

    @callback
    def mark_ready(self, key: str) -> None:
        """A stateful component has its restored state (or starts fresh)."""
        if key in self._ready:
            return
        self._ready.add(key)
        self._async_handle_update()

    @property
    def has_ihm(self) -> bool:
        return self.ihm is not None

    @property
    def battery_capacity(self) -> float | None:
        """Usable capacity in kWh, or None when nothing reports it.

        Order: the options override (when above 0), then what the hardware
        reports: iHM total rated capacity (8147), inverter battery capacity
        (13039), inverter battery capacity (5639). No hard-coded fallback, so
        Battery stored energy shows unavailable rather than a wrong number on
        a system with a different battery.
        """
        if self.capacity_option > 0:
            return self.capacity_option
        candidates = (
            (self._inputs(self.ihm), "ihm_total_rated_battery_capacity"),
            (self._inputs(self.inverter), "mb_battery_capacity_13039"),
            (self._inputs(self.inverter), "mb_battery_capacity"),
        )
        for values, key in candidates:
            capacity = _f(values.get(key))
            if capacity is not None and 0 < capacity <= MAX_BATTERY_CAPACITY:
                return capacity
        return None

    # -- updates -------------------------------------------------------------

    @callback
    def _async_handle_update(self) -> None:
        self._update()
        for listener in list(self._listeners):
            listener()

    @callback
    def _async_midnight(self, _now: datetime) -> None:
        self._async_handle_update()

    @staticmethod
    def _inputs(coordinator: SungrowEmsCoordinator | None) -> Mapping[str, Any]:
        if (
            coordinator is None
            or coordinator.data is None
            or not coordinator.last_update_success
        ):
            return {}
        return coordinator.data.values

    def _update(self) -> None:
        today = dt_util.now().date()
        self.day_start = dt_util.start_of_local_day(today)
        inv = self._inputs(self.inverter)
        ihm = self._inputs(self.ihm)
        ready = self._ready

        if GRID_IMPORT_SAFE in ready:
            self.import_safe.update(_f(ihm.get("ihm_grid_import_energy")))
        if GRID_EXPORT_SAFE in ready:
            self.export_safe.update(_f(ihm.get("ihm_grid_export_energy")))
        if DAILY_IMPORT in ready and GRID_IMPORT_SAFE in ready:
            self.daily_import.update(self.import_safe.value, today)
        if DAILY_EXPORT in ready and GRID_EXPORT_SAFE in ready:
            self.daily_export.update(self.export_safe.value, today)

        battery_w = _f(inv.get("mb_battery_power"))
        charging_w = None if battery_w is None else max(-battery_w, 0.0)
        if BATTERY_CHARGE_CUMULATIVE in ready:
            self.charge_cumulative.update(charging_w, time.monotonic())
        if DAILY_BATTERY_CHARGE_TOTAL in ready and BATTERY_CHARGE_CUMULATIVE in ready:
            self.daily_charge_total.update(self.charge_cumulative.value, today)

        meters: dict[str, float | None] = {
            DAILY_IMPORT: self.meter_value(DAILY_IMPORT),
            DAILY_EXPORT: self.meter_value(DAILY_EXPORT),
            DAILY_BATTERY_CHARGE_TOTAL: self.meter_value(DAILY_BATTERY_CHARGE_TOTAL),
        }
        self.values = compute_derived(
            inv,
            ihm,
            meters,
            efficiency=self.efficiency,
            max_daily=self.max_daily,
            capacity=self.battery_capacity,
        )

    def meter_value(self, key: str) -> float | None:
        """Current value of a stateful component, None until it is ready."""
        if key not in self._ready:
            return None
        match key:
            case "grid_import_safe":
                return self.import_safe.value
            case "grid_export_safe":
                return self.export_safe.value
            case "daily_import":
                return self.daily_import.value if self.daily_import.period else None
            case "daily_export":
                return self.daily_export.value if self.daily_export.period else None
            case "battery_charge_cumulative":
                return self.charge_cumulative.value
            case "daily_battery_charge_total":
                return (
                    self.daily_charge_total.value
                    if self.daily_charge_total.period
                    else None
                )
        raise KeyError(key)


# ---------------------------------------------------------------------------
# Entity descriptions
# ---------------------------------------------------------------------------

Kind = Literal["computed", "status", "spike", "meter", "integral"]


@dataclass(frozen=True, kw_only=True)
class SungrowDerivedSensorEntityDescription(SensorEntityDescription):
    """Derived sensor description."""

    device: Device
    kind: Kind = "computed"
    requires_ihm: bool = False
    daily_total: bool = False  # state_class TOTAL with last_reset at local midnight


_ENERGY: dict[str, Any] = {
    "device_class": SensorDeviceClass.ENERGY,
    "native_unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
    "suggested_display_precision": 2,
}
_POWER: dict[str, Any] = {
    "device_class": SensorDeviceClass.POWER,
    "state_class": SensorStateClass.MEASUREMENT,
    "native_unit_of_measurement": UnitOfPower.WATT,
    "suggested_display_precision": 0,
}
_PERCENT: dict[str, Any] = {
    "state_class": SensorStateClass.MEASUREMENT,
    "native_unit_of_measurement": PERCENTAGE,
    "icon": "mdi:percent",
}

DERIVED_SENSORS: tuple[SungrowDerivedSensorEntityDescription, ...] = (
    # --- iHomeManager device ------------------------------------------------
    SungrowDerivedSensorEntityDescription(
        key=GRID_IMPORT_SAFE,
        name="Grid import safe",
        device=Device.IHM,
        kind="spike",
        requires_ihm=True,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:transmission-tower-import",
        **_ENERGY,
    ),
    SungrowDerivedSensorEntityDescription(
        key=GRID_EXPORT_SAFE,
        name="Grid export safe",
        device=Device.IHM,
        kind="spike",
        requires_ihm=True,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:transmission-tower-export",
        **_ENERGY,
    ),
    SungrowDerivedSensorEntityDescription(
        key=DAILY_IMPORT,
        name="Daily import",
        device=Device.IHM,
        kind="meter",
        requires_ihm=True,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:transmission-tower-import",
        **_ENERGY,
    ),
    SungrowDerivedSensorEntityDescription(
        key=DAILY_EXPORT,
        name="Daily export",
        device=Device.IHM,
        kind="meter",
        requires_ihm=True,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:transmission-tower-export",
        **_ENERGY,
    ),
    SungrowDerivedSensorEntityDescription(
        key="active_import_power",
        name="Active import from grid",
        device=Device.IHM,
        requires_ihm=True,
        icon="mdi:transmission-tower-import",
        **_POWER,
    ),
    SungrowDerivedSensorEntityDescription(
        key="active_export_power",
        name="Active export to grid",
        device=Device.IHM,
        requires_ihm=True,
        icon="mdi:transmission-tower-export",
        **_POWER,
    ),
    # --- Inverter device ----------------------------------------------------
    SungrowDerivedSensorEntityDescription(
        key="battery_charging_power",
        name="Battery charging power",
        device=Device.INVERTER,
        icon="mdi:battery-arrow-up",
        **_POWER,
    ),
    SungrowDerivedSensorEntityDescription(
        key="battery_discharging_power",
        name="Battery discharging power",
        device=Device.INVERTER,
        icon="mdi:battery-arrow-down",
        **_POWER,
    ),
    SungrowDerivedSensorEntityDescription(
        key=BATTERY_CHARGE_CUMULATIVE,
        name="Total battery charge cumulative",
        device=Device.INVERTER,
        kind="integral",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:battery-plus",
        **_ENERGY,
    ),
    SungrowDerivedSensorEntityDescription(
        key=DAILY_BATTERY_CHARGE_TOTAL,
        name="Daily battery charge total",
        device=Device.INVERTER,
        kind="meter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:battery-plus",
        **_ENERGY,
    ),
    SungrowDerivedSensorEntityDescription(
        key="daily_battery_charge_from_grid",
        name="Daily battery charge from grid",
        device=Device.INVERTER,
        daily_total=True,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:battery-charging-wireless",
        **_ENERGY,
    ),
    SungrowDerivedSensorEntityDescription(
        key="daily_consumption",
        name="Daily consumption",
        device=Device.INVERTER,
        requires_ihm=True,
        daily_total=True,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:home-lightning-bolt",
        **_ENERGY,
    ),
    SungrowDerivedSensorEntityDescription(
        key="daily_consumption_from_pv",
        name="Daily consumption from PV",
        device=Device.INVERTER,
        requires_ihm=False,
        daily_total=True,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:solar-power",
        **_ENERGY,
    ),
    SungrowDerivedSensorEntityDescription(
        key="self_sufficient",
        name="Self sufficient",
        device=Device.INVERTER,
        requires_ihm=True,
        daily_total=True,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:home-battery",
        **_ENERGY,
    ),
    SungrowDerivedSensorEntityDescription(
        key="self_sufficient_pct",
        name="Self sufficient percentage",
        device=Device.INVERTER,
        requires_ihm=True,
        **_PERCENT,
    ),
    SungrowDerivedSensorEntityDescription(
        key="purchased_pct",
        name="Purchased percentage",
        device=Device.INVERTER,
        requires_ihm=True,
        **_PERCENT,
    ),
    SungrowDerivedSensorEntityDescription(
        key="daily_export_pct",
        name="Daily export to grid percentage",
        device=Device.INVERTER,
        requires_ihm=False,
        **_PERCENT,
    ),
    SungrowDerivedSensorEntityDescription(
        key="battery_charged_from_pv_pct",
        name="Battery charged from PV percentage",
        device=Device.INVERTER,
        **_PERCENT,
    ),
    SungrowDerivedSensorEntityDescription(
        key="daily_consumption_from_pv_pct",
        name="Daily consumption from PV percentage",
        device=Device.INVERTER,
        requires_ihm=False,
        **_PERCENT,
    ),
    SungrowDerivedSensorEntityDescription(
        key="battery_stored_energy",
        name="Battery stored energy",
        device=Device.INVERTER,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        icon="mdi:home-battery-outline",
    ),
    SungrowDerivedSensorEntityDescription(
        key="system_status",
        name="System status",
        device=Device.INVERTER,
        kind="status",
    ),
)


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


class SungrowDerivedSensor(SensorEntity):
    """Computed sensor; value comes from the engine."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: SungrowDerivedSensorEntityDescription

    def __init__(
        self,
        engine: DerivedEngine,
        coordinator: SungrowEmsCoordinator,
        description: SungrowDerivedSensorEntityDescription,
    ) -> None:
        self._engine = engine
        self.entity_description = description
        self._attr_unique_id = make_unique_id(coordinator.config_entry, description.key)
        self._attr_device_info = coordinator.device_info

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._engine.add_listener(self._handle_engine_update))

    @callback
    def _handle_engine_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._engine.values.get(self.entity_description.key) is not None

    @property
    def native_value(self) -> Any:
        return self._engine.values.get(self.entity_description.key)

    @property
    def last_reset(self) -> datetime | None:
        if self.entity_description.daily_total:
            return self._engine.day_start
        return None


class SungrowSystemStatusSensor(SungrowDerivedSensor):
    """System Status: power-flow bitfield (13001) with the YAML labels and icons."""

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return self._status()[0]

    @property
    def icon(self) -> str:
        return self._status()[1]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._status()[2]

    def _status(self) -> tuple[str, str, dict[str, Any]]:
        status = self._engine.values.get("system_status")
        if status is None:
            return system_status(None)
        return status


class SungrowSafeTotalSensor(SungrowDerivedSensor, RestoreSensor):
    """Grid Import/Export Safe (restored across restarts)."""

    def __init__(
        self,
        engine: DerivedEngine,
        coordinator: SungrowEmsCoordinator,
        description: SungrowDerivedSensorEntityDescription,
        component: SpikeFilter,
    ) -> None:
        super().__init__(engine, coordinator, description)
        self._component = component

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        restored = _f(last.native_value) if last is not None else None
        if restored is not None and restored > 0 and self._component.value is None:
            self._component.value = restored
        self._engine.mark_ready(self.entity_description.key)

    @property
    def available(self) -> bool:
        return self._component.value is not None and self._engine.is_ready(
            self.entity_description.key
        )

    @property
    def native_value(self) -> float | None:
        value = self._component.value
        return None if value is None else round(value, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        component = self._component
        return {
            "raw_reading": component.last_raw,
            "pending_outliers": list(component.candidates),
            "rejected_readings": component.rejected,
            "rebaselines": component.rebaselines,
        }


@dataclass
class DailyMeterExtraStoredData(SensorExtraStoredData):
    """Sensor restore data plus the meter's source position and period."""

    last_source: float | None = None
    period: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = super().as_dict()
        data["last_source"] = self.last_source
        data["period"] = self.period
        return data


class SungrowDailyMeterSensor(SungrowDerivedSensor, RestoreSensor):
    """utility_meter (cycle: daily), restored across restarts."""

    def __init__(
        self,
        engine: DerivedEngine,
        coordinator: SungrowEmsCoordinator,
        description: SungrowDerivedSensorEntityDescription,
        component: DailyMeter,
    ) -> None:
        super().__init__(engine, coordinator, description)
        self._component = component

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_extra = await self.async_get_last_extra_data()
        if last_extra is not None:
            data = last_extra.as_dict()
            sensor_data = SensorExtraStoredData.from_dict(data)
            value = _f(sensor_data.native_value) if sensor_data is not None else None
            period = _parse_date(data.get("period"))
            if value is not None and value >= 0 and period is not None:
                self._component.value = value
                self._component.period = period
            last_source = _f(data.get("last_source"))
            if last_source is not None:
                self._component.last_source = last_source
        self._engine.mark_ready(self.entity_description.key)

    @property
    def extra_restore_state_data(self) -> DailyMeterExtraStoredData:
        return DailyMeterExtraStoredData(
            native_value=self.native_value,
            native_unit_of_measurement=self.native_unit_of_measurement,
            last_source=self._component.last_source,
            period=self._component.period.isoformat()
            if self._component.period
            else None,
        )

    @property
    def available(self) -> bool:
        return self._engine.meter_value(self.entity_description.key) is not None

    @property
    def native_value(self) -> float | None:
        if self._component.period is None:
            return None
        return round(self._component.value, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        component = self._component
        return {
            "last_reset": self._engine.day_start.isoformat(),
            "source_value": component.last_source,
            "skipped_steps": component.skipped,
        }


class SungrowIntegralSensor(SungrowDerivedSensor, RestoreSensor):
    """Total Battery Charge Cumulative (Riemann left), restored across restarts."""

    def __init__(
        self,
        engine: DerivedEngine,
        coordinator: SungrowEmsCoordinator,
        description: SungrowDerivedSensorEntityDescription,
        component: LeftRiemann,
    ) -> None:
        super().__init__(engine, coordinator, description)
        self._component = component

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        restored = _f(last.native_value) if last is not None else None
        if restored is not None and restored >= 0:
            self._component.value = restored
        self._engine.mark_ready(self.entity_description.key)

    @property
    def available(self) -> bool:
        return self._engine.is_ready(self.entity_description.key)

    @property
    def native_value(self) -> float:
        return round(self._component.value, 3)


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@callback
def async_build_derived_sensors(
    hass: HomeAssistant, entry: SungrowEmsConfigEntry
) -> list[SensorEntity]:
    """Create derived sensor entities for this entry.

    Stateful components whose entity is disabled in the registry are marked
    ready immediately (they will not be restored), so dependent sensors still
    compute.
    """
    runtime = entry.runtime_data
    engine = runtime.derived
    if engine is None:
        return []
    registry = er.async_get(hass)
    components: dict[str, SpikeFilter | DailyMeter | LeftRiemann] = {
        GRID_IMPORT_SAFE: engine.import_safe,
        GRID_EXPORT_SAFE: engine.export_safe,
        DAILY_IMPORT: engine.daily_import,
        DAILY_EXPORT: engine.daily_export,
        BATTERY_CHARGE_CUMULATIVE: engine.charge_cumulative,
        DAILY_BATTERY_CHARGE_TOTAL: engine.daily_charge_total,
    }

    entities: list[SensorEntity] = []
    for description in DERIVED_SENSORS:
        if description.requires_ihm and not engine.has_ihm:
            continue
        coordinator = runtime.coordinator_for(description.device)
        if coordinator is None:
            continue

        if description.kind in ("spike", "meter", "integral"):
            unique_id = make_unique_id(entry, description.key)
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            reg_entry = registry.async_get(entity_id) if entity_id else None
            if reg_entry is not None and reg_entry.disabled:
                engine.mark_ready(description.key)
                continue

        component = components.get(description.key)
        match description.kind:
            case "status":
                entities.append(
                    SungrowSystemStatusSensor(engine, coordinator, description)
                )
            case "spike":
                assert isinstance(component, SpikeFilter)
                entities.append(
                    SungrowSafeTotalSensor(engine, coordinator, description, component)
                )
            case "meter":
                assert isinstance(component, DailyMeter)
                entities.append(
                    SungrowDailyMeterSensor(engine, coordinator, description, component)
                )
            case "integral":
                assert isinstance(component, LeftRiemann)
                entities.append(
                    SungrowIntegralSensor(engine, coordinator, description, component)
                )
            case _:
                entities.append(SungrowDerivedSensor(engine, coordinator, description))
    return entities
