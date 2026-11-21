"""Model profiles and the write allowlist for Sungrow EMS.

Pure Python, no Home Assistant imports (like ``registers.py``).

Two things live here:

1. **Model profiles.** The inverter reports its model in input register 5000
   (device type code, [INV] Appendix 1). A profile groups the codes of one
   family (SH8.0-10RS, SH5-25T, ...) with what differs between them: phases,
   MPPT count, registers a family does not have, and which write targets the
   protocol documents for it. The coordinator, the entities and the writer
   all ask the profile instead of testing model names, so the single-phase
   SHxxRS and the three-phase SHxxT run through the same code.

2. **Write targets.** ``registers.py`` stays a read map with no write
   metadata. Everything that may ever be written is listed in
   ``WRITE_TARGETS`` with its address, type, scale, bounds or allowed raw
   values, cooldown and daily budget. ``writer.SafeWriter`` refuses any key
   that is not in this table before a single byte goes on the wire.

Sources: [INV] Hybrid Inverter Protocol V1.1.17 (Table 4, Appendix 1, 6);
[IHM] iHM Protocol V1.0.2 (Table 4); AC22 wallbox map (community, not a
Sungrow document: unverified).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from types import MappingProxyType
from typing import Final

from .registers import DEVICE_TYPE_CODES, DataType, Device, Table

# ---------------------------------------------------------------------------
# Model families and profiles
# ---------------------------------------------------------------------------


class ModelFamily(StrEnum):
    """Inverter series as grouped in [INV] Appendix 1, Table 10."""

    SH_RS_SMALL = "SH3.0-6.0RS"
    SH_RS = "SH8.0-10RS"
    SH_RT = "SH5.0-10RT"
    SH_T = "SH5-25T"
    MG_RL = "MG5-12RL"
    SH_RL = "SH3-10RL"
    SH_CX = "SH50-125CX"
    MG_TL = "MG10-25TL"
    UNKNOWN = "unknown"


_FAMILY_CODES: Final[Mapping[ModelFamily, frozenset[int]]] = MappingProxyType(
    {
        ModelFamily.SH_RS_SMALL: frozenset({0xD17, 0xD0D, 0xD18, 0xD0F, 0xD10}),
        ModelFamily.SH_RS: frozenset({0xD1A, 0xD1B}),
        ModelFamily.SH_RT: frozenset(range(0xE00, 0xE04))
        | frozenset(range(0xE08, 0xE14)),
        ModelFamily.SH_T: frozenset(
            {0xE20, 0xE21, 0xE22, 0xE23, 0xE24, 0xE25, 0xE26, 0xE28}
        ),
        ModelFamily.MG_RL: frozenset({0xD27, 0xD28, 0xD31, 0xD29, 0xD2A, 0xD2F}),
        ModelFamily.SH_RL: frozenset({0xD41, 0xD42, 0xD43, 0xD2B, 0xD2C, 0xD2D, 0xD2E}),
        ModelFamily.SH_CX: frozenset({0xE51, 0xE52, 0xE39, 0xE3A, 0xE3D}),
        ModelFamily.MG_TL: frozenset({0xE53, 0xE54, 0xE55, 0xE56, 0xE57}),
    }
)

#: MPPT count per device type code ([INV] Table 10).
_MPPT_COUNT: Final[Mapping[int, int]] = MappingProxyType(
    {
        **dict.fromkeys(_FAMILY_CODES[ModelFamily.SH_RS_SMALL], 2),
        **dict.fromkeys(_FAMILY_CODES[ModelFamily.SH_RS], 4),
        **dict.fromkeys(_FAMILY_CODES[ModelFamily.SH_RT], 2),
        **dict.fromkeys((0xE20, 0xE21, 0xE22, 0xE23, 0xE24), 2),
        **dict.fromkeys((0xE25, 0xE26, 0xE28), 3),
        **dict.fromkeys((0xD27, 0xD28), 2),
        **dict.fromkeys((0xD31, 0xD29, 0xD2A, 0xD2F), 3),
        **dict.fromkeys((0xD41, 0xD42, 0xD43, 0xD2B, 0xD2C), 2),
        **dict.fromkeys((0xD2D, 0xD2E), 3),
        **dict.fromkeys(_FAMILY_CODES[ModelFamily.SH_CX], 10),
        **dict.fromkeys(_FAMILY_CODES[ModelFamily.MG_TL], 3),
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelProfile:
    """What the integration needs to know about one inverter model."""

    family: ModelFamily
    model: str
    device_type_code: int | None
    phases: int  # 1 or 3; from the family, confirmed live by IR 5002
    mppt_count: int
    #: Read blocks the model does not have (skipped instead of refused).
    skip_blocks: frozenset[str] = frozenset()
    #: Register keys the protocol says this model does not support.
    unsupported_keys: frozenset[str] = frozenset()
    #: Write target keys the protocol documents for this model.
    write_targets: frozenset[str] = frozenset()

    @property
    def three_phase(self) -> bool:
        return self.phases == 3

    @property
    def known(self) -> bool:
        return self.family is not ModelFamily.UNKNOWN


def family_for(device_type_code: int | None) -> ModelFamily:
    """Model family for an IR 5000 device type code."""
    if device_type_code is None:
        return ModelFamily.UNKNOWN
    for family, codes in _FAMILY_CODES.items():
        if device_type_code in codes:
            return family
    return ModelFamily.UNKNOWN


#: Keys behind the phase B/C entities that only carry data on three-phase
#: models. On a single-phase model they read 0 and are disabled by default.
THREE_PHASE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "mb_phase_b_voltage",
        "mb_phase_c_voltage",
        "mb_phase_b_current",
        "mb_phase_c_current",
        "mb_backup_phase_b_current",
        "mb_backup_phase_c_current",
        "mb_backup_phase_b_power",
        "mb_backup_phase_c_power",
        "mb_backup_phase_b_voltage",
        "mb_backup_phase_c_voltage",
    }
)

#: Inverter write targets per family ([INV] Table 4 remarks). 13100 and
#: 13050-13052 carry no model exclusions; 33208-33219 exclude MG5-12RL,
#: SH3-10RL, MG10-25TL and SH50-125CX.
_INVERTER_WRITES_ALL: Final = frozenset(
    {
        "backup_reserve_soc",
        "inverter_ems_mode",
        "inverter_charge_command",
        "inverter_charge_power",
    }
)


@lru_cache(maxsize=16)
def profile_for(device_type_code: int | None) -> ModelProfile:
    """Profile for a device type code (unknown codes get a cautious profile)."""
    family = family_for(device_type_code)
    model = (
        DEVICE_TYPE_CODES.get(device_type_code, f"0x{device_type_code:04X}")
        if device_type_code is not None
        else "unknown"
    )
    mppt = _MPPT_COUNT.get(device_type_code or -1, 4)
    phases = (
        3
        if family
        in (ModelFamily.SH_RT, ModelFamily.SH_T, ModelFamily.SH_CX, ModelFamily.MG_TL)
        else 1
    )
    skip: set[str] = set()
    if mppt < 4:
        skip.add("inv_mppt4")
    unsupported: set[str] = set()
    if family in (
        ModelFamily.SH_T,
        ModelFamily.MG_RL,
        ModelFamily.SH_RL,
        ModelFamily.MG_TL,
    ):
        # [INV] #115: 13039 only lists SH5.0-10RT, SH3.0-6.0RS, SH8.0-10RS, CX.
        unsupported.add("mb_battery_capacity_13039")
    if family in (ModelFamily.MG_RL, ModelFamily.SH_RL):
        unsupported.update(
            {
                "mb_drm_state",
                "mb_feed_in_limitation_ratio",
                "mb_active_power_limitation",
                "mb_active_power_limit_ratio",
            }
        )
    if family is ModelFamily.UNKNOWN:
        writes: frozenset[str] = frozenset()
    else:
        writes = _INVERTER_WRITES_ALL
    return ModelProfile(
        family=family,
        model=model,
        device_type_code=device_type_code,
        phases=phases,
        mppt_count=mppt,
        skip_blocks=frozenset(skip),
        unsupported_keys=frozenset(unsupported),
        write_targets=writes,
    )


# ---------------------------------------------------------------------------
# Write targets
# ---------------------------------------------------------------------------


class Evidence(StrEnum):
    """How much we know about a write target.

    DOCUMENTED: address, type and values are in the Sungrow protocol PDF.
    FIELD: values seen on real hardware but not (all) in the PDF.
    UNVERIFIED: community map only. Needs "Allow unverified writes".
    """

    DOCUMENTED = "documented"
    FIELD = "field"
    UNVERIFIED = "unverified"


#: Where a target's device lives. AC22 sits behind the inverter's WiNet-S
#: on its own unit ID.
class WriteDevice(StrEnum):
    INVERTER = "inverter"
    IHM = "ihm"
    AC22 = "ac22"


@dataclass(frozen=True, slots=True, kw_only=True)
class WriteTarget:
    """One register the integration may write, with its guardrails.

    Values are passed to the writer in engineering units (%, kW, A, or the
    raw enum code for ``allowed`` targets) and converted with ``scale``.
    """

    key: str
    device: WriteDevice
    address: int  # documented, 1-based, holding table
    dtype: DataType
    scale: float = 1.0
    unit: str | None = None
    #: Static bounds in engineering units (dynamic bounds come from the caller).
    min_value: float | None = None
    max_value: float | None = None
    #: Raw codes that may be written (enum targets). None = numeric target.
    allowed: frozenset[int] | None = None
    #: Raw codes that additionally need "Allow unverified writes".
    allowed_unverified: frozenset[int] = frozenset()
    #: Seconds between two writes of this register (EEPROM / dispatch guard).
    cooldown: float = 10.0
    #: Writes per rolling 24 h before the writer refuses (EEPROM wear guard).
    daily_budget: int = 48
    #: Seconds to wait before reading back to verify.
    readback_delay: float = 2.0
    evidence: Evidence = Evidence.DOCUMENTED
    #: Register key in ``registers.py`` that reads the same value back for the
    #: coordinator, if any.
    read_key: str | None = None
    note: str = ""

    @property
    def table(self) -> Table:
        return Table.HOLDING

    @property
    def words(self) -> int:
        return 2 if self.dtype in (DataType.U32, DataType.S32) else 1

    @property
    def register_device(self) -> Device | None:
        """Matching ``registers.Device`` (None for AC22, not in the read map)."""
        return {
            WriteDevice.INVERTER: Device.INVERTER,
            WriteDevice.IHM: Device.IHM,
        }.get(self.device)


_TARGETS: Final[tuple[WriteTarget, ...]] = (
    # --- Inverter [INV] Table 4 ---------------------------------------------
    WriteTarget(
        key="backup_reserve_soc",
        device=WriteDevice.INVERTER,
        address=13100,
        dtype=DataType.U16,
        unit="%",
        min_value=0,
        max_value=100,
        cooldown=30.0,
        daily_budget=24,
        read_key="mb_backup_reserve_soc",
        note="[INV] #46 / 3.3.2. On grid, SoC below the reserve triggers emergency "
        "charging up to the reserve. Persistent setting: kept to 24 writes a day.",
    ),
    WriteTarget(
        key="inverter_ems_mode",
        device=WriteDevice.INVERTER,
        address=13050,
        dtype=DataType.U16,
        allowed=frozenset({0, 2}),
        read_key="mb_ems_mode",
        note="[INV] #28. Only used without an iHM: with an iHM the inverter runs "
        "in External EMS (3) and belongs to the iHM.",
    ),
    WriteTarget(
        key="inverter_charge_command",
        device=WriteDevice.INVERTER,
        address=13051,
        dtype=DataType.U16,
        allowed=frozenset({0xAA, 0xCC}),
        read_key="mb_charge_discharge_command",
    ),
    WriteTarget(
        key="inverter_charge_power",
        device=WriteDevice.INVERTER,
        address=13052,
        dtype=DataType.U16,
        unit="W",
        min_value=0,
        max_value=65535,
        read_key="mb_charge_discharge_power",
        note="[INV] #30: 0-100 % of BDC rated power (IR 5628).",
    ),
    # --- iHomeManager [IHM] Table 4 -------------------------------------------
    WriteTarget(
        key="ihm_ems_mode",
        device=WriteDevice.IHM,
        address=8024,
        dtype=DataType.U16,
        allowed=frozenset({1, 5}),
        read_key="ihm_ems_mode",
        note="1 Self-consumption, 5 Compulsory (app: Forced mode). 4 VPP is "
        "refused: it needs a heartbeat (8033) this integration does not send. "
        "The app's AI mode and Time plan codes are not documented.",
    ),
    WriteTarget(
        key="ihm_charge_command",
        device=WriteDevice.IHM,
        address=8025,
        dtype=DataType.U16,
        allowed=frozenset({0xAA, 0xCC}),
        read_key="ihm_charge_discharge_command",
        note="0xBB (discharge) deliberately not allowed yet.",
    ),
    WriteTarget(
        key="ihm_charge_power",
        device=WriteDevice.IHM,
        address=8026,
        dtype=DataType.U32,
        scale=0.1,
        unit="kW",
        min_value=0,
        read_key="ihm_charge_discharge_power",
        note="U32 x0.1 kW low word first, FC16. Upper bound: IR 8149-8150.",
    ),
    WriteTarget(
        key="ihm_charger_mode",
        device=WriteDevice.IHM,
        address=8048,
        dtype=DataType.U16,
        allowed=frozenset({0xA4, 0xA5, 0xA6}),
        allowed_unverified=frozenset({0xA7}),
        evidence=Evidence.FIELD,
        read_key="ihm_charger_mode",
        note="Codes read on iHM SV930.001.00.P014 + AC22E-01 (PDF lists only "
        "0xA0/0xA1). 0xA7 Time plan: unknown whether a write restores the "
        "saved schedule, so it needs 'Allow unverified writes'.",
    ),
    # --- AC22 behind the WiNet-S (community map) --------------------------------
    WriteTarget(
        key="ac22_charge_current",
        device=WriteDevice.AC22,
        address=21203,
        dtype=DataType.U16,
        scale=0.1,
        unit="A",
        min_value=6.0,
        max_value=32.0,
        cooldown=15.0,
        daily_budget=200,
        evidence=Evidence.UNVERIFIED,
        note="Output current setpoint, HR 21203 x0.1 A (wallbox community map). "
        "Upper bound: rated current IR 21263. While the iHM manages the "
        "charger it may overwrite this.",
    ),
)

WRITE_TARGETS: Final[Mapping[str, WriteTarget]] = MappingProxyType(
    {t.key: t for t in _TARGETS}
)

#: Read-back registers for an AC22 (not in the coordinator's read map).
AC22_RATED_CURRENT_ADDRESS: Final = 21263  # IR, U16 x0.1 A
AC22_RATED_CURRENT_SCALE: Final = 0.1

#: The iHM app modes that have a confirmed 8024 code. AI mode and Time plan
#: are app features whose 8024 value is not documented: they show as
#: "Unknown (n)" until a read on site maps them.
IHM_APP_EMS_MODES: Final[Mapping[int, str]] = MappingProxyType(
    {1: "Self-consumption", 5: "Forced mode"}
)

#: EV charger modes offered in the select, raw 8048 code -> app label.
IHM_EV_CHARGER_MODES: Final[Mapping[int, str]] = MappingProxyType(
    {
        0xA4: "Fast charging",
        0xA5: "ECO & fast charging",
        0xA6: "ECO & economic charging",
        0xA7: "Time plan",
    }
)


def validate_targets() -> None:
    """Internal consistency of the allowlist. Runs at import."""
    seen: set[tuple[WriteDevice, int]] = set()
    for t in _TARGETS:
        span = {(t.device, a) for a in range(t.address, t.address + t.words)}
        if seen & span:
            raise ValueError(f"Write target {t.key} overlaps another target")
        seen |= span
        if t.allowed is None and t.allowed_unverified:
            raise ValueError(f"{t.key}: allowed_unverified needs allowed")
        if t.allowed is not None and (
            t.min_value is not None or t.max_value is not None
        ):
            raise ValueError(f"{t.key}: enum targets take no numeric bounds")
        if t.cooldown <= 0 or t.daily_budget < 1:
            raise ValueError(f"{t.key}: cooldown and budget must be positive")


validate_targets()
