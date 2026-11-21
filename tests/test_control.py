"""Opt-in control in Home Assistant: entities, actions and the forced charge."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
import functools
from typing import Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sungrow_ems import controller as controller_mod
from custom_components.sungrow_ems.const import (
    CONF_AC22_UNIT,
    CONF_CONTROL_ALLOW_UNVERIFIED,
    CONF_CONTROL_ENABLED,
    CONF_FORCE_CHARGE_TIMEOUT,
    DOMAIN,
)
from custom_components.sungrow_ems.entity import make_unique_id
from custom_components.sungrow_ems.writer import SafeWriter

from .conftest import ENTRY_DATA, SERIAL, FakeDevice

RESERVE = "number.sungrow_ems_inverter_backup_reserve"
EMS_MODE = "select.sungrow_ems_ihomemanager_energy_management_mode"
EV_MODE = "select.sungrow_ems_ihomemanager_ev_charger_mode"
FC_STATE = "sensor.sungrow_ems_ihomemanager_force_charge_state"
AC22 = "number.sungrow_ems_ihomemanager_ev_charge_current"


async def _no_sleep(_seconds: float) -> None:
    return None


class Clock:
    """Monotonic clock for the writer's cooldowns, advanced by the tests."""

    def __init__(self) -> None:
        self.t = 10_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture(autouse=True)
def fast_readback(clock: Clock) -> Iterator[None]:
    """The writer waits 2 s before reading back; not in tests."""
    with patch.object(
        controller_mod,
        "SafeWriter",
        functools.partial(SafeWriter, sleep=_no_sleep, clock=clock),
    ):
        yield


@pytest.fixture(autouse=True)
def site(inverter: FakeDevice, ihm: FakeDevice) -> None:
    """Holding registers as recorded on the SH10RS site before testing."""
    inverter.set("mb_backup_reserve_soc", 6)
    inverter.set("mb_min_soc", 50)  # 5.0 %
    inverter.set("mb_max_soc", 1000)  # 100.0 %
    inverter.set("mb_ems_mode", 3)
    inverter.set("mb_bdc_rated_power", 100)  # 10 kW
    ihm.set("ihm_ems_mode", 1)
    ihm.set("ihm_charge_discharge_command", 0xAA)
    ihm.set("ihm_charge_discharge_power", 0)
    ihm.set("ihm_battery_power_limit", 100)  # 10.0 kW
    ihm.set("ihm_max_charge_power", 80)  # 8.0 kW
    ihm.set("ihm_charger_mode", 0xA7)
    ihm.set("ihm_battery_soc", 655)


def _entry(**options: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Sungrow SH10RS",
        data=dict(ENTRY_DATA),
        options={CONF_CONTROL_ENABLED: True, **options},
        unique_id=SERIAL,
        version=1,
        minor_version=3,
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    for coordinator in entry.runtime_data.coordinators:
        await coordinator.async_refresh()
    await hass.async_block_till_done()


def _writes(dev: FakeDevice) -> list[tuple[int, list[int]]]:
    return [(a, v) for a, v, _u in dev.writes]


async def test_control_entities_and_reserve(
    hass: HomeAssistant, inverter: FakeDevice
) -> None:
    entry = _entry()
    await _setup(hass, entry)
    state = hass.states.get(RESERVE)
    assert state is not None and float(state.state) == 6
    assert state.attributes["min"] == 5.0  # Min SoC from 13059
    assert hass.states.get(EMS_MODE).state == "Self-consumption"
    assert hass.states.get(EV_MODE).state == "Time plan"
    assert hass.states.get(FC_STATE).state == "idle"
    assert hass.states.get(AC22) is None  # no AC22 unit configured

    await hass.services.async_call(
        "number", "set_value", {"entity_id": RESERVE, "value": 10}, blocking=True
    )
    assert _writes(inverter) == [(13100, [10])]
    assert float(hass.states.get(RESERVE).state) == 10  # pushed in, no wait
    assert (
        float(hass.states.get("sensor.sungrow_ems_inverter_backup_reserve_soc").state)
        == 10
    )

    # Unchanged value: nothing written. Below Min SoC: refused.
    await hass.services.async_call(
        DOMAIN, "set_backup_reserve", {"percent": 10}, blocking=True
    )
    assert len(inverter.writes) == 1
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "set_backup_reserve", {"percent": 3}, blocking=True
        )
    # Cooldown (30 s for 13100).
    with pytest.raises(ServiceValidationError, match=r"cooldown|wait"):
        await hass.services.async_call(
            DOMAIN, "set_backup_reserve", {"percent": 12}, blocking=True
        )
    assert len(inverter.writes) == 1


