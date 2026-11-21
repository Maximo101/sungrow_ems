"""Register map, block plan and decoder."""

from __future__ import annotations

import pytest

from custom_components.sungrow_ems.registers import (
    ALL_REGISTERS,
    BLOCKS,
    MAX_BLOCK_WORDS,
    REGISTERS_BY_KEY,
    DataType,
    Device,
    Register,
    Table,
    blocks_for,
    contiguous_runs,
    decode_register,
    decode_registers,
    decode_string,
    decode_tables,
    to_wire,
    validate_map,
)


def _reg(dtype: DataType, **kw: object) -> Register:
    return Register(
        key="t",
        device=Device.INVERTER,
        table=Table.INPUT,
        address=100,
        dtype=dtype,
        **kw,
    )  # type: ignore[arg-type]


def test_map_is_consistent() -> None:
    validate_map()
    assert len(ALL_REGISTERS) == 163
    assert len(BLOCKS) == 17
    assert len(REGISTERS_BY_KEY) == len(ALL_REGISTERS)


def test_every_register_is_in_exactly_one_small_block() -> None:
    covered = [r.key for b in BLOCKS for r in b.registers]
    assert sorted(covered) == sorted(REGISTERS_BY_KEY)
    assert all(b.count <= MAX_BLOCK_WORDS for b in BLOCKS)


def test_settings_blocks_are_polled_slowly() -> None:
    """Holding registers are readback only: none is read on every poll."""
    holding = [b for b in BLOCKS if b.table is Table.HOLDING]
    assert holding
    assert all(b.refresh_interval and b.refresh_interval >= 60 for b in holding)
    fast_ihm = [b.name for b in blocks_for(Device.IHM) if b.refresh_interval is None]
    assert fast_ihm == ["ihm_system", "ihm_meter_charger"]


def test_to_wire() -> None:
    assert to_wire(5003) == 5002
    with pytest.raises(ValueError):
        to_wire(0)


@pytest.mark.parametrize(
    ("dtype", "words", "scale", "expected"),
    [
        (DataType.U16, [655], 0.1, 65.5),
        (DataType.S16, [0xFFF6], 1.0, -10),
        (DataType.U32, [0xE240, 0x0001], 0.1, 12345.6),  # low word first
        (DataType.S32, [0xFF9C, 0xFFFF], 1.0, -100),
        (DataType.U16, [3], 10.0, 30),
    ],
)
def test_decode_numeric(
    dtype: DataType, words: list[int], scale: float, expected: float
) -> None:
    value, raw = decode_register(_reg(dtype, scale=scale), words)
    assert value == expected
    assert raw is not None


@pytest.mark.parametrize(
    ("dtype", "words"),
    [
        (DataType.U16, [0xFFFF]),
        (DataType.S16, [0x7FFF]),
        (DataType.U32, [0xFFFF, 0xFFFF]),
        (DataType.S32, [0xFFFF, 0x7FFF]),
    ],
)
def test_sentinels_are_invalid(dtype: DataType, words: list[int]) -> None:
    assert decode_register(_reg(dtype), words) == (None, None)


def test_out_of_range_and_short_reads_are_invalid() -> None:
    reg = _reg(DataType.U16, scale=0.1, valid_min=0.0, valid_max=100.0)
    assert decode_register(reg, [1001]) == (None, 1001)
    assert decode_register(reg, [1000]) == (100.0, 1000)
    assert decode_register(_reg(DataType.U32), [1]) == (None, None)


def test_corrupt_lifetime_counter_is_rejected() -> None:
    """SH10RS returns a 0xFFFF high word for 13041; it must not become a value."""
    reg = REGISTERS_BY_KEY["mb_total_battery_charge"]
    result = decode_registers([reg], {reg.address: 0x1234, reg.address + 1: 0xFFFF})
    assert result.values == {}
    assert result.invalid == {reg.key}


def test_decode_string() -> None:
    assert decode_string([0x4142, 0x4300]) == "ABC"
    assert decode_string([0x0000, 0x0000]) is None
    assert decode_string([0xFFFF, 0xFFFF]) is None


def test_enum_label() -> None:
    reg = REGISTERS_BY_KEY["ihm_ems_mode"]
    assert reg.label(1) == "Self-consumption"
    assert reg.label(99) == "Unknown (99)"
    assert REGISTERS_BY_KEY["ihm_feed_in_limitation"].label(0x55) == "Off"


def test_contiguous_runs() -> None:
    a = Register(
        key="a", device=Device.IHM, table=Table.INPUT, address=10, dtype=DataType.U32
    )
    b = Register(
        key="b", device=Device.IHM, table=Table.INPUT, address=12, dtype=DataType.U16
    )
    c = Register(
        key="c", device=Device.IHM, table=Table.INPUT, address=20, dtype=DataType.U16
    )
    assert contiguous_runs([c, a, b]) == [(10, 3), (20, 1)]


def test_missing_words_are_skipped_not_invalid() -> None:
    reg = REGISTERS_BY_KEY["mb_battery_soc"]
    result = decode_registers([reg], {})
    assert not result.values and not result.invalid


