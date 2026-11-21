"""Sungrow EMS register map, block plan and decoder (the read side).

This module only describes what is read: registers carry no write metadata
and there is no encoder here. Holding registers are read (FC03) and shown as
diagnostic sensors. The few registers that may be written (opt-in control)
are listed separately in ``profiles.WRITE_TARGETS`` and written only by
``writer.SafeWriter``.

Pure Python. No Home Assistant imports, no I/O. Everything here can be unit
tested offline.

Sources (cross-referenced for every register below):
  * "Communication Protocol of Residential and Small Industrial Hybrid
    Inverter" V1.1.17 (2026-09-30), SH8.0-10RS series, SH10RS = device type
    code 0xD1B. Referred to as [INV]. (Originally built against V1.1.4.)
  * "iHM Communication Protocol" V1.0.2 (2026-01-21). Referred to as [IHM].
  * Upstream fork (Cyber40014 sungrow-solarcloud-haos-integration) for the
    entity keys already in use, and SunGather registers-sungrow.yaml for
    community-known registers. Registers that come only from these sources
    are flagged ``verified=False``.

Conventions:
  * ``Register.address`` is always the DOCUMENTED (1-based) address exactly as
    printed in the PDFs. ``to_wire()`` is the single place that subtracts 1.
  * 32-bit values are LOW WORD FIRST on the wire for both devices:
    value = words[0] | (words[1] << 16). Bytes within a word are big-endian.
  * "Unavailable" sentinels per [INV]/[IHM]: U16 0xFFFF, S16 0x7FFF,
    U32 0xFFFFFFFF, S32 0x7FFFFFFF, UTF-8 0x00. Sentinels are never returned
    as values: they are reported separately in ``DecodeResult.invalid``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise
import math
from types import MappingProxyType
from typing import Final

# ---------------------------------------------------------------------------
# Basic types
# ---------------------------------------------------------------------------

#: Hard upper bound for one read request. Sungrow WiNet-S firmware rejects
#: large reads (SunGather had to drop from 100 to 38/61 words), so every block
#: in this file is kept at or below this size.
MAX_BLOCK_WORDS: Final = 60


class Device(StrEnum):
    """Physical device a register lives on."""

    INVERTER = "inverter"
    IHM = "ihm"


class Table(StrEnum):
    """Modbus register table."""

    INPUT = "input"  # FC04, read-only
    HOLDING = "holding"  # FC03 settings readback; writes: profiles.WRITE_TARGETS


class DataType(StrEnum):
    """Wire data types used by Sungrow."""

    U16 = "U16"
    S16 = "S16"
    U32 = "U32"
    S32 = "S32"
    UTF8 = "UTF8"


_WORDS: Final[dict[DataType, int]] = {
    DataType.U16: 1,
    DataType.S16: 1,
    DataType.U32: 2,
    DataType.S32: 2,
}

#: Raw (unsigned) sentinel per data type, from [INV] and [IHM] 1.2/3.
SENTINELS: Final[Mapping[DataType, int]] = MappingProxyType(
    {
        DataType.U16: 0xFFFF,
        DataType.S16: 0x7FFF,
        DataType.U32: 0xFFFFFFFF,
        DataType.S32: 0x7FFFFFFF,
    }
)


def to_wire(address: int) -> int:
    """Convert a documented (1-based) address to the 0-based wire address."""
    if address < 1:
        raise ValueError(f"Documented Sungrow addresses start at 1, got {address}")
    return address - 1


def _freeze(mapping: Mapping[int, str] | None) -> Mapping[int, str] | None:
    return None if mapping is None else MappingProxyType(dict(mapping))


@dataclass(frozen=True, slots=True, kw_only=True)
class Register:
    """One Sungrow register (or multi-word value)."""

    key: str
    device: Device
    table: Table
    address: int  # documented, 1-based
    dtype: DataType
    scale: float = 1.0
    unit: str | None = None
    length: int | None = None  # words, UTF8 only
    options: Mapping[int, str] | None = field(default=None, compare=False)
    valid_min: float | None = None  # scaled units; outside range -> invalid
    valid_max: float | None = None
    extra_invalid: frozenset[int] = frozenset()  # additional raw sentinels
    verified: bool = True  # False: not in [INV]/[IHM], or SH10RS behaviour unconfirmed
    note: str = ""

    def __post_init__(self) -> None:
        if self.dtype is DataType.UTF8:
            if not self.length or self.length < 1:
                raise ValueError(f"{self.key}: UTF8 registers need a length")
        elif self.length is not None:
            raise ValueError(f"{self.key}: length is only valid for UTF8")
        if self.scale <= 0:
            raise ValueError(f"{self.key}: scale must be positive")
        object.__setattr__(self, "options", _freeze(self.options))

    @property
    def count(self) -> int:
        """Number of 16-bit words this value occupies."""
        if self.dtype is DataType.UTF8:
            assert self.length is not None
            return self.length
        return _WORDS[self.dtype]

    @property
    def end(self) -> int:
        """Last documented address occupied (inclusive)."""
        return self.address + self.count - 1

    @property
    def wire_address(self) -> int:
        """0-based address used on the wire."""
        return to_wire(self.address)

    @property
    def decimals(self) -> int:
        """Display precision implied by the scale factor."""
        return _decimals(self.scale)

    def label(self, raw: int | None) -> str | None:
        """Human label for an enum value, or None if this is not an enum."""
        if self.options is None or raw is None:
            return None
        return self.options.get(int(raw), f"Unknown ({int(raw)})")


@dataclass(frozen=True, slots=True, kw_only=True)
class Block:
    """A contiguous read request (may contain undocumented gaps).

    ``refresh_interval`` is a hint for the coordinator, in seconds. ``None``
    means "every poll". Static identity blocks are read rarely.
    """

    name: str
    device: Device
    table: Table
    start: int  # documented, inclusive
    end: int  # documented, inclusive
    refresh_interval: int | None = None
    registers: tuple[Register, ...] = field(default=(), compare=False)

    @property
    def count(self) -> int:
        return self.end - self.start + 1

    @property
    def wire_start(self) -> int:
        return to_wire(self.start)

    def runs(self) -> list[tuple[int, int]]:
        """Contiguous documented spans (start, count) with no gaps.

        Used by the hub as the first fallback when a gap inside the block is
        rejected by the device.
        """
        return contiguous_runs(self.registers)


def contiguous_runs(registers: Iterable[Register]) -> list[tuple[int, int]]:
    """Group registers into gap-free (start, count) spans, sorted by address."""
    runs: list[tuple[int, int]] = []
    for reg in sorted(registers, key=lambda r: r.address):
        if runs and reg.address <= runs[-1][0] + runs[-1][1]:
            start, count = runs[-1]
            runs[-1] = (start, max(count, reg.end - start + 1))
        else:
            runs.append((reg.address, reg.count))
    return runs


# ---------------------------------------------------------------------------
# Enumerations and bit maps (from the PDFs)
# ---------------------------------------------------------------------------

#: [INV] V1.1.17 Appendix 1, Table 10 (Adaptive Inverter Models).
DEVICE_TYPE_CODES: Final[Mapping[int, str]] = MappingProxyType(
    {
        0xD17: "SH3.0RS",
        0xD0D: "SH3.6RS",
        0xD18: "SH4.0RS",
        0xD0F: "SH5.0RS",
        0xD10: "SH6.0RS",
        0xD1A: "SH8.0RS",
        0xD1B: "SH10RS",
        0xE00: "SH5.0RT",
        0xE01: "SH6.0RT",
        0xE02: "SH8.0RT",
        0xE03: "SH10RT",
        0xE10: "SH5.0RT-20",
        0xE11: "SH6.0RT-20",
        0xE12: "SH8.0RT-20",
        0xE13: "SH10RT-20",
        0xE0C: "SH5.0RT-V112",
        0xE0D: "SH6.0RT-V112",
        0xE0E: "SH8.0RT-V112",
        0xE0F: "SH10RT-V112",
        0xE08: "SH5.0RT-V122",
        0xE09: "SH6.0RT-V122",
        0xE0A: "SH8.0RT-V122",
        0xE0B: "SH10RT-V122",
        0xE20: "SH5T",
        0xE21: "SH6T",
        0xE22: "SH8T",
        0xE23: "SH10T",
        0xE24: "SH12T",
        0xE25: "SH15T",
        0xE26: "SH20T",
        0xE28: "SH25T",
        0xD27: "MG5RL",
        0xD28: "MG6RL",
        0xD31: "MG7.5RL",
        0xD29: "MG8RL",
        0xD2A: "MG10RL",
        0xD2F: "MG12RL",
        0xD41: "SH3RL",
        0xD42: "SH3.6RL",
        0xD43: "SH4RL",
        0xD2B: "SH5RL",
        0xD2C: "SH6RL",
        0xD2D: "SH8RL",
        0xD2E: "SH10RL",
        0xE51: "SH50CX",
        0xE52: "SH80CX",
        0xE39: "SH100CX",
        0xE3A: "SH110CX",
        0xE3D: "SH125CX",
        0xE53: "MG10TL",
        0xE54: "MG12TL",
        0xE55: "MG15TL",
        0xE56: "MG20TL",
        0xE57: "MG25TL",
    }
)

#: [IHM] Table 3 item 1: register 8000 always reads 0x072A on an
#: iHomeManager. It identifies the device model, like the inverter's 5000.
IHM_DEVICE_TYPE_CODE: Final = 0x072A
IHM_DEVICE_TYPES: Final[Mapping[int, str]] = MappingProxyType(
    {IHM_DEVICE_TYPE_CODE: "iHomeManager"}
)

#: [INV] V1.1.17 Appendix 2. Both the new (0x1200) and legacy (0x0010) codes
#: appear. 0x0014, 0x0080 and 0x2500 were added after V1.1.4.
RUNNING_STATES: Final[Mapping[int, str]] = MappingProxyType(
    {
        0x0000: "Running",
        0x0040: "Running",
        0x0014: "Microgrid operation",
        0x0041: "Off-grid charge",
        0x0080: "Derating running",
        0x0200: "Update failed",
        0x0400: "Running in maintain mode",
        0x0800: "Running in forced mode",
        0x1000: "Running in off-grid mode",
        0x1111: "Uninitialized",
        0x1200: "Initial standby",
        0x0010: "Initial standby",
        0x1300: "Shutdown",
        0x0002: "Shutdown",
        0x1400: "Standby",
        0x0008: "Standby",
        0x1500: "Emergency stop",
        0x0004: "Emergency stop",
        0x1600: "Startup",
        0x0020: "Startup",
        0x1700: "AFCI self-test shutdown",
        0x1800: "Intelligent station building",
        0x1900: "Safe mode",
        0x2000: "Open loop",
        0x2500: "Communication fault",
        0x2501: "Restarting",
        0x4000: "Running in external EMS mode",
        0x4001: "Emergency charging",
        0x5500: "Fault",
        0x0100: "Fault",
        0x8000: "Stop",
        0x0001: "Stop",
        0x8100: "Derating running",
        0x8200: "Dispatch running",
        0x9100: "Warn run",
    }
)

#: Exact running-state code for off-grid operation. Do NOT use a bitmask:
#: ``state & 0x1000`` also matches standby, shutdown, fault and warn states.
RUNNING_STATE_OFF_GRID: Final = 0x1000

#: [INV] Appendix 4.2 power flow bits for register 13001.
POWER_FLOW_BITS: Final[Mapping[int, str]] = MappingProxyType(
    {
        0: "pv_generating",
        1: "battery_charging",
        2: "battery_discharging",
        3: "load_active",
        4: "exporting",
        5: "importing",
        7: "load_generating",
    }
)

OUTPUT_TYPES: Final[Mapping[int, str]] = MappingProxyType(
    {0: "Single-phase", 1: "Three-phase four-wire", 2: "Three-phase three-wire"}
)

ENABLE_AA_55: Final[Mapping[int, str]] = MappingProxyType(
    {0xAA: "Enabled", 0x55: "Disabled"}
)

CHARGE_COMMANDS: Final[Mapping[int, str]] = MappingProxyType(
    {0xAA: "Charge", 0xBB: "Discharge", 0xCC: "Stop"}
)

#: [INV] holding 13050 / Appendix 4.5.
INVERTER_EMS_MODES: Final[Mapping[int, str]] = MappingProxyType(
    {0: "Self-consumption", 2: "Forced", 3: "External EMS", 4: "VPP"}
)

#: [IHM] Table 4 item 1 (note: different numbering from the inverter).
IHM_EMS_MODES: Final[Mapping[int, str]] = MappingProxyType(
    {1: "Self-consumption", 4: "VPP", 5: "Compulsory"}
)

IHM_CHARGER_STATUS: Final[Mapping[int, str]] = MappingProxyType(
    {1: "Idle", 2: "Standby", 3: "Charging", 6: "Charging completed"}
)

#: [IHM] Table 4 item 8 lists only 0xA0 Fast / 0xA1 Eco. iHM firmware
#: SV930.001.00.P014 with an AC22E-01 (AU, iSolarCloud "Charging management")
#: reports 0xA4-0xA7 instead. Confirmed 2026-11-15 by switching each mode in
#: iSolarCloud and reading 8048. Labels match the app.
IHM_CHARGER_MODES: Final[Mapping[int, str]] = MappingProxyType(
    {
        0xA0: "Fast (protocol)",
        0xA1: "Eco (protocol)",
        0xA4: "Fast charging",
        0xA5: "ECO & fast charging",
        0xA6: "ECO & economic charging",
        0xA7: "Time plan",
    }
)

IHM_START_STOP: Final[Mapping[int, str]] = MappingProxyType(
    {0xCF: "Boot", 0xCE: "Shutdown"}
)

#: [IHM] V1.0.2 documents 1/0 for register 8028, but iHM firmware SV930.001.00.P014
#: reports 0x55 (the 0xAA/0x55 convention used by the other iHM switches).
#: Both encodings are decoded.
IHM_ON_OFF: Final[Mapping[int, str]] = MappingProxyType(
    {1: "On", 0: "Off", 0xAA: "On", 0x55: "Off"}
)

#: 13030 is "Reserved" in [INV] (V1.1.4 to V1.1.17). Older firmware reported 0xAA / 0x55
#: here (the SunGather mapping); current firmware returns 0, shown as "Not
#: reported". Use the Off-grid (backup) mode binary sensor (13000) instead.
GRID_STATES: Final[Mapping[int, str]] = MappingProxyType(
    {0xAA: "Off-grid", 0x55: "On-grid", 0: "Not reported"}
)

#: [INV] V1.1.17 Table 3 #118: register 13043, 1-9 = DRM0-DRM8, anything else
#: is invalid (shown as "Unknown (n)").
DRM_STATES: Final[Mapping[int, str]] = MappingProxyType(
    {code: f"DRM{code - 1}" for code in range(1, 10)}
)


def _bits(
    labels: Mapping[int, str], ranges: Iterable[tuple[int, int, str]] = ()
) -> Mapping[int, str]:
    out = dict(labels)
    for first, last, text in ranges:
        for bit in range(first, last + 1):
            out[bit] = text
    return MappingProxyType(dict(sorted(out.items())))


#: [INV] Appendix 4.3 / 4.4. Keyed by register key. Reserved bits omitted.
FAULT_BITS: Final[Mapping[str, Mapping[int, str]]] = MappingProxyType(
    {
        "mb_inverter_alarm": _bits(
            {
                0: "Fan alarm",
                1: "AC lightning protection alarm",
                2: "DC lightning protection alarm",
                3: "Bypass switch abnormal",
                4: "Communication alarm",
                5: "Parallel communication alarm",
                6: "Device abnormality",
                7: "Junction box leakage protector alarm",
                8: "PV1 abnormal",
                9: "PV2 abnormal",
                10: "PV3 abnormal",
                11: "PV4 abnormal",
                12: "Fan 2 low speed",
                13: "Fan 2 stall",
                14: "Meter ports reversed or miswired",
                15: "Software version mismatch",
                16: "SPD or fuse alarm",
                17: "FRAM read alarm",
                18: "SPI communication alarm",
                19: "Ambient temperature sensor open circuit",
                20: "Ambient temperature sensor short circuit",
                21: "Inversion temperature sensor open circuit",
                22: "Inversion temperature sensor short circuit",
                23: "Boost temperature sensor open circuit",
                24: "Boost temperature sensor short circuit",
                25: "Input config or clock reset abnormal",
                26: "PV high voltage fault",
                27: "Ambient low temperature sensor open circuit",
                28: "Fan 1 low speed",
                29: "Fan 1 stall",
                30: "Meter communication alarm",
                31: "Boost short circuit alarm",
            }
        ),
        "mb_grid_side_fault": _bits(
            {
                0: "Grid overvoltage",
                1: "Grid transient overvoltage",
                2: "Grid undervoltage",
                3: "Grid low voltage",
                5: "AC overcurrent",
                6: "Grid overfrequency",
                7: "Grid underfrequency",
                8: "Grid power outage",
                9: "Device abnormality",
                10: "Excessive leakage current",
                11: "Grid abnormal",
                12: "10-minute grid overvoltage",
                13: "High grid voltage",
                14: "Output overload",
                15: "Grid voltage imbalance",
                16: "Inversion overcurrent hardware fault",
                17: "High grid frequency",
                18: "Grid frequency low",
                19: "Grid voltage uniform fault",
                20: "Grid frequency uniform fault",
                21: "Grid protection self-test failed",
                22: "Grounding cable fault",
                23: "Inversion DC injection overvoltage",
                24: "AC side undervoltage",
                25: "AC side overvoltage",
                26: "AC side underfrequency",
                27: "AC side overfrequency",
                28: "AC voltage unbalance",
                29: "Bypass instantaneous overcurrent",
                30: "Bypass switch overcurrent",
            }
        ),
        "mb_system_fault_1": _bits(
            {
                0: "Inversion switch over-temperature",
                1: "Boost switch over-temperature",
                2: "PV insulation fault",
                3: "Bypass switch fault",
                4: "EPS relay failure",
                5: "Junction box relay fault",
                6: "Input and output mismatch",
                7: "PV high voltage overpower",
                8: "Redundancy fault",
                9: "R-phase voltage sampling abnormal",
                10: "S-phase voltage sampling abnormal",
                11: "T-phase voltage sampling abnormal",
                12: "R-phase DC component sampling abnormal",
                13: "S-phase DC component sampling abnormal",
                14: "T-phase DC component sampling abnormal",
                15: "PV1 current sampling abnormal",
                16: "PV2 current sampling abnormal",
                17: "PV1 MPPT current sampling abnormal",
                18: "PV2 MPPT current sampling abnormal",
                19: "Control board power supply undervoltage",
                20: "Leakage CT self-detection fault",
                21: "SPI communication fault",
                22: "LCD and main DSP communication fault",
                24: "PV3 current sampling failure",
                25: "N-wire current sampling abnormal",
                26: "Balance bridge current sampling abnormal",
            }
        ),
        "mb_system_fault_2": _bits(
            {
                0: "Module over-temperature",
                1: "Ambient temperature too high",
                2: "Device abnormality",
                3: "Low system insulation resistance",
                7: "Low ambient temperature",
                11: "Input configuration abnormality",
                15: "Backup load overpower",
            },
            [
                (4, 6, "Device abnormality"),
                (8, 10, "Device abnormality"),
                (12, 14, "Device abnormality"),
            ],
        ),
        "mb_dc_side_fault": _bits(
            {
                4: "PV access failure",
                9: "PV1 reverse connection",
                10: "PV2 reverse connection",
                16: "Bus overvoltage hardware fault",
                17: "Bus average undervoltage",
                18: "PV hardware overcurrent",
                19: "PV voltage exceeds bus voltage",
                20: "Boost1 short circuit",
                21: "Boost2 short circuit",
                22: "DC overcurrent",
                23: "PV3 overcurrent",
                24: "PV3 reverse connection",
                25: "Boost3 short circuit",
                26: "Half-bus overvoltage hardware fault",
                27: "Balance bridge overcurrent hardware fault",
                28: "PV4 reverse connection",
            },
            [
                (0, 3, "Device abnormality"),
                (5, 6, "Device abnormality"),
                (11, 15, "Device abnormality"),
            ],
        ),
        "mb_permanent_fault": _bits(
            {
                0: "PV overcurrent permanent fault",
                1: "Inversion overcurrent permanent fault",
                2: "High DC injection permanent fault",
                3: "Bus overvoltage permanent fault",
                4: "Relay permanent fault",
                5: "PV1 self-check permanent fault",
                6: "PV2 self-check permanent fault",
                7: "Inversion open-loop self-detection permanent fault",
                8: "Temperature sensor failure",
                9: "Boost3 open-loop self-check permanent fault",
                10: "Balance bridge overcurrent permanent fault",
                11: "Half-bus overvoltage permanent fault",
            }
        ),
        "mb_bdc_side_fault": _bits(
            {
                0: "BDC charge transient overcurrent",
                1: "BDC discharge transient overcurrent",
                2: "Clamping capacitor low voltage",
                3: "Clamping capacitor transient overvoltage",
                5: "Battery pre-charge relay failure",
                8: "BDC self-detect fault",
                12: "BDC over-temperature",
                16: "BDC hardware overcurrent",
                20: "BDC current sampling abnormal",
                22: "Leakage current sampling abnormal",
                23: "Secondary DSP communication fault",
                24: "BDC soft start fault",
            }
        ),
        "mb_bdc_side_permanent_fault": _bits(
            {
                0: "BDC overcurrent permanent fault",
                2: "Battery overvoltage permanent fault",
                4: "BDC self-detect permanent fault",
                7: "BDC soft start permanent fault",
            }
        ),
        "mb_battery_fault": _bits(
            {
                3: "Battery average undervoltage",
                7: "Battery over-temperature",
                8: "Battery low temperature",
                11: "Battery transient overvoltage",
                12: "Battery average overvoltage",
                14: "BMS communication fault",
                15: "Battery hardware overvoltage",
                16: "Battery reversed polarity",
                17: "BMS and battery fault",
            }
        ),
        "mb_battery_alarm": _bits(
            {
                0: "BDC temperature sensor open circuit",
                1: "BDC temperature sensor short circuit",
            }
        ),
        "mb_bms_alarm": _bits(
            {
                0: "Overvoltage alarm",
                1: "Over-temperature alarm",
                2: "Low temperature alarm",
                3: "Charge/discharge overcurrent alarm",
                5: "Battery voltage unbalance",
                7: "Undervoltage alarm",
                8: "Abnormal SD card",
                9: "Cell voltage imbalance",
                10: "Cell temperature difference",
                11: "Battery module inconsistency",
                12: "Mixed cell alarm",
            }
        ),
        "mb_bms_protection": _bits(
            {
                0: "Overvoltage protection",
                1: "Over-temperature protection",
                2: "Low temperature protection",
                3: "Charge/discharge overcurrent protection",
                4: "Over charge power limit",
                5: "Over discharge power limit",
                7: "Low voltage protection",
                8: "Slave battery fault",
                9: "Pre-charge failed",
                10: "Abnormal external power line status",
                11: "Current sampling fault",
                12: "Temperature sampling fault",
                13: "Voltage sampling fault",
                14: "Battery internal communication failure",
                15: "DC contactor failure",
            }
        ),
        "mb_bms_fault_1": _bits(
            {
                0: "FET or battery switch failure",
                1: "Battery internal hardware failure",
                2: "Overcurrent fault",
                3: "Battery short circuit",
                4: "Internal communication fault",
                5: "Input overvoltage",
                7: "Software version mismatch",
                8: "Heating alarm",
                9: "Heating circuit abnormal",
                12: "Battery self-test fault",
            }
        ),
        "mb_bms_fault_2": _bits({}),
        "mb_bms_alarm_2": _bits({0: "BMS internal alarm"}),
    }
)


def decode_bits(value: int | None, labels: Mapping[int, str]) -> dict[str, bool]:
    """Expand a bitfield into {label: bool} using a bit-label map.

    Labels that appear on several bits (for example "Device abnormality") are
    OR-ed together.
    """
    out: dict[str, bool] = {}
    for bit, text in labels.items():
        active = value is not None and bool((int(value) >> bit) & 1)
        out[text] = out.get(text, False) or active
    return out


def active_bits(value: int | None, labels: Mapping[int, str]) -> list[str]:
    """Labels of the set bits, de-duplicated, in bit order. Unknown bits included."""
    if not value:
        return []
    out: list[str] = []
    for bit in range(32):
        if (int(value) >> bit) & 1:
            text = labels.get(bit, f"Bit {bit}")
            if text not in out:
                out.append(text)
    return out


def format_inverter_protocol_version(value: int | None) -> str | None:
    """Format inverter register 4952-4953 as 'V1.1.7'.

    [INV] V1.1.17 Table 3 #2: 0x01010700 represents V1.1.7 (after the usual
    low-word-first U32 decode).
    """
    if value is None:
        return None
    v = int(value)
    return f"V{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}"


def format_ihm_protocol_version(value: int | None) -> str | None:
    """Format iHM register 8003-8004 as 'V1.0.2'.

    [IHM] example: V1.0.2 is transmitted as bytes 02 00 01 00, which decodes
    low-word-first to 0x01000200.
    """
    if value is None:
        return None
    v = int(value)
    return f"V{(v >> 24) & 0xFF}.{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}"


# ---------------------------------------------------------------------------
# Register map
# ---------------------------------------------------------------------------

_I = Device.INVERTER
_H = Device.IHM
_IN = Table.INPUT
_HO = Table.HOLDING
U16, S16, U32, S32, UTF8 = (
    DataType.U16,
    DataType.S16,
    DataType.U32,
    DataType.S32,
    DataType.UTF8,
)


def _inv(key: str, address: int, dtype: DataType, **kw: object) -> Register:
    return Register(key=key, device=_I, table=_IN, address=address, dtype=dtype, **kw)  # type: ignore[arg-type]


def _inv_hold(key: str, address: int, dtype: DataType, **kw: object) -> Register:
    return Register(key=key, device=_I, table=_HO, address=address, dtype=dtype, **kw)  # type: ignore[arg-type]


def _ihm(key: str, address: int, dtype: DataType, **kw: object) -> Register:
    return Register(key=key, device=_H, table=_IN, address=address, dtype=dtype, **kw)  # type: ignore[arg-type]


def _ihm_hold(key: str, address: int, dtype: DataType, **kw: object) -> Register:
    return Register(key=key, device=_H, table=_HO, address=address, dtype=dtype, **kw)  # type: ignore[arg-type]


_FREQ = {"valid_min": 40.0, "valid_max": 70.0}
_PCT = {"valid_min": 0.0, "valid_max": 100.0}
#: Lifetime energy counters: anything outside 0..1 GWh is a corrupt read
#: (for example a 0xFFFF high word), never a real value for a home system.
_LIFETIME = {"valid_min": 0.0, "valid_max": 1_000_000.0}

# Keys keep the upstream fork names (mb_*/ihm_*) wherever the register already
# existed there, so side-by-side comparison is one-to-one.

INVERTER_INPUT_REGISTERS: Final[tuple[Register, ...]] = (
    # --- Identity [INV] Table 3 #2-9 ----------------------------------------
    # 4950-4951 (Protocol No.) has been Reserved since V1.1.7: not read.
    _inv("mb_protocol_version", 4952, U32),
    _inv("mb_arm_version", 4954, UTF8, length=15),
    _inv("mb_dsp_version", 4969, UTF8, length=15),
    _inv("mb_serial_number", 4990, UTF8, length=10),
    _inv("mb_device_type_code", 5000, U16, options=DEVICE_TYPE_CODES),
    _inv("mb_nominal_output_power", 5001, U16, scale=0.1, unit="kW"),
    _inv("mb_output_type", 5002, U16, options=OUTPUT_TYPES),
    # --- AC/DC measurements [INV] 3.1 #10-28 --------------------------------
    _inv("mb_daily_output_energy", 5003, U16, scale=0.1, unit="kWh"),
    _inv("mb_total_output_energy", 5004, U32, scale=0.1, unit="kWh", **_LIFETIME),
    _inv(
        "mb_inverter_temperature",
        5008,
        S16,
        scale=0.1,
        unit="°C",
        valid_min=-40.0,
        valid_max=120.0,
    ),
    _inv("mb_mppt1_voltage", 5011, U16, scale=0.1, unit="V"),
    _inv("mb_mppt1_current", 5012, U16, scale=0.1, unit="A"),
    _inv("mb_mppt2_voltage", 5013, U16, scale=0.1, unit="V"),
    _inv("mb_mppt2_current", 5014, U16, scale=0.1, unit="A"),
    _inv("mb_mppt3_voltage", 5015, U16, scale=0.1, unit="V"),
    _inv("mb_mppt3_current", 5016, U16, scale=0.1, unit="A"),
    _inv("mb_total_dc_power", 5017, U32, unit="W", valid_max=100_000.0),
    _inv("mb_phase_a_voltage", 5019, U16, scale=0.1, unit="V"),
    _inv("mb_phase_b_voltage", 5020, U16, scale=0.1, unit="V"),
    _inv("mb_phase_c_voltage", 5021, U16, scale=0.1, unit="V"),
    _inv("mb_reactive_power", 5033, S32, unit="var"),
    _inv("mb_power_factor", 5035, S16, scale=0.001, valid_min=-1.0, valid_max=1.0),
    _inv("mb_grid_frequency_low_res", 5036, U16, scale=0.1, unit="Hz", **_FREQ),
    # --- MPPT4, SH8.0-10RS (4 MPPTs) [INV] Table 3 #29-30 -------------------
    # MPPT 5-10 (5117-5133) are SH50~125CX only and deliberately not read.
    _inv("mb_mppt4_voltage", 5115, U16, scale=0.1, unit="V"),
    _inv("mb_mppt4_current", 5116, U16, scale=0.1, unit="A"),
    # --- Wide-range battery power, high-precision frequency #31-32 ----------
    _inv(
        "mb_battery_power",
        5214,
        S32,
        unit="W",
        valid_min=-30_000.0,
        valid_max=30_000.0,
        note="Sign: positive = discharge (as used today). Confirm against 13001 bits.",
    ),
    _inv("mb_grid_frequency", 5242, U16, scale=0.01, unit="Hz", **_FREQ),
    # --- Meter / BDC / BMS #33-42 -------------------------------------------
    _inv(
        "mb_meter_active_power",
        5601,
        S32,
        unit="W",
        note="[INV] V1.1.7+: > 0 import, < 0 export. Only valid when a smart meter "
        "is wired directly to the inverter (not via the iHM).",
    ),
    _inv("mb_meter_phase_a_active_power", 5603, S32, unit="W"),
    _inv("mb_meter_phase_b_active_power", 5605, S32, unit="W"),
    _inv("mb_meter_phase_c_active_power", 5607, S32, unit="W"),
    _inv("mb_export_limit_min", 5622, U16, scale=10, unit="W"),
    _inv("mb_export_limit_max", 5623, U16, scale=10, unit="W"),
    _inv("mb_bdc_rated_power", 5628, U16, scale=100, unit="W"),
    _inv("mb_battery_current", 5631, S16, scale=0.1, unit="A"),
    _inv("mb_bms_max_charge_current", 5635, U16, unit="A"),
    _inv("mb_bms_max_discharge_current", 5636, U16, unit="A"),
    _inv(
        "mb_battery_capacity",
        5639,
        U16,
        scale=0.01,
        unit="kWh",
        note="SunGather reports this worked over HTTP but not Modbus. Sentinel-filtered if unsupported.",
    ),
    # --- Backup (EPS) port #43-53 -------------------------------------------
    _inv("mb_backup_phase_a_current", 5720, S16, scale=0.1, unit="A"),
    _inv("mb_backup_phase_b_current", 5721, S16, scale=0.1, unit="A"),
    _inv("mb_backup_phase_c_current", 5722, S16, scale=0.1, unit="A"),
    _inv("mb_backup_phase_a_power", 5723, S16, unit="W"),
    _inv("mb_backup_phase_b_power", 5724, S16, unit="W"),
    _inv("mb_backup_phase_c_power", 5725, S16, unit="W"),
    _inv("mb_backup_total_power", 5726, S32, unit="W"),  # V1.1.3: U32 -> S32
    _inv("mb_backup_phase_a_voltage", 5731, U16, scale=0.1, unit="V"),
    _inv("mb_backup_phase_b_voltage", 5732, U16, scale=0.1, unit="V"),
    _inv("mb_backup_phase_c_voltage", 5733, U16, scale=0.1, unit="V"),
    _inv("mb_backup_frequency", 5734, U16, scale=0.01, unit="Hz", **_FREQ),
    # --- System block [INV] #73-107 -----------------------------------------
    _inv("mb_running_state", 13000, U16, options=RUNNING_STATES),
    _inv("mb_power_flow_status", 13001, U16),
    _inv("mb_daily_pv_generation", 13002, U16, scale=0.1, unit="kWh"),
    _inv("mb_total_pv_generation", 13003, U32, scale=0.1, unit="kWh", **_LIFETIME),
    _inv("mb_daily_export_from_pv", 13005, U16, scale=0.1, unit="kWh"),
    _inv("mb_total_export_from_pv", 13006, U32, scale=0.1, unit="kWh", **_LIFETIME),
    _inv("mb_load_power", 13008, S32, unit="W"),
    _inv("mb_export_power", 13010, S32, unit="W"),
    _inv("mb_daily_battery_charge_from_pv", 13012, U16, scale=0.1, unit="kWh"),
    _inv(
        "mb_total_battery_charge_from_pv",
        13013,
        U32,
        scale=0.1,
        unit="kWh",
        **_LIFETIME,
    ),
    _inv("mb_daily_direct_consumption", 13017, U16, scale=0.1, unit="kWh"),
    _inv("mb_total_direct_consumption", 13018, U32, scale=0.1, unit="kWh", **_LIFETIME),
    _inv("mb_battery_voltage", 13020, U16, scale=0.1, unit="V"),
    _inv(
        "mb_battery_current_13021",
        13021,
        S16,
        scale=0.1,
        unit="A",
        note="Table says U16; V1.1.17 remark: S16 on SH3.0-6.0RS and SH8.0-10RS. "
        "Cross-check only, 5631 is primary.",
    ),
    _inv(
        "mb_battery_power_13022",
        13022,
        S16,
        unit="W",
        note="Table says U16; V1.1.17 remark: S16 on SH3.0-6.0RS and SH8.0-10RS. "
        "Cross-check only, 5214 is primary.",
    ),
    _inv("mb_battery_soc", 13023, U16, scale=0.1, unit="%", **_PCT),
    _inv("mb_battery_soh", 13024, U16, scale=0.1, unit="%", **_PCT),
    _inv(
        "mb_battery_temperature",
        13025,
        S16,
        scale=0.1,
        unit="°C",
        valid_min=-40.0,
        valid_max=100.0,
    ),
    _inv("mb_daily_battery_discharge", 13026, U16, scale=0.1, unit="kWh"),
    _inv("mb_total_battery_discharge", 13027, U32, scale=0.1, unit="kWh", **_LIFETIME),
    _inv("mb_self_consumption_today", 13029, U16, scale=0.1, unit="%", **_PCT),
    _inv(
        "mb_grid_state",
        13030,
        U16,
        options=GRID_STATES,
        verified=False,
        note="Reserved in V1.1.4-V1.1.17 (reads 0); SunGather maps 0xAA off-grid / 0x55 on-grid.",
    ),
    _inv("mb_phase_a_current", 13031, S16, scale=0.1, unit="A"),
    _inv("mb_phase_b_current", 13032, S16, scale=0.1, unit="A"),
    _inv("mb_phase_c_current", 13033, S16, scale=0.1, unit="A"),
    _inv("mb_total_active_power", 13034, S32, unit="W"),
    _inv("mb_daily_import_energy", 13036, U16, scale=0.1, unit="kWh"),
    _inv("mb_total_import_energy", 13037, U32, scale=0.1, unit="kWh", **_LIFETIME),
    _inv(
        "mb_battery_capacity_13039",
        13039,
        U16,
        scale=0.1,
        unit="kWh",
        note="Li-ion 0.1 kWh per spec (SunGather used 0.01 for SH6.0RS).",
    ),
    _inv("mb_daily_battery_charge", 13040, U16, scale=0.1, unit="kWh"),
    _inv(
        "mb_total_battery_charge",
        13041,
        U32,
        scale=0.1,
        unit="kWh",
        verified=False,
        **_LIFETIME,
        note="SH10RS (ARM_SUNSTONE-H) returns a high word of 0xFFFF here; true total is "
        "~17 MWh per iSolarCloud. Rejected by the lifetime range check.",
    ),
    _inv("mb_drm_state", 13043, U16, options=DRM_STATES),
    _inv("mb_daily_export_energy", 13045, U16, scale=0.1, unit="kWh"),
    _inv("mb_total_export_energy", 13046, U32, scale=0.1, unit="kWh", **_LIFETIME),
    # --- Fault and alarm bitfields [INV] #109-123 ----------------------------
    _inv("mb_inverter_alarm", 13050, U32),
    _inv("mb_grid_side_fault", 13052, U32),
    _inv("mb_system_fault_1", 13054, U32),
    _inv("mb_system_fault_2", 13056, U32),
    _inv("mb_dc_side_fault", 13058, U32),
    _inv("mb_permanent_fault", 13060, U32),
    _inv("mb_bdc_side_fault", 13062, U32),
    _inv("mb_bdc_side_permanent_fault", 13064, U32),
    _inv("mb_battery_fault", 13066, U32),
    _inv("mb_battery_alarm", 13068, U32),
    _inv("mb_bms_alarm", 13070, U32),
    _inv("mb_bms_protection", 13072, U32),
    _inv("mb_bms_fault_1", 13074, U32),
    _inv("mb_bms_fault_2", 13076, U32),
    _inv("mb_bms_alarm_2", 13078, U32),
)

# Holding registers (inverter and iHM) are read for diagnostics only.
INVERTER_HOLDING_REGISTERS: Final[tuple[Register, ...]] = (
    _inv_hold("mb_ems_mode", 13050, U16, options=INVERTER_EMS_MODES),
    _inv_hold("mb_charge_discharge_command", 13051, U16, options=CHARGE_COMMANDS),
    _inv_hold(
        "mb_charge_discharge_power",
        13052,
        U16,
        unit="W",
        note="[INV] V1.1.17: 1 W, 0-100 % of BDC rated power (5628).",
    ),
    _inv_hold(
        "mb_max_soc", 13058, U16, scale=0.1, unit="%", valid_min=50.0, valid_max=100.0
    ),
    _inv_hold(
        "mb_min_soc", 13059, U16, scale=0.1, unit="%", valid_min=0.0, valid_max=50.0
    ),
    _inv_hold("mb_export_power_limit", 13074, U16, unit="W"),
    _inv_hold("mb_off_grid_option", 13075, U16, options=ENABLE_AA_55),
    _inv_hold("mb_ems_heartbeat", 13080, U16, unit="s"),
    _inv_hold("mb_meter_comm_detection", 13086, U16, options=ENABLE_AA_55),
    _inv_hold("mb_export_power_limitation", 13087, U16, options=ENABLE_AA_55),
    # [INV] V1.1.7+. Supported on SH8.0-10RS (exclusions: MG5-12RL, SH3-10RL,
    # SH50~125CX). 13088 takes precedence over 13074 when both are set.
    _inv_hold(
        "mb_feed_in_limitation_ratio",
        13088,
        U16,
        scale=0.1,
        unit="%",
        **_PCT,
        note="Takes precedence over 13074 when both are set.",
    ),
    _inv_hold("mb_active_power_limitation", 13089, U16, options=ENABLE_AA_55),
    _inv_hold(
        "mb_active_power_limit_ratio",
        13090,
        U16,
        scale=0.1,
        unit="%",
        **_PCT,
        note="Limit at the inverter AC port, % of rated power.",
    ),
    _inv_hold("mb_backup_reserve_soc", 13100, U16, unit="%", **_PCT),
    _inv_hold("mb_feed_in_limitation_wide", 31222, U16, scale=0.01, unit="kW"),
    _inv_hold("mb_charge_cutoff_voltage", 33042, U16, scale=0.1, unit="V"),
    _inv_hold("mb_max_charging_power", 33047, U16, scale=0.01, unit="kW"),
    _inv_hold("mb_max_discharging_power", 33048, U16, scale=0.01, unit="kW"),
    _inv_hold("mb_charge_discharge_power_wide", 33148, U16, scale=0.01, unit="kW"),
)

IHM_INPUT_REGISTERS: Final[tuple[Register, ...]] = (
    # --- Identity [IHM] Table 3 #1-5, #20 -----------------------------------
    _ihm("ihm_device_type_code", 8000, U16, options=IHM_DEVICE_TYPES),
    _ihm("ihm_protocol_number", 8001, UTF8, length=2),
    _ihm("ihm_protocol_version", 8003, U32),
    _ihm("ihm_connected_devices", 8005, U16),
    _ihm("ihm_faulted_devices", 8006, U16),
    _ihm("ihm_software_version", 8318, UTF8, length=15),
    # --- System power and energy #6-19 --------------------------------------
    _ihm("ihm_total_rated_active_power", 8145, U32, scale=0.1, unit="kW"),
    _ihm("ihm_total_rated_battery_capacity", 8147, U32, scale=0.1, unit="kWh"),
    _ihm("ihm_battery_power_limit", 8149, U32, scale=0.1, unit="kW"),
    _ihm("ihm_max_charge_power", 8151, U16, scale=0.1, unit="kW"),
    _ihm("ihm_min_charge_power", 8152, U16, scale=0.1, unit="kW"),
    _ihm("ihm_max_discharge_power", 8153, U16, scale=0.1, unit="kW"),
    _ihm("ihm_min_discharge_power", 8154, U16, scale=0.1, unit="kW"),
    _ihm("ihm_inverter_active_power", 8155, S32, scale=0.01, unit="kW"),
    _ihm("ihm_grid_active_power", 8157, S32, scale=0.01, unit="kW"),
    _ihm("ihm_total_load_power", 8159, S32, scale=0.01, unit="kW"),
    _ihm("ihm_battery_power", 8161, S32, scale=0.01, unit="kW"),
    _ihm("ihm_battery_soc", 8163, U16, scale=0.1, unit="%", **_PCT),
    _ihm("ihm_grid_import_energy", 8176, U32, scale=0.1, unit="kWh", **_LIFETIME),
    _ihm("ihm_grid_export_energy", 8178, U32, scale=0.1, unit="kWh", **_LIFETIME),
    # --- Charger and meter #21-33 -------------------------------------------
    _ihm("ihm_charger_status", 8552, U16, options=IHM_CHARGER_STATUS),
    _ihm("ihm_meter_output_type", 8554, U16, options=OUTPUT_TYPES),
    _ihm("ihm_meter_voltage_a", 8555, U16, scale=0.1, unit="V"),
    _ihm("ihm_meter_voltage_b", 8556, U16, scale=0.1, unit="V"),
    _ihm("ihm_meter_voltage_c", 8557, U16, scale=0.1, unit="V"),
    _ihm("ihm_grid_frequency", 8558, U16, scale=0.1, unit="Hz", **_FREQ),
    _ihm("ihm_meter_phase_a_power", 8559, S32, unit="W"),
    _ihm("ihm_meter_phase_b_power", 8561, S32, unit="W"),
    _ihm("ihm_meter_phase_c_power", 8563, S32, unit="W"),
    _ihm("ihm_ch2_frequency", 8568, U16, scale=0.1, unit="Hz", **_FREQ),
    _ihm("ihm_ch2_phase_a_power", 8569, S32, unit="W"),
    _ihm("ihm_ch2_phase_b_power", 8571, S32, unit="W"),
    _ihm("ihm_ch2_phase_c_power", 8573, S32, unit="W"),
    _ihm(
        "ihm_ev_charger_power",
        8594,
        U32,
        unit="W",
        verified=False,
        note="Not in iHM V1.0.2; carried over from the fork.",
    ),
)

IHM_HOLDING_REGISTERS: Final[tuple[Register, ...]] = (
    _ihm_hold("ihm_ems_mode", 8024, U16, options=IHM_EMS_MODES),
    _ihm_hold(
        "ihm_charge_discharge_command",
        8025,
        U16,
        options=CHARGE_COMMANDS,
    ),
    _ihm_hold(
        "ihm_charge_discharge_power",
        8026,
        U32,
        scale=0.1,
        unit="kW",
        valid_min=0.0,
        note="Upper bound is dynamic: ihm_battery_power_limit (8149).",
    ),
    _ihm_hold("ihm_feed_in_limitation", 8028, U16, options=IHM_ON_OFF),
    _ihm_hold(
        "ihm_feed_in_limitation_ratio",
        8031,
        S16,
        scale=0.1,
        unit="%",
        **_PCT,
        note="S16 since V1.0.2 (was S32). Table 7 in the PDF still shows S32.",
    ),
    _ihm_hold(
        "ihm_vpp_heartbeat",
        8033,
        U16,
        unit="s",
        valid_min=0.0,
        valid_max=1000.0,
    ),
    _ihm_hold("ihm_start_stop", 8047, U16, options=IHM_START_STOP),
    _ihm_hold("ihm_charger_mode", 8048, U16, options=IHM_CHARGER_MODES),
    _ihm_hold("ihm_charger_enable", 8049, U16, options=ENABLE_AA_55),
    _ihm_hold(
        "ihm_charger_grid_draw",
        8050,
        U16,
        options=ENABLE_AA_55,
        note="Only settable in Eco charging mode.",
    ),
    _ihm_hold("ihm_active_power_limitation", 8051, U16, options=ENABLE_AA_55),
    _ihm_hold(
        "ihm_active_power_limit_ratio",
        8052,
        U16,
        scale=0.1,
        unit="%",
        **_PCT,
    ),
)

ALL_REGISTERS: Final[tuple[Register, ...]] = (
    INVERTER_INPUT_REGISTERS
    + INVERTER_HOLDING_REGISTERS
    + IHM_INPUT_REGISTERS
    + IHM_HOLDING_REGISTERS
)

REGISTERS_BY_KEY: Final[Mapping[str, Register]] = MappingProxyType(
    {reg.key: reg for reg in ALL_REGISTERS}
)

# ---------------------------------------------------------------------------
# Block plan
# ---------------------------------------------------------------------------
# (name, device, table, start, end, refresh_interval_seconds)
# Inverter requests are cheap (no pacing), so blocks stay tight.
# iHM requests cost >= 1 s each (pacing), so iHM blocks deliberately span
# undocumented gaps. If the device rejects a gap, modbus.py falls back to the
# gap-free runs and remembers that split.

_STATIC = 3600  # identity / firmware: hourly
_SLOW = 60  # iHM settings readback (holding registers): once a minute
#: Inverter settings readback. [INV] 1.2 rule 6: RW registers must not be read
#: frequently through WiNet-S/WiNet-S2/Logger. Values persist between reads
#: and a failed read is retried on the next poll, so a long interval is safe.
_INV_SETTINGS = 300

_BLOCK_SPECS: Final[tuple[tuple[str, Device, Table, int, int, int | None], ...]] = (
    # Inverter, FC04
    ("inv_identity", _I, _IN, 4952, 5002, _STATIC),
    ("inv_ac_dc", _I, _IN, 5003, 5036, None),
    ("inv_mppt4", _I, _IN, 5115, 5116, None),
    ("inv_battery_power", _I, _IN, 5214, 5242, None),
    ("inv_meter_bms", _I, _IN, 5601, 5639, None),
    ("inv_backup", _I, _IN, 5720, 5734, None),
    ("inv_system", _I, _IN, 13000, 13047, None),
    ("inv_faults", _I, _IN, 13050, 13079, None),
    # Inverter, FC03 (settings readback)
    ("inv_hold_ems", _I, _HO, 13050, 13100, _INV_SETTINGS),
    ("inv_hold_feed_in_wide", _I, _HO, 31222, 31222, _INV_SETTINGS),
    ("inv_hold_battery_limits", _I, _HO, 33042, 33048, _INV_SETTINGS),
    ("inv_hold_power_wide", _I, _HO, 33148, 33148, _INV_SETTINGS),
    # iHM, FC04
    ("ihm_identity", _H, _IN, 8000, 8006, _STATIC),
    ("ihm_software", _H, _IN, 8318, 8332, _STATIC),
    ("ihm_system", _H, _IN, 8145, 8179, None),
    ("ihm_meter_charger", _H, _IN, 8552, 8595, None),
    # iHM, FC03 (settings readback). Slow: each iHM request costs >= 1 s of
    # pacing and these values only change when someone changes a setting.
    ("ihm_settings", _H, _HO, 8024, 8052, _SLOW),
)


def _build_blocks() -> tuple[Block, ...]:
    blocks: list[Block] = []
    for name, device, table, start, end, refresh in _BLOCK_SPECS:
        regs = tuple(
            sorted(
                (
                    r
                    for r in ALL_REGISTERS
                    if r.device is device
                    and r.table is table
                    and r.address >= start
                    and r.end <= end
                ),
                key=lambda r: r.address,
            )
        )
        blocks.append(
            Block(
                name=name,
                device=device,
                table=table,
                start=start,
                end=end,
                refresh_interval=refresh,
                registers=regs,
            )
        )
    return tuple(blocks)


BLOCKS: Final[tuple[Block, ...]] = _build_blocks()
BLOCKS_BY_NAME: Final[Mapping[str, Block]] = MappingProxyType(
    {b.name: b for b in BLOCKS}
)


def blocks_for(device: Device, table: Table | None = None) -> tuple[Block, ...]:
    """Blocks for one device, optionally one table, in plan order."""
    return tuple(
        b for b in BLOCKS if b.device is device and (table is None or b.table is table)
    )


def registers_for(device: Device, table: Table | None = None) -> tuple[Register, ...]:
    """Registers for one device, optionally one table."""
    return tuple(
        r
        for r in ALL_REGISTERS
        if r.device is device and (table is None or r.table is table)
    )


def validate_map() -> None:
    """Check the register map and block plan for internal consistency.

    Raises ValueError on the first problem. Run at import time, so a bad edit
    fails loudly instead of silently mis-decoding.
    """
    seen_keys: set[str] = set()
    for reg in ALL_REGISTERS:
        if reg.key in seen_keys:
            raise ValueError(f"Duplicate register key {reg.key}")
        seen_keys.add(reg.key)

    for device in Device:
        for table in Table:
            regs = sorted(registers_for(device, table), key=lambda r: r.address)
            for prev, cur in pairwise(regs):
                if cur.address <= prev.end:
                    raise ValueError(f"Overlapping registers {prev.key} and {cur.key}")
            for reg in regs:
                owners = [
                    b.name
                    for b in blocks_for(device, table)
                    if b.start <= reg.address and reg.end <= b.end
                ]
                if len(owners) != 1:
                    raise ValueError(
                        f"{reg.key} is covered by {len(owners)} blocks: {owners}"
                    )

    for block in BLOCKS:
        if block.start > block.end:
            raise ValueError(f"Block {block.name} has start > end")
        if block.count > MAX_BLOCK_WORDS:
            raise ValueError(
                f"Block {block.name} is {block.count} words (max {MAX_BLOCK_WORDS})"
            )
        if not block.registers:
            raise ValueError(f"Block {block.name} contains no registers")
        if (
            block.registers[0].address != block.start
            or block.registers[-1].end != block.end
        ):
            raise ValueError(f"Block {block.name} has untrimmed edges")
        for reg in block.registers:
            if reg.end > block.end:
                raise ValueError(f"{reg.key} straddles the end of block {block.name}")

    same = [
        (a.name, b.name)
        for i, a in enumerate(BLOCKS)
        for b in BLOCKS[i + 1 :]
        if a.device is b.device
        and a.table is b.table
        and a.start <= b.end
        and b.start <= a.end
    ]
    if same:
        raise ValueError(f"Overlapping blocks: {same}")


validate_map()

# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

Value = int | float | str


@dataclass(slots=True)
class DecodeResult:
    """Decoded values plus the keys that came back invalid.

    ``values`` only ever contains real readings. ``invalid`` holds keys whose
    raw value was a sentinel, out of range, or undecodable; the coordinator
    keeps the last good value for those instead of publishing junk.
    ``raw`` keeps the unscaled integer for enum/bitfield consumers.
    """

    values: dict[str, Value] = field(default_factory=dict)
    raw: dict[str, int] = field(default_factory=dict)
    invalid: set[str] = field(default_factory=set)

    def merge(self, other: DecodeResult) -> None:
        self.values.update(other.values)
        self.raw.update(other.raw)
        self.invalid.difference_update(other.values)
        self.invalid.update(k for k in other.invalid if k not in other.values)
        for key in other.invalid:
            self.values.pop(key, None)
            self.raw.pop(key, None)


def _decimals(scale: float) -> int:
    if scale >= 1:
        return 0
    return max(0, -math.floor(math.log10(scale) + 1e-9))


def combine_u32(low: int, high: int) -> int:
    """Combine two words, low word first (Sungrow ordering)."""
    return (low & 0xFFFF) | ((high & 0xFFFF) << 16)


def _to_signed(raw: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return raw - (1 << bits) if raw & sign else raw


def raw_value(reg: Register, words: Sequence[int]) -> int:
    """Unsigned raw integer for a numeric register (sentinel check happens on this)."""
    if reg.dtype is DataType.UTF8:
        raise ValueError(f"{reg.key} is a string register")
    if len(words) < reg.count:
        raise ValueError(f"{reg.key} needs {reg.count} words, got {len(words)}")
    if reg.count == 1:
        return words[0] & 0xFFFF
    return combine_u32(words[0], words[1])


def is_sentinel(reg: Register, raw: int) -> bool:
    """True if the raw (unsigned) value means 'unavailable'."""
    if reg.dtype is DataType.UTF8:
        return False
    return raw == SENTINELS[reg.dtype] or raw in reg.extra_invalid


def decode_string(words: Sequence[int]) -> str | None:
    """UTF-8, high byte first, NUL padded. Empty or all-0xFF -> None."""
    data = b"".join(int(w & 0xFFFF).to_bytes(2, "big") for w in words)
    data = data.split(b"\x00", 1)[0]
    if not data or set(data) == {0xFF}:
        return None
    text = data.decode("utf-8", errors="replace").strip()
    return text or None


def decode_register(
    reg: Register, words: Sequence[int]
) -> tuple[Value | None, int | None]:
    """Decode one register from its words.

    Returns (value, raw). value is None when the reading is invalid (sentinel,
    out of the register's valid range, or undecodable). raw is the signed
    integer before scaling for numeric registers, None for strings.
    """
    if len(words) < reg.count:
        return None, None
    if reg.dtype is DataType.UTF8:
        return decode_string(words[: reg.count]), None

    unsigned = raw_value(reg, words)
    if is_sentinel(reg, unsigned):
        return None, None

    if reg.dtype is DataType.S16:
        raw = _to_signed(unsigned, 16)
    elif reg.dtype is DataType.S32:
        raw = _to_signed(unsigned, 32)
    else:
        raw = unsigned

    if reg.scale == 1:
        value: Value = raw
    elif reg.scale > 1 and float(reg.scale).is_integer():
        value = raw * int(reg.scale)
    else:
        value = round(raw * reg.scale, reg.decimals)

    if reg.valid_min is not None and value < reg.valid_min:  # type: ignore[operator]
        return None, raw
    if reg.valid_max is not None and value > reg.valid_max:  # type: ignore[operator]
        return None, raw
    return value, raw


def decode_registers(
    registers: Iterable[Register], words: Mapping[int, int]
) -> DecodeResult:
    """Decode registers from a {documented_address: word} map for ONE table.

    Registers whose words are not all present are skipped (not read this
    cycle), which is different from invalid.
    """
    result = DecodeResult()
    for reg in registers:
        try:
            chunk = [words[addr] for addr in range(reg.address, reg.end + 1)]
        except KeyError:
            continue
        value, raw = decode_register(reg, chunk)
        if value is None:
            result.invalid.add(reg.key)
            continue
        result.values[reg.key] = value
        if raw is not None:
            result.raw[reg.key] = raw
    return result


def decode_block(block: Block, words: Mapping[int, int]) -> DecodeResult:
    """Decode every register of a block from a {documented_address: word} map."""
    return decode_registers(block.registers, words)


def decode_tables(
    blocks: Iterable[Block], words_by_table: Mapping[Table, Mapping[int, int]]
) -> DecodeResult:
    """Decode several blocks. Input and holding words are kept apart because
    the same documented address (for example 13050) means different things in
    each table."""
    result = DecodeResult()
    for block in blocks:
        result.merge(decode_block(block, words_by_table.get(block.table, {})))
    return result
