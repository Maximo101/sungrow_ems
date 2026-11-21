"""Synthetic snapshots in the ``dump_registers.py`` format, for tests.

No real site data: serial TEST0000001, hosts *.test. Values are plausible
for an SH10RS (or SH20T) behind an iHM with a 38.6 kWh battery.
"""

from __future__ import annotations

from typing import Any

from custom_components.sungrow_ems.registers import BLOCKS, Device

SERIAL = "TEST0000001"


def _put_string(words: dict[int, int], start: int, length: int, text: str) -> None:
    data = text.encode().ljust(length * 2, b"\x00")
    for i in range(length):
        words[start + i] = int.from_bytes(data[2 * i : 2 * i + 2], "big")


def _put_u32(words: dict[int, int], address: int, value: int) -> None:
    value &= 0xFFFFFFFF
    words[address] = value & 0xFFFF
    words[address + 1] = value >> 16


def synthetic_snapshot(
    *,
    device_type_code: int = 0xD1B,
    soc_percent: float = 40.0,
    reserve: int = 6,
    with_ihm: bool = True,
    with_ac22: bool = False,
) -> dict[str, Any]:
    """Build a snapshot dict. ``device_type_code`` 0xE26 gives an SH20T."""
    three_phase = device_type_code >= 0xE00
    inv: dict[str, dict[int, int]] = {"input": {}, "holding": {}}
    ihm: dict[str, dict[int, int]] = {"input": {}, "holding": {}}
    for block in BLOCKS:
        store = inv if block.device is Device.INVERTER else ihm
        for address in range(block.start, block.end + 1):
            store[str(block.table)][address] = 0
    # Ranges dump_registers.py reads beyond the integration's blocks.
    for address in range(33208, 33220):
        inv["holding"][address] = 0
    for address in range(13250, 13295):
        inv["input"][address] = 0

    i, h = inv["input"], inv["holding"]
    _put_string(i, 4990, 10, SERIAL)
    i[5000] = device_type_code
    i[5001] = 200 if three_phase else 100
    i[5002] = 1 if three_phase else 0
    i[13000] = 0x4000  # External EMS (the iHM drives the inverter)
    i[13001] = 0b1001
    i[13023] = round(soc_percent * 10)
    i[13024] = 980
    i[13039] = 386
    _put_u32(i, 5214, 0)
    i[5628] = 100  # BDC rated 10 kW
    h[13050] = 3
    h[13051] = 0xCC
    h[13058] = 1000
    h[13059] = 50
    h[13075] = 0xAA
    h[13100] = reserve
    h[33047] = 1000
    h[33048] = 1000
    # Forced-charging schedule: on, every day, 11:00-14:00 -> 100 %.
    h[33208], h[33209] = 0xAA, 1
    h[33210], h[33211], h[33212], h[33213], h[33214] = 11, 0, 14, 0, 100
    _put_string(i, 13250, 15, "TEST-ARM.V001")

    targets: list[dict[str, Any]] = [
        {
            "name": "inverter",
            "host": "inverter.test",
            "port": 502,
            "unit": 1,
            "identity": {"device_type_code": device_type_code, "serial": SERIAL},
            "tables": {t: {str(a): w for a, w in v.items()} for t, v in inv.items()},
            # SH20T has three MPPTs: MPPT4 is refused. SH10RS: wide power reg.
            "rejected": {
                "input": [5115, 5116] if three_phase else [],
                "holding": [33148] if not three_phase else [],
            },
        }
    ]
    if with_ihm:
        ii, ih = ihm["input"], ihm["holding"]
        ii[8000] = 0x072A
        _put_u32(ii, 8147, 386)
        _put_u32(ii, 8149, 100)
        ii[8151] = 100
        ii[8153] = 100
        ii[8163] = round(soc_percent * 10)
        ii[8552] = 2
        ii[8554] = 1 if three_phase else 0
        ih[8024] = 1
        ih[8025] = 0xAA
        _put_u32(ih, 8026, 0)
        ih[8028] = 0x55
        ih[8048] = 0xA7
        ih[8049] = 0x55
        ih[8050] = 0x55
        targets.append(
            {
                "name": "ihm",
                "host": "ihm.test",
                "port": 503,
                "unit": 247,
                "identity": {"device_type_code": 0x072A},
                "tables": {
                    t: {str(a): w for a, w in v.items()} for t, v in ihm.items()
                },
                "rejected": {"input": [], "holding": []},
            }
        )
    if with_ac22:
        ac_in = {a: 0 for a in range(21300, 21323)}
        ac_in.update({a: 0 for a in range(21262, 21274)})
        ac_in[21263] = 320  # rated 32.0 A
        ac_in[21262] = 230
        ac_hold = {a: 0 for a in range(21200, 21214)}
        ac_hold[21203] = 160  # 16.0 A
        targets.append(
            {
                "name": "ac22",
                "host": "inverter.test",
                "port": 502,
                "unit": 3,
                "identity": {},
                "tables": {
                    "input": {str(a): w for a, w in ac_in.items()},
                    "holding": {str(a): w for a, w in ac_hold.items()},
                },
                "rejected": {"input": [], "holding": []},
            }
        )
    return {
        "schema": "sungrow_ems.snapshot/1",
        "created": "2026-11-20T00:00:00+11:00",
        "tool": "tests/snapshot_factory.py",
        "targets": targets,
        "rows": [],
    }