async def test_selects(hass: HomeAssistant, ihm: FakeDevice) -> None:
    entry = _entry()
    await _setup(hass, entry)
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": EV_MODE, "option": "Fast charging"},
        blocking=True,
    )
    assert _writes(ihm) == [(8048, [0xA4])]
    assert hass.states.get(EV_MODE).state == "Fast charging"
    # Time plan is shown but needs "Allow unverified writes".
    with pytest.raises(ServiceValidationError, match="unverified"):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": EV_MODE, "option": "Time plan"},
            blocking=True,
        )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": EMS_MODE, "option": "Forced mode"},
        blocking=True,
    )
    assert _writes(ihm)[-1] == (8024, [5])

    # An app mode without a documented code shows as unknown, with the code.
    ihm.set("ihm_ems_mode", 2)
    entry.runtime_data.ihm._block_last_read.clear()
    await _poll(hass, entry)
    state = hass.states.get(EMS_MODE)
    assert state.state == "unknown"
    assert state.attributes["raw_code"] == 2


async def test_force_charge_reaches_target_and_restores(
    hass: HomeAssistant, inverter: FakeDevice, ihm: FakeDevice
) -> None:
    entry = _entry()
    await _setup(hass, entry)
    response = await hass.services.async_call(
        DOMAIN,
        "force_charge_start",
        {"target_soc": 80},
        blocking=True,
        return_response=True,
    )
    assert response["method"] == "ihm_compulsory"
    assert response["power_kw"] == 10.0  # iHM upper limit 8149
    assert response["previous_settings"] == {
        "ihm_charge_power": 0,
        "ihm_charge_command": 0xAA,
        "ihm_ems_mode": 1,
    }
    # Power first, then the command (unchanged: skipped), then the mode.
    assert _writes(ihm) == [(8026, [100, 0]), (8024, [5])]
    assert hass.states.get(FC_STATE).state == "charging"
    assert hass.states.get(EMS_MODE).state == "Forced mode"

    # The EMS mode select is locked while the session runs.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": EMS_MODE, "option": "Self-consumption"},
            blocking=True,
        )

    inverter.set("mb_battery_soc", 801)
    await _poll(hass, entry)
    await hass.async_block_till_done()
    assert hass.states.get(FC_STATE).state == "target_reached"
    # Restore: mode first, then power. Exactly the previous values.
    assert _writes(ihm)[2:] == [(8024, [1]), (8026, [0, 0])]
    attrs = hass.states.get(FC_STATE).attributes
    assert attrs["previous_settings"]["ihm_ems_mode"] == 1
    assert attrs["reason"].startswith("SoC")


async def test_force_charge_timeout_cancel_and_stop(
    hass: HomeAssistant, ihm: FakeDevice, clock: Clock
) -> None:
    entry = _entry(**{CONF_FORCE_CHARGE_TIMEOUT: 30})
    await _setup(hass, entry)

    await hass.services.async_call(
        DOMAIN, "force_charge_start", {"target_soc": 90, "power_kw": 5}, blocking=True
    )
    assert ihm.writes[0][1] == [50, 0]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=31))
    await hass.async_block_till_done()
    state = hass.states.get(FC_STATE)
    assert state.state == "aborted" and state.attributes["reason"] == "timeout"
    assert ihm.words["holding"][8024] == 1

    # Straight away: the 10 s per-register cooldown protects the device.
    with pytest.raises(Exception, match="wait"):
        await hass.services.async_call(
            DOMAIN,
            "force_charge_start",
            {"target_soc": 90, "power_kw": 5},
            blocking=True,
        )
    assert ihm.words["holding"][8024] == 1  # the failed start changed nothing
    clock.t += 60
    await hass.services.async_call(
        DOMAIN, "force_charge_start", {"target_soc": 90, "power_kw": 5}, blocking=True
    )
    await hass.services.async_call(DOMAIN, "force_charge_cancel", {}, blocking=True)
    assert hass.states.get(FC_STATE).attributes["reason"] == "cancelled"
    assert ihm.words["holding"][8024] == 1
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "force_charge_cancel", {}, blocking=True)

    # Above the iHM limit (8149 = 10 kW): refused before any write.
    count = len(ihm.writes)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "force_charge_start",
            {"target_soc": 90, "power_kw": 12},
            blocking=True,
        )
    assert len(ihm.writes) == count

    clock.t += 60
    await hass.services.async_call(
        DOMAIN, "force_charge_start", {"target_soc": 90, "power_kw": 5}, blocking=True
    )
    assert ihm.words["holding"][8024] == 5
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert ihm.words["holding"][8024] == 1  # restored before the sockets closed


