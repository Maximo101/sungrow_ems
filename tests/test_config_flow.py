"""Config, reconfigure and options flows."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow_ems.const import (
    CONF_IHM_ENABLED,
    CONF_IHM_HOST,
    CONF_IHM_PORT,
    CONF_INVERTER_SCAN_INTERVAL,
    DOMAIN,
)

from .conftest import ENTRY_DATA, SERIAL, FakeDevice


async def _start(hass: HomeAssistant, user_input: dict) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(result["flow_id"], user_input)


async def test_user_flow_creates_entry(
    hass: HomeAssistant, inverter: FakeDevice, ihm: FakeDevice
) -> None:
    result = await _start(hass, dict(ENTRY_DATA))
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Sungrow SH10RS"
    assert result["result"].unique_id == SERIAL
    assert result["data"][CONF_IHM_PORT] == 503
    # The probe only reads.
    assert inverter.functions() | ihm.functions() <= {
        "read_input_registers",
        "read_holding_registers",
    }


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, inverter: FakeDevice
) -> None:
    inverter.offline = True
    result = await _start(hass, dict(ENTRY_DATA))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert inverter.open_sockets == 0


async def test_user_flow_ihm_errors(hass: HomeAssistant, ihm: FakeDevice) -> None:
    data = dict(ENTRY_DATA)
    data.pop(CONF_IHM_HOST)
    result = await _start(hass, data)
    assert result["errors"] == {"base": "ihm_host_required"}

    ihm.set("ihm_device_type_code", 0x1234)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(ENTRY_DATA)
    )
    assert result["errors"] == {"base": "not_ihm"}
    assert ihm.open_sockets == 0


async def test_same_inverter_cannot_be_added_twice(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    result = await _start(hass, dict(ENTRY_DATA))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_reuses_the_live_sockets(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    inverter: FakeDevice,
    ihm: FakeDevice,
) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], dict(ENTRY_DATA)
    )
    # The iHM allows one connection per port: the probe must not open another.
    assert (inverter.open_sockets, ihm.open_sockets) == (1, 1)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.state is ConfigEntryState.LOADED
    assert (inverter.open_sockets, ihm.open_sockets) == (1, 1)


async def test_options_change_reloads_once(
    hass: HomeAssistant, entry: MockConfigEntry, inverter: FakeDevice
) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert inverter.connects == 1

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"]
    user_input = {str(key): key.description["suggested_value"] for key in schema.schema}
    user_input[CONF_INVERTER_SCAN_INTERVAL] = 30
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_INVERTER_SCAN_INTERVAL] == 30
    assert entry.state is ConfigEntryState.LOADED
    assert inverter.connects == 2  # exactly one reload
    assert inverter.open_sockets == 1
    assert entry.runtime_data.inverter.update_interval.total_seconds() == 30


async def test_ihm_can_be_disabled(hass: HomeAssistant, ihm: FakeDevice) -> None:
    data = {**ENTRY_DATA, CONF_IHM_ENABLED: False}
    result = await _start(hass, data)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert ihm.connects == 0


async def test_options_turn_control_on_and_off(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Control is off by default; turning it on asks for its settings, and
    turning it off keeps them for next time."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]
    user_input = {str(key): key.description["suggested_value"] for key in schema.schema}
    assert user_input["control_enabled"] is False
    user_input["control_enabled"] = True
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "control"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "control_allow_unverified": False,
            "ac22_unit": 3,
            "force_charge_timeout": 120,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["control_enabled"] is True
    assert entry.options["ac22_unit"] == 3
    assert entry.runtime_data.controller is not None
    assert hass.states.get("number.sungrow_ems_inverter_backup_reserve") is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    schema = result["data_schema"]
    user_input = {str(key): key.description["suggested_value"] for key in schema.schema}
    user_input["control_enabled"] = False
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["control_enabled"] is False
    assert entry.options["ac22_unit"] == 3  # kept for next time
    assert entry.runtime_data.controller is None
    assert hass.states.get("number.sungrow_ems_inverter_backup_reserve") is None
