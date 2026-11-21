"""End to end: Home Assistant + real pymodbus client + the mock Modbus server.

The whole integration (config entry, hubs, coordinators, controller, force
charge) runs against tests/mock_modbus_server.py over localhost TCP, the way
it would run against hardware.
"""

from __future__ import annotations

from collections.abc import Iterator
import functools
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pymodbus.client import AsyncModbusTcpClient
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow_ems import controller as controller_mod
from custom_components.sungrow_ems.const import (
    CONF_CONTROL_ENABLED,
    CONF_IHM_DEVICE_ID,
    CONF_IHM_ENABLED,
    CONF_IHM_HOST,
    CONF_IHM_PORT,
    CONF_INVERTER_DEVICE_ID,
    CONF_INVERTER_HOST,
    CONF_INVERTER_PORT,
    DOMAIN,
)
from custom_components.sungrow_ems.writer import SafeWriter

from .mock_modbus_server import HOLDING, INPUT, build_server
from .snapshot_factory import SERIAL, synthetic_snapshot

pytestmark = pytest.mark.usefixtures("socket_enabled")


async def _no_sleep(_s: float) -> None:
    return None


@pytest.fixture(autouse=True)
def real_pymodbus() -> Iterator[None]:
    with (
        patch(
            "custom_components.sungrow_ems.modbus.AsyncModbusTcpClient",
            AsyncModbusTcpClient,
        ),
        patch.object(
            controller_mod, "SafeWriter", functools.partial(SafeWriter, sleep=_no_sleep)
        ),
    ):
        yield


async def test_forced_charge_against_mock(hass: HomeAssistant) -> None:
    server = build_server(
        synthetic_snapshot(soc_percent=40.0), ports={"inverter": 0, "ihm": 0}, tick=None
    )
    await server.start()
    try:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Sungrow SH10RS",
            data={
                CONF_INVERTER_HOST: "127.0.0.1",
                CONF_INVERTER_PORT: server.port_of("inverter"),
                CONF_INVERTER_DEVICE_ID: 1,
                CONF_IHM_ENABLED: True,
                CONF_IHM_HOST: "127.0.0.1",
                CONF_IHM_PORT: server.port_of("ihm"),
                CONF_IHM_DEVICE_ID: 247,
            },
            options={CONF_CONTROL_ENABLED: True},
            unique_id=SERIAL,
            version=1,
            minor_version=3,
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert (
            hass.states.get("sensor.sungrow_ems_inverter_battery_soc").state == "40.0"
        )

        await hass.services.async_call(
            DOMAIN, "force_charge_start", {"target_soc": 45}, blocking=True
        )
        ihm = server.devices["ihm"]
        assert ihm.get(HOLDING, 8024) == 5
        assert ihm.get_u32(HOLDING, 8026) == 100  # 10.0 kW, the 8149 limit

        # 10 kW into 38.6 kWh: 5 % takes ~12 minutes.
        assert server.dynamics is not None
        server.dynamics.step(15 * 60)
        assert server.devices["inverter"].get(INPUT, 13023) >= 450
        await entry.runtime_data.inverter.async_refresh()
        await hass.async_block_till_done()

        state = hass.states.get("sensor.sungrow_ems_ihomemanager_force_charge_state")
        assert state.state == "target_reached"
        assert ihm.get(HOLDING, 8024) == 1  # Self-consumption again
        assert ihm.get_u32(HOLDING, 8026) == 0
        assert [w[1] for w in ihm.writes] == [8026, 8024, 8024, 8026]

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        await server.close()
