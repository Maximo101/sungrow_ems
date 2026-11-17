"""Config entry lifecycle, coordinators and entities inside Home Assistant."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sungrow_ems.const import (
    CONF_FAILURE_THRESHOLD,
    CONF_IHM_ENABLED,
    DOMAIN,
)
from custom_components.sungrow_ems.entity import make_unique_id

from .conftest import SERIAL, FakeDevice

INV = "sensor.sungrow_ems_inverter_"
IHM = "sensor.sungrow_ems_ihomemanager_"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def _poll(hass: HomeAssistant, seconds: float = 11) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_setup_creates_all_entities_and_two_devices(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == 192
    assert {e.domain for e in entities} == {"sensor", "binary_sensor"}
    assert all(e.unique_id.endswith("_ems") for e in entities)

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert {d.name for d in devices} == {"SH10RS", "iHM"}
    inverter_device = next(d for d in devices if d.name == "SH10RS")
    assert inverter_device.model == "SH10RS @ inverter.test"
    ihm_device = next(d for d in devices if d.name == "iHM")
    assert ihm_device.model == "iHomeManager @ ihm.test"
    assert inverter_device.serial_number == SERIAL
    assert inverter_device.manufacturer == "Sungrow"


async def test_states_are_decoded(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await _setup(hass, entry)
    assert hass.states.get(f"{INV}battery_soc").state == "65.5"
    assert hass.states.get(f"{INV}pv_dc_power").state == "4200"
    assert hass.states.get(f"{IHM}grid_import_energy").state == "12345.6"
    model = hass.states.get(f"{IHM}model")  # register 8000, fixed 0x072A
    assert model.state == "iHomeManager"
    assert model.attributes["code_hex"] == "0x072A"
    assert (
        hass.states.get("binary_sensor.sungrow_ems_inverter_inverter_fault").state
        == STATE_OFF
    )
    assert (
        hass.states.get("binary_sensor.sungrow_ems_ihomemanager_ev_charger_fault").state
        == STATE_OFF
    )


async def test_binary_sensors_follow_the_registers(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    inverter: FakeDevice,
    ihm: FakeDevice,
) -> None:
    await _setup(hass, entry)
    inverter.set("mb_running_state", 0x1000)
    inverter.set("mb_grid_side_fault", 1)
    ihm.set("ihm_charger_status", 9)
    await _poll(hass)
    for entity_id in (
        "binary_sensor.sungrow_ems_inverter_off_grid_backup_mode",
        "binary_sensor.sungrow_ems_inverter_inverter_fault",
        "binary_sensor.sungrow_ems_ihomemanager_ev_charger_fault",
    ):
        assert hass.states.get(entity_id).state == STATE_ON, entity_id


async def test_battery_sensors(
    hass: HomeAssistant, entry: MockConfigEntry, inverter: FakeDevice
) -> None:
    inverter.set("mb_battery_power", -3000)  # charging at 3 kW
    inverter.set("mb_power_flow_status", 0b0011)
    inverter.set("mb_min_soc", 50)  # 5.0 %
    inverter.set("mb_max_soc", 1000)  # 100.0 %
    inverter.set("mb_backup_reserve_soc", 6)
    await _setup(hass, entry)
    assert hass.states.get(f"{INV}battery_status").state == "Charging"
    usable = hass.states.get(f"{INV}battery_usable_energy")
    assert usable.state == "21.71"
    assert usable.attributes["usable_capacity_kwh"] == 36.48
    assert usable.attributes["discharge_floor"] == "backup_reserve"
    to_full = hass.states.get(f"{INV}time_until_fully_charged")
    assert to_full.state == "4h 12m"  # 252 minutes
    assert to_full.attributes["minutes"] == 252
    assert to_full.attributes["finishes_at"] is not None
    # Not discharging: the estimate is unknown, not unavailable.
    assert hass.states.get(f"{INV}time_until_fully_discharged").state == STATE_UNKNOWN

    inverter.set("mb_battery_power", 2000)
    inverter.set("mb_power_flow_status", 0b1100)
    await _poll(hass)
    assert hass.states.get(f"{INV}battery_status").state == "Discharging"
    assert hass.states.get(f"{INV}time_until_fully_charged").state == STATE_UNKNOWN
    assert hass.states.get(f"{INV}time_until_fully_discharged").state == "10h 51m"

    # Off grid, the backup reserve becomes usable.
    inverter.set("mb_running_state", 0x1000)
    await _poll(hass)
    usable = hass.states.get(f"{INV}battery_usable_energy")
    assert usable.state == "23.89"
    assert usable.attributes["discharge_floor"] == "min_soc"


async def test_battery_stored_energy_entity_is_removed(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        make_unique_id(entry, "battery_stored_energy"),
        config_entry=entry,
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None
    assert registry.async_get(f"{INV}battery_usable_energy") is not None


async def test_no_write_function_is_ever_called(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    inverter: FakeDevice,
    ihm: FakeDevice,
) -> None:
    await _setup(hass, entry)
    await _poll(hass)
    await _poll(hass)
    allowed = {"read_input_registers", "read_holding_registers"}
    assert inverter.functions() <= allowed
    assert ihm.functions() <= allowed
    # Only the PVOutput upload action, which never touches the devices.
    assert set(hass.services.async_services().get(DOMAIN, {})) <= {"pvoutput_push_now"}


async def test_ihm_settings_are_not_read_every_poll(
    hass: HomeAssistant, entry: MockConfigEntry, ihm: FakeDevice
) -> None:
    await _setup(hass, entry)
    assert len(ihm.addresses("read_holding_registers")) == 1  # first poll
    ihm.calls.clear()
    await _poll(hass)
    assert ihm.addresses("read_holding_registers") == []
    assert len(ihm.calls) == 2  # ihm_system + ihm_meter_charger
    # The cadence runs on the monotonic clock: age the block by a minute.
    entry.runtime_data.ihm._block_last_read["ihm_settings"] -= 61
    await _poll(hass)
    assert len(ihm.addresses("read_holding_registers")) == 1


async def test_unload_closes_both_sockets(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    inverter: FakeDevice,
    ihm: FakeDevice,
) -> None:
    await _setup(hass, entry)
    assert (inverter.open_sockets, ihm.open_sockets) == (1, 1)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert (inverter.open_sockets, ihm.open_sockets) == (0, 0)
    calls = len(inverter.calls)
    await _poll(hass)
    assert len(inverter.calls) == calls  # polling stopped


async def test_inverter_offline_retries_setup_and_leaks_nothing(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    inverter: FakeDevice,
    ihm: FakeDevice,
) -> None:
    inverter.offline = True
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert (inverter.open_sockets, ihm.open_sockets) == (0, 0)
    assert ihm.calls == []


async def test_ihm_offline_does_not_block_the_inverter(
    hass: HomeAssistant, entry: MockConfigEntry, ihm: FakeDevice
) -> None:
    ihm.offline = True
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(f"{INV}battery_soc").state == "65.5"
    assert hass.states.get(f"{IHM}grid_import_energy").state == STATE_UNAVAILABLE


async def test_without_ihm_only_inverter_entities_exist(
    hass: HomeAssistant, inverter: FakeDevice, ihm: FakeDevice
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"inverter_host": "inverter.test", CONF_IHM_ENABLED: False},
        unique_id=SERIAL,
        version=1,
        minor_version=2,
    )
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert ihm.connects == 0
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.name for d in devices] == ["SH10RS"]


async def test_last_known_good_until_failure_threshold(
    hass: HomeAssistant, inverter: FakeDevice
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"inverter_host": "inverter.test", CONF_IHM_ENABLED: False},
        options={CONF_FAILURE_THRESHOLD: 3, "retries": 0},
        unique_id=SERIAL,
        version=1,
        minor_version=2,
    )
    await _setup(hass, entry)
    entity_id = f"{INV}battery_soc"
    inverter.timeout_from = 0

    for _ in range(2):
        await _poll(hass, 70)
        assert hass.states.get(entity_id).state == "65.5"  # held
    await _poll(hass, 70)
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    inverter.timeout_from = None
    inverter.set("mb_battery_soc", 700)
    await _poll(hass, 70)
    assert hass.states.get(entity_id).state == "70.0"


async def test_sentinel_keeps_previous_value(
    hass: HomeAssistant, entry: MockConfigEntry, inverter: FakeDevice
) -> None:
    await _setup(hass, entry)
    inverter.set("mb_battery_soc", 0xFFFF)
    await _poll(hass)
    assert hass.states.get(f"{INV}battery_soc").state == "65.5"


async def test_poll_status_sensor_is_disabled_by_default(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await _setup(hass, entry)
    registry = er.async_get(hass)
    for prefix in (INV, IHM):
        entity = registry.async_get(f"{prefix}last_successful_poll")
        assert (
            entity is not None
            and entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        )
        assert hass.states.get(f"{prefix}last_successful_poll") is None


async def test_connection_sensor_is_quiet_until_the_device_drops(
    hass: HomeAssistant, entry: MockConfigEntry, inverter: FakeDevice
) -> None:
    await _setup(hass, entry)
    entity_id = "binary_sensor.sungrow_ems_inverter_connection"
    first = hass.states.get(entity_id)
    assert first.state == STATE_ON
    assert first.attributes["device_class"] == "connectivity"
    assert "last_error" not in first.attributes
    ihm_state = hass.states.get("binary_sensor.sungrow_ems_ihomemanager_connection")
    assert ihm_state.name == "iHM Connection"

    # Good polls with changing values: no new state, so nothing is logged.
    for power in (1000, 2000, 3000):
        inverter.set("mb_total_dc_power", power)
        await _poll(hass)
    assert hass.states.get(f"{INV}pv_dc_power").state == "3000"
    assert hass.states.get(entity_id).last_updated == first.last_updated


async def test_connection_sensor_goes_off_with_the_device(
    hass: HomeAssistant, inverter: FakeDevice
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"inverter_host": "inverter.test", CONF_IHM_ENABLED: False},
        options={CONF_FAILURE_THRESHOLD: 3, "retries": 0},
        unique_id=SERIAL,
        version=1,
        minor_version=2,
    )
    await _setup(hass, entry)
    entity_id = "binary_sensor.sungrow_ems_inverter_connection"
    inverter.timeout_from = 0

    for _ in range(2):
        await _poll(hass, 70)
        assert hass.states.get(entity_id).state == STATE_ON  # riding it out
    await _poll(hass, 70)
    state = hass.states.get(entity_id)
    assert state.state == STATE_OFF
    assert state.attributes["last_error"]
    assert hass.states.get(f"{INV}battery_soc").state == STATE_UNAVAILABLE

    inverter.timeout_from = None
    await _poll(hass, 70)
    assert hass.states.get(entity_id).state == STATE_ON
    assert "last_error" not in hass.states.get(entity_id).attributes


async def test_device_names_and_entity_ids(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Devices are "SH10RS" / "iHM"; entity IDs keep the sungrow_ems_ prefixes."""
    await _setup(hass, entry)
    device_registry = dr.async_get(hass)
    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    by_device: dict[str, list[er.RegistryEntry]] = {}
    for entity in entities:
        by_device.setdefault(
            device_registry.async_get(entity.device_id).name, []
        ).append(entity)
    assert len(by_device["iHM"]) > 50
    assert all(
        e.entity_id.split(".", 1)[1].startswith("sungrow_ems_ihomemanager_")
        for e in by_device["iHM"]
    )
    assert all(
        e.entity_id.split(".", 1)[1].startswith("sungrow_ems_inverter_")
        for e in by_device["SH10RS"]
    )
    assert not any("iHM" in (e.original_name or "") for e in entities)

    assert hass.states.get(f"{IHM}battery_soc").name == "iHM Battery SoC"
    assert hass.states.get(f"{INV}battery_soc").name == "SH10RS Battery SoC"
    assert hass.states.get("binary_sensor.sungrow_ems_ihomemanager_connection")
    assert hass.states.get(f"{IHM}grid_import_safe") is not None


async def test_existing_entity_ids_are_kept(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """An entity registered under another ID keeps it after the device rename."""
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        make_unique_id(entry, "mb_battery_soc"),
        suggested_object_id="my_custom_battery_soc",
        config_entry=entry,
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.my_custom_battery_soc")
    assert state is not None
    assert state.name == "SH10RS Battery SoC"
    assert hass.states.get(f"{INV}battery_soc") is None
