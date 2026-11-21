"""SafeWriter guardrails, against an in-memory hub (no Home Assistant)."""

from __future__ import annotations

import pytest

from custom_components.sungrow_ems.modbus import (
    SungrowExceptionResponse,
    SungrowTimeoutError,
)
from custom_components.sungrow_ems.profiles import (
    WRITE_TARGETS,
    ModelFamily,
    WriteDevice,
    profile_for,
)
from custom_components.sungrow_ems.registers import Table
from custom_components.sungrow_ems.writer import (
    Endpoint,
    ReadbackMismatch,
    SafeWriter,
    WriteFailed,
    WriteRefused,
    words_for,
)


class MemHub:
    name = "mem"

    def __init__(self) -> None:
        self.holding: dict[tuple[int, int], int] = {}
        self.writes: list[tuple[int, list[int], int]] = []
        self.ignore: set[int] = set()
        self.reject: int | None = None
        self.timeout_but_apply = False

    async def async_read_raw(self, table, address, count, *, device_id=None):
        assert table is Table.HOLDING
        return [self.holding.get((device_id, address + i), 0) for i in range(count)]

    async def async_write_holding(self, address, values, *, device_id=None):
        self.writes.append((address, list(values), device_id))
        if self.reject is not None:
            raise SungrowExceptionResponse("write_register", address, self.reject)
        for i, v in enumerate(values):
            if address + i not in self.ignore:
                self.holding[(device_id, address + i)] = v
        if self.timeout_but_apply:
            raise SungrowTimeoutError("timed out after applying")


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def setup():
    inv, ihm = MemHub(), MemHub()
    clock = Clock()
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    healthy = {WriteDevice.INVERTER: True, WriteDevice.IHM: True}
    profile = {"p": profile_for(0xD1B)}
    writer = SafeWriter(
        {
            WriteDevice.INVERTER: Endpoint(inv, 1),
            WriteDevice.IHM: Endpoint(ihm, 247),
            WriteDevice.AC22: Endpoint(inv, 3),
        },
        profile=lambda: profile["p"],
        healthy=lambda d: healthy.get(d, True),
        sleep=sleep,
        clock=clock,
    )
    return writer, inv, ihm, clock, sleeps, healthy, profile


async def test_write_verifies_and_skips_unchanged(setup) -> None:
    writer, inv, _ihm, clock, sleeps, *_ = setup
    inv.holding[(1, 13100)] = 6
    seen = []
    writer.on_written = lambda t, v, r: seen.append((t.key, v, r))

    result = await writer.async_write("backup_reserve_soc", 10)
    assert result.changed and result.verified and result.previous == 6
    assert inv.writes == [(13100, [10], 1)]
    assert sleeps == [2.0]  # read-back after the delay
    assert seen == [("backup_reserve_soc", 10, 10)]

    # Same value again: no write, no cooldown hit.
    clock.t += 1
    result = await writer.async_write("backup_reserve_soc", 10)
    assert not result.changed
    assert len(inv.writes) == 1


async def test_allowlist_bounds_and_codes(setup) -> None:
    writer, inv, *_ = setup
    with pytest.raises(WriteRefused) as err:
        await writer.async_write("mb_max_soc", 90)
    assert err.value.reason == "not_allowlisted"
    with pytest.raises(WriteRefused) as err:
        await writer.async_write("backup_reserve_soc", 101)
    assert err.value.reason == "out_of_range"
    # Dynamic bounds (Min SoC .. 100) tighten the static ones.
    with pytest.raises(WriteRefused) as err:
        await writer.async_write("backup_reserve_soc", 3, bounds=(5, 100))
    assert err.value.reason == "out_of_range"
    with pytest.raises(WriteRefused) as err:
        await writer.async_write("ihm_ems_mode", 4)  # VPP needs a heartbeat
    assert err.value.reason == "invalid_value"
    with pytest.raises(WriteRefused) as err:
        await writer.async_write("ihm_charger_mode", 0xA7)
    assert err.value.reason == "unverified"
    with pytest.raises(WriteRefused) as err:
        await writer.async_write("ac22_charge_current", 16)
    assert err.value.reason == "unverified"
    assert inv.writes == []

    writer.allow_unverified = True
    await writer.async_write("ac22_charge_current", 16.04)
    assert inv.writes == [(21203, [160], 3)]  # 0.1 A resolution, unit 3


