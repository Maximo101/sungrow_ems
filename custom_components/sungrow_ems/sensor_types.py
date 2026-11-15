"""Sensor descriptions for every register in registers.py.

Reconciled against Hybrid Inverter Protocol V1.1.4 and iHM Protocol V1.0.2.
Keys are the register keys, so ``unique_id = f"{entry_id}_{key}_ems"``.

Defaults for "enabled by default" mirror what was enabled in the original
integration's entity registry; registers new to this integration (backup port,
fault bitfields, settings readback) are diagnostic and mostly disabled.
Disabled after live validation on the SH10RS + iHM site: the inverter's own
grid counters (13036-13038, 13045-13047) and self-consumption (13029) read 0
when an iHM does the metering, and total battery charge (13041) is corrupt.

iHM power registers are transmitted in kW (x0.01 or x0.1); they are exposed
with native unit W (value x 1000). Rated/limit values suggest kW for display.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfReactivePower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.helpers.typing import StateType

from .registers import (
    FAULT_BITS,
    POWER_FLOW_BITS,
    REGISTERS_BY_KEY,
    Device,
    Register,
    Value,
    active_bits,
    decode_bits,
    format_ihm_protocol_version,
)

ValueFn = Callable[[Value, int | None], StateType]
AttrsFn = Callable[[Value, int | None], dict[str, Any]]


@dataclass(frozen=True, kw_only=True)
class SungrowSensorEntityDescription(SensorEntityDescription):
    """Sensor description bound to one register."""

    device: Device
    value_fn: ValueFn | None = None
    attrs_fn: AttrsFn | None = None


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

_DIAG = EntityCategory.DIAGNOSTIC


def _kw_to_w(value: Value, _raw: int | None) -> StateType:
    return round(float(value) * 1000)


def _label_fn(reg: Register) -> ValueFn:
    def _fn(_value: Value, raw: int | None) -> StateType:
        return reg.label(raw)

    return _fn


def _code_attrs(_value: Value, raw: int | None) -> dict[str, Any]:
    if raw is None:
        return {}
    return {"code": raw, "code_hex": f"0x{raw:04X}"}


def _hex_fn(reg: Register) -> ValueFn:
    width = 4 * reg.count

    def _fn(_value: Value, raw: int | None) -> StateType:
        return None if raw is None else f"0x{raw:0{width}X}"

    return _fn


def _version_fn(_value: Value, raw: int | None) -> StateType:
    return format_ihm_protocol_version(raw)


def _bitfield_attrs(key: str) -> AttrsFn:
    labels = FAULT_BITS.get(key, {})

    def _fn(_value: Value, raw: int | None) -> dict[str, Any]:
        if raw is None:
            return {}
        return {"active": active_bits(raw, labels), "raw_hex": f"0x{raw:08X}"}

    return _fn


def _power_flow_attrs(_value: Value, raw: int | None) -> dict[str, Any]:
    if raw is None:
        return {}
    return {"raw_hex": f"0x{raw:04X}", **decode_bits(raw, POWER_FLOW_BITS)}


class Kind(StrEnum):
    """What a register measures; selects its preset below."""

    POWER_W = "power_w"
    POWER_KW = "power_kw"
    IHM_POWER = "ihm_power"  # kW on the wire -> native W
    IHM_POWER_RATING = "ihm_power_rating"  # kW on the wire -> native W, shown as kW
    REACTIVE = "reactive"
    ENERGY = "energy"
    ENERGY_STORAGE = "energy_storage"
    VOLTAGE = "voltage"
    CURRENT = "current"
    TEMPERATURE = "temperature"
    FREQUENCY = "frequency"
    BATTERY = "battery"
    PERCENT = "percent"
    POWER_FACTOR = "power_factor"
    DURATION = "duration"
    COUNT = "count"
    ENUM = "enum"
    HEX = "hex"
    VERSION = "version"
    BITFIELD = "bitfield"
    POWER_FLOW = "power_flow"
    TEXT = "text"
    PLAIN = "plain"


_MEASUREMENT = SensorStateClass.MEASUREMENT


def _numeric(
    device_class: SensorDeviceClass | None,
    unit: str | None,
    *,
    state_class: SensorStateClass | None = _MEASUREMENT,
    precision: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Static preset. ``precision=None`` means "from the register's scale"."""
    return {
        "device_class": device_class,
        "state_class": state_class,
        "native_unit_of_measurement": unit,
        "suggested_display_precision": precision,
        **extra,
    }


