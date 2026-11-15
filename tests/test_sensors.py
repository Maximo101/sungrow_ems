"""Entity descriptions, derived calculators, translations and diagnostics."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow_ems import sensor_types
from custom_components.sungrow_ems.binary_sensor import BINARY_SENSORS
from custom_components.sungrow_ems.derived import (
    DERIVED_SENSORS,
    DailyMeter,
    LeftRiemann,
    SpikeFilter,
    _split_pct,
    system_status,
)
from custom_components.sungrow_ems.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.sungrow_ems.registers import REGISTERS_BY_KEY, Table
from custom_components.sungrow_ems.sensor_types import SENSOR_DESCRIPTIONS, Kind

from .conftest import SERIAL

PACKAGE = Path(sensor_types.__file__).parent


def test_entity_counts() -> None:
    # 161 register sensors + 2 poll status + 21 derived + 3 binary = 187
    assert len(SENSOR_DESCRIPTIONS) == 161 == len(REGISTERS_BY_KEY)
    assert len(DERIVED_SENSORS) == 21
    assert len(BINARY_SENSORS) == 3
    keys = [d.key for d in (*SENSOR_DESCRIPTIONS, *DERIVED_SENSORS, *BINARY_SENSORS)]
    assert len(keys) == len(set(keys))


def test_every_kind_has_a_preset() -> None:
    reg = REGISTERS_BY_KEY["mb_battery_soc"]
    for kind in Kind:
        assert isinstance(sensor_types._preset(kind, reg), dict)
    with pytest.raises(ValueError):
        Kind("not_a_kind")


def test_device_class_unit_and_state_class_agree() -> None:
    for d in (*SENSOR_DESCRIPTIONS, *DERIVED_SENSORS):
        unit, dc, sc = d.native_unit_of_measurement, d.device_class, d.state_class
        if dc is SensorDeviceClass.ENERGY:
            assert unit == UnitOfEnergy.KILO_WATT_HOUR, d.key
            assert sc in (SensorStateClass.TOTAL_INCREASING, SensorStateClass.TOTAL), (
                d.key
            )
        elif dc is SensorDeviceClass.ENERGY_STORAGE:
            assert sc is SensorStateClass.MEASUREMENT, d.key
        elif dc is SensorDeviceClass.POWER:
            assert unit in (UnitOfPower.WATT, UnitOfPower.KILO_WATT), d.key
            assert sc is SensorStateClass.MEASUREMENT, d.key
        if sc is not None and dc not in (None, SensorDeviceClass.POWER_FACTOR):
            assert unit is not None, d.key
        if unit is None and dc is None:
            # Text, enum and bitfield sensors must not produce statistics,
            # except plain counts.
            assert sc is None or d.suggested_display_precision == 0, d.key


def test_numeric_precision_follows_the_register_scale() -> None:
    by_key = {d.key: d for d in SENSOR_DESCRIPTIONS}
    assert by_key["mb_battery_soc"].suggested_display_precision == 1
    assert by_key["mb_total_dc_power"].suggested_display_precision == 0
    assert by_key["mb_battery_capacity"].suggested_display_precision == 2


def test_ihm_power_is_published_in_watts() -> None:
    d = next(x for x in SENSOR_DESCRIPTIONS if x.key == "ihm_grid_active_power")
    assert d.native_unit_of_measurement == UnitOfPower.WATT
    assert d.value_fn is not None and d.value_fn(1.23, 123) == 1230


def test_settings_readback_is_diagnostic() -> None:
    """Holding registers are shown for information only."""
    for d in SENSOR_DESCRIPTIONS:
        if REGISTERS_BY_KEY[d.key].table is Table.HOLDING:
            assert d.entity_category is EntityCategory.DIAGNOSTIC, d.key


def test_daily_derived_totals_reset_at_midnight() -> None:
    for d in DERIVED_SENSORS:
        if d.state_class is SensorStateClass.TOTAL:
            assert d.daily_total, d.key


def test_translations_match_strings_and_mention_no_control() -> None:
    strings = (PACKAGE / "strings.json").read_text(encoding="utf-8")
    assert json.loads(strings) == json.loads(
        (PACKAGE / "translations" / "en.json").read_text(encoding="utf-8")
    )
    assert "Phase 2" not in strings
    assert "ontrol" not in strings


def test_manifest() -> None:
    manifest = json.loads((PACKAGE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == "sungrow_ems"
    assert manifest["iot_class"] == "local_polling"
    assert manifest["config_flow"] is True
    assert manifest["requirements"] == ["pymodbus>=3.10.0"]


# -- derived calculators ------------------------------------------------------


def test_spike_filter_rejects_then_rebaselines() -> None:
    f = SpikeFilter(max_jump=50, rebaseline_count=3)
    assert f.update(1000.0) and f.value == 1000.0
    assert f.update(1001.5) and f.value == 1001.5
    assert not f.update(1001.0)  # small step back: ignored, no meter reset
    assert not f.update(0.0)  # at or below min_valid
    assert not f.update(99999.0) and f.rejected == 1  # single spike
    assert f.update(1002.0) and f.value == 1002.0
    assert not f.update(5000.0)
    assert not f.update(5000.5)
    assert f.update(5001.0) and f.value == 5001.0  # 3 consistent readings
    assert f.rebaselines == 1


def test_daily_meter_rolls_and_skips_jumps() -> None:
    m = DailyMeter(max_delta=50)
    day1, day2 = date(2026, 11, 14), date(2026, 11, 15)
    m.update(100.0, day1)
    assert m.update(102.5, day1) and m.value == 2.5
    m.update(500.0, day1)  # implausible step
    assert m.value == 2.5 and m.skipped == 1
    m.update(90.0, day1)  # source reset: re-based, nothing added
    assert m.value == 2.5
    assert m.update(91.0, day2)
    assert m.value == 1.0 and m.period == day2


def test_left_riemann_skips_long_gaps() -> None:
    r = LeftRiemann(max_gap=300)
    r.update(3600.0, 0.0)
    assert r.update(3600.0, 10.0)
    assert r.value == pytest.approx(0.01)  # 3.6 kW for 10 s
    r.update(3600.0, 10_000.0)  # outage: not integrated
    assert r.value == pytest.approx(0.01)


def test_split_pct_always_sums_to_100() -> None:
    assert _split_pct([28.2, 70.4, 1.4], 100.0) == [28, 71, 1]
    assert sum(_split_pct([1, 1, 1], 3)) == 100
    assert _split_pct([1, 2], 0) == [0, 0]


def test_system_status() -> None:
    assert system_status(None)[0] == "Offline / Unavailable"
    label, icon, attrs = system_status(0x0B)
    assert label == "PV & Battery Charging (Active Load)"
    assert icon == "mdi:battery-charging"
    assert attrs["core_code"] == 11
    assert system_status(0x05)[0] == "PV Generating / Battery Discharging"


# -- diagnostics --------------------------------------------------------------


async def test_diagnostics_are_redacted(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag, default=str)
    assert SERIAL not in text
    assert "inverter.test" not in text and "ihm.test" not in text
    assert set(diag["coordinators"]) == {"inverter", "ihm"}
    assert diag["coordinators"]["inverter"]["values"]["mb_battery_soc"] == 65.5
    assert diag["derived"]
