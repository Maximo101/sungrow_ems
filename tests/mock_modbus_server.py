#!/usr/bin/env python3
"""Mock Sungrow Modbus TCP server, seeded from a ``dump_registers.py`` snapshot.

Lets the integration (and its write path) run against realistic registers
while the real inverter is disconnected:

  * serves every word in the snapshot, per port and unit ID (WiNet-S inverter
    on one port, iHM unit 247 on another, battery / AC22 units behind the
    inverter port);
  * answers exception 0x02 (illegal address) for addresses the snapshot says
    were refused and, unless ``--lenient``, for addresses that were never
    read, so the hub's block-splitting logic is exercised as on hardware;
  * FC03 / FC04 reads, FC06 / FC16 writes to holding registers. Writes
    persist in memory and, with ``--state FILE``, across restarts;
  * simple state dynamics, once a second (``--speed`` to fast-forward):
      - battery power follows the active command: iHM Compulsory mode
        (8024 = 5, 8025 0xAA/0xBB, 8026 kW), else inverter Compulsory mode
        (13050 = 2, 13051, 13052 W), else emergency charging while on grid
        and SoC is below the Backup reserve (13100, protocol 3.3.2), else
        the snapshot's own battery power (self-consumption, frozen);
      - charging stops at Max SoC (13058), discharging at max(Min SoC,
        Backup reserve);
      - SoC (13023, iHM 8163), battery power (5214, 13022, iHM 8161), power
        flow bits 1/2 (13001), running state (13000) and the daily charge /
        discharge counters (13040 / 13026) follow.

The wire layer is a small asyncio Modbus TCP implementation (MBAP + FC03,
FC04, FC06, FC16) rather than pymodbus's server datastore: the pymodbus
datastore API is mid-migration (ModbusDeviceContext is deprecated in 3.16 in
favour of SimDevice and changes again in 4.0), and the mock must answer per
address exceptions and unit routing exactly. Clients are real pymodbus
clients, and the tests drive it with the integration's own hub.

Usage:
    python3 tests/mock_modbus_server.py snapshots/snapshot_SH10RS_....json \
        --inverter-port 1502 --ihm-port 1503 --speed 10 --state /tmp/mock_state.json

Then point a development Home Assistant (or ``scripts/dump_registers.py``)
at 127.0.0.1:1502 / 127.0.0.1:1503.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Iterable
import contextlib
from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
import struct
import time
from typing import Any

_LOGGER = logging.getLogger("mock_modbus_server")

INPUT = "input"
HOLDING = "holding"

EXC_ILLEGAL_FUNCTION = 0x01
EXC_ILLEGAL_ADDRESS = 0x02
EXC_ILLEGAL_VALUE = 0x03
EXC_GATEWAY_NO_RESPONSE = 0x0B

#: Sungrow on/off and command codes.
CMD_CHARGE = 0xAA
CMD_DISCHARGE = 0xBB
CMD_STOP = 0xCC
IHM_MODE_COMPULSORY = 5
INV_MODE_COMPULSORY = 2

RUNNING = 0x0040
RUNNING_COMPULSORY = 0x0800
RUNNING_EMERGENCY_CHARGE = 0x4001
RUNNING_OFF_GRID = 0x1000


# ---------------------------------------------------------------------------
# Device model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SimDevice:
    """Register store for one unit ID. Addresses are documented (1-based)."""

    name: str
    unit: int
    words: dict[str, dict[int, int]] = field(
        default_factory=lambda: {INPUT: {}, HOLDING: {}}
    )
    rejected: dict[str, set[int]] = field(
        default_factory=lambda: {INPUT: set(), HOLDING: set()}
    )
    lenient: bool = False
    #: Holding addresses that refuse writes with exception 0x03 (tests).
    write_refused: set[int] = field(default_factory=set)
    #: Holding addresses that accept a write but keep the old value (tests:
    #: read-back mismatch).
    write_ignored: set[int] = field(default_factory=set)
    writes: list[tuple[float, int, list[int]]] = field(default_factory=list)
    reads: int = 0

    # -- typed helpers -------------------------------------------------------

    def get(self, table: str, address: int, default: int = 0) -> int:
        return self.words[table].get(address, default)

    def get_s16(self, table: str, address: int) -> int:
        raw = self.get(table, address)
        return raw - 0x10000 if raw & 0x8000 else raw

    def get_u32(self, table: str, address: int) -> int:
        return self.get(table, address) | (self.get(table, address + 1) << 16)

    def get_s32(self, table: str, address: int) -> int:
        raw = self.get_u32(table, address)
        return raw - 0x1_0000_0000 if raw & 0x8000_0000 else raw

    def set(self, table: str, address: int, value: int) -> None:
        self.words[table][address] = int(value) & 0xFFFF

    def set_s32(self, table: str, address: int, value: int) -> None:
        raw = int(value) & 0xFFFF_FFFF
        self.words[table][address] = raw & 0xFFFF
        self.words[table][address + 1] = (raw >> 16) & 0xFFFF

    def has(self, table: str, address: int) -> bool:
        return address in self.words[table]

    # -- Modbus semantics ----------------------------------------------------

    def readable(self, table: str, start: int, count: int) -> bool:
        span = range(start, start + count)
        if self.rejected[table].intersection(span):
            return False
        if self.lenient:
            return True
        return all(a in self.words[table] for a in span)

    def read(self, table: str, start: int, count: int) -> list[int]:
        self.reads += 1
        return [self.words[table].get(a, 0) for a in range(start, start + count)]

    def write(self, start: int, values: list[int]) -> int | None:
        """Write holding registers; returns an exception code or None."""
        span = range(start, start + len(values))
        if not self.readable(HOLDING, start, len(values)):
            return EXC_ILLEGAL_ADDRESS
        if self.write_refused.intersection(span):
            return EXC_ILLEGAL_VALUE
        self.writes.append((time.time(), start, list(values)))
        for offset, value in enumerate(values):
            if start + offset in self.write_ignored:
                continue
            self.words[HOLDING][start + offset] = value & 0xFFFF
        _LOGGER.info("%s: write HR %d = %s", self.name, start, values)
        return None


def devices_from_snapshot(
    snapshot: dict[str, Any], *, lenient: bool = False
) -> dict[str, SimDevice]:
    """One SimDevice per snapshot target, keyed by target name."""
    if not str(snapshot.get("schema", "")).startswith("sungrow_ems.snapshot/"):
        raise ValueError("Not a sungrow_ems snapshot (missing schema)")
    out: dict[str, SimDevice] = {}
    for target in snapshot["targets"]:
        dev = SimDevice(name=target["name"], unit=int(target["unit"]), lenient=lenient)
        for table in (INPUT, HOLDING):
            for address, word in target.get("tables", {}).get(table, {}).items():
                dev.words[table][int(address)] = int(word) & 0xFFFF
            dev.rejected[table] = {
                int(a) for a in target.get("rejected", {}).get(table, [])
            }
        out[dev.name] = dev
    return out


# ---------------------------------------------------------------------------
# Battery / power-flow dynamics
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Dynamics:
    """Very small battery model, enough to watch a forced charge finish."""

    inverter: SimDevice | None
    ihm: SimDevice | None
    capacity_kwh: float = 38.4
    emergency_charge_w: float = 3000.0
    default_max_charge_w: float = 10000.0
    #: Battery power of the snapshot (W, + = discharge) used in self-consumption.
    baseline_w: float = 0.0
    on_grid: bool = True
    _soc: float = 0.0
    _charge_acc: float = 0.0
    _discharge_acc: float = 0.0
    _baseline_state: int | None = None
    mode: str = "self_consumption"

    def __post_init__(self) -> None:
        inv = self.inverter
        if inv is not None:
            self._soc = inv.get(INPUT, 13023) / 10.0
            if inv.has(INPUT, 5214):
                self.baseline_w = float(inv.get_s32(INPUT, 5214))
            self._baseline_state = (
                inv.get(INPUT, 13000) if inv.has(INPUT, 13000) else None
            )
            self.on_grid = inv.get(INPUT, 13000) != RUNNING_OFF_GRID
            cap = inv.get(INPUT, 13039)
            if 0 < cap < 0xFFFF:
                self.capacity_kwh = cap / 10.0
        elif self.ihm is not None:
            self._soc = self.ihm.get(INPUT, 8163) / 10.0
        if self.ihm is not None:
            cap = self.ihm.get_u32(INPUT, 8147)
            if 0 < cap < 0xFFFF_FFFF:
                self.capacity_kwh = cap / 10.0

    @property
    def soc(self) -> float:
        return self._soc

    def _limits(self) -> tuple[float, float, float]:
        inv = self.inverter
        if inv is None:
            return 0.0, 100.0, 0.0
        max_soc = inv.get(HOLDING, 13058, 1000) / 10.0
        min_soc = inv.get(HOLDING, 13059, 0) / 10.0
        reserve = float(inv.get(HOLDING, 13100, 0))
        return min_soc, max_soc, reserve

    def commanded_power(self) -> float:
        """Battery power the system is asked for, W, + = charge."""
        ihm, inv = self.ihm, self.inverter
        _min_soc, _max_soc, reserve = self._limits()
        if ihm is not None and ihm.get(HOLDING, 8024) == IHM_MODE_COMPULSORY:
            kw = ihm.get_u32(HOLDING, 8026) / 10.0
            cmd = ihm.get(HOLDING, 8025)
            limit = ihm.get(INPUT, 8151) * 100.0 or self.default_max_charge_w
            self.mode = "ihm_compulsory"
            if cmd == CMD_CHARGE:
                return min(kw * 1000.0, limit)
            if cmd == CMD_DISCHARGE:
                limit_d = ihm.get(INPUT, 8153) * 100.0 or self.default_max_charge_w
                return -min(kw * 1000.0, limit_d)
            return 0.0
        if inv is not None and inv.get(HOLDING, 13050) == INV_MODE_COMPULSORY:
            watts = float(inv.get(HOLDING, 13052))
            cmd = inv.get(HOLDING, 13051)
            self.mode = "inverter_compulsory"
            if cmd == CMD_CHARGE:
                return watts
            if cmd == CMD_DISCHARGE:
                return -watts
            return 0.0
        if self.on_grid and self._soc < reserve - 1e-9:
            # Protocol 3.3.2: below the reserve on grid -> emergency charging.
            self.mode = "emergency_charging"
            return self.emergency_charge_w
        self.mode = "self_consumption"
        return -self.baseline_w

    def step(self, dt: float) -> float:
        """Advance the model by ``dt`` seconds; returns battery power (W, + charge).

        Long steps (fast-forward) are cut into 5 s slices so limits and
        mode changes are seen on time.
        """
        power = 0.0
        while dt > 1e-9:
            slice_ = min(dt, 5.0)
            power = self._step(slice_)
            dt -= slice_
        return power

    def _step(self, dt: float) -> float:
        min_soc, max_soc, reserve = self._limits()
        power = self.commanded_power()
        floor = max(min_soc, reserve if self.on_grid else min_soc)
        if power > 0 and self._soc >= max_soc:
            power = 0.0
        if power < 0 and self._soc <= floor:
            power = 0.0
        energy_kwh = power * dt / 3_600_000.0
        if self.capacity_kwh > 0:
            self._soc += energy_kwh / self.capacity_kwh * 100.0
        self._soc = max(0.0, min(100.0, self._soc))
        if power > 0:
            self._soc = min(self._soc, max_soc)
            if self.mode == "emergency_charging":
                self._soc = min(self._soc, reserve)
            self._charge_acc += energy_kwh
        elif power < 0:
            self._soc = max(self._soc, floor)
            self._discharge_acc -= energy_kwh
        self._publish(power)
        return power

    def _publish(self, power: float) -> None:
        soc_raw = round(self._soc * 10)
        inv, ihm = self.inverter, self.ihm
        if inv is not None:
            inv.set(INPUT, 13023, soc_raw)
            inv.set_s32(INPUT, 5214, round(-power))  # + = discharge
            inv.set(INPUT, 13022, round(-power) & 0xFFFF)
            flow = inv.get(INPUT, 13001) & ~0b110
            if power > 50:
                flow |= 0b010
            elif power < -50:
                flow |= 0b100
            inv.set(INPUT, 13001, flow)
            if self.mode == "inverter_compulsory":
                inv.set(INPUT, 13000, RUNNING_COMPULSORY)
            elif self.mode == "emergency_charging" and power > 0:
                inv.set(INPUT, 13000, RUNNING_EMERGENCY_CHARGE)
            elif self._baseline_state is not None:
                inv.set(INPUT, 13000, self._baseline_state)
            if self._charge_acc >= 0.1:
                steps = int(self._charge_acc * 10)
                inv.set(INPUT, 13040, inv.get(INPUT, 13040) + steps)
                self._charge_acc -= steps / 10
            if self._discharge_acc >= 0.1:
                steps = int(self._discharge_acc * 10)
                inv.set(INPUT, 13026, inv.get(INPUT, 13026) + steps)
                self._discharge_acc -= steps / 10
        if ihm is not None:
            ihm.set(INPUT, 8163, soc_raw)
            ihm.set_s32(INPUT, 8161, round(-power / 10))  # 0.01 kW, + = discharge


# ---------------------------------------------------------------------------
# Modbus TCP wire layer
# ---------------------------------------------------------------------------


def _exception(tid: int, unit: int, function: int, code: int) -> bytes:
    pdu = struct.pack(">BB", function | 0x80, code)
    return struct.pack(">HHHB", tid, 0, len(pdu) + 1, unit) + pdu


def handle_pdu(
    devices: dict[int, SimDevice], unit: int, pdu: bytes
) -> tuple[int, bytes] | int:
    """Process one request PDU. Returns (function, response_pdu) or an exception code."""
    if not pdu:
        return EXC_ILLEGAL_FUNCTION
    function = pdu[0]
    dev = devices.get(unit)
    if dev is None:
        return EXC_GATEWAY_NO_RESPONSE
    if function in (0x03, 0x04):
        if len(pdu) < 5:
            return EXC_ILLEGAL_VALUE
        address, count = struct.unpack(">HH", pdu[1:5])
        if not 1 <= count <= 125:
            return EXC_ILLEGAL_VALUE
        table = HOLDING if function == 0x03 else INPUT
        start = address + 1  # wire 0-based -> documented 1-based
        if not dev.readable(table, start, count):
            return EXC_ILLEGAL_ADDRESS
        words = dev.read(table, start, count)
        return function, struct.pack(f">BB{count}H", function, count * 2, *words)
    if function == 0x06:
        if len(pdu) < 5:
            return EXC_ILLEGAL_VALUE
        address, value = struct.unpack(">HH", pdu[1:5])
        code = dev.write(address + 1, [value])
        return code if code is not None else (function, pdu[:5])
    if function == 0x10:
        if len(pdu) < 6:
            return EXC_ILLEGAL_VALUE
        address, count, nbytes = struct.unpack(">HHB", pdu[1:6])
        if not 1 <= count <= 123 or nbytes != count * 2 or len(pdu) < 6 + nbytes:
            return EXC_ILLEGAL_VALUE
        values = list(struct.unpack(f">{count}H", pdu[6 : 6 + nbytes]))
        code = dev.write(address + 1, values)
        return (
            code
            if code is not None
            else (function, struct.pack(">BHH", function, address, count))
        )
    return EXC_ILLEGAL_FUNCTION


class _Connection:
    def __init__(self, server: MockServer, key: str) -> None:
        self.server = server
        self.key = key

    async def __call__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        devices = self.server.by_port[self.key]
        self.server.open_connections += 1
        try:
            while True:
                header = await reader.readexactly(7)
                tid, proto, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1) if length > 1 else b""
                if proto != 0:
                    continue
                if self.server.latency:
                    await asyncio.sleep(self.server.latency)
                result = handle_pdu(devices, unit, pdu)
                function = pdu[0] if pdu else 0
                if isinstance(result, int):
                    writer.write(_exception(tid, unit, function, result))
                else:
                    _fc, body = result
                    writer.write(
                        struct.pack(">HHHB", tid, 0, len(body) + 1, unit) + body
                    )
                await writer.drain()
                if not isinstance(result, int) and function in (0x06, 0x10):
                    self.server.on_write()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.server.open_connections -= 1
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


class MockServer:
    """Serves SimDevices on one or more TCP ports."""

    def __init__(
        self,
        devices: dict[str, SimDevice],
        port_map: dict[str, int],
        *,
        host: str = "127.0.0.1",
        dynamics: Dynamics | None = None,
        tick: float | None = 1.0,
        speed: float = 1.0,
        state_file: Path | None = None,
        latency: float = 0.0,
    ) -> None:
        self.devices = devices
        self.host = host
        #: Listener key -> {unit: device}. Targets on the same port share a
        #: listener (WiNet-S: inverter, battery and AC22 by unit ID). Port 0
        #: asks for a free port, one listener per target.
        self.by_port: dict[str, dict[int, SimDevice]] = {}
        self._requested: dict[str, int] = {}
        self._listener_of: dict[str, str] = {}
        # Inverter first, so the units behind its WiNet-S can join its listener.
        order = sorted(devices, key=lambda n: n != "inverter")
        for name in order:
            dev = devices[name]
            port = port_map.get(name)
            if port is None:
                continue
            key = str(port) if port else f"auto:{name}"
            if (
                name in ("battery", "ac22")
                and "inverter" in self._listener_of
                and port == port_map.get("inverter")
            ):
                key = self._listener_of["inverter"]
            self.by_port.setdefault(key, {})[dev.unit] = dev
            self._requested[key] = port
            self._listener_of[name] = key
        self.dynamics = dynamics
        self.tick = tick
        self.speed = speed
        self.state_file = state_file
        self.latency = latency
        self.open_connections = 0
        self._servers: list[asyncio.base_events.Server] = []
        self._task: asyncio.Task[None] | None = None
        self.ports: dict[str, int] = {}  # listener key -> actual port

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        self.load_state()
        for key, devices in self.by_port.items():
            server = await asyncio.start_server(
                _Connection(self, key), self.host, self._requested[key]
            )
            actual = server.sockets[0].getsockname()[1]
            self.ports[key] = actual
            self._servers.append(server)
            _LOGGER.info(
                "Serving %s (units %s) on %s:%d",
                ", ".join(d.name for d in devices.values()),
                sorted(devices),
                self.host,
                actual,
            )
        if self.dynamics is not None and self.tick:
            self._task = asyncio.create_task(self._run_dynamics())

    def port_of(self, name: str) -> int:
        """Actual TCP port serving a target (after start())."""
        return self.ports[self._listener_of[name]]

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for server in self._servers:
            server.close()
            await server.wait_closed()
        self.save_state()

    async def _run_dynamics(self) -> None:
        assert self.dynamics is not None and self.tick
        last = time.monotonic()
        while True:
            await asyncio.sleep(self.tick)
            now = time.monotonic()
            self.dynamics.step((now - last) * self.speed)
            last = now

    # -- persistence -----------------------------------------------------------

    def on_write(self) -> None:
        self.save_state()

    def save_state(self) -> None:
        if self.state_file is None:
            return
        state = {
            name: {str(a): w for a, w in dev.words[HOLDING].items()}
            for name, dev in self.devices.items()
        }
        self.state_file.write_text(json.dumps(state), encoding="utf-8")

    def load_state(self) -> None:
        if self.state_file is None or not self.state_file.exists():
            return
        state = json.loads(self.state_file.read_text(encoding="utf-8"))
        for name, words in state.items():
            dev = self.devices.get(name)
            if dev is None:
                continue
            for address, word in words.items():
                dev.words[HOLDING][int(address)] = int(word) & 0xFFFF
        _LOGGER.info("Restored holding registers from %s", self.state_file)


def build_server(
    snapshot: dict[str, Any],
    *,
    ports: dict[str, int],
    host: str = "127.0.0.1",
    lenient: bool = False,
    tick: float | None = 1.0,
    speed: float = 1.0,
    state_file: Path | None = None,
    capacity_kwh: float | None = None,
    emergency_charge_w: float = 3000.0,
    overrides: Iterable[str] = (),
) -> MockServer:
    """Create (not start) a MockServer from a snapshot dict.

    ``ports`` maps target names to ports. Targets that share a port (battery,
    AC22 behind the WiNet-S) are told apart by unit ID; by default they share
    the inverter's port.
    """
    devices = devices_from_snapshot(snapshot, lenient=lenient)
    port_map = dict(ports)
    for name in devices:
        if (
            name not in port_map
            and name in ("battery", "ac22")
            and "inverter" in port_map
        ):
            port_map[name] = port_map["inverter"]
    for override in overrides:
        apply_override(devices, override)
    dynamics = Dynamics(
        inverter=devices.get("inverter"),
        ihm=devices.get("ihm"),
        emergency_charge_w=emergency_charge_w,
    )
    if capacity_kwh:
        dynamics.capacity_kwh = capacity_kwh
    return MockServer(
        devices,
        port_map,
        host=host,
        dynamics=dynamics,
        tick=tick,
        speed=speed,
        state_file=state_file,
    )


def apply_override(devices: dict[str, SimDevice], spec: str) -> None:
    """``target:table:address=value`` e.g. ``inverter:holding:13100=10``."""
    try:
        left, value = spec.split("=", 1)
        name, table, address = left.split(":")
        devices[name].set(table, int(address), int(value, 0))
    except (ValueError, KeyError) as err:
        raise SystemExit(f"Bad --set {spec!r}: {err}") from err


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mock Sungrow Modbus TCP server")
    p.add_argument(
        "snapshot", type=Path, help="snapshot JSON from scripts/dump_registers.py"
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--inverter-port", type=int, default=1502)
    p.add_argument("--ihm-port", type=int, default=1503)
    p.add_argument(
        "--lenient",
        action="store_true",
        help="answer 0 for addresses not in the snapshot",
    )
    p.add_argument("--speed", type=float, default=1.0, help="simulation speed factor")
    p.add_argument(
        "--capacity", type=float, help="battery capacity kWh (default: from snapshot)"
    )
    p.add_argument(
        "--emergency-kw", type=float, default=3.0, help="charge power below the reserve"
    )
    p.add_argument(
        "--state", type=Path, help="persist holding registers to this JSON file"
    )
    p.add_argument(
        "--set", action="append", default=[], metavar="TARGET:TABLE:ADDR=VALUE"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


async def _async_main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
    server = build_server(
        snapshot,
        ports={"inverter": args.inverter_port, "ihm": args.ihm_port},
        host=args.host,
        lenient=args.lenient,
        speed=args.speed,
        state_file=args.state,
        capacity_kwh=args.capacity,
        emergency_charge_w=args.emergency_kw * 1000.0,
        overrides=args.set,
    )
    await server.start()
    status: Callable[[], None] = lambda: _LOGGER.info(  # noqa: E731
        "SoC %.1f %% mode %s",
        server.dynamics.soc if server.dynamics else -1,
        server.dynamics.mode if server.dynamics else "-",
    )
    try:
        while True:
            await asyncio.sleep(30)
            status()
    finally:
        await server.close()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_async_main())


if __name__ == "__main__":
    main()