async def test_u32_low_word_first_fc16(setup) -> None:
    writer, _inv, ihm, *_ = setup
    await writer.async_write("ihm_charge_power", 10.0, bounds=(0, 10.0))
    assert ihm.writes == [(8026, [100, 0], 247)]
    assert words_for(WRITE_TARGETS["ihm_charge_power"], 70000) == [70000 & 0xFFFF, 1]
    with pytest.raises(WriteRefused):
        await writer.async_write("ihm_charge_power", 10.1, bounds=(0, 10.0))


async def test_cooldown_budget_and_priority(setup) -> None:
    writer, _inv, _ihm, clock, *_ = setup
    await writer.async_write("backup_reserve_soc", 10)
    clock.t += 5
    with pytest.raises(WriteRefused) as err:
        await writer.async_write("backup_reserve_soc", 11)
    assert err.value.reason == "cooldown"
    assert err.value.retry_in == pytest.approx(25)
    # A restore is never blocked by the cooldown.
    await writer.async_write("backup_reserve_soc", 6, priority=True)

    target = WRITE_TARGETS["backup_reserve_soc"]
    for i in range(target.daily_budget - 2):
        clock.t += target.cooldown
        await writer.async_write("backup_reserve_soc", 20 + i % 2)
    clock.t += target.cooldown
    with pytest.raises(WriteRefused) as err:
        await writer.async_write("backup_reserve_soc", 50)
    assert err.value.reason == "daily_budget"
    clock.t += 86400
    await writer.async_write("backup_reserve_soc", 50)


async def test_readback_mismatch_and_device_errors(setup) -> None:
    writer, inv, _ihm, clock, sleeps, healthy, _p = setup
    inv.ignore.add(13100)
    with pytest.raises(ReadbackMismatch):
        await writer.async_write("backup_reserve_soc", 10)
    assert sleeps == [2.0, 4.0]  # checked twice
    inv.ignore.clear()

    clock.t += 60
    inv.reject = 0x02
    with pytest.raises(WriteFailed):
        await writer.async_write("backup_reserve_soc", 10)
    inv.reject = None

    # A timeout after the device applied the write: the read-back confirms it.
    clock.t += 60
    inv.timeout_but_apply = True
    result = await writer.async_write("backup_reserve_soc", 12)
    assert result.verified
    inv.timeout_but_apply = False

    healthy[WriteDevice.INVERTER] = False
    clock.t += 60
    with pytest.raises(WriteRefused) as err:
        await writer.async_write("backup_reserve_soc", 13)
    assert err.value.reason == "device_unhealthy"
    assert any(r.outcome == "mismatch" for r in writer.log)


async def test_model_gate(setup) -> None:
    writer, _inv, _ihm, _c, _s, _h, profile = setup
    profile["p"] = profile_for(0xE26)
    assert profile["p"].family is ModelFamily.SH_T
    assert profile["p"].three_phase
    assert "inv_mppt4" in profile["p"].skip_blocks
    await writer.async_write("backup_reserve_soc", 10)  # documented for SH-T
    profile["p"] = profile_for(0x1234)
    with pytest.raises(WriteRefused) as err:
        await writer.async_write("backup_reserve_soc", 11)
    assert err.value.reason == "unknown_model"
    # iHM targets do not depend on the inverter model.
    await writer.async_write("ihm_ems_mode", 5)


def test_profiles() -> None:
    rs = profile_for(0xD1B)
    assert rs.family is ModelFamily.SH_RS and rs.phases == 1 and rs.mppt_count == 4
    assert not rs.skip_blocks
    t = profile_for(0xE26)
    assert t.model == "SH20T" and t.mppt_count == 3
    assert "mb_battery_capacity_13039" in t.unsupported_keys
    assert profile_for(None).family is ModelFamily.UNKNOWN
