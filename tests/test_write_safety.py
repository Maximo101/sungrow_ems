"""Write safety. These tests replace the old read-only tests (0.1.x).

Control was added deliberately in 0.2.0, starting here. The rules:

  * Only ``modbus.py`` may contain pymodbus write calls, and only the two
    holding-register writes (FC06 / FC16).
  * Only ``writer.py`` (SafeWriter) may call the hub's write method.
  * Everything that can be written is in ``profiles.WRITE_TARGETS``. The
    allowlist is pinned here: adding a target means changing this test.
  * ``registers.py`` stays a pure read map without write metadata.
"""

from __future__ import annotations

from pathlib import Path
import re

from custom_components.sungrow_ems import modbus, registers
from custom_components.sungrow_ems.modbus import SungrowModbusHub
from custom_components.sungrow_ems.profiles import WRITE_TARGETS, WriteDevice

PACKAGE = Path(modbus.__file__).parent

#: Never anywhere: coils, mask writes, read/write combos, input-table writes.
FORBIDDEN_EVERYWHERE = re.compile(
    r"write_coil|write_coils|mask_write|readwrite_registers|encode_value|encode_block"
)
PYMODBUS_WRITES = re.compile(r"\bwrite_registers?\b")
HUB_WRITE = re.compile(r"\basync_write_holding\b")

#: The allowlist, pinned: key -> (device, documented holding address, words).
EXPECTED_TARGETS = {
    "backup_reserve_soc": (WriteDevice.INVERTER, 13100, 1),
    "inverter_ems_mode": (WriteDevice.INVERTER, 13050, 1),
    "inverter_charge_command": (WriteDevice.INVERTER, 13051, 1),
    "inverter_charge_power": (WriteDevice.INVERTER, 13052, 1),
    "ihm_ems_mode": (WriteDevice.IHM, 8024, 1),
    "ihm_charge_command": (WriteDevice.IHM, 8025, 1),
    "ihm_charge_power": (WriteDevice.IHM, 8026, 2),
    "ihm_charger_mode": (WriteDevice.IHM, 8048, 1),
    "ac22_charge_current": (WriteDevice.AC22, 21203, 1),
}


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in PACKAGE.rglob("*.py")}


def test_no_forbidden_write_calls() -> None:
    for name, text in _sources().items():
        hits = FORBIDDEN_EVERYWHERE.findall(text)
        assert not hits, f"{name} contains {sorted(set(hits))}"


def test_pymodbus_writes_only_in_modbus_py() -> None:
    for name, text in _sources().items():
        if name == "modbus.py":
            continue
        assert not PYMODBUS_WRITES.findall(text), f"{name} calls pymodbus writes"


def test_hub_write_only_called_by_writer() -> None:
    for name, text in _sources().items():
        if name in ("modbus.py", "writer.py"):
            continue
        assert not HUB_WRITE.findall(text), f"{name} bypasses SafeWriter"


def test_hub_functions() -> None:
    assert set(modbus._FUNCTION_READ.values()) == {
        "read_input_registers",
        "read_holding_registers",
    }
    assert modbus._FUNCTION_WRITE_SINGLE == "write_register"  # FC06
    assert modbus._FUNCTION_WRITE_MULTI == "write_registers"  # FC16
    assert modbus.MAX_WRITE_WORDS == 2
    public = [
        n
        for n in dir(SungrowModbusHub)
        if "write" in n.lower() and not n.startswith("_")
    ]
    assert public == ["async_write_holding"]


def test_allowlist_is_pinned() -> None:
    actual = {key: (t.device, t.address, t.words) for key, t in WRITE_TARGETS.items()}
    assert actual == EXPECTED_TARGETS
    for target in WRITE_TARGETS.values():
        assert target.table is registers.Table.HOLDING
        if target.read_key is not None:
            reg = registers.REGISTERS_BY_KEY[target.read_key]
            assert reg.table is registers.Table.HOLDING
            assert reg.address == target.address
            assert reg.count == target.words


def test_register_map_has_no_write_metadata() -> None:
    assert "writable" not in registers.Register.__dataclass_fields__
    assert not hasattr(registers, "encode_value")
