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
    remainder rounding). PV to grid is the iHM daily export when an iHM is
    configured (the inverter's meter-based 13005 reads 0 behind an iHM),
    otherwise the inverter's PV-only export counter (13005). It is capped
    at the PV left after battery charging; PV to home is the remainder.
  * Daily Consumption is the AC energy balance (0.2.0, validated against
    the iHM's own load measurement on 19-20 Nov 2026, within 1-2 %):
    PV + import - export - total battery charge (13040) + battery discharge
    (13026). No efficiency factor. Grid energy that went into the battery is
    not house load. It reads about 2-3 % below iSolarCloud's Load, which
    counts battery discharge on the DC side.
  * Self Sufficient = PV to home + battery to home (discharge minus battery
    energy exported), capped at Daily Consumption. Battery energy counts as
    self-supplied whatever charged it, so energy bought for the home is
    import minus grid-to-battery. Self Sufficient % and Purchased % are
    shares of Daily Consumption and add up to 100.
  * Daily Battery Charge from Grid never goes down within a day (13040 and
    13012 tick at different moments, which made it flicker by 0.1 kWh).
  * Battery capacity has no hard-coded fallback: options override, then iHM
    8147, inverter 13039, inverter 5639, otherwise unavailable.

Also replaces three UI template helpers (Battery Status, Time Until Fully
Charged, Battery Time Remaining). None of them uses a fixed number:
  * Battery status comes from the inverter's own power-flow bits (13001),
    with the sign of the battery power as the fallback.
  * The reported SoC is treated as a percentage of the window between Min
    SoC (13059) and Max SoC (13058), so usable capacity = rated capacity x
    (Max SoC - Min SoC). This is how the inverter scales the SoC it reports;
    it is not written in the protocol document.
  * Battery usable energy is what can be drawn before discharging stops: down
    to the Backup reserve SoC (13100) on grid, down to Min SoC when off grid.
  * The two time estimates divide the energy still to go by the battery power
    averaged over two minutes.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
from homeassistant.const import UnitOfEnergy
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    BATTERY_IDLE_POWER_W,
    BATTERY_POWER_AVERAGE_SECONDS,
    CONF_BATTERY_CAPACITY,
    CONF_SPIKE_MAX_DAILY,
    CONF_SPIKE_MAX_JUMP,
    CONF_SPIKE_REBASELINE_COUNT,
    DEFAULT_BATTERY_CAPACITY_OPTION,
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
    get_option,
)
from .entity import SungrowNamedEntity, make_unique_id
from .registers import POWER_FLOW_BITS, RUNNING_STATE_OFF_GRID, Device, decode_bits
from .sensor_types import ENERGY_KWH_PRESET, PERCENT_PRESET, POWER_W_PRESET

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


@dataclass(slots=True)
class PowerAverager:
    """Mean of the power samples seen in the last ``window`` seconds."""

    window: float
    samples: deque[tuple[float, float]] = field(default_factory=deque)

    def clear(self) -> None:
        self.samples.clear()

    def update(self, power_w: float | None, now: float) -> None:
        if power_w is not None:
            self.samples.append((now, float(power_w)))
        cutoff = now - self.window
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    @property
    def mean(self) -> float | None:
        if not self.samples:
            return None
        return sum(power for _, power in self.samples) / len(self.samples)


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


BATTERY_CHARGING = "Charging"
BATTERY_DISCHARGING = "Discharging"
BATTERY_IDLE = "Idle"
BATTERY_STATES: tuple[str, ...] = (BATTERY_CHARGING, BATTERY_DISCHARGING, BATTERY_IDLE)
_BATTERY_ICONS: Mapping[str, str] = {
    BATTERY_CHARGING: "mdi:battery-charging",
    BATTERY_DISCHARGING: "mdi:battery-arrow-down",
    BATTERY_IDLE: "mdi:battery",
}

#: Entity keys of the battery sensors.
BATTERY_USABLE_ENERGY = "battery_usable_energy"
BATTERY_STATUS = "battery_status"
BATTERY_TIME_TO_FULL = "battery_time_to_full"
BATTERY_TIME_TO_EMPTY = "battery_time_to_empty"

FLOOR_BACKUP_RESERVE = "backup_reserve"
FLOOR_MIN_SOC = "min_soc"


def battery_status(flow_code: Any, battery_w: float | None) -> str | None:
    """Charging / Discharging / Idle.

    The inverter's power-flow bits (13001: bit 1 charging, bit 2 discharging)
    decide. Without them, the sign of the battery power does (+ discharge,
    - charge), with a small idle band around zero.
    """
    if flow_code is not None:
        code = int(flow_code)
        if (code >> 1) & 1:
            return BATTERY_CHARGING
        if (code >> 2) & 1:
            return BATTERY_DISCHARGING
        return BATTERY_IDLE
    if battery_w is None:
        return None
    if battery_w <= -BATTERY_IDLE_POWER_W:
        return BATTERY_CHARGING
    if battery_w >= BATTERY_IDLE_POWER_W:
        return BATTERY_DISCHARGING
    return BATTERY_IDLE


@dataclass(frozen=True, slots=True)
class BatteryEnergy:
    """Battery energy figures in kWh, all derived from live readings."""

    usable_capacity: float  # rated capacity x (Max SoC - Min SoC)
    stored: float  # above Min SoC
    usable: float  # above the discharge floor
    to_full: float  # still to charge before Max SoC
    floor: str  # FLOOR_BACKUP_RESERVE or FLOOR_MIN_SOC
    limits_known: bool  # Min SoC and Max SoC were both read


def battery_energy(
    soc: float | None,
    capacity: float | None,
    *,
    min_soc: float | None,
    max_soc: float | None,
    reserve_soc: float | None,
    off_grid: bool | None,
) -> BatteryEnergy | None:
    """Energy in the battery, from the reported SoC and the rated capacity.

    The reported SoC runs from 0 % at Min SoC to 100 % at Max SoC, so the
    capacity it refers to is the rated capacity scaled by that window. If the
    two limits cannot be read, the full rated capacity is used and
    ``limits_known`` is False.

    Discharging stops at the Backup reserve SoC while the grid is up; the
    reserve is only released in off-grid (backup) operation. An unknown grid
    state is treated as on grid.
    """
    if soc is None or capacity is None:
        return None
    limits_known = min_soc is not None and max_soc is not None and max_soc > min_soc
    window = 1.0
    if limits_known:
        assert min_soc is not None and max_soc is not None
        window = (max_soc - min_soc) / 100
    usable_capacity = capacity * window
    level = min(max(soc, 0.0), 100.0)
    reserve = 0.0
    if not off_grid and reserve_soc is not None:
        reserve = min(max(reserve_soc, 0.0), 100.0)
    return BatteryEnergy(
        usable_capacity=usable_capacity,
        stored=usable_capacity * level / 100,
        usable=usable_capacity * max(level - reserve, 0.0) / 100,
        to_full=usable_capacity * (100.0 - level) / 100,
        floor=FLOOR_BACKUP_RESERVE if reserve > 0 else FLOOR_MIN_SOC,
        limits_known=limits_known,
    )


def format_hours_minutes(minutes: int | None) -> str | None:
    """675 -> "11h 15m", 6 -> "0h 6m". None stays None (state unknown)."""
    if minutes is None:
        return None
    hours, rest = divmod(max(int(minutes), 0), 60)
    return f"{hours}h {rest}m"


def _minutes(energy_kwh: float, power_w: float | None) -> int | None:
    """Whole minutes to move ``energy_kwh`` at ``power_w``; None if too slow."""
    if power_w is None or power_w < BATTERY_IDLE_POWER_W:
        return None
    return round(energy_kwh / (power_w / 1000) * 60)


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
    max_daily: float,
    capacity: float | None,
    off_grid: bool | None = None,
    average_battery_w: float | None = None,
) -> dict[str, Any]:
    """All non-stateful derived values. A value is None when an input is missing.

    ``average_battery_w`` is the smoothed battery power (+ discharge) used by
    the two time estimates; the instantaneous reading is used when it is None.
    """
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
    # Not max(-battery_w, 0.0): that returns -0.0 at rest, shown as "-0 W".
    out["battery_charging_power"] = (
        None if battery_w is None else (-battery_w if battery_w < 0 else 0.0)
    )

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
    # PV to grid comes from the iHM daily export when the iHM is there: the
    # grid meter is wired to the iHM, so the inverter's meter-based counters
    # (13005 and friends, protocol note 1) read 0 on such systems. Without an
    # iHM the inverter's PV-only export counter (13005) is used. Either way
    # it is capped at what PV had left after charging the battery, and PV to
    # home is the remainder, so the three shares always add up to the day's
    # PV generation.
    pv_export_src = exp_c
    if pv_export_src is None:
        pv_export_src = _f(inv.get("mb_daily_export_from_pv"))
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

    # --- Daily consumption: AC energy balance --------------------------------
    # PV + import - export - everything that went into the battery (from PV
    # and from the grid) + everything that came out of it.
    if None in (pv, imp_c, exp_c, charge_total, discharge):
        consumption = None
    else:
        assert pv is not None and imp_c is not None and exp_c is not None
        assert charge_total is not None and discharge is not None
        net = pv + imp_c - exp_c - charge_total + discharge
        consumption = max(net, 0.0)
    out["daily_consumption"] = None if consumption is None else round(consumption, 2)

    # --- Self sufficiency: consumption covered by PV and battery ------------
    # Battery to home = discharge minus any battery energy that went to the
    # grid instead (grid export beyond the PV share). With that, PV to home +
    # battery to home + (import - grid to battery) = daily consumption, so
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
        battery_to_home = max(discharge - battery_export, 0.0)
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
    code = inv.get("mb_power_flow_status")
    out["system_status"] = system_status(None if code is None else int(code))

    # --- Battery: status, usable energy, time to full / empty ---------------
    status = battery_status(code, battery_w)
    energy = battery_energy(
        soc,
        capacity,
        min_soc=_f(inv.get("mb_min_soc")),
        max_soc=_f(inv.get("mb_max_soc")),
        reserve_soc=_f(inv.get("mb_backup_reserve_soc")),
        off_grid=off_grid,
    )
    average_w = battery_w if average_battery_w is None else average_battery_w
    to_full = to_empty = None
    if energy is not None and status == BATTERY_CHARGING and average_w is not None:
        to_full = _minutes(energy.to_full, -average_w)
    if energy is not None and status == BATTERY_DISCHARGING:
        to_empty = _minutes(energy.usable, average_w)
    out[BATTERY_STATUS] = status
    out[BATTERY_USABLE_ENERGY] = None if energy is None else round(energy.usable, 2)
    out[BATTERY_TIME_TO_FULL] = to_full
    out[BATTERY_TIME_TO_EMPTY] = to_empty

    details: dict[str, dict[str, Any]] = {}
    if energy is not None:
        power = None if average_w is None else round(abs(average_w))
        details[BATTERY_USABLE_ENERGY] = {
            "usable_capacity_kwh": round(energy.usable_capacity, 2),
            "stored_energy_kwh": round(energy.stored, 2),
            "reserved_energy_kwh": round(energy.stored - energy.usable, 2),
            "energy_to_full_kwh": round(energy.to_full, 2),
            "discharge_floor": energy.floor,
            "min_soc": _f(inv.get("mb_min_soc")),
            "max_soc": _f(inv.get("mb_max_soc")),
            "backup_reserve_soc": _f(inv.get("mb_backup_reserve_soc")),
            "soc_limits_known": energy.limits_known,
        }
        details[BATTERY_TIME_TO_FULL] = {
            "energy_to_full_kwh": round(energy.to_full, 2),
            "average_power_w": power if status == BATTERY_CHARGING else None,
        }
        details[BATTERY_TIME_TO_EMPTY] = {
            "usable_energy_kwh": round(energy.usable, 2),
            "average_power_w": power if status == BATTERY_DISCHARGING else None,
            "discharge_floor": energy.floor,
        }
    out["battery_details"] = details
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
        inverter: SungrowEmsCoordinator,
        ihm: SungrowEmsCoordinator | None,
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
        #: Highest Daily battery charge from grid seen today (it never goes
        #: down within a day).
        self._grid_charge_max: tuple[date, float] | None = None
        self.capacity_option = float(
            get_option(entry, CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY_OPTION)
        )

        self.import_safe = SpikeFilter(max_jump=max_jump, rebaseline_count=rebaseline)
        self.export_safe = SpikeFilter(max_jump=max_jump, rebaseline_count=rebaseline)
        self.daily_import = DailyMeter(max_delta=max_jump)
        self.daily_export = DailyMeter(max_delta=max_jump)
        self.charge_cumulative = LeftRiemann(max_gap=INTEGRATION_MAX_GAP_SECONDS)
        self.daily_charge_total = DailyMeter(max_delta=max_jump)
        self.battery_average = PowerAverager(window=BATTERY_POWER_AVERAGE_SECONDS)
        self._battery_status: str | None = None

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
        """Rated capacity in kWh, or None when nothing reports it.

        Order: the options override (when above 0), then what the hardware
        reports: iHM total rated capacity (8147), inverter battery capacity
        (13039), inverter battery capacity (5639). No hard-coded fallback, so
        Battery usable energy shows unavailable rather than a wrong number on
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

    @property
    def off_grid(self) -> bool | None:
        """True in off-grid (backup) operation, None when the state is unknown."""
        coordinator = self.inverter
        if coordinator.data is None or not coordinator.last_update_success:
            return None
        code = coordinator.data.raw.get("mb_running_state")
        return None if code is None else code == RUNNING_STATE_OFF_GRID

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

        now = time.monotonic()
        battery_w = _f(inv.get("mb_battery_power"))
        charging_w = None if battery_w is None else max(-battery_w, 0.0)
        if BATTERY_CHARGE_CUMULATIVE in ready:
            self.charge_cumulative.update(charging_w, now)

        # Start the average again whenever the battery changes direction, so
        # the time estimates never mix charging and discharging samples.
        status = battery_status(inv.get("mb_power_flow_status"), battery_w)
        if status != self._battery_status:
            self._battery_status = status
            self.battery_average.clear()
        self.battery_average.update(battery_w, now)
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
            max_daily=self.max_daily,
            capacity=self.battery_capacity,
            off_grid=self.off_grid,
            average_battery_w=self.battery_average.mean,
        )
        grid_charge = self.values.get("daily_battery_charge_from_grid")
        if grid_charge is not None:
            if self._grid_charge_max is None or self._grid_charge_max[0] != today:
                self._grid_charge_max = (today, grid_charge)
            elif grid_charge < self._grid_charge_max[1]:
                self.values["daily_battery_charge_from_grid"] = self._grid_charge_max[1]
            else:
                self._grid_charge_max = (today, grid_charge)

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

Kind = Literal["computed", "status", "battery", "spike", "meter", "integral"]


@dataclass(frozen=True, kw_only=True)
class SungrowDerivedSensorEntityDescription(SensorEntityDescription):
    """Derived sensor description."""

    device: Device
    kind: Kind = "computed"
    requires_ihm: bool = False
    daily_total: bool = False  # state_class TOTAL with last_reset at local midnight
    #: Engine values that must be present for the entity to be available. When
    #: set, the entity's own value may be None (state "unknown"), for example
    #: Time until fully charged while the battery is discharging.
    available_keys: tuple[str, ...] = ()


#: Energy without a state class: each description sets its own (TOTAL for
#: daily values that can fall during the day, TOTAL_INCREASING for meters).
_ENERGY: dict[str, Any] = {
    **{k: v for k, v in ENERGY_KWH_PRESET.items() if k != "state_class"},
    "suggested_display_precision": 2,
}
_POWER: dict[str, Any] = POWER_W_PRESET
_PERCENT: dict[str, Any] = {
    "state_class": PERCENT_PRESET["state_class"],
    "native_unit_of_measurement": PERCENT_PRESET["native_unit_of_measurement"],
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
        key=BATTERY_USABLE_ENERGY,
        name="Battery usable energy",
        device=Device.INVERTER,
        kind="battery",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        icon="mdi:home-battery-outline",
    ),
    SungrowDerivedSensorEntityDescription(
        key=BATTERY_STATUS,
        name="Battery status",
        device=Device.INVERTER,
        kind="battery",
        device_class=SensorDeviceClass.ENUM,
        options=list(BATTERY_STATES),
    ),
    # Shown as "7h 27m" (text). The number of minutes and the estimated
    # finish time are attributes, for automations and templates.
    SungrowDerivedSensorEntityDescription(
        key=BATTERY_TIME_TO_FULL,
        name="Time until fully charged",
        device=Device.INVERTER,
        kind="battery",
        icon="mdi:battery-clock",
        available_keys=(BATTERY_USABLE_ENERGY, BATTERY_STATUS),
    ),
    SungrowDerivedSensorEntityDescription(
        key=BATTERY_TIME_TO_EMPTY,
        name="Time until fully discharged",
        device=Device.INVERTER,
        kind="battery",
        icon="mdi:battery-clock-outline",
        available_keys=(BATTERY_USABLE_ENERGY, BATTERY_STATUS),
    ),
    SungrowDerivedSensorEntityDescription(
        key="system_status",
        name="System status",
        device=Device.INVERTER,
        kind="status",
    ),
)


#: Entity keys from earlier versions. Their registry entries are removed on
#: setup. ``battery_stored_energy`` (SoC x capacity) became Battery usable
#: energy in 0.1.6, which is a different quantity, so it starts a new entity.
#: ``mb_protocol_number`` (4950-4951) is Reserved since protocol V1.1.7 and is
#: no longer read (0.1.9).
REMOVED_KEYS: tuple[str, ...] = ("battery_stored_energy", "mb_protocol_number")

# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


class SungrowDerivedSensor(SungrowNamedEntity, SensorEntity):
    """Computed sensor; value comes from the engine."""

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
        self._set_names(description.device, str(description.name))

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


class SungrowBatterySensor(SungrowDerivedSensor):
    """Battery status, usable energy and the two time estimates."""

    @property
    def available(self) -> bool:
        keys = self.entity_description.available_keys
        if not keys:
            return super().available
        return all(self._engine.values.get(key) is not None for key in keys)

    @property
    def icon(self) -> str | None:
        if self.entity_description.key == BATTERY_STATUS:
            return _BATTERY_ICONS.get(self._engine.values.get(BATTERY_STATUS, ""))
        return self.entity_description.icon

    @property
    def native_value(self) -> Any:
        value = super().native_value
        if self.entity_description.key in _BATTERY_TIME_KEYS:
            return format_hours_minutes(value)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        details = self._engine.values.get("battery_details") or {}
        attrs = details.get(self.entity_description.key)
        key = self.entity_description.key
        if key not in _BATTERY_TIME_KEYS:
            return attrs
        minutes = self._engine.values.get(key)
        return {
            **(attrs or {}),
            "minutes": minutes,
            "finishes_at": (dt_util.now() + timedelta(minutes=minutes))
            .replace(second=0, microsecond=0)
            .isoformat()
            if minutes is not None
            else None,
        }


_BATTERY_TIME_KEYS = frozenset({BATTERY_TIME_TO_FULL, BATTERY_TIME_TO_EMPTY})


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
    for removed_key in REMOVED_KEYS:
        stale = registry.async_get_entity_id(
            "sensor", DOMAIN, make_unique_id(entry, removed_key)
        )
        if stale is not None:
            registry.async_remove(stale)
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
            case "battery":
                entities.append(SungrowBatterySensor(engine, coordinator, description))
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
