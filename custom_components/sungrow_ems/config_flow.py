"""Config flow for Sungrow EMS (local Modbus TCP, read-only)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)
import voluptuous as vol

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_FAILURE_THRESHOLD,
    CONF_IHM_DEVICE_ID,
    CONF_IHM_ENABLED,
    CONF_IHM_HOST,
    CONF_IHM_PORT,
    CONF_IHM_REQUEST_INTERVAL,
    CONF_IHM_SCAN_INTERVAL,
    CONF_INVERTER_DEVICE_ID,
    CONF_INVERTER_HOST,
    CONF_INVERTER_PORT,
    CONF_INVERTER_SCAN_INTERVAL,
    CONF_RETRIES,
    CONF_SPIKE_MAX_DAILY,
    CONF_SPIKE_MAX_JUMP,
    CONF_SPIKE_REBASELINE_COUNT,
    CONF_TIMEOUT,
    DEFAULT_BATTERY_CAPACITY_OPTION,
    DEFAULT_DISCHARGE_EFFICIENCY,
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_IHM_DEVICE_ID,
    DEFAULT_IHM_PORT,
    DEFAULT_IHM_REQUEST_INTERVAL,
    DEFAULT_IHM_SCAN_INTERVAL,
    DEFAULT_INVERTER_DEVICE_ID,
    DEFAULT_INVERTER_PORT,
    DEFAULT_INVERTER_SCAN_INTERVAL,
    DEFAULT_RETRIES,
    DEFAULT_SPIKE_MAX_DAILY,
    DEFAULT_SPIKE_MAX_JUMP,
    DEFAULT_SPIKE_REBASELINE_COUNT,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MAX_BATTERY_CAPACITY,
    MAX_FAILURE_THRESHOLD,
    MAX_IHM_REQUEST_INTERVAL,
    MAX_RETRIES,
    MAX_SCAN_INTERVAL,
    MAX_SPIKE_REBASELINE_COUNT,
    MAX_TIMEOUT,
    MIN_FAILURE_THRESHOLD,
    MIN_IHM_REQUEST_INTERVAL,
    MIN_SCAN_INTERVAL,
    MIN_SPIKE_REBASELINE_COUNT,
    MIN_TIMEOUT,
)
from .modbus import SungrowConnectionError, SungrowModbusError, SungrowModbusHub
from .registers import IHM_DEVICE_TYPE_CODE, REGISTERS_BY_KEY

_LOGGER = logging.getLogger(__name__)

#: Probes use a short timeout so the form answers quickly.
_PROBE_TIMEOUT = 5.0
_PROBE_RETRIES = 1

_INT_KEYS = (
    CONF_INVERTER_PORT,
    CONF_INVERTER_DEVICE_ID,
    CONF_IHM_PORT,
    CONF_IHM_DEVICE_ID,
)


class _ProbeError(Exception):
    """Validation failed; ``args[0]`` is the translation key for the error."""


@dataclass(slots=True)
class _ProbeInfo:
    serial: str
    model: str
    ihm_found: bool


def _number(
    minimum: float, maximum: float, step: float = 1, unit: str | None = None
) -> NumberSelector:
    config = NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _connection_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    ihm_host = defaults.get(CONF_IHM_HOST) or ""
    return vol.Schema(
        {
            vol.Required(
                CONF_INVERTER_HOST, default=defaults.get(CONF_INVERTER_HOST, "")
            ): TextSelector(),
            vol.Required(
                CONF_INVERTER_PORT,
                default=defaults.get(CONF_INVERTER_PORT, DEFAULT_INVERTER_PORT),
            ): _number(1, 65535),
            vol.Required(
                CONF_INVERTER_DEVICE_ID,
                default=defaults.get(
                    CONF_INVERTER_DEVICE_ID, DEFAULT_INVERTER_DEVICE_ID
                ),
            ): _number(1, 247),
            vol.Required(
                CONF_IHM_ENABLED, default=defaults.get(CONF_IHM_ENABLED, True)
            ): BooleanSelector(),
            vol.Optional(
                CONF_IHM_HOST, description={"suggested_value": ihm_host}
            ): TextSelector(),
            vol.Required(
                CONF_IHM_PORT, default=defaults.get(CONF_IHM_PORT, DEFAULT_IHM_PORT)
            ): _number(1, 65535),
            vol.Required(
                CONF_IHM_DEVICE_ID,
                default=defaults.get(CONF_IHM_DEVICE_ID, DEFAULT_IHM_DEVICE_ID),
            ): _number(1, 247),
        }
    )


def _normalise(user_input: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    data[CONF_INVERTER_HOST] = str(data[CONF_INVERTER_HOST]).strip()
    host = str(data.get(CONF_IHM_HOST) or "").strip()
    if host:
        data[CONF_IHM_HOST] = host
    else:
        data.pop(CONF_IHM_HOST, None)
    for key in _INT_KEYS:
        if key in data:
            data[key] = int(data[key])
    data[CONF_IHM_ENABLED] = bool(data.get(CONF_IHM_ENABLED))
    return data


def _running_hub(
    entry: ConfigEntry | None, name: str, host: str, port: int, device_id: int
) -> SungrowModbusHub | None:
    """The loaded entry's hub for this exact endpoint, if any.

    The iHM accepts one TCP connection per port, so a reconfigure that keeps
    the same host/port must probe through the existing socket.
    """
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        return None
    runtime = getattr(entry, "runtime_data", None)
    for hub in getattr(runtime, "hubs", ()) or ():
        if (
            hub.name == name
            and hub.host == host
            and hub.port == port
            and hub.device_id == device_id
        ):
            return hub
    return None


async def _async_probe(
    data: Mapping[str, Any], entry: ConfigEntry | None = None
) -> _ProbeInfo:
    """Connect to the inverter (and iHM if enabled) and read identity registers."""
    host = data[CONF_INVERTER_HOST]
    port = int(data[CONF_INVERTER_PORT])
    unit = int(data[CONF_INVERTER_DEVICE_ID])
    existing = _running_hub(entry, "inverter", host, port, unit)
    hub = existing or SungrowModbusHub(
        host=host,
        port=port,
        device_id=unit,
        name="inverter-probe",
        timeout=_PROBE_TIMEOUT,
        retries=_PROBE_RETRIES,
    )
    try:
        decoded = await hub.async_read_registers(
            [
                REGISTERS_BY_KEY["mb_serial_number"],
                REGISTERS_BY_KEY["mb_device_type_code"],
            ]
        )
    except SungrowConnectionError as err:
        _LOGGER.debug("Inverter probe failed: %s", err)
        raise _ProbeError("cannot_connect") from err
    except SungrowModbusError as err:
        _LOGGER.debug("Inverter probe rejected: %s", err)
        raise _ProbeError("not_sungrow") from err
    finally:
        if existing is None:
            await hub.async_close()

    serial = decoded.values.get("mb_serial_number")
    if not serial:
        raise _ProbeError("no_serial")
    code = decoded.raw.get("mb_device_type_code")
    model = (
        REGISTERS_BY_KEY["mb_device_type_code"].label(code)
        if code is not None
        else None
    )

    ihm_found = False
    if data.get(CONF_IHM_ENABLED):
        ihm_host = data.get(CONF_IHM_HOST)
        if not ihm_host:
            raise _ProbeError("ihm_host_required")
        ihm_port = int(data[CONF_IHM_PORT])
        ihm_unit = int(data[CONF_IHM_DEVICE_ID])
        existing_ihm = _running_hub(entry, "ihm", ihm_host, ihm_port, ihm_unit)
        ihm_hub = existing_ihm or SungrowModbusHub(
            host=ihm_host,
            port=ihm_port,
            device_id=ihm_unit,
            name="ihm-probe",
            timeout=_PROBE_TIMEOUT,
            retries=_PROBE_RETRIES,
            min_request_interval=MIN_IHM_REQUEST_INTERVAL,
        )
        try:
            ihm_decoded = await ihm_hub.async_read_registers(
                [REGISTERS_BY_KEY["ihm_device_type_code"]]
            )
        except SungrowModbusError as err:
            _LOGGER.debug("iHM probe failed: %s", err)
            raise _ProbeError("ihm_cannot_connect") from err
        finally:
            if existing_ihm is None:
                await ihm_hub.async_close()
        if ihm_decoded.raw.get("ihm_device_type_code") != IHM_DEVICE_TYPE_CODE:
            raise _ProbeError("not_ihm")
        ihm_found = True

    return _ProbeInfo(
        serial=str(serial), model=model or "Hybrid inverter", ihm_found=ihm_found
    )


class SungrowEmsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sungrow EMS."""

    VERSION = 1
    MINOR_VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initial setup: inverter (required) and iHomeManager (optional)."""
        errors: dict[str, str] = {}
        defaults: Mapping[str, Any] = user_input or {}
        if user_input is not None:
            data = _normalise(user_input)
            try:
                info = await _async_probe(data)
            except _ProbeError as err:
                errors["base"] = str(err.args[0])
            except Exception:
                _LOGGER.exception("Unexpected error during Sungrow EMS setup")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info.serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=f"Sungrow {info.model}", data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(defaults),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change hosts, ports or unit IDs of an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        defaults: Mapping[str, Any] = user_input or entry.data
        if user_input is not None:
            data = _normalise(user_input)
            try:
                info = await _async_probe(data, entry)
            except _ProbeError as err:
                errors["base"] = str(err.args[0])
            except Exception:
                _LOGGER.exception("Unexpected error during Sungrow EMS reconfigure")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info.serial)
                self._abort_if_unique_id_mismatch(reason="unique_id_mismatch")
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(defaults),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SungrowEmsOptionsFlow:
        """Options: polling, reliability and energy model."""
        return SungrowEmsOptionsFlow()


class SungrowEmsOptionsFlow(OptionsFlowWithReload):
    """Polling intervals, failure threshold, battery capacity, efficiency, spike filter.

    The entry reloads automatically when the options actually change.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=_normalise_options(user_input))

        current = {**_OPTION_DEFAULTS, **self.config_entry.options}
        schema = vol.Schema(
            {
                vol.Required(CONF_INVERTER_SCAN_INTERVAL): _number(
                    MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL, 1, "s"
                ),
                vol.Required(CONF_IHM_SCAN_INTERVAL): _number(
                    MIN_SCAN_INTERVAL, MAX_SCAN_INTERVAL, 1, "s"
                ),
                vol.Required(CONF_TIMEOUT): _number(MIN_TIMEOUT, MAX_TIMEOUT, 0.5, "s"),
                vol.Required(CONF_RETRIES): _number(0, MAX_RETRIES, 1),
                vol.Required(CONF_IHM_REQUEST_INTERVAL): _number(
                    MIN_IHM_REQUEST_INTERVAL, MAX_IHM_REQUEST_INTERVAL, 0.1, "s"
                ),
                vol.Required(CONF_FAILURE_THRESHOLD): _number(
                    MIN_FAILURE_THRESHOLD, MAX_FAILURE_THRESHOLD, 1
                ),
                vol.Required(CONF_BATTERY_CAPACITY): _number(
                    0, MAX_BATTERY_CAPACITY, 0.01, "kWh"
                ),
                vol.Required(CONF_DISCHARGE_EFFICIENCY): _number(0.5, 1.0, 0.01),
                vol.Required(CONF_SPIKE_MAX_JUMP): _number(1, 1000, 1, "kWh"),
                vol.Required(CONF_SPIKE_MAX_DAILY): _number(10, 2000, 1, "kWh"),
                vol.Required(CONF_SPIKE_REBASELINE_COUNT): _number(
                    MIN_SPIKE_REBASELINE_COUNT, MAX_SPIKE_REBASELINE_COUNT, 1
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, current),
        )


_OPTION_DEFAULTS: dict[str, Any] = {
    CONF_INVERTER_SCAN_INTERVAL: DEFAULT_INVERTER_SCAN_INTERVAL,
    CONF_IHM_SCAN_INTERVAL: DEFAULT_IHM_SCAN_INTERVAL,
    CONF_TIMEOUT: DEFAULT_TIMEOUT,
    CONF_RETRIES: DEFAULT_RETRIES,
    CONF_IHM_REQUEST_INTERVAL: DEFAULT_IHM_REQUEST_INTERVAL,
    CONF_FAILURE_THRESHOLD: DEFAULT_FAILURE_THRESHOLD,
    CONF_BATTERY_CAPACITY: DEFAULT_BATTERY_CAPACITY_OPTION,
    CONF_DISCHARGE_EFFICIENCY: DEFAULT_DISCHARGE_EFFICIENCY,
    CONF_SPIKE_MAX_JUMP: DEFAULT_SPIKE_MAX_JUMP,
    CONF_SPIKE_MAX_DAILY: DEFAULT_SPIKE_MAX_DAILY,
    CONF_SPIKE_REBASELINE_COUNT: DEFAULT_SPIKE_REBASELINE_COUNT,
}

_OPTION_INTS = (
    CONF_INVERTER_SCAN_INTERVAL,
    CONF_IHM_SCAN_INTERVAL,
    CONF_RETRIES,
    CONF_FAILURE_THRESHOLD,
    CONF_SPIKE_REBASELINE_COUNT,
)


def _normalise_options(user_input: Mapping[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for key, default in _OPTION_DEFAULTS.items():
        value = user_input.get(key, default)
        options[key] = int(value) if key in _OPTION_INTS else float(value)
    return options
