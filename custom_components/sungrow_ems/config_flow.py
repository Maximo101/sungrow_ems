"""Config flow for Sungrow EMS (local Modbus TCP, read-only).

Optional PVOutput.org upload: a toggle on the first form (setup) and on the
options form. When it is on, three more steps follow: account, schedule and
(live mode) parameter mapping. Settings are stored in the entry options.
"""

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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
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
    CONF_PVOUTPUT_API_KEY,
    CONF_PVOUTPUT_CUMULATIVE,
    CONF_PVOUTPUT_DAILY_TIME,
    CONF_PVOUTPUT_ENABLED,
    CONF_PVOUTPUT_INTERVAL_MODE,
    CONF_PVOUTPUT_SEND_VALUES,
    CONF_PVOUTPUT_STATUS_INTERVAL,
    CONF_PVOUTPUT_SYSTEM_ID,
    CONF_PVOUTPUT_TIER,
    CONF_PVOUTPUT_UPLOAD_MODE,
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
    DEFAULT_PVOUTPUT_DAILY_TIME,
    DEFAULT_PVOUTPUT_SEND_VALUES,
    DEFAULT_PVOUTPUT_STATUS_INTERVAL,
    DEFAULT_PVOUTPUT_TIER,
    DEFAULT_PVOUTPUT_UPLOAD_MODE,
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
    PVOUTPUT_BATTERY_PARAMS,
    PVOUTPUT_ENERGY_PARAMS,
    PVOUTPUT_EXTENDED_PARAMS,
    PVOUTPUT_INTERVAL_AUTO,
    PVOUTPUT_INTERVALS,
    PVOUTPUT_MODE_DAILY,
    PVOUTPUT_MODE_LIVE,
    PVOUTPUT_POWER_PARAMS,
    PVOUTPUT_SEND_ENERGY,
    PVOUTPUT_SEND_POWER,
    PVOUTPUT_SOURCE_AUTO,
    PVOUTPUT_SOURCE_NONE,
    PVOUTPUT_STANDARD_PARAMS,
    PVOUTPUT_TIER_DONATION,
    PVOUTPUT_TIER_FREE,
)
from .modbus import SungrowConnectionError, SungrowModbusError, SungrowModbusHub
from .pvoutput import (
    PVOUTPUT_SOURCES,
    PVOutputAuthError,
    PVOutputBadRequestError,
    PVOutputError,
    PVOutputRateLimitError,
    async_validate_credentials,
    is_entity_id,
    option_key,
)
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
    data.pop(CONF_PVOUTPUT_ENABLED, None)  # an option, not connection data
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


# ---------------------------------------------------------------------------
# PVOutput steps (shared by the config flow and the options flow)
# ---------------------------------------------------------------------------

#: Every option the PVOutput steps own (copied as a block between flows).
PVOUTPUT_OPTION_KEYS: tuple[str, ...] = (
    CONF_PVOUTPUT_ENABLED,
    CONF_PVOUTPUT_API_KEY,
    CONF_PVOUTPUT_SYSTEM_ID,
    CONF_PVOUTPUT_TIER,
    CONF_PVOUTPUT_UPLOAD_MODE,
    CONF_PVOUTPUT_INTERVAL_MODE,
    CONF_PVOUTPUT_STATUS_INTERVAL,
    CONF_PVOUTPUT_DAILY_TIME,
    CONF_PVOUTPUT_CUMULATIVE,
    CONF_PVOUTPUT_SEND_VALUES,
    *(
        option_key(p)
        for p in (
            *PVOUTPUT_STANDARD_PARAMS,
            *PVOUTPUT_EXTENDED_PARAMS,
            *PVOUTPUT_BATTERY_PARAMS,
        )
    ),
)