def test_tables_do_not_alias() -> None:
    """13050 is an alarm bitfield in the input table and EMS mode in holding."""
    blocks = [b for b in BLOCKS if b.name in ("inv_faults", "inv_hold_ems")]
    result = decode_tables(
        blocks, {Table.INPUT: {13050: 4, 13051: 0}, Table.HOLDING: {13050: 0}}
    )
    assert result.raw["mb_inverter_alarm"] == 4
    assert result.values["mb_ems_mode"] == 0


# ---------------------------------------------------------------------------
# Protocol V1.1.17 (SH8.0-10RS)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "label"),
    [
        (0x0014, "Microgrid operation"),
        (0x0080, "Derating running"),
        (0x8100, "Derating running"),
        (0x2500, "Communication fault"),
        (0x2501, "Restarting"),
    ],
)
def test_running_states_v1_1_17(code: int, label: str) -> None:
    assert REGISTERS_BY_KEY["mb_running_state"].label(code) == label


@pytest.mark.parametrize(
    ("key", "words", "expected"),
    [
        ("mb_battery_current_13021", [0xFFFF], -0.1),
        ("mb_battery_power_13022", [0xFFFF], -1),
        ("mb_battery_power_13022", [0xF830], -2000),
    ],
)
def test_13021_13022_are_s16_on_sh10rs(
    key: str, words: list[int], expected: float
) -> None:
    """V1.1.17: S16 on SH8.0-10RS, so 0xFFFF is -1 count, not a sentinel."""
    value, _ = decode_register(REGISTERS_BY_KEY[key], words)
    assert value == expected


def test_13021_13022_s16_sentinel_still_invalid() -> None:
    for key in ("mb_battery_current_13021", "mb_battery_power_13022"):
        assert decode_register(REGISTERS_BY_KEY[key], [0x7FFF]) == (None, None)


def test_inverter_protocol_version_format() -> None:
    from custom_components.sungrow_ems.registers import (
        format_inverter_protocol_version,
    )

    # V1.1.17 Table 3 #2 example: 0x01010700 is V1.1.7.
    assert format_inverter_protocol_version(0x01010700) == "V1.1.7"
    assert format_inverter_protocol_version(0x01011100) == "V1.1.17"
    assert format_inverter_protocol_version(None) is None


def test_drm_state_labels() -> None:
    reg = REGISTERS_BY_KEY["mb_drm_state"]
    assert reg.label(1) == "DRM0"
    assert reg.label(9) == "DRM8"
    assert reg.label(0) == "Unknown (0)"


def test_sh10rs_model_code() -> None:
    assert REGISTERS_BY_KEY["mb_device_type_code"].label(0xD1B) == "SH10RS"


def test_power_limitation_registers_in_existing_block() -> None:
    """13088-13090 ride in the existing 13050-13100 read: no extra request."""
    block = next(b for b in BLOCKS if b.name == "inv_hold_ems")
    keys = {r.key for r in block.registers}
    assert {
        "mb_feed_in_limitation_ratio",
        "mb_active_power_limitation",
        "mb_active_power_limit_ratio",
    } <= keys
    assert (block.start, block.end) == (13050, 13100)
    words = {a: 0 for a in range(13050, 13101)}
    words.update({13088: 1000, 13089: 0x55, 13090: 500})
    result = decode_tables([block], {Table.HOLDING: words})
    assert result.values["mb_feed_in_limitation_ratio"] == 100.0
    assert (
        REGISTERS_BY_KEY["mb_active_power_limitation"].label(
            result.raw["mb_active_power_limitation"]
        )
        == "Disabled"
    )
    assert result.values["mb_active_power_limit_ratio"] == 50.0


def test_inverter_settings_read_rarely() -> None:
    """[INV] 1.2 rule 6: RW registers are not polled often over WiNet-S."""
    inv_holding = blocks_for(Device.INVERTER, Table.HOLDING)
    assert inv_holding
    assert all(b.refresh_interval == 300 for b in inv_holding)


@pytest.mark.parametrize(
    ("table", "first", "last"),
    [
        (Table.INPUT, 4950, 4951),  # Reserved since V1.1.7
        (Table.INPUT, 5117, 5133),  # MPPT 5-10, SH50~125CX only
        (Table.INPUT, 13200, 13207),  # Channel 2 meter, not on SH3.0-10RS
        (Table.INPUT, 13250, 13369),  # Firmware info, not on SH3.0-10RS
        (Table.HOLDING, 13001, 13001),  # DO configuration, not on SH3.0-10RS
        (Table.HOLDING, 13017, 13018),  # Forced startup (command), PV limit (SHT)
        (Table.HOLDING, 33274, 33274),  # Load rated power (needs DO)
    ],
)
def test_sh10rs_unsupported_registers_not_mapped(
    table: Table, first: int, last: int
) -> None:
    clash = [
        r.key
        for r in ALL_REGISTERS
        if r.device is Device.INVERTER
        and r.table is table
        and r.address <= last
        and r.end >= first
    ]
    assert clash == []
