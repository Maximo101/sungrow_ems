"""Persistent Modbus TCP hub for Sungrow devices.

One ``SungrowModbusHub`` per host:port. No Home Assistant imports, so it can be
exercised against a simulator outside HA.

Behaviour:
  * One long-lived ``AsyncModbusTcpClient``. The socket is only torn down after
    a connection error or two consecutive timeouts, never per poll. pymodbus's
    own auto-reconnect is disabled so a background reconnect can never take a
    second TCP slot on the iHM (one connection per port).
  * Every transaction runs under one ``asyncio.Lock``: exactly one request in
    flight per socket, reads and writes alike.
  * Optional pacing between requests (iHM: 1000 ms, measured from the end of
    the previous transaction). The first request after a connect is paced too.
  * Block reads follow ``registers.BLOCKS``. If the device answers a block with
    a Modbus exception (illegal address/value, device failure), the block is
    split: first into its gap-free runs, then by halves on register boundaries,
    down to single registers. The working split is remembered, rejected spans
    are retried hourly, and learned splits are reset every few hours.
  * Retries cover timeouts, dropped sockets and busy/gateway exception codes.
    Reconnects back off exponentially after a failed connect.
  * A read cycle aborts at the first block that is unreachable after retries,
    returning what it already has. It raises only when nothing was read.

pymodbus compatibility: 3.8 and later. The unit-id keyword is detected
(``device_id`` from 3.10, ``slave`` before), with a fallback if detection is
wrong.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterable, Sequence
import contextlib
from dataclasses import asdict, dataclass, field
import inspect
import logging
import time
from typing import Any

import pymodbus
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

from .registers import (
    MAX_BLOCK_WORDS,
    Block,
    DecodeResult,
    Register,
    Table,
    contiguous_runs,
    decode_registers,
    decode_tables,
    encode_block,
    encode_value,
    to_wire,
)

_LOGGER = logging.getLogger(__name__)

# Modbus exception codes
EXC_ILLEGAL_FUNCTION = 0x01
EXC_ILLEGAL_DATA_ADDRESS = 0x02
EXC_ILLEGAL_DATA_VALUE = 0x03
EXC_DEVICE_FAILURE = 0x04
EXC_ACKNOWLEDGE = 0x05
EXC_DEVICE_BUSY = 0x06
EXC_GATEWAY_PATH_UNAVAILABLE = 0x0A
EXC_GATEWAY_TARGET_NO_RESPONSE = 0x0B

#: The device rejects this address range: split the request and try again.
SPLIT_CODES: frozenset[int] = frozenset(
    {EXC_ILLEGAL_DATA_ADDRESS, EXC_ILLEGAL_DATA_VALUE, EXC_DEVICE_FAILURE}
)
#: The device (or the WiNet-S gateway in front of it) is busy or cannot reach
#: the target: retry the same request, and treat exhaustion as unreachable.
TRANSIENT_CODES: frozenset[int] = frozenset(
    {
        EXC_ACKNOWLEDGE,
        EXC_DEVICE_BUSY,
        EXC_GATEWAY_PATH_UNAVAILABLE,
        EXC_GATEWAY_TARGET_NO_RESPONSE,
    }
)

#: FC16 payload limit from the Modbus spec.
MAX_WRITE_WORDS = 123

_FUNCTION_READ = {
    Table.INPUT: "read_input_registers",
    Table.HOLDING: "read_holding_registers",
}
_UNIT_KWARGS = ("device_id", "slave", "unit")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SungrowModbusError(Exception):
    """Base error for the hub."""


class SungrowConnectionError(SungrowModbusError):
    """Device unreachable: cannot connect, socket dropped, or retries exhausted."""


class SungrowTimeoutError(SungrowConnectionError):
    """No response within the timeout."""


class SungrowProtocolError(SungrowModbusError):
    """Response arrived but was malformed (for example too few registers)."""


class SungrowExceptionResponse(SungrowModbusError):
    """Device answered with a Modbus exception code."""

    def __init__(self, function: str, address: int, code: int | None) -> None:
        self.function = function
        self.address = address  # documented, 1-based
        self.code = code
        code_text = "unknown" if code is None else f"0x{code:02X}"
        super().__init__(f"{function} @ {address} rejected with exception {code_text}")


class SungrowWriteError(SungrowModbusError):
    """A write was refused or could not be encoded."""


class SungrowWriteVerifyError(SungrowWriteError):
    """Read-back after a write did not match what was written."""


# ---------------------------------------------------------------------------
# Results and stats
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReadResult:
    """Raw words from one read cycle, keyed by table then documented address."""

    words: dict[Table, dict[int, int]] = field(
        default_factory=lambda: {Table.INPUT: {}, Table.HOLDING: {}}
    )
    blocks_ok: list[str] = field(default_factory=list)
    blocks_failed: dict[str, str] = field(default_factory=dict)
    blocks_skipped: list[str] = field(default_factory=list)
    unsupported_spans: list[tuple[str, int, int]] = field(default_factory=list)
    requests: int = 0
    duration: float = 0.0

    @property
    def complete(self) -> bool:
        """True if every requested block was read."""
        return not self.blocks_failed and not self.blocks_skipped

    def decode(self, blocks: Iterable[Block]) -> DecodeResult:
        """Decode the given blocks from the words collected in this cycle."""
        return decode_tables(blocks, self.words)


@dataclass(slots=True)
class HubStats:
    """Counters for diagnostics."""

    requests: int = 0
    timeouts: int = 0
    connection_errors: int = 0
    exception_responses: int = 0
    connects: int = 0
    connect_failures: int = 0
    read_cycles: int = 0
    read_cycles_failed: int = 0
    read_cycles_partial: int = 0
    writes: int = 0
    write_failures: int = 0
    last_error: str | None = None
    last_success: float | None = None  # wall clock (time.time())
    last_cycle_duration: float | None = None
    last_request_duration: float | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _filter_kwargs(func: Any, wanted: dict[str, Any]) -> dict[str, Any]:
    """Keep only keyword arguments that ``func`` accepts."""
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return wanted
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return wanted
    return {k: v for k, v in wanted.items() if k in params}


def _detect_unit_kwarg(client: AsyncModbusTcpClient) -> str:
    """Return the unit-id keyword this pymodbus version uses."""
    try:
        params = inspect.signature(client.read_holding_registers).parameters
    except (TypeError, ValueError):
        return "device_id"
    for name in _UNIT_KWARGS:
        if name in params:
            return name
    return "device_id"


def split_span(block: Block, start: int, count: int) -> list[tuple[int, int]]:
    """Smaller spans to try after the device rejects (start, count).

    Order of preference:
      1. the gap-free runs of documented registers inside the span;
      2. the span trimmed to its documented registers;
      3. two halves, split on a register boundary.
    Returns [] if the span is a single register and cannot be split.
    """
    end = start + count - 1
    regs = [r for r in block.registers if r.address >= start and r.end <= end]
    if not regs:
        return []
    runs = contiguous_runs(regs)
    if len(runs) > 1:
        return runs
    run = runs[0]
    if run != (start, count):
        return [run]
    if len(regs) == 1:
        return []
    mid = len(regs) // 2
    left, right = regs[:mid], regs[mid:]
    return [
        (left[0].address, left[-1].end - left[0].address + 1),
        (right[0].address, right[-1].end - right[0].address + 1),
    ]


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------


class SungrowModbusHub:
    """Serialised, persistent Modbus TCP connection to one host:port."""

    def __init__(
        self,
        *,
        host: str,
        port: int = 502,
        device_id: int = 1,
        name: str | None = None,
        timeout: float = 3.0,
        retries: int = 2,
        min_request_interval: float = 0.0,
        reconnect_backoff_max: float = 60.0,
        unsupported_retry: float = 3600.0,
        plan_reset: float = 6 * 3600.0,
    ) -> None:
        if not host:
            raise ValueError("host is required")
        self._host = host
        self._port = int(port)
        self._device_id = int(device_id)
        self._name = name or f"{host}:{port}"
        self._timeout = float(timeout)
        self._retries = max(0, int(retries))
        self._min_interval = max(0.0, float(min_request_interval))
        self._backoff_max = max(1.0, float(reconnect_backoff_max))
        self._unsupported_retry = float(unsupported_retry)
        self._plan_reset = float(plan_reset)

        self._lock = asyncio.Lock()
        self._client: AsyncModbusTcpClient | None = None
        self._unit_kwarg: str | None = None
        self._closed = False

        self._last_io_end = 0.0
        self._connect_failures = 0
        self._next_connect_at = 0.0
        self._consecutive_timeouts = 0

        # Learned block splits: plan key -> spans, and when the plan was learned.
        self._plans: dict[str, list[tuple[int, int]]] = {}
        self._plan_learned_at: dict[str, float] = {}
        # Rejected spans: (device_id, table, start, count) -> retry time (monotonic).
        self._unsupported: dict[tuple[int, Table, int, int], float] = {}

        self.stats = HubStats()

    # -- properties --------------------------------------------------------

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def device_id(self) -> int:
        return self._device_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def connected(self) -> bool:
        return self._client is not None and bool(self._client.connected)

    def __repr__(self) -> str:
        return f"<SungrowModbusHub {self._name} unit={self._device_id} connected={self.connected}>"

    # -- lifecycle ---------------------------------------------------------

    async def async_connect(self) -> None:
        """Open the connection now (ignoring backoff). Raises SungrowConnectionError."""
        async with self._lock:
            await self._ensure_connected(ignore_backoff=True)

    async def async_close(self) -> None:
        """Close the socket and refuse further requests."""
        self._closed = True
        try:
            async with asyncio.timeout(self._timeout * (self._retries + 1) + 5):
                async with self._lock:
                    self._drop_client()
        except TimeoutError:
            self._drop_client()

    # -- reads -------------------------------------------------------------

    async def async_read_blocks(
        self, blocks: Iterable[Block], *, device_id: int | None = None
    ) -> ReadResult:
        """Read blocks in order and return their raw words.

        Stops at the first block that is unreachable after retries; later
        blocks are listed in ``blocks_skipped``. Raises SungrowConnectionError
        only if no block was read at all.
        """
        unit = self._device_id if device_id is None else int(device_id)
        pending = list(blocks)
        result = ReadResult()
        started = time.monotonic()
        self.stats.read_cycles += 1

        for index, block in enumerate(pending):
            try:
                await self._read_block(block, unit, result)
            except SungrowConnectionError as err:
                result.blocks_failed[block.name] = str(err)
                result.blocks_skipped.extend(b.name for b in pending[index + 1 :])
                break
            except SungrowModbusError as err:
                result.blocks_failed[block.name] = str(err)
                continue
            result.blocks_ok.append(block.name)

        result.duration = time.monotonic() - started
        self.stats.last_cycle_duration = result.duration

        if pending and not result.blocks_ok:
            self.stats.read_cycles_failed += 1
            first = next(iter(result.blocks_failed.values()), "no response")
            raise SungrowConnectionError(
                f"{self._name}: no block could be read ({first})"
            )
        if not result.complete:
            self.stats.read_cycles_partial += 1
            _LOGGER.debug(
                "%s: partial read, failed=%s skipped=%s",
                self._name,
                result.blocks_failed,
                result.blocks_skipped,
            )
        if result.blocks_ok:
            self.stats.last_success = time.time()
        return result

    async def async_read_raw(
        self, table: Table, address: int, count: int, *, device_id: int | None = None
    ) -> list[int]:
        """Read ``count`` words starting at a documented address."""
        if not 1 <= count <= MAX_BLOCK_WORDS:
            raise ValueError(f"count must be 1..{MAX_BLOCK_WORDS}")
        unit = self._device_id if device_id is None else int(device_id)
        return await self._read_span(table, address, count, unit)

    async def async_read_registers(
        self, registers: Sequence[Register], *, device_id: int | None = None
    ) -> DecodeResult:
        """Read and decode an ad-hoc set of registers (for example the config
        flow reading serial number and device type).

        Registers are grouped per table into gap-free runs; runs longer than
        MAX_BLOCK_WORDS are chunked on register boundaries.
        """
        unit = self._device_id if device_id is None else int(device_id)
        result = DecodeResult()
        for table in Table:
            regs = sorted(
                (r for r in registers if r.table is table), key=lambda r: r.address
            )
            if not regs:
                continue
            words: dict[int, int] = {}
            for start, count in self._chunk_runs(regs):
                data = await self._read_span(table, start, count, unit)
                words.update({start + i: w for i, w in enumerate(data)})
            result.merge(decode_registers(regs, words))
        return result

    # -- writes ------------------------------------------------------------

    async def async_write_words(
        self,
        address: int,
        words: Sequence[int],
        *,
        device_id: int | None = None,
        use_fc16: bool | None = None,
    ) -> None:
        """Write raw words at a documented holding address.

        FC06 for a single word unless ``use_fc16`` is set; FC16 otherwise.
        Writes are retried on timeouts and dropped sockets, which is safe for
        Sungrow set-point registers because they are idempotent.
        """
        payload = [int(w) & 0xFFFF for w in words]
        if not 1 <= len(payload) <= MAX_WRITE_WORDS:
            raise SungrowWriteError(f"write length must be 1..{MAX_WRITE_WORDS}")
        fc16 = (len(payload) > 1) if use_fc16 is None else bool(use_fc16)
        if not fc16 and len(payload) != 1:
            raise SungrowWriteError("FC06 writes exactly one register")
        unit = self._device_id if device_id is None else int(device_id)

        self.stats.writes += 1
        try:
            if fc16:
                await self._transact_retry(
                    "write_registers", address, device_id=unit, values=payload
                )
            else:
                await self._transact_retry(
                    "write_register", address, device_id=unit, value=payload[0]
                )
        except SungrowExceptionResponse as err:
            self.stats.write_failures += 1
            raise SungrowWriteError(
                f"{self._name}: write to {address} refused: {err}"
            ) from err
        except SungrowModbusError:
            self.stats.write_failures += 1
            raise
        _LOGGER.debug(
            "%s: wrote %s to %d via %s",
            self._name,
            [f"0x{w:04X}" for w in payload],
            address,
            "FC16" if fc16 else "FC06",
        )

    async def async_write_register(
        self,
        register: Register,
        value: float | int,
        *,
        device_id: int | None = None,
        verify: bool = False,
        use_fc16: bool | None = None,
    ) -> list[int]:
        """Encode and write one writable holding register. Returns the raw words.

        32-bit registers always use FC16 (both words in one transaction).
        With ``verify`` the register is read back and compared.
        """
        if register.table is not Table.HOLDING or not register.writable:
            raise SungrowWriteError(
                f"{register.key} is not a writable holding register"
            )
        try:
            words = encode_value(register, value)
        except (TypeError, ValueError) as err:
            raise SungrowWriteError(str(err)) from err
        fc16 = True if len(words) > 1 else use_fc16
        await self.async_write_words(
            register.address, words, device_id=device_id, use_fc16=fc16
        )
        if verify:
            await self._verify(register.address, words, device_id)
        return words

    async def async_write_registers(
        self,
        items: Sequence[tuple[Register, float | int]],
        *,
        device_id: int | None = None,
        verify: bool = False,
    ) -> list[int]:
        """Write several contiguous holding registers in one FC16 transaction.

        Example (iHM spec, section 4): EMS mode 8024, command 8025 and power
        8026-8027 in one request so the iHM never sees a half-applied command.
        """
        try:
            start, words = encode_block(items)
        except (TypeError, ValueError) as err:
            raise SungrowWriteError(str(err)) from err
        await self.async_write_words(start, words, device_id=device_id, use_fc16=True)
        if verify:
            await self._verify(start, words, device_id)
        return words

    # -- diagnostics -------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Snapshot for diagnostics.py. Contains no secrets."""
        now = time.monotonic()
        return {
            "name": self._name,
            "host": self._host,
            "port": self._port,
            "device_id": self._device_id,
            "connected": self.connected,
            "pymodbus_version": getattr(pymodbus, "__version__", "unknown"),
            "unit_kwarg": self._unit_kwarg,
            "timeout": self._timeout,
            "retries": self._retries,
            "min_request_interval": self._min_interval,
            "reconnect_backoff_remaining": max(
                0.0, round(self._next_connect_at - now, 1)
            ),
            "stats": asdict(self.stats),
            "learned_plans": {k: list(v) for k, v in self._plans.items()},
            "unsupported_spans": [
                {
                    "device_id": unit,
                    "table": str(table),
                    "start": start,
                    "count": count,
                    "retry_in": max(0.0, round(retry_at - now, 0)),
                }
                for (unit, table, start, count), retry_at in self._unsupported.items()
            ],
        }

    # -- internals: blocks -------------------------------------------------

    async def _read_block(self, block: Block, unit: int, result: ReadResult) -> None:
        """Read one block using its learned plan, splitting on rejection.

        Words are written into ``result`` as they arrive, so a connection
        failure halfway through still keeps the spans already read.
        """
        now = time.monotonic()
        key = f"{unit}:{block.table}:{block.name}"
        learned_at = self._plan_learned_at.get(key)
        if learned_at is not None and now - learned_at > self._plan_reset:
            _LOGGER.debug("%s: retrying full block %s", self._name, block.name)
            self._plans.pop(key, None)
            self._plan_learned_at.pop(key, None)

        plan = self._plans.get(key) or [(block.start, block.count)]
        queue: deque[tuple[int, int]] = deque(plan)
        learned: list[tuple[int, int]] = []
        changed = False
        sink = result.words[block.table]

        while queue:
            start, count = queue.popleft()
            ukey = (unit, block.table, start, count)
            retry_at = self._unsupported.get(ukey)
            if retry_at is not None and now < retry_at:
                learned.append((start, count))
                continue

            try:
                data = await self._read_span(block.table, start, count, unit)
            except SungrowExceptionResponse as err:
                parts = (
                    split_span(block, start, count) if err.code in SPLIT_CODES else []
                )
                if parts:
                    _LOGGER.debug(
                        "%s: %s %d+%d rejected (%s), retrying as %s",
                        self._name,
                        block.name,
                        start,
                        count,
                        err,
                        parts,
                    )
                    queue.extendleft(reversed(parts))
                    changed = True
                    continue
                if retry_at is None:
                    _LOGGER.info(
                        "%s: %s registers %d-%d not supported by the device (%s); "
                        "will retry in %.0f s",
                        self._name,
                        block.name,
                        start,
                        start + count - 1,
                        err,
                        self._unsupported_retry,
                    )
                self._unsupported[ukey] = time.monotonic() + self._unsupported_retry
                result.unsupported_spans.append((block.name, start, count))
                learned.append((start, count))
                changed = True
                continue

            result.requests += 1
            if self._unsupported.pop(ukey, None) is not None:
                _LOGGER.info(
                    "%s: %s %d+%d readable again", self._name, block.name, start, count
                )
            for offset, word in enumerate(data[:count]):
                sink[start + offset] = word
            learned.append((start, count))

        if changed:
            self._plans[key] = learned
            self._plan_learned_at[key] = time.monotonic()
            _LOGGER.debug(
                "%s: learned plan for %s: %s", self._name, block.name, learned
            )

    @staticmethod
    def _chunk_runs(regs: Sequence[Register]) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for start, count in contiguous_runs(regs):
            if count <= MAX_BLOCK_WORDS:
                spans.append((start, count))
                continue
            chunk_start = start
            chunk_end = start - 1
            for reg in (r for r in regs if start <= r.address <= start + count - 1):
                if (
                    reg.end - chunk_start + 1 > MAX_BLOCK_WORDS
                    and chunk_end >= chunk_start
                ):
                    spans.append((chunk_start, chunk_end - chunk_start + 1))
                    chunk_start = reg.address
                chunk_end = reg.end
            spans.append((chunk_start, chunk_end - chunk_start + 1))
        return spans

    async def _read_span(
        self, table: Table, start: int, count: int, unit: int
    ) -> list[int]:
        response = await self._transact_retry(
            _FUNCTION_READ[table], start, device_id=unit, count=count
        )
        registers = list(getattr(response, "registers", None) or [])
        if len(registers) < count:
            raise SungrowProtocolError(
                f"{self._name}: {table} {start}+{count} returned {len(registers)} words"
            )
        return registers[:count]

    async def _verify(
        self, address: int, words: Sequence[int], device_id: int | None
    ) -> None:
        unit = self._device_id if device_id is None else int(device_id)
        readback = await self._read_span(Table.HOLDING, address, len(words), unit)
        if list(readback) != list(words):
            raise SungrowWriteVerifyError(
                f"{self._name}: wrote {list(words)} to {address}, read back {readback}"
            )

    # -- internals: transactions -------------------------------------------

    async def _transact_retry(
        self, function: str, address: int, *, device_id: int, **kwargs: Any
    ) -> Any:
        """Run one transaction with retries. ``address`` is documented (1-based)."""
        last: SungrowModbusError | None = None
        for attempt in range(self._retries + 1):
            try:
                return await self._transact(
                    function, address, device_id=device_id, **kwargs
                )
            except SungrowExceptionResponse as err:
                if err.code not in TRANSIENT_CODES:
                    raise
                last = err
            except SungrowConnectionError as err:
                last = err
                if self._closed:
                    raise
            if attempt < self._retries:
                await asyncio.sleep(0.25 * (attempt + 1))

        assert last is not None
        self.stats.last_error = str(last)
        if isinstance(last, SungrowExceptionResponse):
            # Busy / gateway target not responding after every retry: the
            # device behind the gateway is effectively unreachable.
            raise SungrowConnectionError(str(last)) from last
        raise last

    async def _transact(
        self, function: str, address: int, *, device_id: int, **kwargs: Any
    ) -> Any:
        """One request/response under the lock, with pacing and error mapping."""
        wire = to_wire(address)
        async with self._lock:
            client = await self._ensure_connected()
            await self._pace()
            self.stats.requests += 1
            started = time.monotonic()
            try:
                async with asyncio.timeout(self._timeout * 2 + 1):
                    response = await self._invoke(
                        client, function, wire, device_id, **kwargs
                    )
            except TimeoutError as err:
                # Our guard fired inside pymodbus: its state is unknown, start clean.
                self.stats.timeouts += 1
                self._drop_client()
                raise SungrowTimeoutError(
                    f"{self._name}: {function} @ {address} timed out"
                ) from err
            except ModbusIOException as err:
                self.stats.timeouts += 1
                self._consecutive_timeouts += 1
                if self._consecutive_timeouts >= 2 or not client.connected:
                    self._drop_client()
                raise SungrowTimeoutError(
                    f"{self._name}: no response to {function} @ {address}: {err}"
                ) from err
            except (ConnectionException, OSError) as err:
                self.stats.connection_errors += 1
                self._drop_client()
                raise SungrowConnectionError(
                    f"{self._name}: connection lost: {err}"
                ) from err
            except ModbusException as err:
                self.stats.connection_errors += 1
                self._drop_client()
                raise SungrowConnectionError(
                    f"{self._name}: {function} failed: {err}"
                ) from err
            finally:
                self._last_io_end = time.monotonic()
                self.stats.last_request_duration = self._last_io_end - started

            self._consecutive_timeouts = 0
            if response is None:
                raise SungrowTimeoutError(
                    f"{self._name}: empty response to {function} @ {address}"
                )
            if response.isError():
                self.stats.exception_responses += 1
                raise SungrowExceptionResponse(
                    function, address, getattr(response, "exception_code", None)
                )
            return response

    async def _invoke(
        self,
        client: AsyncModbusTcpClient,
        function: str,
        wire_address: int,
        device_id: int,
        **kwargs: Any,
    ) -> Any:
        """Call a pymodbus method with whichever unit-id keyword it accepts."""
        method = getattr(client, function)
        current = self._unit_kwarg or "device_id"
        try:
            return await method(wire_address, **kwargs, **{current: device_id})
        except TypeError as err:
            if "unexpected keyword" not in str(err):
                raise
            for alternative in _UNIT_KWARGS:
                if alternative == current:
                    continue
                try:
                    response = await method(
                        wire_address, **kwargs, **{alternative: device_id}
                    )
                except TypeError as inner:
                    if "unexpected keyword" in str(inner):
                        continue
                    raise
                _LOGGER.debug(
                    "%s: pymodbus unit keyword is %s", self._name, alternative
                )
                self._unit_kwarg = alternative
                return response
            raise

    async def _pace(self) -> None:
        if self._min_interval <= 0:
            return
        delay = self._last_io_end + self._min_interval - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    # -- internals: connection ---------------------------------------------

    async def _ensure_connected(
        self, *, ignore_backoff: bool = False
    ) -> AsyncModbusTcpClient:
        """Return a connected client, connecting if needed. Lock must be held."""
        if self._closed:
            raise SungrowConnectionError(f"{self._name}: hub is closed")
        if self._client is not None and self._client.connected:
            return self._client

        now = time.monotonic()
        if not ignore_backoff and now < self._next_connect_at:
            raise SungrowConnectionError(
                f"{self._name}: waiting {self._next_connect_at - now:.1f} s before reconnecting"
                + (
                    f" (last error: {self.stats.last_error})"
                    if self.stats.last_error
                    else ""
                )
            )

        self._drop_client()
        kwargs = _filter_kwargs(
            AsyncModbusTcpClient.__init__,
            {
                "host": self._host,
                "port": self._port,
                "timeout": self._timeout,
                "retries": 0,  # retries are handled here, under the lock
                "reconnect_delay": 0,  # no background reconnect (iHM: 1 socket per port)
                "name": f"sungrow_ems:{self._name}",
            },
        )
        client = AsyncModbusTcpClient(**kwargs)
        reason = "connect() returned False"
        connected = False
        try:
            async with asyncio.timeout(self._timeout + 2):
                connected = bool(await client.connect())
        except TimeoutError:
            reason = "connect timed out"
        except (OSError, ModbusException) as err:
            reason = str(err) or type(err).__name__

        if not connected or not client.connected:
            with contextlib.suppress(Exception):
                client.close()
            self._connect_failures += 1
            self.stats.connect_failures += 1
            delay = min(
                self._backoff_max, float(2 ** min(self._connect_failures - 1, 10))
            )
            self._next_connect_at = time.monotonic() + delay
            message = (
                f"{self._name}: cannot connect to {self._host}:{self._port}: {reason}"
            )
            self.stats.last_error = message
            if self._connect_failures == 1:
                _LOGGER.warning("%s; retrying in %.0f s", message, delay)
            else:
                _LOGGER.debug("%s; retrying in %.0f s", message, delay)
            raise SungrowConnectionError(message)

        if self._connect_failures:
            _LOGGER.info(
                "%s: reconnected after %d failed attempts",
                self._name,
                self._connect_failures,
            )
        self._client = client
        self._connect_failures = 0
        self._next_connect_at = 0.0
        self._consecutive_timeouts = 0
        self.stats.connects += 1
        # Treat the connect as I/O so the first request is paced as well.
        self._last_io_end = time.monotonic()
        if self._unit_kwarg is None:
            self._unit_kwarg = _detect_unit_kwarg(client)
        _LOGGER.debug("%s: connected (unit keyword %s)", self._name, self._unit_kwarg)
        return client

    def _drop_client(self) -> None:
        client, self._client = self._client, None
        self._consecutive_timeouts = 0
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