_PV_DEFAULTS: dict[str, Any] = {
    CONF_PVOUTPUT_TIER: DEFAULT_PVOUTPUT_TIER,
    CONF_PVOUTPUT_UPLOAD_MODE: DEFAULT_PVOUTPUT_UPLOAD_MODE,
    CONF_PVOUTPUT_INTERVAL_MODE: PVOUTPUT_INTERVAL_AUTO,
    CONF_PVOUTPUT_STATUS_INTERVAL: DEFAULT_PVOUTPUT_STATUS_INTERVAL,
    CONF_PVOUTPUT_DAILY_TIME: DEFAULT_PVOUTPUT_DAILY_TIME,
    CONF_PVOUTPUT_CUMULATIVE: False,
    CONF_PVOUTPUT_SEND_VALUES: DEFAULT_PVOUTPUT_SEND_VALUES,
    **{option_key(p): PVOUTPUT_SOURCE_AUTO for p in PVOUTPUT_STANDARD_PARAMS},
    # Battery values default to auto; v7-v12 have no default (do not send).
    **{option_key(p): PVOUTPUT_SOURCE_AUTO for p in PVOUTPUT_BATTERY_PARAMS},
}


def _select(options: list[str], translation_key: str) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=options,
            translation_key=translation_key,
            mode=SelectSelectorMode.DROPDOWN,
        )
    )


def pvoutput_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """The PVOutput part of an options mapping."""
    return {k: options[k] for k in PVOUTPUT_OPTION_KEYS if k in options}