async def test_force_charge_reserve_method(
    hass: HomeAssistant, inverter: FakeDevice, ihm: FakeDevice
) -> None:
    entry = _entry()
    await _setup(hass, entry)
    await hass.services.async_call(
        DOMAIN,
        "force_charge_start",
        {"target_soc": 70, "method": "reserve"},
        blocking=True,
    )
    assert _writes(inverter) == [(13100, [70])]
    assert ihm.writes == []
    # The reserve number is locked while the reserve session runs.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": RESERVE, "value": 20}, blocking=True
        )
    inverter.set("mb_battery_soc", 700)
    await _poll(hass, entry)
    await hass.async_block_till_done()
    assert hass.states.get(FC_STATE).state == "target_reached"
    assert _writes(inverter)[-1] == (13100, [6])


async def test_force_charge_restored_after_crash(
    hass: HomeAssistant, hass_storage: dict[str, Any], ihm: FakeDevice
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    # HA died while a forced charge was running.
    ihm.set("ihm_ems_mode", 5)
    ihm.set("ihm_charge_discharge_power", 80)
    hass_storage[f"{DOMAIN}.force_charge.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.force_charge.{entry.entry_id}",
        "data": {
            "session": {
                "method": "ihm_compulsory",
                "target_soc": 90,
                "power_kw": 8.0,
                "started": "2026-11-20T00:00:00+00:00",
                "deadline": "2026-11-20T04:00:00+00:00",
                "start_soc": 40.0,
                "previous": {
                    "ihm_charge_power": 0,
                    "ihm_charge_command": 0xAA,
                    "ihm_ems_mode": 1,
                },
                "applied": ["ihm_charge_power", "ihm_charge_command", "ihm_ems_mode"],
            }
        },
    }
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert ihm.words["holding"][8024] == 1
    assert ihm.words["holding"][8026] == 0
    state = hass.states.get(FC_STATE)
    assert state.state == "aborted" and state.attributes["reason"] == "restart"


async def test_external_change_is_not_fought(
    hass: HomeAssistant, ihm: FakeDevice
) -> None:
    entry = _entry()
    await _setup(hass, entry)
    await hass.services.async_call(
        DOMAIN, "force_charge_start", {"target_soc": 90}, blocking=True
    )
    writes = len(ihm.writes)
    ihm.set("ihm_ems_mode", 1)  # someone switched back in the app
    for _ in range(2):
        entry.runtime_data.ihm._block_last_read.clear()
        await _poll(hass, entry)
    await hass.async_block_till_done()
    state = hass.states.get(FC_STATE)
    assert state.state == "aborted"
    assert state.attributes["reason"] == "changed outside Home Assistant"
    assert len(ihm.writes) == writes  # nothing written back


async def test_unknown_model_refuses_inverter_writes(
    hass: HomeAssistant, inverter: FakeDevice
) -> None:
    inverter.set("mb_device_type_code", 0x0BAD)
    entry = _entry()
    await _setup(hass, entry)
    with pytest.raises(ServiceValidationError, match="model"):
        await hass.services.async_call(
            DOMAIN, "set_backup_reserve", {"percent": 10}, blocking=True
        )
    assert inverter.writes == []


async def test_ac22_current_needs_unverified(
    hass: HomeAssistant, inverter: FakeDevice
) -> None:
    inverter.words["holding"][21203] = 160
    inverter.words["input"][21263] = 320
    entry = _entry(**{CONF_AC22_UNIT: 3})
    await _setup(hass, entry)
    state = hass.states.get(AC22)
    assert state is not None and float(state.state) == 16.0
    assert state.attributes["max"] == 32.0
    with pytest.raises(ServiceValidationError, match="unverified"):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": AC22, "value": 20}, blocking=True
        )
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, CONF_CONTROL_ALLOW_UNVERIFIED: True},
    )
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "number", "set_value", {"entity_id": AC22, "value": 20}, blocking=True
    )
    assert (21203, [200], 3) in inverter.writes  # unit 3 on the inverter socket


async def test_three_phase_profile(
    hass: HomeAssistant, entry: MockConfigEntry, inverter: FakeDevice
) -> None:
    """SH20T: MPPT4 block skipped, phase B/C sensors enabled once."""
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    unique = make_unique_id(entry, "mb_phase_b_voltage")
    registry.async_get_or_create(
        "sensor",
        DOMAIN,
        unique,
        config_entry=entry,
        suggested_object_id="sungrow_ems_inverter_phase_b_voltage",
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    inverter.set("mb_device_type_code", 0xE26)
    inverter.rejected.update({5115, 5116})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique)
    assert registry.async_get(entity_id).disabled_by is None
    assert entry.data["three_phase_entities_enabled"] is True
    inverter.calls.clear()
    await entry.runtime_data.inverter.async_refresh()
    assert 5115 not in inverter.addresses("read_input_registers")