#: Shared with derived.py so computed and raw sensors agree.
POWER_W_PRESET: Final[dict[str, Any]] = _numeric(
    SensorDeviceClass.POWER, UnitOfPower.WATT, precision=0
)
ENERGY_KWH_PRESET: Final[dict[str, Any]] = _numeric(
    SensorDeviceClass.ENERGY,
    UnitOfEnergy.KILO_WATT_HOUR,
    state_class=SensorStateClass.TOTAL_INCREASING,
)
PERCENT_PRESET: Final[dict[str, Any]] = _numeric(None, PERCENTAGE)

#: Presets that do not depend on the register.
_STATIC_PRESETS: Final[dict[Kind, dict[str, Any]]] = {
    Kind.POWER_W: POWER_W_PRESET,
    Kind.POWER_KW: _numeric(SensorDeviceClass.POWER, UnitOfPower.KILO_WATT),
    Kind.IHM_POWER: {**POWER_W_PRESET, "value_fn": _kw_to_w},
    Kind.IHM_POWER_RATING: {
        **POWER_W_PRESET,
        "suggested_unit_of_measurement": UnitOfPower.KILO_WATT,
        "suggested_display_precision": 1,
        "value_fn": _kw_to_w,
    },
    Kind.REACTIVE: _numeric(
        SensorDeviceClass.REACTIVE_POWER,
        UnitOfReactivePower.VOLT_AMPERE_REACTIVE,
        precision=0,
    ),
    Kind.ENERGY: ENERGY_KWH_PRESET,
    Kind.ENERGY_STORAGE: _numeric(
        SensorDeviceClass.ENERGY_STORAGE, UnitOfEnergy.KILO_WATT_HOUR
    ),
    Kind.VOLTAGE: _numeric(SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    Kind.CURRENT: _numeric(SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    Kind.TEMPERATURE: _numeric(
        SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS
    ),
    Kind.FREQUENCY: _numeric(SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ),
    Kind.BATTERY: _numeric(SensorDeviceClass.BATTERY, PERCENTAGE),
    Kind.PERCENT: PERCENT_PRESET,
    Kind.POWER_FACTOR: _numeric(SensorDeviceClass.POWER_FACTOR, None, precision=3),
    Kind.DURATION: _numeric(
        SensorDeviceClass.DURATION, UnitOfTime.SECONDS, state_class=None, precision=0
    ),
    Kind.COUNT: {"state_class": _MEASUREMENT, "suggested_display_precision": 0},
    Kind.VERSION: {"value_fn": _version_fn},
    Kind.POWER_FLOW: {"attrs_fn": _power_flow_attrs},
    Kind.TEXT: {},
    Kind.PLAIN: {},
}

#: Presets built from the register (labels, word width, bit names).
_REGISTER_PRESETS: Final[dict[Kind, Callable[[Register], dict[str, Any]]]] = {
    Kind.ENUM: lambda reg: {"value_fn": _label_fn(reg), "attrs_fn": _code_attrs},
    Kind.HEX: lambda reg: {"value_fn": _hex_fn(reg)},
    Kind.BITFIELD: lambda reg: {"attrs_fn": _bitfield_attrs(reg.key)},
}


def _preset(kind: Kind, reg: Register) -> dict[str, Any]:
    """Device class, state class, unit and converters for a kind of register."""
    if (factory := _REGISTER_PRESETS.get(kind)) is not None:
        return factory(reg)
    preset = dict(_STATIC_PRESETS[kind])
    if (
        "suggested_display_precision" in preset
        and preset["suggested_display_precision"] is None
    ):
        preset["suggested_display_precision"] = reg.decimals
    return preset


def _d(
    key: str,
    name: str,
    kind: str,
    *,
    enabled: bool = True,
    diagnostic: bool = False,
    icon: str | None = None,
) -> SungrowSensorEntityDescription:
    reg = REGISTERS_BY_KEY[key]
    return SungrowSensorEntityDescription(
        key=key,
        name=name,
        device=reg.device,
        entity_registry_enabled_default=enabled,
        entity_category=_DIAG if diagnostic else None,
        icon=icon,
        **_preset(Kind(kind), reg),
    )


# ---------------------------------------------------------------------------
# Inverter (SH10RS), input registers FC04
# ---------------------------------------------------------------------------

INVERTER_SENSORS: tuple[SungrowSensorEntityDescription, ...] = (
    # Identity
    _d("mb_protocol_number", "Protocol number", "hex", enabled=False, diagnostic=True),
    _d(
        "mb_protocol_version", "Protocol version", "hex", enabled=False, diagnostic=True
    ),
    _d(
        "mb_arm_version",
        "ARM firmware",
        "text",
        enabled=False,
        diagnostic=True,
        icon="mdi:chip",
    ),
    _d(
        "mb_dsp_version",
        "DSP firmware",
        "text",
        enabled=False,
        diagnostic=True,
        icon="mdi:chip",
    ),
    _d(
        "mb_serial_number",
        "Serial number",
        "text",
        enabled=False,
        diagnostic=True,
        icon="mdi:identifier",
    ),
    _d(
        "mb_device_type_code",
        "Model",
        "enum",
        diagnostic=True,
        icon="mdi:information-outline",
    ),
    _d(
        "mb_nominal_output_power",
        "Nominal output power",
        "power_kw",
        enabled=False,
        diagnostic=True,
    ),
    _d("mb_output_type", "Output type", "enum", enabled=False, diagnostic=True),
    # AC / DC
    _d("mb_daily_output_energy", "Daily output energy", "energy"),
    _d("mb_total_output_energy", "Total output energy", "energy"),
    _d("mb_inverter_temperature", "Temperature", "temperature"),
    _d("mb_mppt1_voltage", "MPPT1 voltage", "voltage"),
    _d("mb_mppt1_current", "MPPT1 current", "current"),
    _d("mb_mppt2_voltage", "MPPT2 voltage", "voltage"),
    _d("mb_mppt2_current", "MPPT2 current", "current"),
    _d("mb_mppt3_voltage", "MPPT3 voltage", "voltage"),
    _d("mb_mppt3_current", "MPPT3 current", "current"),
    _d("mb_total_dc_power", "PV DC power", "power_w", icon="mdi:solar-power"),
    _d("mb_phase_a_voltage", "Phase A voltage", "voltage"),
    _d("mb_phase_b_voltage", "Phase B voltage", "voltage", enabled=False),
    _d("mb_phase_c_voltage", "Phase C voltage", "voltage", enabled=False),
    _d("mb_reactive_power", "Reactive power", "reactive"),
    _d("mb_power_factor", "Power factor", "power_factor", enabled=False),
    _d(
        "mb_grid_frequency_low_res",
        "Grid frequency (0.1 Hz)",
        "frequency",
        enabled=False,
    ),
    _d("mb_mppt4_voltage", "MPPT4 voltage", "voltage"),
    _d("mb_mppt4_current", "MPPT4 current", "current"),
    _d("mb_battery_power", "Battery power", "power_w", icon="mdi:battery-charging"),
    _d("mb_grid_frequency", "Grid frequency", "frequency"),
    # Meter / BDC / BMS
    _d("mb_meter_active_power", "Meter active power", "power_w", enabled=False),
    _d(
        "mb_meter_phase_a_active_power", "Meter phase A power", "power_w", enabled=False
    ),
    _d(
        "mb_meter_phase_b_active_power", "Meter phase B power", "power_w", enabled=False
    ),
    _d(
        "mb_meter_phase_c_active_power", "Meter phase C power", "power_w", enabled=False
    ),
    _d(
        "mb_export_limit_min",
        "Export limit minimum",
        "power_w",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_export_limit_max",
        "Export limit maximum",
        "power_w",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_bdc_rated_power",
        "Battery converter rated power",
        "power_w",
        enabled=False,
        diagnostic=True,
    ),
    _d("mb_battery_current", "Battery current", "current"),
    _d(
        "mb_bms_max_charge_current",
        "BMS max charge current",
        "current",
        diagnostic=True,
    ),
    _d(
        "mb_bms_max_discharge_current",
        "BMS max discharge current",
        "current",
        diagnostic=True,
    ),
    _d(
        "mb_battery_capacity",
        "Battery capacity (5639)",
        "energy_storage",
        diagnostic=True,
    ),
    # Backup (EPS) port
    _d("mb_backup_phase_a_current", "Backup phase A current", "current", enabled=False),
    _d("mb_backup_phase_b_current", "Backup phase B current", "current", enabled=False),
    _d("mb_backup_phase_c_current", "Backup phase C current", "current", enabled=False),
    _d("mb_backup_phase_a_power", "Backup phase A power", "power_w", enabled=False),
    _d("mb_backup_phase_b_power", "Backup phase B power", "power_w", enabled=False),
    _d("mb_backup_phase_c_power", "Backup phase C power", "power_w", enabled=False),
    _d(
        "mb_backup_total_power",
        "Backup power",
        "power_w",
        icon="mdi:power-plug-battery",
    ),
    _d("mb_backup_phase_a_voltage", "Backup phase A voltage", "voltage", enabled=False),
    _d("mb_backup_phase_b_voltage", "Backup phase B voltage", "voltage", enabled=False),
    _d("mb_backup_phase_c_voltage", "Backup phase C voltage", "voltage", enabled=False),
    _d("mb_backup_frequency", "Backup frequency", "frequency", enabled=False),
    # System block
    _d("mb_running_state", "Running state", "enum", icon="mdi:state-machine"),
    _d(
        "mb_power_flow_status",
        "Power flow status",
        "power_flow",
        icon="mdi:transmission-tower",
    ),
    _d(
        "mb_daily_pv_generation",
        "Daily PV generation",
        "energy",
        icon="mdi:solar-power",
    ),
    _d(
        "mb_total_pv_generation",
        "Total PV generation",
        "energy",
        icon="mdi:solar-power",
    ),
    _d("mb_daily_export_from_pv", "Daily PV export", "energy", enabled=False),
    _d("mb_total_export_from_pv", "Total PV export", "energy"),
    _d(
        "mb_load_power",
        "Load power",
        "power_w",
        enabled=False,
        icon="mdi:home-lightning-bolt",
    ),
    _d("mb_export_power", "Export power", "power_w", enabled=False),
    _d("mb_daily_battery_charge_from_pv", "Daily battery charge from PV", "energy"),
    _d("mb_total_battery_charge_from_pv", "Total battery charge from PV", "energy"),
    _d("mb_daily_direct_consumption", "Daily direct consumption", "energy"),
    _d("mb_total_direct_consumption", "Total direct consumption", "energy"),
    _d("mb_battery_voltage", "Battery voltage", "voltage"),
    _d(
        "mb_battery_current_13021",
        "Battery current (13021)",
        "current",
        diagnostic=True,
    ),
    _d("mb_battery_power_13022", "Battery power (13022)", "power_w", diagnostic=True),
    _d("mb_battery_soc", "Battery SoC", "battery"),
    _d("mb_battery_soh", "Battery health", "percent", icon="mdi:battery-heart-variant"),
    _d("mb_battery_temperature", "Battery temperature", "temperature"),
    _d("mb_daily_battery_discharge", "Daily battery discharge", "energy"),
    _d("mb_total_battery_discharge", "Total battery discharge", "energy"),
    _d(
        "mb_self_consumption_today",
        "Self-consumption today",
        "percent",
        enabled=False,
        icon="mdi:percent",
    ),
    _d("mb_grid_state", "Grid state", "enum", enabled=False, diagnostic=True),
    _d("mb_phase_a_current", "Phase A current", "current"),
    _d("mb_phase_b_current", "Phase B current", "current", enabled=False),
    _d("mb_phase_c_current", "Phase C current", "current", enabled=False),
    _d("mb_total_active_power", "Active power", "power_w"),
    _d("mb_daily_import_energy", "Daily import (inverter)", "energy", enabled=False),
    _d("mb_total_import_energy", "Total import (inverter)", "energy", enabled=False),
    _d(
        "mb_battery_capacity_13039",
        "Battery capacity (13039)",
        "energy_storage",
        diagnostic=True,
    ),
    _d("mb_daily_battery_charge", "Daily battery charge", "energy"),
    _d("mb_total_battery_charge", "Total battery charge", "energy", enabled=False),
    _d("mb_drm_state", "DRM state", "plain", enabled=False, diagnostic=True),
    _d("mb_daily_export_energy", "Daily export (inverter)", "energy", enabled=False),
    _d("mb_total_export_energy", "Total export (inverter)", "energy", enabled=False),
    # Fault and alarm bitfields
    _d(
        "mb_inverter_alarm",
        "Inverter alarm code",
        "bitfield",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_grid_side_fault",
        "Grid-side fault code",
        "bitfield",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_system_fault_1",
        "System fault 1 code",
        "bitfield",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_system_fault_2",
        "System fault 2 code",
        "bitfield",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_dc_side_fault",
        "DC-side fault code",
        "bitfield",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_permanent_fault",
        "Permanent fault code",
        "bitfield",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_bdc_side_fault",
        "Battery converter fault code",
        "bitfield",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_bdc_side_permanent_fault",
        "Battery converter permanent fault code",
        "bitfield",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_battery_fault",
        "Battery fault code",
        "bitfield",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_battery_alarm",
        "Battery alarm code",
        "bitfield",
        enabled=False,
        diagnostic=True,
    ),
    _d("mb_bms_alarm", "BMS alarm code", "bitfield", enabled=False, diagnostic=True),
    _d(
        "mb_bms_protection",
        "BMS protection code",
        "bitfield",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_bms_fault_1", "BMS fault 1 code", "bitfield", enabled=False, diagnostic=True
    ),
    _d(
        "mb_bms_fault_2", "BMS fault 2 code", "bitfield", enabled=False, diagnostic=True
    ),
    _d(
        "mb_bms_alarm_2", "BMS alarm 2 code", "bitfield", enabled=False, diagnostic=True
    ),
)

# ---------------------------------------------------------------------------
# Inverter, holding registers FC03 (settings readback, diagnostic only)
# ---------------------------------------------------------------------------

INVERTER_SETTING_SENSORS: tuple[SungrowSensorEntityDescription, ...] = (
    _d("mb_ems_mode", "EMS mode (inverter)", "enum", diagnostic=True, icon="mdi:cog"),
    _d(
        "mb_charge_discharge_command",
        "Charge/discharge command (inverter)",
        "enum",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_charge_discharge_power",
        "Charge/discharge power (inverter)",
        "power_w",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_max_soc", "Max SoC", "percent", diagnostic=True, icon="mdi:battery-arrow-up"
    ),
    _d(
        "mb_min_soc",
        "Min SoC",
        "percent",
        diagnostic=True,
        icon="mdi:battery-arrow-down",
    ),
    _d(
        "mb_export_power_limit",
        "Export power limit",
        "power_w",
        enabled=False,
        diagnostic=True,
    ),
    _d("mb_off_grid_option", "Off-grid option", "enum", diagnostic=True),
    _d(
        "mb_ems_heartbeat",
        "External EMS heartbeat",
        "duration",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_meter_comm_detection",
        "Meter comm detection",
        "enum",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_export_power_limitation",
        "Export power limitation",
        "enum",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_backup_reserve_soc",
        "Backup reserve SoC",
        "percent",
        diagnostic=True,
        icon="mdi:battery-lock",
    ),
    _d(
        "mb_feed_in_limitation_wide",
        "Feed-in limitation (wide range)",
        "power_kw",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "mb_charge_cutoff_voltage",
        "Charge cutoff voltage",
        "voltage",
        enabled=False,
        diagnostic=True,
    ),
    _d("mb_max_charging_power", "Max charging power", "power_kw", diagnostic=True),
    _d(
        "mb_max_discharging_power", "Max discharging power", "power_kw", diagnostic=True
    ),
    _d(
        "mb_charge_discharge_power_wide",
        "Charge/discharge power (wide range)",
        "power_kw",
        enabled=False,
        diagnostic=True,
    ),
)

# ---------------------------------------------------------------------------
# iHomeManager, input registers FC04
# ---------------------------------------------------------------------------

IHM_SENSORS: tuple[SungrowSensorEntityDescription, ...] = (
    _d(
        "ihm_device_type_code",
        "Device type",
        "hex",
        diagnostic=True,
        icon="mdi:information-outline",
    ),
    _d(
        "ihm_protocol_number", "Protocol number", "text", enabled=False, diagnostic=True
    ),
    _d(
        "ihm_protocol_version",
        "Protocol version",
        "version",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "ihm_connected_devices",
        "Connected devices",
        "count",
        diagnostic=True,
        icon="mdi:devices",
    ),
    _d(
        "ihm_faulted_devices",
        "Devices in fault",
        "count",
        diagnostic=True,
        icon="mdi:alert-circle-outline",
    ),
    _d(
        "ihm_software_version",
        "Software version",
        "text",
        diagnostic=True,
        icon="mdi:chip",
    ),
    _d(
        "ihm_total_rated_active_power",
        "Rated active power",
        "ihm_power_rating",
        diagnostic=True,
    ),
    _d(
        "ihm_total_rated_battery_capacity",
        "Rated battery capacity",
        "energy_storage",
        diagnostic=True,
    ),
    _d(
        "ihm_battery_power_limit",
        "Battery power limit",
        "ihm_power_rating",
        diagnostic=True,
    ),
    _d("ihm_max_charge_power", "Max charge power", "ihm_power_rating", diagnostic=True),
    _d(
        "ihm_min_charge_power",
        "Min charge power",
        "ihm_power_rating",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "ihm_max_discharge_power",
        "Max discharge power",
        "ihm_power_rating",
        diagnostic=True,
    ),
    _d(
        "ihm_min_discharge_power",
        "Min discharge power",
        "ihm_power_rating",
        enabled=False,
        diagnostic=True,
    ),
    _d("ihm_inverter_active_power", "Inverter active power", "ihm_power"),
    _d(
        "ihm_grid_active_power",
        "Grid active power",
        "ihm_power",
        icon="mdi:transmission-tower",
    ),
    _d(
        "ihm_total_load_power",
        "Load power",
        "ihm_power",
        icon="mdi:home-lightning-bolt",
    ),
    _d("ihm_battery_power", "Battery power", "ihm_power", icon="mdi:battery-charging"),
    _d("ihm_battery_soc", "Battery SoC", "battery"),
    _d(
        "ihm_grid_import_energy",
        "Grid import energy",
        "energy",
        icon="mdi:transmission-tower-import",
    ),
    _d(
        "ihm_grid_export_energy",
        "Grid export energy",
        "energy",
        icon="mdi:transmission-tower-export",
    ),
    _d("ihm_charger_status", "EV charger status", "enum", icon="mdi:ev-station"),
    _d(
        "ihm_meter_output_type",
        "Meter output type",
        "enum",
        enabled=False,
        diagnostic=True,
    ),
    _d("ihm_meter_voltage_a", "Grid voltage A", "voltage"),
    _d("ihm_meter_voltage_b", "Grid voltage B", "voltage", enabled=False),
    _d("ihm_meter_voltage_c", "Grid voltage C", "voltage", enabled=False),
    _d("ihm_grid_frequency", "Grid frequency", "frequency"),
    _d("ihm_meter_phase_a_power", "Grid phase A power", "power_w", enabled=False),
    _d("ihm_meter_phase_b_power", "Grid phase B power", "power_w", enabled=False),
    _d("ihm_meter_phase_c_power", "Grid phase C power", "power_w", enabled=False),
    _d("ihm_ch2_frequency", "Channel 2 frequency", "frequency", enabled=False),
    _d("ihm_ch2_phase_a_power", "Channel 2 phase A power", "power_w", enabled=False),
    _d("ihm_ch2_phase_b_power", "Channel 2 phase B power", "power_w", enabled=False),
    _d("ihm_ch2_phase_c_power", "Channel 2 phase C power", "power_w", enabled=False),
    _d("ihm_ev_charger_power", "EV charger power", "power_w", icon="mdi:ev-station"),
)

# ---------------------------------------------------------------------------
# iHomeManager, holding registers FC03 (settings readback, diagnostic only)
# ---------------------------------------------------------------------------

IHM_SETTING_SENSORS: tuple[SungrowSensorEntityDescription, ...] = (
    _d("ihm_ems_mode", "EMS mode", "enum", diagnostic=True, icon="mdi:cog"),
    _d(
        "ihm_charge_discharge_command",
        "Charge/discharge command",
        "enum",
        diagnostic=True,
        icon="mdi:battery-sync",
    ),
    _d(
        "ihm_charge_discharge_power",
        "Charge/discharge power setpoint",
        "ihm_power_rating",
        diagnostic=True,
    ),
    _d("ihm_feed_in_limitation", "Feed-in limitation", "enum", diagnostic=True),
    _d(
        "ihm_feed_in_limitation_ratio",
        "Feed-in limitation ratio",
        "percent",
        diagnostic=True,
    ),
    _d(
        "ihm_vpp_heartbeat", "VPP heartbeat", "duration", enabled=False, diagnostic=True
    ),
    _d("ihm_start_stop", "Start/stop", "enum", enabled=False, diagnostic=True),
    _d(
        "ihm_charger_mode",
        "EV charger mode",
        "enum",
        diagnostic=True,
        icon="mdi:ev-station",
    ),
    _d(
        "ihm_charger_enable",
        "EV charger enabled",
        "enum",
        diagnostic=True,
        icon="mdi:ev-station",
    ),
    _d(
        "ihm_charger_grid_draw",
        "EV charger grid draw",
        "enum",
        diagnostic=True,
        icon="mdi:ev-station",
    ),
    _d(
        "ihm_active_power_limitation",
        "Active power limitation",
        "enum",
        enabled=False,
        diagnostic=True,
    ),
    _d(
        "ihm_active_power_limit_ratio",
        "Active power limit ratio",
        "percent",
        enabled=False,
        diagnostic=True,
    ),
)

SENSOR_DESCRIPTIONS: tuple[SungrowSensorEntityDescription, ...] = (
    INVERTER_SENSORS + INVERTER_SETTING_SENSORS + IHM_SENSORS + IHM_SETTING_SENSORS
)


def _check_complete() -> None:
    keys = [d.key for d in SENSOR_DESCRIPTIONS]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate sensor description keys")
    missing = set(REGISTERS_BY_KEY) - set(keys)
    if missing:
        raise ValueError(f"Registers without a sensor description: {sorted(missing)}")


_check_complete()