class _PVOutputSteps:
    """Account -> schedule -> mapping. The host flow implements the finish."""

    hass: HomeAssistant
    _pv: dict[str, Any]
    _pv_system_name: str | None

    def _pv_init(self, current: Mapping[str, Any] | None = None) -> None:
        self._pv = {**_PV_DEFAULTS, **pvoutput_options(current or {})}
        self._pv[CONF_PVOUTPUT_ENABLED] = True
        # v7-v12 now hold entity IDs. Older versions stored internal source
        # keys there; drop those so the field starts as "do not send".
        for param in PVOUTPUT_EXTENDED_PARAMS:
            key = option_key(param)
            if not is_entity_id(self._pv.get(key)):
                self._pv.pop(key, None)
        self._pv_system_name = None

    async def _async_pvoutput_finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_pvoutput_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """API key, system ID, account tier and upload mode (validated online)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_key = str(user_input[CONF_PVOUTPUT_API_KEY]).strip()
            system_id = str(user_input[CONF_PVOUTPUT_SYSTEM_ID]).strip()
            try:
                system = await async_validate_credentials(self.hass, api_key, system_id)
            except PVOutputAuthError:
                errors["base"] = "invalid_auth"
            except PVOutputRateLimitError:
                errors["base"] = "pvoutput_rate_limited"
            except PVOutputBadRequestError:
                errors["base"] = "pvoutput_invalid_system"
            except PVOutputError as err:
                _LOGGER.debug("PVOutput validation failed: %s", err)
                errors["base"] = "pvoutput_cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating PVOutput")
                errors["base"] = "unknown"
            else:
                if (
                    user_input[CONF_PVOUTPUT_TIER] == PVOUTPUT_TIER_DONATION
                    and system.donation is False
                ):
                    # PVOutput reports the free hourly limit for this key.
                    errors["base"] = "pvoutput_donation_not_active"
                else:
                    self._pv.update(
                        {
                            CONF_PVOUTPUT_API_KEY: api_key,
                            CONF_PVOUTPUT_SYSTEM_ID: system_id,
                            CONF_PVOUTPUT_TIER: user_input[CONF_PVOUTPUT_TIER],
                            CONF_PVOUTPUT_UPLOAD_MODE: user_input[
                                CONF_PVOUTPUT_UPLOAD_MODE
                            ],
                        }
                    )
                    if system.status_interval is not None:
                        self._pv[CONF_PVOUTPUT_STATUS_INTERVAL] = system.status_interval
                    self._pv_system_name = system.name
                    return await self.async_step_pvoutput_schedule()

        schema = vol.Schema(
            {
                vol.Required(CONF_PVOUTPUT_API_KEY): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_PVOUTPUT_SYSTEM_ID): TextSelector(),
                vol.Required(CONF_PVOUTPUT_TIER): _select(
                    [PVOUTPUT_TIER_FREE, PVOUTPUT_TIER_DONATION], "pvoutput_tier"
                ),
                vol.Required(CONF_PVOUTPUT_UPLOAD_MODE): _select(
                    [PVOUTPUT_MODE_LIVE, PVOUTPUT_MODE_DAILY], "pvoutput_upload_mode"
                ),
            }
        )
        return self.async_show_form(  # type: ignore[attr-defined, no-any-return]
            step_id="pvoutput_account",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                schema, {**self._pv, **(user_input or {})}
            ),
            errors=errors,
        )

    async def async_step_pvoutput_schedule(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Route to the live or daily schedule step."""
        if self._pv[CONF_PVOUTPUT_UPLOAD_MODE] == PVOUTPUT_MODE_DAILY:
            return await self.async_step_pvoutput_daily()
        return await self.async_step_pvoutput_live()

    async def async_step_pvoutput_live(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Live status: interval (auto from the PVOutput profile, or manual)."""
        if user_input is not None:
            self._pv[CONF_PVOUTPUT_INTERVAL_MODE] = str(
                user_input[CONF_PVOUTPUT_INTERVAL_MODE]
            )
            self._pv[CONF_PVOUTPUT_SEND_VALUES] = str(
                user_input.get(CONF_PVOUTPUT_SEND_VALUES, DEFAULT_PVOUTPUT_SEND_VALUES)
            )
            self._pv[CONF_PVOUTPUT_CUMULATIVE] = bool(
                user_input.get(CONF_PVOUTPUT_CUMULATIVE, False)
            )
            return await self.async_step_pvoutput_mapping()

        schema = vol.Schema(
            {
                vol.Required(CONF_PVOUTPUT_INTERVAL_MODE): _select(
                    [PVOUTPUT_INTERVAL_AUTO, *(str(i) for i in PVOUTPUT_INTERVALS)],
                    "pvoutput_interval_mode",
                ),
                vol.Required(CONF_PVOUTPUT_SEND_VALUES): _select(
                    [PVOUTPUT_SEND_POWER, PVOUTPUT_SEND_ENERGY],
                    "pvoutput_send_values",
                ),
                vol.Required(CONF_PVOUTPUT_CUMULATIVE): BooleanSelector(),
            }
        )
        return self.async_show_form(  # type: ignore[attr-defined, no-any-return]
            step_id="pvoutput_live",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                schema, self._pv
            ),
            description_placeholders=self._pv_placeholders(),
        )

    async def async_step_pvoutput_daily(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Daily output: local time of the once-a-day upload."""
        if user_input is not None:
            self._pv[CONF_PVOUTPUT_DAILY_TIME] = str(
                user_input[CONF_PVOUTPUT_DAILY_TIME]
            )
            return await self._async_pvoutput_finish()

        schema = vol.Schema({vol.Required(CONF_PVOUTPUT_DAILY_TIME): TimeSelector()})
        return self.async_show_form(  # type: ignore[attr-defined, no-any-return]
            step_id="pvoutput_daily",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                schema, self._pv
            ),
            description_placeholders=self._pv_placeholders(),
        )

    def _pv_placeholders(self) -> dict[str, str]:
        return {
            "system": self._pv_system_name or str(self._pv[CONF_PVOUTPUT_SYSTEM_ID]),
            "interval": str(self._pv[CONF_PVOUTPUT_STATUS_INTERVAL]),
        }

    async def async_step_pvoutput_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Which value feeds each parameter.

        Standard: v2 + v4 (power) or v1 + v3 (energy), plus v5 and v6, from
        Sungrow EMS values. Donation accounts also get b1-b5 (battery, auto by
        default) and v7-v12, each fed from any Home Assistant entity (empty =
        do not send).
        """
        donation = self._pv[CONF_PVOUTPUT_TIER] == PVOUTPUT_TIER_DONATION
        standard = (
            PVOUTPUT_ENERGY_PARAMS
            if self._pv.get(CONF_PVOUTPUT_SEND_VALUES) == PVOUTPUT_SEND_ENERGY
            else PVOUTPUT_POWER_PARAMS
        )
        if user_input is not None:
            selects = standard + (PVOUTPUT_BATTERY_PARAMS if donation else ())
            for param in selects:
                key = option_key(param)
                if key in user_input:
                    self._pv[key] = str(user_input[key])
            if donation:
                for param in PVOUTPUT_EXTENDED_PARAMS:
                    key = option_key(param)
                    value = user_input.get(key)
                    if is_entity_id(value):
                        self._pv[key] = str(value)
                    else:  # cleared: do not send
                        self._pv.pop(key, None)
            return await self._async_pvoutput_finish()

        sources = list(PVOUTPUT_SOURCES)
        fields: dict[Any, Any] = {}
        for param in standard:
            fields[vol.Required(option_key(param))] = _select(
                [PVOUTPUT_SOURCE_AUTO, *sources, PVOUTPUT_SOURCE_NONE],
                "pvoutput_source",
            )
        if donation:
            for param in PVOUTPUT_BATTERY_PARAMS:
                fields[vol.Required(option_key(param))] = _select(
                    [PVOUTPUT_SOURCE_AUTO, PVOUTPUT_SOURCE_NONE, *sources],
                    "pvoutput_source",
                )
            for param in PVOUTPUT_EXTENDED_PARAMS:
                fields[vol.Optional(option_key(param))] = EntitySelector(
                    EntitySelectorConfig()
                )
        return self.async_show_form(  # type: ignore[attr-defined, no-any-return]
            step_id="pvoutput_mapping",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                vol.Schema(fields), self._pv
            ),
        )


class SungrowEmsConfigFlow(_PVOutputSteps, ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sungrow EMS."""

    VERSION = 1
    MINOR_VERSION = 3

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[str, Any] = {}
        self._title = ""
        self._pv_init()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Initial setup: inverter (required) and iHomeManager (optional)."""
        errors: dict[str, str] = {}
        defaults: Mapping[str, Any] = user_input or {}
        if user_input is not None:
            pvoutput = bool(user_input.get(CONF_PVOUTPUT_ENABLED, False))
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
                self._data = data
                self._title = f"Sungrow {info.model}"
                if pvoutput:
                    return await self.async_step_pvoutput_account()
                return self.async_create_entry(title=self._title, data=data)

        schema = _connection_schema(defaults).extend(
            {
                vol.Required(
                    CONF_PVOUTPUT_ENABLED,
                    default=bool(defaults.get(CONF_PVOUTPUT_ENABLED, False)),
                ): BooleanSelector()
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _async_pvoutput_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self._title, data=self._data, options=dict(self._pv)
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


class SungrowEmsOptionsFlow(_PVOutputSteps, OptionsFlowWithReload):
    """Polling intervals, failure threshold, battery capacity, efficiency, spike
    filter, and the optional PVOutput upload.

    The entry reloads automatically when the options actually change, so a new
    PVOutput schedule, tier or mapping takes effect without a restart.
    """

    def __init__(self) -> None:
        super().__init__()
        self._general: dict[str, Any] = {}
        self._pv = {}
        self._pv_system_name = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        existing_pv = pvoutput_options(self.config_entry.options)
        if user_input is not None:
            self._general = _normalise_options(user_input)
            if user_input.get(CONF_PVOUTPUT_ENABLED):
                self._pv_init(self.config_entry.options)
                return await self.async_step_pvoutput_account()
            # Off: keep the stored PVOutput settings so turning it back on
            # later starts from them.
            return self.async_create_entry(
                data={
                    **self._general,
                    **existing_pv,
                    CONF_PVOUTPUT_ENABLED: False,
                }
            )

        current = {
            **_OPTION_DEFAULTS,
            CONF_PVOUTPUT_ENABLED: False,
            **self.config_entry.options,
        }
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
                vol.Required(CONF_PVOUTPUT_ENABLED): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, current),
        )

    async def _async_pvoutput_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(data={**self._general, **self._pv})


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
