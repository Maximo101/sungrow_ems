#!/usr/bin/env python3
"""Snapshot every documented Sungrow register to JSON and CSV.

Standalone tool (no Home Assistant needed). Run it once against the live
system before the inverter is swapped, keep the files, and feed the JSON to
``tests/mock_modbus_server.py`` to keep developing while the hardware is
disconnected.

What it reads (FC04 input and FC03 holding, never a write):
  * inverter (WiNet-S, default port 502, unit 1): every block of the
    integration's register map plus the documented ranges the integration
    does not poll (channel 2 meter 13200-13207, firmware strings
    13250-13369, system clock 5000-5005, load control 13000-13018, forced
    charging schedule 33208-33219, load rated power 33274);
  * iHomeManager (default port 503, unit 247), paced at 1 s per request;
  * optional: the battery unit behind the WiNet-S (``--battery-unit``, IR
    10741-10749) and the AC22 charger behind the WiNet-S (``--ac22-unit``,
    IR 21216-21322 and HR 21200-21313).

A block that the device answers with exception 0x02 (illegal address),
0x03 or 0x04 is read again one register at a time, so one unsupported
register never hides the rest of the block. Registers that are still
refused are recorded as ``illegal_address`` instead of failing the run.

Output (``--out``, default ``./snapshots``):
  snapshot_<model>_<YYYYmmdd_HHMMSS>.json   full snapshot, one entry per word
  snapshot_<model>_<YYYYmmdd_HHMMSS>.csv    the same rows as a table

Each row has: target, unit_id, table, register_1based, register_0based,
name, raw_u16, value (decoded on the first word of a register), uom, status.

The files contain the serial number and the IP addresses unless
``--redact`` is given. Do not commit them to the repository.

Examples:
    python3 scripts/dump_registers.py --inverter 192.0.2.10 --ihm 192.0.2.11
    python3 scripts/dump_registers.py --inverter 192.0.2.10 --ac22-unit 3 \
        --battery-unit 2 --redact

Stop any other Modbus client (mkaiser, SunGather, the integration itself)
while this runs: the WiNet-S and the iHM do not cope with two pollers, and
the iHM allows one TCP connection per port.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass, field
from datetime import datetime
import importlib.util
import json
import logging
from pathlib import Path
import sys
import time
from types import ModuleType
from typing import Any

import pymodbus
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

_LOGGER = logging.getLogger("dump_registers")

SCHEMA = "sungrow_ems.snapshot/1"
REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTERS_PY = REPO_ROOT / "custom_components" / "sungrow_ems" / "registers.py"

#: Exception codes after which a block is re-read register by register.
SPLIT_CODES = frozenset({0x02, 0x03, 0x04})
#: Busy / gateway codes: retry the same request.
RETRY_CODES = frozenset({0x05, 0x06, 0x0A, 0x0B})
MAX_WORDS = 60


def load_register_map(path: Path = REGISTERS_PY) -> ModuleType:
    """Import registers.py by path.

    Importing it as ``custom_components.sungrow_ems.registers`` would run the
    package ``__init__`` and pull in Home Assistant. registers.py itself has
    no Home Assistant or relative imports.
    """
    spec = importlib.util.spec_from_file_location("sungrow_ems_registers", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load the register map from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Documented ranges that the integration does not poll. Names follow the
# protocol tables ([INV] V1.1.17 Table 3/4, [IHM] V1.0.2, AC22 wallbox map).
# (table, start, end, {address: (name, words)})
# ---------------------------------------------------------------------------

Extra = tuple[str, int, int, dict[int, tuple[str, int]]]

INVERTER_EXTRA: tuple[Extra, ...] = (
    (
        "holding",
        5000,
        5005,
        {
            5000: ("clock_year", 1),
            5001: ("clock_month", 1),
            5002: ("clock_day", 1),
            5003: ("clock_hour", 1),
            5004: ("clock_minute", 1),
            5005: ("clock_second", 1),
        },
    ),
    (
        "holding",
        13000,
        13018,
        {
            13000: ("start_stop", 1),
            13001: ("do_configuration", 1),
            13002: ("load_control_mode", 1),
            **{
                13003 + i: (
                    f"load_timing_{i // 4 + 1}_{('start_h', 'start_m', 'end_h', 'end_m')[i % 4]}",
                    1,
                )
                for i in range(8)
            },
            13011: ("load_on_off", 1),
            13012: ("load_optimised_start_h", 1),
            13013: ("load_optimised_start_m", 1),
            13014: ("load_optimised_end_h", 1),
            13015: ("load_optimised_end_m", 1),
            13016: ("load_optimised_power_w", 1),
            13017: ("forced_startup_low_soc", 1),
            13018: ("pv_power_limitation_sht_only", 1),
        },
    ),
    (
        "holding",
        33208,
        33219,
        {
            33208: ("forced_charging_enable", 1),
            33209: ("forced_charging_days", 1),
            33210: ("forced_charging_1_start_h", 1),
            33211: ("forced_charging_1_start_m", 1),
            33212: ("forced_charging_1_end_h", 1),
            33213: ("forced_charging_1_end_m", 1),
            33214: ("forced_charging_1_target_soc", 1),
            33215: ("forced_charging_2_start_h", 1),
            33216: ("forced_charging_2_start_m", 1),
            33217: ("forced_charging_2_end_h", 1),
            33218: ("forced_charging_2_end_m", 1),
            33219: ("forced_charging_2_target_soc", 1),
        },
    ),
    ("holding", 33274, 33274, {33274: ("load_rated_power", 1)}),
    (
        "input",
        13200,
        13207,
        {
            13200: ("ch2_total_active_power", 2),
            13202: ("ch2_phase_a_active_power", 2),
            13204: ("ch2_phase_b_active_power", 2),
            13206: ("ch2_phase_c_active_power", 2),
        },
    ),
    ("input", 13250, 13264, {13250: ("firmware_inverter", 15)}),
    ("input", 13265, 13279, {13265: ("firmware_comm_module", 15)}),
    ("input", 13280, 13294, {13280: ("firmware_battery", 15)}),
)

BATTERY_EXTRA: tuple[Extra, ...] = (
    (
        "input",
        10741,
        10749,
        {
            10741: ("battery_voltage", 1),
            10742: ("battery_current", 1),
            10743: ("battery_temperature", 1),
            10744: ("battery_soc", 1),
            10745: ("battery_soh", 1),
            10746: ("battery_total_charge", 2),
            10748: ("battery_total_discharge", 2),
        },
    ),
)

AC22_EXTRA: tuple[Extra, ...] = (
    (
        "input",
        21216,
        21235,
        {
            21216: ("ac22_model", 5),
            21224: ("ac22_device_type", 1),
            21225: ("ac22_phases", 1),
            21226: ("ac22_version", 10),
        },
    ),
    (
        "input",
        21262,
        21273,
        {
            21262: ("ac22_nominal_voltage", 1),
            21263: ("ac22_rated_current", 1),
            21270: ("ac22_phase_mode", 1),
            21272: ("ac22_min_power_w", 1),
            21273: ("ac22_max_power_w", 1),
        },
    ),
    (
        "input",
        21300,
        21322,
        {
            21300: ("ac22_total_energy_wh", 2),
            21302: ("ac22_phase_a_voltage", 1),
            21303: ("ac22_phase_b_voltage", 1),
            21304: ("ac22_phase_c_voltage", 1),
            21305: ("ac22_phase_a_current", 1),
            21306: ("ac22_phase_b_current", 1),
            21307: ("ac22_phase_c_current", 1),
            21308: ("ac22_power_w", 2),
            21310: ("ac22_session_energy_wh", 2),
            21312: ("ac22_cp_voltage", 1),
            21314: ("ac22_start_mode", 1),
            21315: ("ac22_power_request", 1),
            21316: ("ac22_power_control_allowed", 1),
            21317: ("ac22_charging_status", 1),
            21318: ("ac22_session_start", 2),
            21320: ("ac22_session_end", 2),
            21322: ("ac22_available_current", 1),
        },
    ),
    (
        "holding",
        21200,
        21213,
        {
            21203: ("ac22_output_current_setpoint", 1),
            21204: ("ac22_phase_mode_setpoint", 1),
            21211: ("ac22_charger_enabled", 1),
            21212: ("ac22_start_stop", 1),
        },
    ),
    ("holding", 21232, 21232, {21232: ("ac22_km_per_kwh", 1)}),
    (
        "holding",
        21298,
        21299,
        {21298: ("ac22_plug_and_charge", 1), 21299: ("ac22_offline_current", 1)},
    ),
    ("holding", 21313, 21313, {21313: ("ac22_power_limit", 1)}),
)


# ---------------------------------------------------------------------------
# Sweep model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Item:
    """One named value inside a span (a mapped register or an extra name)."""

    name: str
    address: int
    words: int
    register: Any | None = None  # registers.Register when mapped


@dataclass(slots=True)
class Span:
    table: str  # "input" | "holding"
    start: int
    end: int
    items: list[Item]
    source: str  # block name or "extra"

    @property
    def count(self) -> int:
        return self.end - self.start + 1


@dataclass(slots=True)
class Target:
    name: str
    host: str
    port: int
    unit: int
    interval: float
    spans: list[Span]
    words: dict[str, dict[int, int]] = field(
        default_factory=lambda: {"input": {}, "holding": {}}
    )
    status: dict[str, dict[int, str]] = field(
        default_factory=lambda: {"input": {}, "holding": {}}
    )
    requests: int = 0
    exceptions: int = 0
    timeouts: int = 0


def _chunks(start: int, end: int, items: list[Item]) -> list[tuple[int, int]]:
    """Split [start, end] into <= MAX_WORDS spans on item boundaries."""
    out: list[tuple[int, int]] = []
    chunk_start = start
    for item in sorted(items, key=lambda i: i.address):
        item_end = item.address + item.words - 1
        if item_end - chunk_start + 1 > MAX_WORDS:
            out.append((chunk_start, item.address - 1))
            chunk_start = item.address
    out.append((chunk_start, end))
    return [(a, b) for a, b in out if a <= b]


def spans_from_map(regmap: ModuleType, device: str) -> list[Span]:
    spans: list[Span] = []
    for block in regmap.BLOCKS:
        if str(block.device) != device:
            continue
        items = [Item(r.key, r.address, r.count, r) for r in block.registers]
        spans.append(Span(str(block.table), block.start, block.end, items, block.name))
    return spans


def spans_from_extra(extra: tuple[Extra, ...]) -> list[Span]:
    spans: list[Span] = []
    for table, start, end, names in extra:
        items = [Item(name, addr, words) for addr, (name, words) in names.items()]
        for a, b in _chunks(start, end, items):
            inside = [i for i in items if a <= i.address <= b]
            spans.append(Span(table, a, b, inside, "extra"))
    return spans


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class Reader:
    """Paced, retrying FC03/FC04 reads over one pymodbus client."""

    def __init__(self, target: Target, timeout: float, retries: int) -> None:
        self.t = target
        self.timeout = timeout
        self.retries = retries
        self.client = AsyncModbusTcpClient(
            target.host, port=target.port, timeout=timeout, retries=0, reconnect_delay=0
        )
        self._last = 0.0

    async def connect(self) -> None:
        if not await self.client.connect():
            raise SystemExit(
                f"{self.t.name}: cannot connect to {self.t.host}:{self.t.port}"
            )

    def close(self) -> None:
        self.client.close()

    async def _pace(self) -> None:
        delay = self._last + self.t.interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def read(self, table: str, start: int, count: int) -> list[int] | int | None:
        """Words, an exception code (int), or None after timeouts."""
        func = (
            self.client.read_input_registers
            if table == "input"
            else self.client.read_holding_registers
        )
        for attempt in range(self.retries + 1):
            await self._pace()
            self.t.requests += 1
            try:
                if not self.client.connected:
                    await self.client.connect()
                response = await func(start - 1, count=count, device_id=self.t.unit)
            except (ModbusException, OSError, TimeoutError) as err:
                self.t.timeouts += 1
                _LOGGER.debug("%s %s %d+%d: %s", self.t.name, table, start, count, err)
                response = None
            finally:
                self._last = time.monotonic()
            if response is None:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if response.isError():
                code = int(getattr(response, "exception_code", 0) or 0)
                self.t.exceptions += 1
                if code in RETRY_CODES and attempt < self.retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                return code
            return list(response.registers)[:count]
        return None


def _store(target: Target, table: str, start: int, words: list[int]) -> None:
    for offset, word in enumerate(words):
        target.words[table][start + offset] = word & 0xFFFF
        target.status[table][start + offset] = "ok"


def _mark(target: Target, table: str, start: int, count: int, status: str) -> None:
    for address in range(start, start + count):
        if target.status[table].get(address) != "ok":
            target.status[table][address] = status


async def sweep_span(reader: Reader, span: Span) -> None:
    t = reader.t
    result = await reader.read(span.table, span.start, span.count)
    if isinstance(result, list):
        _store(t, span.table, span.start, result)
        return
    if result is None:
        _mark(t, span.table, span.start, span.count, "timeout")
        _LOGGER.warning(
            "%s: %s %d-%d timed out", t.name, span.table, span.start, span.end
        )
        return
    if result not in SPLIT_CODES:
        _mark(t, span.table, span.start, span.count, f"exception_0x{result:02X}")
        return

    # Block refused: fall back to one register at a time. Mapped registers
    # are read whole (a U32 read as two halves can be refused); addresses in
    # gaps are read one word at a time.
    _LOGGER.info(
        "%s: %s %d-%d refused (0x%02X), reading register by register",
        t.name,
        span.table,
        span.start,
        span.end,
        result,
    )
    covered: set[int] = set()
    singles: list[tuple[int, int]] = []
    for item in span.items:
        singles.append((item.address, item.words))
        covered.update(range(item.address, item.address + item.words))
    singles.extend((a, 1) for a in range(span.start, span.end + 1) if a not in covered)
    for start, count in sorted(singles):
        one = await reader.read(span.table, start, count)
        if isinstance(one, list):
            _store(t, span.table, start, one)
        elif one is None:
            _mark(t, span.table, start, count, "timeout")
        elif one == 0x02:
            _mark(t, span.table, start, count, "illegal_address")
        else:
            _mark(t, span.table, start, count, f"exception_0x{one:02X}")


# ---------------------------------------------------------------------------
# Decoding and output
# ---------------------------------------------------------------------------


def _decode_item(regmap: ModuleType, item: Item, words: dict[int, int]) -> Any:
    chunk = [words.get(a) for a in range(item.address, item.address + item.words)]
    if any(w is None for w in chunk):
        return None
    if item.register is not None:
        value, _raw = regmap.decode_register(item.register, chunk)
        if value is not None and item.register.options is not None:
            raw = regmap.raw_value(item.register, chunk)
            return f"{value} ({item.register.label(raw)})"
        return value
    if item.words >= 3:
        return regmap.decode_string(chunk)
    if item.words == 2:
        return regmap.combine_u32(chunk[0], chunk[1])
    return chunk[0]


def build_rows(
    regmap: ModuleType, target: Target, spans: list[Span]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for span in spans:
        by_addr = {i.address: i for i in span.items}
        owner: dict[int, tuple[Item, int]] = {}
        for item in span.items:
            for off in range(item.words):
                owner[item.address + off] = (item, off)
        for address in range(span.start, span.end + 1):
            if (span.table, address) in seen:
                continue
            seen.add((span.table, address))
            item, off = owner.get(address, (None, 0))
            raw = target.words[span.table].get(address)
            value = None
            if item is not None and off == 0 and address in by_addr:
                value = _decode_item(regmap, item, target.words[span.table])
            name = ""
            if item is not None:
                name = item.name if off == 0 else f"{item.name}[{off}]"
            uom = (
                getattr(item.register, "unit", None)
                if item is not None and item.register
                else None
            )
            rows.append(
                {
                    "target": target.name,
                    "unit_id": target.unit,
                    "table": span.table,
                    "register_1based": address,
                    "register_0based": address - 1,
                    "name": name,
                    "raw_u16": raw,
                    "value": value,
                    "uom": uom,
                    "status": target.status[span.table].get(address, "not_read"),
                    "block": span.source,
                }
            )
    return rows


def _identity(regmap: ModuleType, target: Target) -> dict[str, Any]:
    words = target.words["input"]
    out: dict[str, Any] = {}
    if target.name == "inverter" and 5000 in words:
        code = words[5000]
        out["device_type_code"] = code
        out["model"] = regmap.DEVICE_TYPE_CODES.get(code, f"0x{code:04X}")
        serial = regmap.decode_string([words.get(a, 0) for a in range(4990, 5000)])
        out["serial"] = serial
    if target.name == "ihm" and 8000 in words:
        out["device_type_code"] = words[8000]
        out["model"] = regmap.IHM_DEVICE_TYPES.get(words[8000], "unknown")
    return out


def redact(snapshot: dict[str, Any]) -> None:
    """Remove the serial number and the IP addresses in place."""
    for t in snapshot["targets"]:
        t["host"] = f"{t['name']}.redacted"
        serial = t.get("identity", {}).get("serial")
        if serial:
            t["identity"]["serial"] = "REDACTED"
            # The serial lives in IR 4990-4999: blank the words too.
            for address in range(4990, 5000):
                if str(address) in t["tables"]["input"]:
                    t["tables"]["input"][str(address)] = 0x5858  # "XX"
    for row in snapshot["rows"]:
        if (
            row["target"] == "inverter"
            and row["table"] == "input"
            and 4990 <= row["register_1based"] <= 4999
        ):
            row["raw_u16"] = 0x5858
            row["value"] = "REDACTED" if row["register_1based"] == 4990 else None


async def run_target(
    regmap: ModuleType, target: Target, timeout: float, retries: int
) -> None:
    reader = Reader(target, timeout, retries)
    await reader.connect()
    try:
        for index, span in enumerate(target.spans, 1):
            _LOGGER.info(
                "%s [%d/%d] %s %s %d-%d",
                target.name,
                index,
                len(target.spans),
                span.source,
                span.table,
                span.start,
                span.end,
            )
            await sweep_span(reader, span)
    finally:
        reader.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Snapshot Sungrow inverter / iHM / AC22 registers to JSON + CSV (read only)."
    )
    p.add_argument("--inverter", help="WiNet-S / inverter host")
    p.add_argument("--inverter-port", type=int, default=502)
    p.add_argument("--inverter-unit", type=int, default=1)
    p.add_argument("--ihm", help="iHomeManager host")
    p.add_argument("--ihm-port", type=int, default=503)
    p.add_argument("--ihm-unit", type=int, default=247)
    p.add_argument(
        "--battery-unit", type=int, help="battery unit behind the WiNet-S (often 2)"
    )
    p.add_argument(
        "--ac22-unit",
        type=int,
        help="AC22 unit behind the WiNet-S (see its device list)",
    )
    p.add_argument("--timeout", type=float, default=3.0)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument(
        "--inverter-interval",
        type=float,
        default=0.1,
        help="seconds between inverter requests",
    )
    p.add_argument(
        "--ihm-interval",
        type=float,
        default=1.0,
        help="seconds between iHM requests (>= 1.0)",
    )
    p.add_argument("--out", type=Path, default=Path("snapshots"))
    p.add_argument(
        "--redact", action="store_true", help="remove serial number and IP addresses"
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if not args.inverter and not args.ihm:
        p.error("give --inverter and/or --ihm")
    if (args.battery_unit or args.ac22_unit) and not args.inverter:
        p.error(
            "--battery-unit / --ac22-unit need --inverter (they sit behind the WiNet-S)"
        )
    args.ihm_interval = max(1.0, args.ihm_interval)
    return args


def build_targets(regmap: ModuleType, args: argparse.Namespace) -> list[Target]:
    targets: list[Target] = []
    if args.inverter:
        targets.append(
            Target(
                "inverter",
                args.inverter,
                args.inverter_port,
                args.inverter_unit,
                args.inverter_interval,
                spans_from_map(regmap, "inverter") + spans_from_extra(INVERTER_EXTRA),
            )
        )
        if args.battery_unit is not None:
            targets.append(
                Target(
                    "battery",
                    args.inverter,
                    args.inverter_port,
                    args.battery_unit,
                    args.inverter_interval,
                    spans_from_extra(BATTERY_EXTRA),
                )
            )
        if args.ac22_unit is not None:
            targets.append(
                Target(
                    "ac22",
                    args.inverter,
                    args.inverter_port,
                    args.ac22_unit,
                    args.inverter_interval,
                    spans_from_extra(AC22_EXTRA),
                )
            )
    if args.ihm:
        targets.append(
            Target(
                "ihm",
                args.ihm,
                args.ihm_port,
                args.ihm_unit,
                args.ihm_interval,
                spans_from_map(regmap, "ihm"),
            )
        )
    return targets


async def async_main(argv: list[str] | None = None) -> Path:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    regmap = load_register_map()
    targets = build_targets(regmap, args)
    started = datetime.now().astimezone()

    for target in targets:
        await run_target(regmap, target, args.timeout, args.retries)

    rows: list[dict[str, Any]] = []
    for target in targets:
        rows.extend(build_rows(regmap, target, target.spans))

    snapshot: dict[str, Any] = {
        "schema": SCHEMA,
        "created": started.isoformat(timespec="seconds"),
        "tool": "scripts/dump_registers.py",
        "pymodbus": getattr(pymodbus, "__version__", "unknown"),
        "targets": [
            {
                "name": t.name,
                "host": t.host,
                "port": t.port,
                "unit": t.unit,
                "identity": _identity(regmap, t),
                "stats": {
                    "requests": t.requests,
                    "exceptions": t.exceptions,
                    "timeouts": t.timeouts,
                },
                "tables": {
                    table: {str(a): w for a, w in sorted(words.items())}
                    for table, words in t.words.items()
                },
                "rejected": {
                    table: sorted(a for a, s in st.items() if s == "illegal_address")
                    for table, st in t.status.items()
                },
            }
            for t in targets
        ],
        "rows": rows,
    }
    if args.redact:
        redact(snapshot)

    model = (
        next(
            (
                t["identity"].get("model")
                for t in snapshot["targets"]
                if t["name"] == "inverter"
            ),
            None,
        )
        or "ihm"
    )
    stem = f"snapshot_{str(model).replace('.', '_')}_{started:%Y%m%d_%H%M%S}"
    args.out.mkdir(parents=True, exist_ok=True)
    json_path = args.out / f"{stem}.json"
    csv_path = args.out / f"{stem}.csv"
    json_path.write_text(json.dumps(snapshot, indent=1, default=str), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "target",
                "unit_id",
                "table",
                "register_1based",
                "register_0based",
                "name",
                "raw_u16",
                "value",
                "uom",
                "status",
                "block",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    for t in targets:
        ok = sum(1 for st in t.status.values() for s in st.values() if s == "ok")
        bad = sum(1 for st in t.status.values() for s in st.values() if s != "ok")
        _LOGGER.info(
            "%s: %d words read, %d refused or missing, %d requests, %d exceptions, %d timeouts",
            t.name,
            ok,
            bad,
            t.requests,
            t.exceptions,
            t.timeouts,
        )
    _LOGGER.info("Wrote %s and %s", json_path, csv_path)
    return json_path


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
