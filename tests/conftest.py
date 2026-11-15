"""Shared fixtures: an in-memory Sungrow device behind a fake pymodbus client."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from typing import Any, ClassVar
from unittest.mock import patch

from pymodbus.exceptions import ModbusIOException
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sungrow_ems.const import (
    CONF_IHM_DEVICE_ID,
    CONF_IHM_ENABLED,
    CONF_IHM_HOST,
    CONF_IHM_PORT,
    CONF_INVERTER_DEVICE_ID,
    CONF_INVERTER_HOST,
    CONF_INVERTER_PORT,
    DOMAIN,
)
from custom_components.sungrow_ems.registers import (
    IHM_DEVICE_TYPE_CODE,
    REGISTERS_BY_KEY,
    DataType,
)

SERIAL = "TEST0000001"
INVERTER_PORT = 502
IHM_PORT = 503

ENTRY_DATA: dict[str, Any] = {
    CONF_INVERTER_HOST: "inverter.test",
    CONF_INVERTER_PORT: INVERTER_PORT,
    CONF_INVERTER_DEVICE_ID: 1,
    CONF_IHM_ENABLED: True,
    CONF_IHM_HOST: "ihm.test",
    CONF_IHM_PORT: IHM_PORT,
    CONF_IHM_DEVICE_ID: 247,
}


class FakeResponse:
    """Minimal stand-in for a pymodbus response PDU."""

    def __init__(
        self, registers: list[int] | None = None, exception_code: int | None = None
    ) -> None:
        self.registers = registers or []
        self.exception_code = exception_code

    def isError(self) -> bool:
        return self.exception_code is not None


class FakeDevice:
    """Register store for one host:port. Addresses are documented (1-based)."""

    def __init__(self) -> None:
        self.words: dict[str, dict[int, int]] = {
            "input": defaultdict(int),
            "holding": defaultdict(int),
        }
        self.calls: list[tuple[str, int, int, int]] = []
        self.rejected: set[int] = set()  # addresses answered with exception 0x02
        self.busy = 0  # next N requests answer exception 0x06
        self.timeouts = 0  # next N requests raise ModbusIOException
        self.timeout_from: int | None = None  # requests after the Nth time out
        self.offline = False  # connect() fails
        self.open_sockets = 0
        self.connects = 0

    def set(self, key: str, value: int | str) -> None:
        """Store a raw (unscaled) value or a string for a register key."""
        reg = REGISTERS_BY_KEY[key]
        table = self.words[str(reg.table)]
        if reg.dtype is DataType.UTF8:
            data = str(value).encode().ljust(reg.count * 2, b"\x00")
            for i in range(reg.count):
                table[reg.address + i] = int.from_bytes(data[2 * i : 2 * i + 2], "big")
        elif reg.count == 1:
            table[reg.address] = int(value) & 0xFFFF
        else:  # low word first
            table[reg.address] = int(value) & 0xFFFF
            table[reg.address + 1] = (int(value) >> 16) & 0xFFFF

    def functions(self) -> set[str]:
        return {call[0] for call in self.calls}

    def addresses(self, function: str | None = None) -> list[int]:
        return [c[1] for c in self.calls if function is None or c[0] == function]


class FakeClient:
    """Replaces ``AsyncModbusTcpClient``. Exposes the two read calls only:
    anything else (for example ``write_register``) raises AttributeError."""

    devices: ClassVar[dict[int, FakeDevice]] = {}

    def __init__(self, host: str, *, port: int = 502, **_kwargs: Any) -> None:
        self.device = self.devices[port]
        self.connected = False

    async def connect(self) -> bool:
        if self.device.offline:
            return False
        self.connected = True
        self.device.open_sockets += 1
        self.device.connects += 1
        return True

    def close(self) -> None:
        if self.connected:
            self.device.open_sockets -= 1
        self.connected = False

    async def _read(
        self, function: str, table: str, address: int, count: int, device_id: int
    ) -> FakeResponse:
        dev = self.device
        documented = address + 1
        dev.calls.append((function, documented, count, device_id))
        if dev.timeout_from is not None and len(dev.calls) > dev.timeout_from:
            raise ModbusIOException("no response")
        if dev.timeouts:
            dev.timeouts -= 1
            raise ModbusIOException("no response")
        if dev.busy:
            dev.busy -= 1
            return FakeResponse(exception_code=0x06)
        span = range(documented, documented + count)
        if dev.rejected.intersection(span):
            return FakeResponse(exception_code=0x02)
        return FakeResponse([dev.words[table][a] for a in span])

    async def read_input_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> FakeResponse:
        return await self._read(
            "read_input_registers", "input", address, count, device_id
        )

    async def read_holding_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> FakeResponse:
        return await self._read(
            "read_holding_registers", "holding", address, count, device_id
        )


@pytest.fixture
def inverter() -> FakeDevice:
    dev = FakeDevice()
    dev.set("mb_serial_number", SERIAL)
    dev.set("mb_device_type_code", 0xD1B)  # SH10RS
    dev.set("mb_battery_soc", 655)  # 65.5 %
    dev.set("mb_total_dc_power", 4200)
    dev.set("mb_running_state", 0x0040)
    return dev


@pytest.fixture
def ihm() -> FakeDevice:
    dev = FakeDevice()
    dev.set("ihm_device_type_code", IHM_DEVICE_TYPE_CODE)
    dev.set("ihm_charger_status", 1)
    dev.set("ihm_grid_import_energy", 123456)  # 12345.6 kWh
    dev.set("ihm_grid_export_energy", 65432)
    dev.set("ihm_total_rated_battery_capacity", 384)  # 38.4 kWh
    return dev


@pytest.fixture(autouse=True)
def fake_modbus(inverter: FakeDevice, ihm: FakeDevice) -> Iterator[None]:
    """Route every hub to the in-memory devices; never touch the network."""
    FakeClient.devices = {INVERTER_PORT: inverter, IHM_PORT: ihm}
    with (
        patch("custom_components.sungrow_ems.modbus.AsyncModbusTcpClient", FakeClient),
        # The iHM's 1 s pacing would make every HA test take seconds.
        patch("custom_components.sungrow_ems.MIN_IHM_REQUEST_INTERVAL", 0.0),
        patch("custom_components.sungrow_ems.DEFAULT_IHM_REQUEST_INTERVAL", 0.0),
        patch(
            "custom_components.sungrow_ems.config_flow.MIN_IHM_REQUEST_INTERVAL", 0.0
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load custom_components/sungrow_ems in tests."""


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Sungrow SH10RS",
        data=dict(ENTRY_DATA),
        unique_id=SERIAL,
        version=1,
        minor_version=2,
    )
