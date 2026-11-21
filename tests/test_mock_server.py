"""Part 1 tooling: the mock Modbus server and the snapshot dumper, end to end."""

from __future__ import annotations

from collections.abc import AsyncIterator
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import patch

from pymodbus.client import AsyncModbusTcpClient
import pytest

from custom_components.sungrow_ems.modbus import SungrowModbusHub
from custom_components.sungrow_ems.registers import (
    REGISTERS_BY_KEY,
    Device,
    blocks_for,
)

from .mock_modbus_server import HOLDING, INPUT, MockServer, build_server
from .snapshot_factory import synthetic_snapshot

REPO = Path(__file__).resolve().parent.parent

# Real localhost sockets: the mock server and pymodbus clients talk TCP.
pytestmark = pytest.mark.usefixtures("socket_enabled")


def _load_dump() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "dump_registers", REPO / "scripts" / "dump_registers.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["dump_registers"] = module
    spec.loader.exec_module(module)
    return module


dump = _load_dump()


async def _start(snapshot: dict, **kw) -> MockServer:
    server = build_server(snapshot, ports={"inverter": 0, "ihm": 0}, tick=None, **kw)
    await server.start()
    return server


@pytest.fixture
async def mock() -> AsyncIterator[MockServer]:
    server = await _start(synthetic_snapshot())
    yield server
    await server.close()


def _port(server: MockServer, name: str) -> int:
    return server.port_of(name)


async def test_pymodbus_client_reads_and_writes(mock: MockServer) -> None:
    client = AsyncModbusTcpClient("127.0.0.1", port=_port(mock, "inverter"))
    assert await client.connect()
    try:
        rr = await client.read_input_registers(13022, count=2, device_id=1)  # 13023
        assert not rr.isError()
        assert rr.registers[0] == 400  # 40.0 %
        # Refused address -> exception 0x02.
        rr = await client.read_holding_registers(33147, count=1, device_id=1)
        assert rr.isError() and rr.exception_code == 0x02
        # FC06 persists.
        wr = await client.write_register(13099, 10, device_id=1)  # HR 13100
        assert not wr.isError()
        rr = await client.read_holding_registers(13099, count=1, device_id=1)
        assert rr.registers == [10]
        # Unknown unit -> gateway exception.
        rr = await client.read_input_registers(12999, count=1, device_id=9)
        assert rr.isError() and rr.exception_code == 0x0B
    finally:
        client.close()

    ihm = AsyncModbusTcpClient("127.0.0.1", port=_port(mock, "ihm"))
    assert await ihm.connect()
    try:
        # FC16, U32 low word first: 8026 = 10.0 kW.
        wr = await ihm.write_registers(8025, [100, 0], device_id=247)
        assert not wr.isError()
        assert mock.devices["ihm"].get_u32(HOLDING, 8026) == 100
    finally:
        ihm.close()


async def test_dynamics_follow_forced_charge_and_reserve() -> None:
    server = build_server(synthetic_snapshot(soc_percent=40.0), ports={}, tick=None)
    dyn = server.dynamics
    assert dyn is not None
    ihm = server.devices["ihm"]
    inv = server.devices["inverter"]

    # Self-consumption with a frozen 0 W baseline: nothing moves.
    dyn.step(600)
    assert inv.get(INPUT, 13023) == 400

    # iHM Compulsory charge at 10 kW for 6 minutes -> +1 kWh of 38.6 kWh.
    ihm.set(HOLDING, 8024, 5)
    ihm.set(HOLDING, 8025, 0xAA)
    ihm.set_s32(HOLDING, 8026, 100)
    dyn.step(360)
    assert inv.get(INPUT, 13023) == pytest.approx(426, abs=1)
    assert ihm.get(INPUT, 8163) == inv.get(INPUT, 13023)
    assert inv.get_s32(INPUT, 5214) == -10000  # + = discharge, so charging is negative
    assert inv.get(INPUT, 13001) & 0b010
    assert inv.get(INPUT, 13040) >= 9  # ~1 kWh charged today

    # Charging stops at Max SoC.
    inv.set(HOLDING, 13058, 450)
    dyn.step(3600)
    assert inv.get(INPUT, 13023) == 450
    dyn.step(1)  # the step that crossed Max SoC still charged; the next one does not
    assert inv.get_s32(INPUT, 5214) == 0

    # Back to self-consumption; a reserve above SoC triggers emergency charging.
    ihm.set(HOLDING, 8024, 1)
    inv.set(HOLDING, 13058, 1000)
    inv.set(HOLDING, 13100, 50)
    dyn.step(60)
    assert dyn.mode == "emergency_charging"
    assert inv.get(INPUT, 13000) == 0x4001
    dyn.step(36000)
    assert inv.get(INPUT, 13023) == 500  # held at the reserve
    dyn.step(60)
    assert dyn.mode == "self_consumption"


