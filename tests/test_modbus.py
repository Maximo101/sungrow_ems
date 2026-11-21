"""SungrowModbusHub against the fake device (no Home Assistant needed)."""

from __future__ import annotations

import time

import pytest

from custom_components.sungrow_ems.modbus import (
    SungrowConnectionError,
    SungrowModbusHub,
    split_span,
)
from custom_components.sungrow_ems.registers import (
    BLOCKS_BY_NAME,
    REGISTERS_BY_KEY,
    Device,
    Table,
    blocks_for,
)

from .conftest import INVERTER_PORT, SERIAL, FakeDevice


def _hub(**kw: object) -> SungrowModbusHub:
    return SungrowModbusHub(
        host="inverter.test", port=INVERTER_PORT, device_id=1, name="test", **kw
    )  # type: ignore[arg-type]


async def test_read_blocks_uses_one_request_per_block(inverter: FakeDevice) -> None:
    hub = _hub()
    blocks = blocks_for(Device.INVERTER)
    result = await hub.async_read_blocks(blocks)
    assert result.complete
    assert result.requests == len(blocks) == len(inverter.calls)
    assert inverter.connects == 1
    decoded = result.decode(blocks)
    assert decoded.values["mb_serial_number"] == SERIAL
    assert decoded.values["mb_battery_soc"] == 65.5
    await hub.async_close()
    assert inverter.open_sockets == 0


async def test_only_read_function_codes_are_sent(inverter: FakeDevice) -> None:
    hub = _hub()
    await hub.async_read_blocks(blocks_for(Device.INVERTER))
    assert inverter.functions() == {"read_input_registers", "read_holding_registers"}
    assert {c[3] for c in inverter.calls} == {1}  # unit id
    await hub.async_close()


async def test_socket_is_reused_between_polls(inverter: FakeDevice) -> None:
    hub = _hub()
    block = [BLOCKS_BY_NAME["inv_system"]]
    for _ in range(3):
        await hub.async_read_blocks(block)
    assert inverter.connects == 1
    await hub.async_close()


async def test_rejected_gap_is_split_and_plan_is_remembered(
    inverter: FakeDevice,
) -> None:
    block = BLOCKS_BY_NAME["inv_battery_power"]
    documented = {a for r in block.registers for a in range(r.address, r.end + 1)}
    gap = next(a for a in range(block.start, block.end) if a not in documented)
    inverter.rejected.add(gap)
    hub = _hub()

    first = await hub.async_read_blocks([block])
    assert first.complete
    assert first.requests > 1
    learned = len(inverter.calls)

    inverter.calls.clear()
    second = await hub.async_read_blocks([block])
    assert second.requests == len(inverter.calls) < learned  # no re-probing
    decoded = second.decode([block])
    assert len(decoded.values) + len(decoded.invalid) == len(block.registers)
    await hub.async_close()


async def test_unsupported_register_is_backed_off(inverter: FakeDevice) -> None:
    block = BLOCKS_BY_NAME["inv_mppt4"]
    inverter.rejected.update(range(block.start, block.end + 1))
    hub = _hub()
    result = await hub.async_read_blocks([block, BLOCKS_BY_NAME["inv_system"]])
    assert result.unsupported_spans
    inverter.calls.clear()
    await hub.async_read_blocks([block])
    assert inverter.calls == []  # not asked again until the hourly retry
    await hub.async_close()


async def test_busy_reply_is_retried(inverter: FakeDevice) -> None:
    inverter.busy = 1
    hub = _hub(retries=1)
    result = await hub.async_read_blocks([BLOCKS_BY_NAME["inv_system"]])
    assert result.complete
    assert len(inverter.calls) == 2
    await hub.async_close()


async def test_timeouts_drop_the_socket_and_abort_the_cycle(
    inverter: FakeDevice,
) -> None:
    inverter.timeouts = 2
    hub = _hub(retries=1)
    with pytest.raises(SungrowConnectionError):
        await hub.async_read_blocks(blocks_for(Device.INVERTER))
    assert len(inverter.calls) == 2  # later blocks were skipped, not hammered
    assert inverter.open_sockets == 0
    assert hub.stats.timeouts == 2

    result = await hub.async_read_blocks([BLOCKS_BY_NAME["inv_system"]])
    assert result.complete
    assert inverter.connects == 2
    await hub.async_close()


async def test_partial_cycle_keeps_what_was_read(inverter: FakeDevice) -> None:
    inverter.timeout_from = 2  # third request onwards gets no answer
    hub = _hub(retries=0)
    blocks = blocks_for(Device.INVERTER)
    result = await hub.async_read_blocks(blocks)
    assert result.blocks_ok == [b.name for b in blocks[:2]]
    assert list(result.blocks_failed) == [blocks[2].name]
    assert result.blocks_skipped == [b.name for b in blocks[3:]]
    assert not result.complete
    assert "mb_serial_number" in result.decode(blocks).values
    await hub.async_close()


