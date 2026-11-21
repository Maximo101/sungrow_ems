"""The single, guarded write path of Sungrow EMS.

Pure Python, no Home Assistant imports. Every write the integration makes
goes through ``SafeWriter.async_write``; nothing else may call the hub's
``async_write_holding`` (tests/test_write_safety.py checks the source).

Guardrails, in the order they are checked:

  1. **Allowlist.** The key must be in ``profiles.WRITE_TARGETS``.
  2. **Device configured.** The target's device (inverter, iHM, AC22) must
     have a hub and unit.
  3. **Model.** Inverter targets must be documented for the connected model
     (``profiles.ModelProfile.write_targets``). Unknown models are refused.
  4. **Evidence.** Targets or values that only come from community maps need
     "Allow unverified writes".
  5. **Value.** Enum targets: one of the allowed raw codes. Numeric targets:
     finite, inside the static bounds and the dynamic bounds given by the
     caller (for example Min SoC .. 100, or 0 .. iHM 8149 kW), rounded to
     the register's resolution.
  6. **Health.** No write while that device's coordinator is failing.
  7. **State delta.** The register is read first; if it already holds the
     value, nothing is written (and no budget is used).
  8. **Cooldown.** At most one write per register per ``cooldown`` seconds.
  9. **Daily budget.** At most ``daily_budget`` writes per register per
     rolling 24 h (EEPROM wear).
 10. **Read-back.** After ``readback_delay`` the register is read again; a
     mismatch (checked twice) raises ``ReadbackMismatch``.

Priority writes (``priority=True``) skip 6, 8 and 9: putting a setting back
must not be blocked by the guard that protected the change. They are still
allowlisted, validated, delta-checked and verified.

Restores (``restore=True``, implies priority) also skip 5: the value is one
the device itself held before (read back at the start of a session), so it
is written back even if it is not one the integration would choose (for
example an iHM mode code the app set). It must still fit the register.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
import logging
import math
import time
from typing import Any, Final, Protocol

from .modbus import SungrowConnectionError, SungrowModbusError
from .profiles import WRITE_TARGETS, Evidence, ModelProfile, WriteDevice, WriteTarget
from .registers import DataType, Table

_LOGGER = logging.getLogger(__name__)

DAY: Final = 86400.0
LOG_SIZE: Final = 50


class _Hub(Protocol):
    """What the writer needs from ``modbus.SungrowModbusHub``."""

    name: str

    async def async_read_raw(
        self, table: Table, address: int, count: int, *, device_id: int | None = None
    ) -> list[int]: ...

    async def async_write_holding(
        self, address: int, values: list[int], *, device_id: int | None = None
    ) -> None: ...


# ---------------------------------------------------------------------------
# Errors and results
# ---------------------------------------------------------------------------


class WriteError(Exception):
    """Base class. ``reason`` is a short machine-readable code."""

    reason = "error"

    def __init__(self, key: str, message: str) -> None:
        self.key = key
        super().__init__(f"{key}: {message}")


class WriteRefused(WriteError):
    """Refused before anything went on the wire."""

    def __init__(
        self, key: str, reason: str, message: str, retry_in: float | None = None
    ) -> None:
        self.reason = reason
        self.retry_in = retry_in
        super().__init__(key, message)


class WriteFailed(WriteError):
    """The device did not take the write (exception, timeout, no answer)."""

    reason = "failed"


class ReadbackMismatch(WriteFailed):
    """The device answered, but the register does not hold the written value."""

    reason = "readback_mismatch"

    def __init__(self, key: str, expected: list[int], actual: list[int]) -> None:
        self.expected = expected
        self.actual = actual
        WriteError.__init__(self, key, f"read back {actual}, expected {expected}")


@dataclass(slots=True, frozen=True)
class WriteResult:
    key: str
    value: float | int
    words: tuple[int, ...]
    previous: float | int | None
    changed: bool  # False: the register already held the value (no write)
    verified: bool


@dataclass(slots=True)
class WriteRecord:
    """One entry of the write log (diagnostics)."""

    at: float  # wall clock
    key: str
    value: float | int | None
    outcome: str  # written / unchanged / refused:<reason> / failed / mismatch
    detail: str = ""
    priority: bool = False


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def to_raw(target: WriteTarget, value: float | int) -> int:
    """Engineering value -> raw integer (enum codes pass through)."""
    if target.allowed is not None:
        return int(value)
    return round(float(value) / target.scale)


def from_raw(target: WriteTarget, raw: int) -> float | int:
    if target.allowed is not None or target.scale == 1:
        return raw
    return round(raw * target.scale, 6)


def words_for(target: WriteTarget, raw: int) -> list[int]:
    """Raw integer -> wire words (32-bit values low word first)."""
    if target.dtype in (DataType.S16, DataType.S32):
        bits = 16 * target.words
        if not -(1 << (bits - 1)) <= raw < (1 << (bits - 1)):
            raise ValueError(f"{target.key}: {raw} does not fit {target.dtype}")
        raw &= (1 << bits) - 1
    elif not 0 <= raw < (1 << (16 * target.words)):
        raise ValueError(f"{target.key}: {raw} does not fit {target.dtype}")
    if target.words == 1:
        return [raw]
    return [raw & 0xFFFF, (raw >> 16) & 0xFFFF]


def raw_from_words(target: WriteTarget, words: list[int]) -> int:
    raw = words[0] & 0xFFFF
    if target.words == 2:
        raw |= (words[1] & 0xFFFF) << 16
    if target.dtype is DataType.S16 and raw & 0x8000:
        raw -= 0x10000
    if target.dtype is DataType.S32 and raw & 0x8000_0000:
        raw -= 0x1_0000_0000
    return raw


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Endpoint:
    """Hub and unit ID serving one write device."""

    hub: _Hub
    unit: int


@dataclass(slots=True)
class _TargetState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_write: float | None = None  # monotonic
    history: deque[float] = field(default_factory=deque)  # monotonic, 24 h


class SafeWriter:
    """Guarded writes for one config entry."""

    def __init__(
        self,
        endpoints: Mapping[WriteDevice, Endpoint],
        *,
        profile: Callable[[], ModelProfile],
        healthy: Callable[[WriteDevice], bool] = lambda _d: True,
        allow_unverified: bool = False,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._endpoints = dict(endpoints)
        self._profile = profile
        self._healthy = healthy
        self.allow_unverified = allow_unverified
        self._sleep = sleep
        self._clock = clock
        self._wall = wall_clock
        self._state: dict[str, _TargetState] = {}
        self.log: deque[WriteRecord] = deque(maxlen=LOG_SIZE)
        #: Called after every verified write: (target, engineering value, raw).
        self.on_written: Callable[[WriteTarget, float | int, int], None] | None = None

    # -- public ------------------------------------------------------------

    def has_device(self, device: WriteDevice) -> bool:
        return device in self._endpoints

    def check(
        self,
        key: str,
        value: float | int | None = None,
        *,
        bounds: tuple[float | None, float | None] | None = None,
    ) -> WriteTarget:
        """Run checks 1-5 without touching the device. Raises WriteRefused."""
        target = WRITE_TARGETS.get(key)
        if target is None:
            raise WriteRefused(key, "not_allowlisted", "not on the write allowlist")
        if target.device not in self._endpoints:
            raise WriteRefused(key, "no_device", f"no {target.device} configured")
        if target.device is WriteDevice.INVERTER:
            profile = self._profile()
            if not profile.known:
                raise WriteRefused(
                    key,
                    "unknown_model",
                    f"model {profile.model} is not in the protocol's model table",
                )
            if key not in profile.write_targets:
                raise WriteRefused(
                    key, "unsupported_model", f"not documented for the {profile.model}"
                )
        if target.evidence is Evidence.UNVERIFIED and not self.allow_unverified:
            raise WriteRefused(
                key,
                "unverified",
                "unverified register; turn on 'Allow unverified writes'",
            )
        if value is not None:
            self._validate(target, value, bounds)
        return target

    async def async_read(self, key: str) -> float | int:
        """Current value of a write target (FC03), in engineering units."""
        target = WRITE_TARGETS.get(key)
        if target is None:
            raise WriteRefused(key, "not_allowlisted", "not on the write allowlist")
        endpoint = self._endpoints.get(target.device)
        if endpoint is None:
            raise WriteRefused(key, "no_device", f"no {target.device} configured")
        words = await endpoint.hub.async_read_raw(
            Table.HOLDING, target.address, target.words, device_id=endpoint.unit
        )
        return from_raw(target, raw_from_words(target, words))

    async def async_write(
        self,
        key: str,
        value: float | int,
        *,
        bounds: tuple[float | None, float | None] | None = None,
        priority: bool = False,
        restore: bool = False,
    ) -> WriteResult:
        """Write one target with every guardrail. See the module docstring."""
        priority = priority or restore
        try:
            target = self.check(key, None if restore else value, bounds=bounds)
        except WriteRefused as err:
            self._log(key, value, f"refused:{err.reason}", str(err), priority)
            raise
        endpoint = self._endpoints[target.device]
        raw = to_raw(target, value)
        try:
            words = words_for(target, raw)
        except ValueError as err:
            self._log(key, value, "refused:invalid_value", str(err), priority)
            raise WriteRefused(key, "invalid_value", str(err)) from err
        clean = from_raw(target, raw)
        state = self._state.setdefault(key, _TargetState())

        async with state.lock:
            if not priority and not self._healthy(target.device):
                err = WriteRefused(
                    key, "device_unhealthy", f"{target.device} is not answering polls"
                )
                self._log(key, clean, "refused:device_unhealthy", str(err), priority)
                raise err

            try:
                current = await endpoint.hub.async_read_raw(
                    Table.HOLDING, target.address, target.words, device_id=endpoint.unit
                )
            except SungrowModbusError as err:
                self._log(key, clean, "failed", f"pre-read: {err}", priority)
                raise WriteFailed(
                    key, f"could not read the current value: {err}"
                ) from err
            previous = from_raw(target, raw_from_words(target, current))
            if [w & 0xFFFF for w in current] == words:
                self._log(key, clean, "unchanged", "", priority)
                return WriteResult(key, clean, tuple(words), previous, False, True)

            now = self._clock()
            self._prune(state, now)
            if not priority:
                if (
                    state.last_write is not None
                    and now - state.last_write < target.cooldown
                ):
                    wait = target.cooldown - (now - state.last_write)
                    err = WriteRefused(
                        key,
                        "cooldown",
                        f"written {now - state.last_write:.0f} s ago; wait {wait:.0f} s",
                        retry_in=wait,
                    )
                    self._log(key, clean, "refused:cooldown", str(err), priority)
                    raise err
                if len(state.history) >= target.daily_budget:
                    wait = state.history[0] + DAY - now
                    err = WriteRefused(
                        key,
                        "daily_budget",
                        f"{target.daily_budget} writes in 24 h used (EEPROM guard)",
                        retry_in=wait,
                    )
                    self._log(key, clean, "refused:daily_budget", str(err), priority)
                    raise err

            state.last_write = now
            state.history.append(now)
            write_error: SungrowModbusError | None = None
            try:
                await endpoint.hub.async_write_holding(
                    target.address, words, device_id=endpoint.unit
                )
            except SungrowConnectionError as err:
                # May or may not have been applied: the read-back decides.
                write_error = err
            except SungrowModbusError as err:
                self._log(key, clean, "failed", str(err), priority)
                raise WriteFailed(key, str(err)) from err

            actual = await self._read_back(target, endpoint, words)
            if actual != words:
                if write_error is not None:
                    self._log(key, clean, "failed", str(write_error), priority)
                    raise WriteFailed(key, str(write_error)) from write_error
                self._log(key, clean, "mismatch", f"read back {actual}", priority)
                raise ReadbackMismatch(key, words, actual)

        self._log(key, clean, "written", f"was {previous}", priority)
        _LOGGER.info(
            "Wrote %s = %s (was %s)%s",
            key,
            clean,
            previous,
            " [restore]" if priority else "",
        )
        if self.on_written is not None:
            try:
                self.on_written(target, clean, raw)
            except Exception:  # never let a listener undo a verified write
                _LOGGER.exception("on_written listener failed for %s", key)
        return WriteResult(key, clean, tuple(words), previous, True, True)

    def diagnostics(self) -> dict[str, Any]:
        now = self._clock()
        return {
            "allow_unverified": self.allow_unverified,
            "devices": sorted(str(d) for d in self._endpoints),
            "targets": {
                key: {
                    "last_write_s_ago": round(now - st.last_write, 1)
                    if st.last_write is not None
                    else None,
                    "writes_24h": sum(1 for t in st.history if now - t < DAY),
                }
                for key, st in self._state.items()
            },
            "log": [asdict(r) for r in self.log],
        }

    # -- internals -----------------------------------------------------------

    def _validate(
        self,
        target: WriteTarget,
        value: float | int,
        bounds: tuple[float | None, float | None] | None,
    ) -> None:
        key = target.key
        if target.allowed is not None:
            code = int(value)
            if code != value:
                raise WriteRefused(key, "invalid_value", f"{value} is not a code")
            if code in target.allowed:
                return
            if code in target.allowed_unverified:
                if self.allow_unverified:
                    return
                raise WriteRefused(
                    key,
                    "unverified",
                    f"0x{code:02X} is unverified; turn on 'Allow unverified writes'",
                )
            allowed = ", ".join(f"0x{c:02X}" for c in sorted(target.allowed))
            raise WriteRefused(
                key, "invalid_value", f"0x{code:02X} not allowed ({allowed})"
            )
        number = float(value)
        if not math.isfinite(number):
            raise WriteRefused(key, "invalid_value", f"{value} is not a number")
        low = target.min_value
        high = target.max_value
        if bounds is not None:
            if bounds[0] is not None:
                low = bounds[0] if low is None else max(low, bounds[0])
            if bounds[1] is not None:
                high = bounds[1] if high is None else min(high, bounds[1])
        if low is not None and number < low - 1e-9:
            raise WriteRefused(
                key, "out_of_range", f"{value} is below the minimum {low}"
            )
        if high is not None and number > high + 1e-9:
            raise WriteRefused(
                key, "out_of_range", f"{value} is above the maximum {high}"
            )

    async def _read_back(
        self, target: WriteTarget, endpoint: Endpoint, words: list[int]
    ) -> list[int]:
        actual: list[int] = []
        for attempt in range(2):
            await self._sleep(target.readback_delay * (attempt + 1))
            try:
                actual = [
                    w & 0xFFFF
                    for w in await endpoint.hub.async_read_raw(
                        Table.HOLDING,
                        target.address,
                        target.words,
                        device_id=endpoint.unit,
                    )
                ]
            except SungrowModbusError as err:
                _LOGGER.debug("%s: read-back failed: %s", target.key, err)
                continue
            if actual == words:
                return actual
        return actual

    def _prune(self, state: _TargetState, now: float) -> None:
        while state.history and now - state.history[0] >= DAY:
            state.history.popleft()

    def _log(
        self,
        key: str,
        value: float | int | None,
        outcome: str,
        detail: str,
        priority: bool,
    ) -> None:
        self.log.append(
            WriteRecord(self._wall(), key, value, outcome, detail, priority)
        )
