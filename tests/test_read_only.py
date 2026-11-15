"""The integration is read-only by design. These tests keep it that way."""

from __future__ import annotations

from pathlib import Path
import re

from homeassistant.const import Platform

from custom_components.sungrow_ems import modbus, registers
from custom_components.sungrow_ems.const import PLATFORMS
from custom_components.sungrow_ems.modbus import SungrowModbusHub

PACKAGE = Path(modbus.__file__).parent

#: pymodbus write calls and Modbus write function codes must never appear.
FORBIDDEN = re.compile(
    r"write_register|write_registers|write_coil|write_coils|mask_write"
    r"|readwrite_registers|async_write_(?!ha_state)|encode_value|encode_block"
)


def test_no_control_platforms_or_services() -> None:
    assert set(PLATFORMS) == {Platform.SENSOR, Platform.BINARY_SENSOR}
    for name in ("select.py", "number.py", "switch.py", "button.py", "services.yaml"):
        assert not (PACKAGE / name).exists(), f"{name} must not exist"


def test_source_contains_no_write_calls() -> None:
    for path in PACKAGE.rglob("*.py"):
        hits = FORBIDDEN.findall(path.read_text(encoding="utf-8"))
        assert not hits, f"{path.name} contains write code: {sorted(set(hits))}"


def test_hub_only_knows_read_functions() -> None:
    assert set(modbus._FUNCTION_READ.values()) == {
        "read_input_registers",
        "read_holding_registers",
    }
    public = [name for name in dir(SungrowModbusHub) if "write" in name.lower()]
    assert public == []


def test_register_map_has_no_write_metadata() -> None:
    assert not hasattr(registers.Register, "writable")
    assert "writable" not in registers.Register.__dataclass_fields__
    assert not hasattr(registers, "encode_value")
    assert not hasattr(registers, "encode_block")