async def test_connect_failure_backs_off(inverter: FakeDevice) -> None:
    inverter.offline = True
    hub = _hub(retries=0)
    with pytest.raises(SungrowConnectionError, match="cannot connect"):
        await hub.async_read_blocks([BLOCKS_BY_NAME["inv_system"]])
    with pytest.raises(SungrowConnectionError, match="waiting"):
        await hub.async_read_blocks([BLOCKS_BY_NAME["inv_system"]])
    assert hub.stats.connect_failures == 1
    inverter.offline = False
    await hub.async_connect()  # ignores the backoff
    assert hub.connected
    await hub.async_close()


async def test_backoff_notices_do_not_nest_in_last_error(inverter: FakeDevice) -> None:
    inverter.offline = True
    hub = _hub(retries=0)
    with pytest.raises(SungrowConnectionError, match="cannot connect"):
        await hub.async_read_blocks([BLOCKS_BY_NAME["inv_system"]])
    for _ in range(3):
        with pytest.raises(SungrowConnectionError, match="waiting"):
            await hub.async_read_blocks([BLOCKS_BY_NAME["inv_system"]])
    error = hub.stats.last_error
    assert error is not None
    assert "cannot connect" in error
    assert "waiting" not in error
    assert hub.stats.last_error_at is not None
    await hub.async_close()


async def test_rejection_of_a_span_read_before_is_temporary(
    inverter: FakeDevice,
) -> None:
    """A whole table rejected for a moment (e.g. a second client on the
    WiNet-S) fails the block for that cycle; it is not marked unsupported."""
    block = BLOCKS_BY_NAME["inv_mppt4"]
    hub = _hub()
    assert (await hub.async_read_blocks([block])).complete

    inverter.rejected.update(range(block.start, block.end + 1))
    result = await hub.async_read_blocks([block, BLOCKS_BY_NAME["inv_system"]])
    assert block.name in result.blocks_failed
    assert "inv_system" in result.blocks_ok
    assert not result.unsupported_spans
    assert hub.stats.transient_rejections == 1

    inverter.rejected.clear()
    inverter.calls.clear()
    again = await hub.async_read_blocks([block])
    assert again.complete
    assert len(inverter.calls) == 1  # full block again, no learned split
    await hub.async_close()


async def test_span_rejected_many_times_becomes_unsupported(
    inverter: FakeDevice,
) -> None:
    block = BLOCKS_BY_NAME["inv_mppt4"]
    hub = _hub()
    await hub.async_read_blocks([block])
    inverter.rejected.update(range(block.start, block.end + 1))
    results = [
        await hub.async_read_blocks([block, BLOCKS_BY_NAME["inv_system"]])
        for _ in range(10)
    ]
    assert all(block.name in r.blocks_failed for r in results[:9])
    assert results[9].unsupported_spans
    await hub.async_close()


async def test_pacing_between_requests(inverter: FakeDevice) -> None:
    hub = _hub(min_request_interval=0.05)
    started = time.monotonic()
    await hub.async_read_raw(Table.INPUT, 13000, 1)
    await hub.async_read_raw(Table.INPUT, 13000, 1)
    assert time.monotonic() - started >= 0.1  # connect + first request are paced too
    await hub.async_close()


async def test_closed_hub_refuses_requests(inverter: FakeDevice) -> None:
    hub = _hub()
    await hub.async_read_raw(Table.INPUT, 13000, 1)
    await hub.async_close()
    assert inverter.open_sockets == 0
    with pytest.raises(SungrowConnectionError, match="closed"):
        await hub.async_read_raw(Table.INPUT, 13000, 1)
    assert inverter.connects == 1


async def test_read_registers_ad_hoc(inverter: FakeDevice) -> None:
    hub = _hub()
    decoded = await hub.async_read_registers(
        [REGISTERS_BY_KEY["mb_serial_number"], REGISTERS_BY_KEY["mb_device_type_code"]]
    )
    assert decoded.values["mb_serial_number"] == SERIAL
    assert decoded.raw["mb_device_type_code"] == 0xD1B
    await hub.async_close()


def test_split_span_prefers_gap_free_runs() -> None:
    block = BLOCKS_BY_NAME["ihm_settings"]
    parts = split_span(block, block.start, block.count)
    assert len(parts) > 1
    assert parts[0][0] == block.start
    single = block.registers[0]
    assert split_span(block, single.address, single.count) == []


def test_diagnostics_counts_writes_from_zero() -> None:
    """Writes are counted separately so diagnostics show any control traffic."""
    stats = _hub().diagnostics()["stats"]
    assert stats["writes"] == 0