async def test_dump_snapshot_round_trip(tmp_path: Path) -> None:
    server = await _start(synthetic_snapshot(device_type_code=0xE26))
    try:
        out = await dump.async_main(
            [
                "--inverter",
                "127.0.0.1",
                "--inverter-port",
                str(_port(server, "inverter")),
                "--ihm",
                "127.0.0.1",
                "--ihm-port",
                str(_port(server, "ihm")),
                "--ihm-interval",
                "0",
                "--inverter-interval",
                "0",
                "--out",
                str(tmp_path),
                "--redact",
            ]
        )
    finally:
        await server.close()

    assert out.name.startswith("snapshot_SH20T_")
    assert out.with_suffix(".csv").exists()
    snap = json.loads(out.read_text())
    inv = next(t for t in snap["targets"] if t["name"] == "inverter")
    assert inv["identity"]["model"] == "SH20T"
    assert inv["identity"]["serial"] == "REDACTED"
    assert inv["host"] == "inverter.redacted"
    # MPPT4 is refused on an SH20T: the block fell back to single reads and
    # recorded both registers instead of failing.
    assert {5115, 5116} <= set(inv["rejected"]["input"])
    # Not in the synthetic snapshot either, so refused like on an SH10RS.
    assert {13200, 13207} <= set(inv["rejected"]["input"])
    rows = {(r["target"], r["table"], r["register_1based"]): r for r in snap["rows"]}
    soc = rows[("inverter", "input", 13023)]
    assert soc["value"] == 40.0
    assert soc["raw_u16"] == 400
    assert soc["register_0based"] == 13022
    assert soc["name"] == "mb_battery_soc"
    assert (
        rows[("inverter", "holding", 33214)]["name"] == "forced_charging_1_target_soc"
    )
    assert rows[("inverter", "input", 5115)]["status"] == "illegal_address"
    mode = rows[("ihm", "holding", 8048)]
    assert mode["value"] == "167 (Time plan)"

    # The dump seeds a new mock server: a full round trip.
    server2 = await _start(snap)
    try:
        assert server2.devices["inverter"].get(INPUT, 13023) == 400
        assert {5115, 5116} <= server2.devices["inverter"].rejected[INPUT]
    finally:
        await server2.close()


async def test_integration_hub_reads_mock_with_block_split(mock: MockServer) -> None:
    """The integration's own hub against the mock (real pymodbus client)."""
    with patch(
        "custom_components.sungrow_ems.modbus.AsyncModbusTcpClient",
        AsyncModbusTcpClient,
    ):
        hub = SungrowModbusHub(
            host="127.0.0.1", port=_port(mock, "inverter"), device_id=1, name="mock"
        )
        try:
            blocks = blocks_for(Device.INVERTER)
            result = await hub.async_read_blocks(blocks)
            decoded = result.decode(blocks)
        finally:
            await hub.async_close()
    assert decoded.values["mb_battery_soc"] == 40.0
    assert decoded.values["mb_backup_reserve_soc"] == 6
    # 33148 is refused: recorded as unsupported, every other block read.
    assert ("inv_hold_power_wide", 33148, 1) in result.unsupported_spans
    assert "mb_charge_discharge_power_wide" not in decoded.values
    assert REGISTERS_BY_KEY["mb_battery_soc"].address == 13023


async def test_safe_writer_over_real_pymodbus(mock: MockServer) -> None:
    """The whole write path (SafeWriter -> hub -> real pymodbus client ->
    Modbus TCP) against the mock, then the mock's battery reacts."""
    from custom_components.sungrow_ems.profiles import WriteDevice, profile_for
    from custom_components.sungrow_ems.writer import Endpoint, SafeWriter

    async def no_sleep(_s: float) -> None:
        return None

    with patch(
        "custom_components.sungrow_ems.modbus.AsyncModbusTcpClient",
        AsyncModbusTcpClient,
    ):
        inv_hub = SungrowModbusHub(
            host="127.0.0.1", port=_port(mock, "inverter"), device_id=1, name="inv"
        )
        ihm_hub = SungrowModbusHub(
            host="127.0.0.1", port=_port(mock, "ihm"), device_id=247, name="ihm"
        )
        writer = SafeWriter(
            {
                WriteDevice.INVERTER: Endpoint(inv_hub, 1),
                WriteDevice.IHM: Endpoint(ihm_hub, 247),
            },
            profile=lambda: profile_for(0xD1B),
            sleep=no_sleep,
        )
        try:
            result = await writer.async_write("backup_reserve_soc", 10)
            assert result.changed and result.previous == 6
            await writer.async_write("ihm_charge_power", 10.0, bounds=(0, 10.0))
            await writer.async_write("ihm_ems_mode", 5)
            assert inv_hub.stats.writes == 1 and ihm_hub.stats.writes == 2
        finally:
            await inv_hub.async_close()
            await ihm_hub.async_close()

    ihm = mock.devices["ihm"]
    assert mock.devices["inverter"].get(HOLDING, 13100) == 10
    assert ihm.get_u32(HOLDING, 8026) == 100  # FC16, low word first
    assert ihm.get(HOLDING, 8024) == 5
    assert ihm.writes[0][1:] == (8026, [100, 0])
    assert mock.dynamics is not None
    mock.dynamics.step(360)
    assert mock.devices["inverter"].get(INPUT, 13023) > 400
