"""Optional PVOutput.org uploader for Sungrow EMS.

Off by default. When enabled in the config or options flow, values the
integration already has (coordinator snapshots and the derived engine) are
sent to PVOutput.org:

  * live  - Add Status (``addstatus.jsp``) every Status Interval (5/10/15 min),
            aligned to the clock so each upload lands on its own PVOutput slot.
  * daily - Add Output (``addoutput.jsp``) once a day at a set local time.

This module only reads coordinator data. It never touches the Modbus hubs,
and every failure (HTTP error, timeout, rate limit, missing value) is caught
and logged here, so PVOutput can never make a Sungrow entity unavailable.

Units: Home Assistant / the register map use kW and kWh in places; PVOutput
wants W and Wh. Every source below returns PVOutput units.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
import logging
import math
from typing import TYPE_CHECKING, Any, Final, Literal

import aiohttp
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    CONF_PVOUTPUT_API_KEY,
    CONF_PVOUTPUT_CUMULATIVE,
    CONF_PVOUTPUT_DAILY_TIME,
    CONF_PVOUTPUT_INTERVAL_MODE,
    CONF_PVOUTPUT_SEND_VALUES,
    CONF_PVOUTPUT_STATUS_INTERVAL,
    CONF_PVOUTPUT_SYSTEM_ID,
    CONF_PVOUTPUT_TIER,
    CONF_PVOUTPUT_UPLOAD_MODE,
    CONF_PVOUTPUT_V_PREFIX,
    DEFAULT_PVOUTPUT_DAILY_TIME,
    DEFAULT_PVOUTPUT_SEND_VALUES,
    DEFAULT_PVOUTPUT_STATUS_INTERVAL,
    DEFAULT_PVOUTPUT_TIER,
    DEFAULT_PVOUTPUT_UPLOAD_MODE,
    PVOUTPUT_BASE_URL,
    PVOUTPUT_BATTERY_PARAMS,
    PVOUTPUT_DONATION_RATE_LIMIT,
    PVOUTPUT_ENERGY_PARAMS,
    PVOUTPUT_EXTENDED_PARAMS,
    PVOUTPUT_INTERVAL_AUTO,
    PVOUTPUT_MODE_DAILY,
    PVOUTPUT_POWER_PARAMS,
    PVOUTPUT_SEND_ENERGY,
    PVOUTPUT_SOURCE_AUTO,
    PVOUTPUT_SOURCE_NONE,
    PVOUTPUT_STANDARD_PARAMS,
    PVOUTPUT_TIER_DONATION,
    PVOUTPUT_TIMEOUT,
)

if TYPE_CHECKING:
    from .coordinator import (
        SungrowEmsConfigEntry,
        SungrowEmsCoordinator,
        SungrowEmsRuntimeData,
    )

_LOGGER = logging.getLogger(__name__)

#: getsystem.jsp: the first ';' section is comma-separated and the 16th field
#: (index 15) is the Status Interval in minutes.
_GETSYSTEM_STATUS_INTERVAL_INDEX: Final = 15

#: Status Interval values that divide an hour evenly (aligned scheduling).
_VALID_INTERVALS: Final = frozenset({1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60})

#: Seconds into the slot minute at which a live upload is sent, so the
#: coordinators have polled at least once since the slot started.
_LIVE_SECOND: Final = 15

_RESPONSE_TEXT_MAX: Final = 200


# ---------------------------------------------------------------------------
# Errors and responses
# ---------------------------------------------------------------------------


class PVOutputError(Exception):
    """Any PVOutput failure.

    ``status`` is the HTTP status and ``text`` the body PVOutput sent back
    (e.g. "Bad request 400: ..."), if there was an answer at all.
    """

    def __init__(
        self, message: str, status: int | None = None, text: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.text = text

    @property
    def donation_required(self) -> bool:
        """PVOutput refused something that needs donation mode."""
        return "donat" in (self.text or str(self)).lower()


class PVOutputConnectionError(PVOutputError):
    """Timeout, DNS or socket error."""


class PVOutputAuthError(PVOutputError):
    """HTTP 401, or 403 that is not a rate limit (wrong key, read-only key)."""


class PVOutputRateLimitError(PVOutputError):
    """HTTP 403 "Exceeded number requests per hour" or HTTP 429."""

    def __init__(
        self,
        message: str,
        status: int | None = None,
        reset: datetime | None = None,
        text: str | None = None,
    ) -> None:
        super().__init__(message, status, text)
        self.reset = reset


class PVOutputBadRequestError(PVOutputError):
    """HTTP 400: PVOutput rejected the data (bad system ID, date too old...)."""


@dataclass(slots=True, frozen=True)
class PVOutputResponse:
    """A successful (HTTP 200) answer."""

    status: int
    text: str
    rate_limit: int | None = None
    rate_remaining: int | None = None
    rate_reset: datetime | None = None


@dataclass(slots=True, frozen=True)
class PVOutputSystem:
    """The parts of getsystem.jsp this integration uses."""

    name: str | None
    status_interval: int | None
    #: X-Rate-Limit-Limit of the answer: 300 in donation mode, 60 otherwise.
    rate_limit: int | None = None

    @property
    def donation(self) -> bool | None:
        """Donation mode as seen from the hourly limit (None if unknown)."""
        return is_donation_limit(self.rate_limit)


def is_donation_limit(limit: int | None) -> bool | None:
    """True for a donation account's hourly limit, None when not reported."""
    if limit is None:
        return None
    return limit >= PVOUTPUT_DONATION_RATE_LIMIT


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    try:
        return int(headers[name])
    except (KeyError, TypeError, ValueError):
        return None


def _reset_header(headers: Mapping[str, str]) -> datetime | None:
    stamp = _int_header(headers, "X-Rate-Limit-Reset")
    return None if stamp is None else dt_util.utc_from_timestamp(stamp)


def parse_getsystem(text: str) -> PVOutputSystem:
    """Parse a getsystem.jsp CSV answer. Never raises."""
    fields = text.strip().split(";", 1)[0].split(",")
    name = fields[0].strip() or None
    interval: int | None = None
    if len(fields) > _GETSYSTEM_STATUS_INTERVAL_INDEX:
        try:
            value = int(fields[_GETSYSTEM_STATUS_INTERVAL_INDEX].strip())
        except ValueError:
            value = 0
        if value in _VALID_INTERVALS:
            interval = value
    return PVOutputSystem(name=name, status_interval=interval)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PVOutputClient:
    """Minimal async PVOutput.org API client on Home Assistant's shared session."""

    def __init__(self, hass: HomeAssistant, api_key: str, system_id: str) -> None:
        self._session = async_get_clientsession(hass)
        self._api_key = api_key
        self._system_id = str(system_id)

    def _headers(self, *, form: bool) -> dict[str, str]:
        headers = {
            "X-Pvoutput-Apikey": self._api_key,
            "X-Pvoutput-SystemId": self._system_id,
            # Ask PVOutput to return X-Rate-Limit-* headers.
            "X-Rate-Limit": "1",
        }
        if form:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    async def _async_request(
        self,
        method: Literal["GET", "POST"],
        service: str,
        data: Mapping[str, Any] | None,
    ) -> PVOutputResponse:
        url = f"{PVOUTPUT_BASE_URL}/{service}"
        payload = None if data is None else {k: str(v) for k, v in data.items()}
        try:
            async with self._session.request(
                method,
                url,
                data=payload,
                headers=self._headers(form=payload is not None),
                timeout=aiohttp.ClientTimeout(total=PVOUTPUT_TIMEOUT),
            ) as resp:
                text = (await resp.text()).strip()
                status = resp.status
                headers = resp.headers
        except TimeoutError as err:
            raise PVOutputConnectionError(f"{service}: timed out") from err
        except aiohttp.ClientError as err:
            raise PVOutputConnectionError(f"{service}: {err}") from err

        short = text[:_RESPONSE_TEXT_MAX]
        if status == 200:
            return PVOutputResponse(
                status=status,
                text=short,
                rate_limit=_int_header(headers, "X-Rate-Limit-Limit"),
                rate_remaining=_int_header(headers, "X-Rate-Limit-Remaining"),
                rate_reset=_reset_header(headers),
            )
        if status == 429 or (status == 403 and "exceed" in text.lower()):
            raise PVOutputRateLimitError(
                f"{service}: rate limited ({status}: {short})",
                status,
                _reset_header(headers),
                short,
            )
        if status in (401, 403):
            raise PVOutputAuthError(f"{service}: {status}: {short}", status, short)
        if status == 400:
            raise PVOutputBadRequestError(f"{service}: {short}", status, short)
        raise PVOutputError(f"{service}: HTTP {status}: {short}", status, short)

    async def async_get_system(self) -> PVOutputSystem:
        """Read the system profile (validates the key and system ID)."""
        response = await self._async_request("GET", "getsystem.jsp", None)
        system = parse_getsystem(response.text)
        return PVOutputSystem(
            name=system.name,
            status_interval=system.status_interval,
            rate_limit=response.rate_limit,
        )

    async def async_send_status(
        self, data: Mapping[str, Any], when: datetime | None = None
    ) -> PVOutputResponse:
        """Add Status. ``d``/``t`` come from HA's local time unless given."""
        stamp = when or dt_util.now()
        payload: dict[str, Any] = {
            "d": stamp.strftime("%Y%m%d"),
            "t": stamp.strftime("%H:%M"),
            **{k: v for k, v in data.items() if v is not None},
        }
        return await self._async_request("POST", "addstatus.jsp", payload)

    async def async_send_daily_output(
        self, data: Mapping[str, Any], day: date | None = None
    ) -> PVOutputResponse:
        """Add Output for one day (``d`` = today in HA's time zone unless given)."""
        payload: dict[str, Any] = {
            "d": (day or dt_util.now().date()).strftime("%Y%m%d"),
            **{k: v for k, v in data.items() if v is not None},
        }
        return await self._async_request("POST", "addoutput.jsp", payload)


async def async_validate_credentials(
    hass: HomeAssistant, api_key: str, system_id: str
) -> PVOutputSystem:
    """Used by the config flow. Raises a PVOutputError subclass on failure."""
    return await PVOutputClient(hass, api_key, system_id).async_get_system()


# ---------------------------------------------------------------------------
# Value sources (all in PVOutput units)
# ---------------------------------------------------------------------------

SourceKind = Literal[
    "energy",
    "lifetime_energy",
    "power",
    "temperature",
    "voltage",
    "percent",
    "frequency",
]
Getter = Callable[["SungrowEmsRuntimeData"], float | None]
Validator = Callable[[float], bool]


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _values(coordinator: SungrowEmsCoordinator | None) -> Mapping[str, Any]:
    """Current values of a healthy coordinator, or nothing."""
    if (
        coordinator is None
        or coordinator.data is None
        or not coordinator.last_update_success
    ):
        return {}
    return coordinator.data.values


def _inv(key: str, factor: float = 1.0) -> Getter:
    def get(rt: SungrowEmsRuntimeData) -> float | None:
        value = _num(_values(rt.inverter).get(key))
        return None if value is None else value * factor

    return get


def _ihm(key: str, factor: float = 1.0) -> Getter:
    def get(rt: SungrowEmsRuntimeData) -> float | None:
        value = _num(_values(rt.ihm).get(key))
        return None if value is None else value * factor

    return get


def _derived(key: str, factor: float = 1.0) -> Getter:
    def get(rt: SungrowEmsRuntimeData) -> float | None:
        if rt.derived is None:
            return None
        value = _num(rt.derived.values.get(key))
        return None if value is None else value * factor

    return get


def _meter(key: str, factor: float = 1.0) -> Getter:
    def get(rt: SungrowEmsRuntimeData) -> float | None:
        if rt.derived is None:
            return None
        value = _num(rt.derived.meter_value(key))
        return None if value is None else value * factor

    return get


def _battery_net_charge(rt: SungrowEmsRuntimeData) -> float | None:
    """Battery power in PVOutput's b1 sign: + charging, - discharging (W).

    The inverter register (5214) is + discharge, so the sign is flipped.
    ``0.0 - value`` instead of ``-value`` so an idle battery is 0, not -0.
    """
    value = _num(_values(rt.inverter).get("mb_battery_power"))
    return None if value is None else 0.0 - value


def _battery_usable_capacity(rt: SungrowEmsRuntimeData) -> float | None:
    """Usable battery size (Wh): reported capacity x (Max SoC - Min SoC).

    Taken from the derived engine (Battery usable energy attributes), so it
    follows the capacity the hardware reports and the configured SoC window;
    no fixed value.
    """
    if rt.derived is None:
        return None
    details = rt.derived.values.get("battery_details") or {}
    usable = details.get("battery_usable_energy") or {}
    value = _num(usable.get("usable_capacity_kwh"))
    return None if value is None or value <= 0 else value * _KWH


def _at_least(minimum: float) -> Validator:
    return lambda value: value >= minimum


def _non_zero(value: float) -> bool:
    return value != 0


def _positive(value: float) -> bool:
    return value > 0


@dataclass(slots=True, frozen=True)
class PVOutputSource:
    """One value PVOutput can be fed with.

    ``valid`` rejects readings that are really "no data" (0 V from a sleeping
    meter, 0 % SoC, 0 degC from a sleeping inverter). A rejected value is left
    out of the upload rather than sent as a misleading 0.
    """

    key: str
    kind: SourceKind
    getter: Getter
    valid: Validator | None = None


_KWH: Final = 1000.0  # kWh -> Wh
_KW: Final = 1000.0  # kW -> W
#: Below this a grid voltage reading is a sleeping meter, not a real grid.
_MIN_GRID_VOLTAGE: Final = 90.0

#: Every selectable source. Keys are stored in the options and translated in
#: strings.json (selector.pvoutput_source).
PVOUTPUT_SOURCES: Final[dict[str, PVOutputSource]] = {
    s.key: s
    for s in (
        # Energy (Wh)
        PVOutputSource(
            "pv_daily_energy", "energy", _inv("mb_daily_pv_generation", _KWH)
        ),
        PVOutputSource(
            "pv_lifetime_energy",
            "lifetime_energy",
            _inv("mb_total_pv_generation", _KWH),
        ),
        PVOutputSource(
            "daily_consumption", "energy", _derived("daily_consumption", _KWH)
        ),
        PVOutputSource("daily_import", "energy", _meter("daily_import", _KWH)),
        PVOutputSource("daily_export", "energy", _meter("daily_export", _KWH)),
        PVOutputSource(
            "daily_pv_export", "energy", _inv("mb_daily_export_from_pv", _KWH)
        ),
        PVOutputSource(
            "daily_battery_charge", "energy", _inv("mb_daily_battery_charge", _KWH)
        ),
        PVOutputSource(
            "daily_battery_discharge",
            "energy",
            _inv("mb_daily_battery_discharge", _KWH),
        ),
        PVOutputSource(
            "battery_usable_energy", "energy", _derived("battery_usable_energy", _KWH)
        ),
        PVOutputSource(
            "battery_usable_capacity", "energy", _battery_usable_capacity, _positive
        ),
        PVOutputSource(
            "battery_lifetime_charge",
            "lifetime_energy",
            _inv("mb_total_battery_charge", _KWH),
            _positive,
        ),
        PVOutputSource(
            "battery_lifetime_discharge",
            "lifetime_energy",
            _inv("mb_total_battery_discharge", _KWH),
            _positive,
        ),
        PVOutputSource(
            "battery_charge_cumulative",
            "lifetime_energy",
            _meter("battery_charge_cumulative", _KWH),
        ),
        # Power (W)
        PVOutputSource("pv_power", "power", _inv("mb_total_dc_power")),
        PVOutputSource("inverter_active_power", "power", _inv("mb_total_active_power")),
        PVOutputSource("load_power_ihm", "power", _ihm("ihm_total_load_power", _KW)),
        PVOutputSource("load_power_inverter", "power", _inv("mb_load_power")),
        PVOutputSource("battery_power", "power", _inv("mb_battery_power")),
        PVOutputSource("battery_net_charge_power", "power", _battery_net_charge),
        PVOutputSource(
            "battery_charging_power", "power", _derived("battery_charging_power")
        ),
        PVOutputSource(
            "battery_discharging_power", "power", _derived("battery_discharging_power")
        ),
        PVOutputSource("grid_power", "power", _ihm("ihm_grid_active_power", _KW)),
        PVOutputSource("grid_import_power", "power", _derived("active_import_power")),
        PVOutputSource("grid_export_power", "power", _derived("active_export_power")),
        PVOutputSource("ev_charger_power", "power", _ihm("ihm_ev_charger_power")),
        # Other
        PVOutputSource(
            "inverter_temperature",
            "temperature",
            _inv("mb_inverter_temperature"),
            _non_zero,
        ),
        PVOutputSource(
            "battery_temperature", "temperature", _inv("mb_battery_temperature")
        ),
        PVOutputSource(
            "grid_voltage_ihm",
            "voltage",
            _ihm("ihm_meter_voltage_a"),
            _at_least(_MIN_GRID_VOLTAGE),
        ),
        PVOutputSource(
            "grid_voltage_inverter",
            "voltage",
            _inv("mb_phase_a_voltage"),
            _at_least(_MIN_GRID_VOLTAGE),
        ),
        PVOutputSource("battery_voltage", "voltage", _inv("mb_battery_voltage")),
        PVOutputSource("grid_frequency", "frequency", _inv("mb_grid_frequency")),
        PVOutputSource("battery_soc", "percent", _inv("mb_battery_soc"), _positive),
        PVOutputSource(
            "battery_soc_ihm", "percent", _ihm("ihm_battery_soc"), _positive
        ),
        PVOutputSource(
            "self_sufficient_pct", "percent", _derived("self_sufficient_pct")
        ),
    )
}

#: "auto": the first source in the chain that has a valid value wins.
AUTO_SOURCES: Final[dict[str, tuple[str, ...]]] = {
    "v1": ("pv_daily_energy",),
    "v2": ("pv_power",),
    "v3": ("daily_consumption",),
    "v4": ("load_power_ihm", "load_power_inverter"),
    "v5": ("inverter_temperature",),
    "v6": ("grid_voltage_ihm", "grid_voltage_inverter"),
    # Battery (donation only). b4: the SH10RS reports a corrupt lifetime
    # charge counter (13041), which the register map rejects; b4 is then left
    # out unless "Total battery charge cumulative" is chosen instead.
    "b1": ("battery_net_charge_power",),
    "b2": ("battery_soc", "battery_soc_ihm"),
    "b3": ("battery_usable_capacity",),
    "b4": ("battery_lifetime_charge",),
    "b5": ("battery_lifetime_discharge",),
}
#: v1 under "auto" when the cumulative option is on.
AUTO_V1_CUMULATIVE: Final = ("pv_lifetime_energy",)

#: v1-v4 must not be negative; b1 and v7-v12 may be (signed power).
_NON_NEGATIVE: Final = frozenset({"v1", "v2", "v3", "v4", "b2", "b3", "b4", "b5"})

#: Parameters only accepted from donation accounts.
DONATION_PARAMS: Final = frozenset(
    (*PVOUTPUT_EXTENDED_PARAMS, *PVOUTPUT_BATTERY_PARAMS)
)


def option_key(param: str) -> str:
    """Options key holding the source for one parameter, e.g. ``pvoutput_v7``."""
    return f"{CONF_PVOUTPUT_V_PREFIX}{param}"


def is_entity_id(value: Any) -> bool:
    """A v7-v12 option holds an entity ID; anything else means "do not send"."""
    return isinstance(value, str) and "." in value and " " not in value


def _format(kind: SourceKind, value: float, *, non_negative: bool) -> int | float:
    if non_negative:
        value = max(value, 0.0)
    if kind in ("energy", "lifetime_energy", "power"):
        return round(value)
    if kind == "frequency":
        return round(value, 2)
    return round(value, 1)


def entity_value(hass: HomeAssistant | None, entity_id: str) -> int | float | None:
    """Numeric state of any Home Assistant entity, or None to leave it out.

    unknown / unavailable / non-numeric states are skipped, so a weather
    station that drops off never sends a false 0 to PVOutput.
    """
    if hass is None:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if value.is_integer():
        return int(value)
    return round(value, 3)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PVOutputSettings:
    """PVOutput options of one config entry."""

    api_key: str
    system_id: str
    tier: str
    upload_mode: str
    interval_mode: str
    status_interval: int
    daily_time: dt_time
    cumulative: bool
    send_values: str = DEFAULT_PVOUTPUT_SEND_VALUES
    #: v1-v6 and b1-b5 -> source key / "auto" / "none".
    mapping: dict[str, str] = field(default_factory=dict)
    #: v7-v12 -> entity ID (absent = do not send).
    extended: dict[str, str] = field(default_factory=dict)

    @property
    def donation(self) -> bool:
        return self.tier == PVOUTPUT_TIER_DONATION

    @property
    def params(self) -> tuple[str, ...]:
        """Standard parameters sent with each live status."""
        if self.send_values == PVOUTPUT_SEND_ENERGY:
            return PVOUTPUT_ENERGY_PARAMS
        return PVOUTPUT_POWER_PARAMS

    @property
    def interval(self) -> int:
        """Minutes between live uploads."""
        if self.interval_mode != PVOUTPUT_INTERVAL_AUTO:
            try:
                manual = int(self.interval_mode)
            except ValueError:
                manual = 0
            if manual in _VALID_INTERVALS:
                return manual
        if self.status_interval in _VALID_INTERVALS:
            return self.status_interval
        return DEFAULT_PVOUTPUT_STATUS_INTERVAL

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> PVOutputSettings:
        daily = dt_util.parse_time(
            str(options.get(CONF_PVOUTPUT_DAILY_TIME, DEFAULT_PVOUTPUT_DAILY_TIME))
        ) or dt_util.parse_time(DEFAULT_PVOUTPUT_DAILY_TIME)
        assert daily is not None
        mapping: dict[str, str] = {}
        for param in (*PVOUTPUT_STANDARD_PARAMS, *PVOUTPUT_BATTERY_PARAMS):
            mapping[param] = str(options.get(option_key(param), PVOUTPUT_SOURCE_AUTO))
        # v7-v12: entity IDs only. Older versions stored internal source keys
        # here; those are dropped (the default is now "do not send").
        extended = {
            param: str(options[option_key(param)])
            for param in PVOUTPUT_EXTENDED_PARAMS
            if is_entity_id(options.get(option_key(param)))
        }
        try:
            status_interval = int(
                options.get(
                    CONF_PVOUTPUT_STATUS_INTERVAL, DEFAULT_PVOUTPUT_STATUS_INTERVAL
                )
            )
        except (TypeError, ValueError):
            status_interval = DEFAULT_PVOUTPUT_STATUS_INTERVAL
        return cls(
            api_key=str(options.get(CONF_PVOUTPUT_API_KEY, "")),
            system_id=str(options.get(CONF_PVOUTPUT_SYSTEM_ID, "")),
            tier=str(options.get(CONF_PVOUTPUT_TIER, DEFAULT_PVOUTPUT_TIER)),
            upload_mode=str(
                options.get(CONF_PVOUTPUT_UPLOAD_MODE, DEFAULT_PVOUTPUT_UPLOAD_MODE)
            ),
            interval_mode=str(
                options.get(CONF_PVOUTPUT_INTERVAL_MODE, PVOUTPUT_INTERVAL_AUTO)
            ),
            status_interval=status_interval,
            daily_time=daily,
            cumulative=bool(options.get(CONF_PVOUTPUT_CUMULATIVE, False)),
            send_values=str(
                options.get(CONF_PVOUTPUT_SEND_VALUES, DEFAULT_PVOUTPUT_SEND_VALUES)
            ),
            mapping=mapping,
            extended=extended,
        )


# ---------------------------------------------------------------------------
# Payload building (pure, unit-testable)
# ---------------------------------------------------------------------------


def resolve_source(
    rt: SungrowEmsRuntimeData, param: str, choice: str, *, cumulative: bool = False
) -> tuple[str, int | float] | None:
    """(source key, formatted value) for one parameter, or None to omit it."""
    if choice == PVOUTPUT_SOURCE_NONE:
        return None
    if choice == PVOUTPUT_SOURCE_AUTO:
        chain = (
            AUTO_V1_CUMULATIVE
            if param == "v1" and cumulative
            else AUTO_SOURCES.get(param, ())
        )
    else:
        chain = (choice,)
    for key in chain:
        source = PVOUTPUT_SOURCES.get(key)
        if source is None:
            continue
        try:
            value = source.getter(rt)
        except Exception:  # a broken getter must never stop an upload
            _LOGGER.debug("PVOutput source %s failed", key, exc_info=True)
            value = None
        if value is None or not math.isfinite(value):
            continue
        if source.valid is not None and not source.valid(value):
            continue
        return key, _format(source.kind, value, non_negative=param in _NON_NEGATIVE)
    return None


def _is_lifetime(key: str | None) -> bool:
    source = PVOUTPUT_SOURCES.get(key or "")
    return source is not None and source.kind == "lifetime_energy"


def build_status_payload(
    rt: SungrowEmsRuntimeData,
    settings: PVOutputSettings,
    hass: HomeAssistant | None = None,
    *,
    include_donation: bool = True,
) -> tuple[dict[str, int | float], dict[str, str]]:
    """Add Status parameters (without d/t) and the sources that fed them.

    Unavailable values are left out. Only one pair is sent: v2 + v4 (power,
    default) or v1 + v3 (energy). ``c1`` is set from the sources actually
    used: 1 when v1 and v3 are both lifetime counters, 2 when only v1 is, 3
    when only v3 is. Daily totals that reset at midnight need no flag.

    Donation accounts (and ``include_donation``) also get v7-v12 from the
    chosen Home Assistant entities and b1-b5 battery values. PVOutput ignores
    b2-b6 without b1, so the battery group is dropped when b1 is missing.
    """
    payload: dict[str, int | float] = {}
    used: dict[str, str] = {}
    for param in settings.params:
        resolved = resolve_source(
            rt,
            param,
            settings.mapping.get(param, PVOUTPUT_SOURCE_NONE),
            cumulative=settings.cumulative,
        )
        if resolved is None:
            continue
        used[param], payload[param] = resolved

    if settings.donation and include_donation:
        for param in PVOUTPUT_EXTENDED_PARAMS:
            entity_id = settings.extended.get(param)
            if not entity_id:
                continue
            value = entity_value(hass, entity_id)
            if value is not None:
                used[param], payload[param] = entity_id, value
        battery: dict[str, int | float] = {}
        battery_used: dict[str, str] = {}
        for param in PVOUTPUT_BATTERY_PARAMS:
            resolved = resolve_source(
                rt, param, settings.mapping.get(param, PVOUTPUT_SOURCE_AUTO)
            )
            if resolved is not None:
                battery_used[param], battery[param] = resolved
        if "b1" in battery:
            payload.update(battery)
            used.update(battery_used)

    v1_life = _is_lifetime(used.get("v1"))
    v3_life = _is_lifetime(used.get("v3"))
    if v1_life and v3_life:
        payload["c1"] = 1
    elif v1_life:
        payload["c1"] = 2
    elif v3_life:
        payload["c1"] = 3
    return payload, used


def strip_donation_params(
    payload: Mapping[str, Any], used: Mapping[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """The payload without v7-v12 / b1-b5 (for a lapsed donation)."""
    return (
        {k: v for k, v in payload.items() if k not in DONATION_PARAMS},
        {k: v for k, v in used.items() if k not in DONATION_PARAMS},
    )


def has_donation_params(payload: Mapping[str, Any]) -> bool:
    return any(k in DONATION_PARAMS for k in payload)


def build_output_payload(
    rt: SungrowEmsRuntimeData, peak: tuple[float, datetime] | None
) -> dict[str, int | str]:
    """Add Output parameters (without d).

    g = generated Wh, c = consumed Wh, e = exported Wh, pp/pt = peak PV power
    (W) and the local time it happened. Missing values are left out.
    """
    payload: dict[str, int | str] = {}
    for param, key in (
        ("g", "pv_daily_energy"),
        ("c", "daily_consumption"),
        ("e", "daily_export"),
    ):
        resolved = resolve_source(rt, "v1", key)  # "v1": clamps to >= 0
        if resolved is not None:
            payload[param] = int(resolved[1])
    if peak is not None and peak[0] > 0:
        payload["pp"] = round(peak[0])
        payload["pt"] = dt_util.as_local(peak[1]).strftime("%H:%M")
    return payload


# ---------------------------------------------------------------------------
# Uploader (scheduling, state, logging)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PVOutputResult:
    """Outcome of one upload attempt."""

    ok: bool
    service: str
    attempted: datetime
    status: int | None = None
    message: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "service": self.service,
            "attempted": self.attempted.isoformat(),
            "status": self.status,
            "message": self.message,
            "payload": dict(self.payload),
            "sources": dict(self.sources),
        }


class PVOutputUploader:
    """Schedules uploads for one config entry and keeps the last result."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SungrowEmsConfigEntry,
        runtime: SungrowEmsRuntimeData,
        settings: PVOutputSettings,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.runtime = runtime
        self.settings = settings
        self.client = PVOutputClient(hass, settings.api_key, settings.system_id)
        self.interval = settings.interval

        self.last_success: datetime | None = None
        self.last_result: PVOutputResult | None = None
        self.rate_limit: int | None = None
        self.rate_remaining: int | None = None
        self.blocked_until: datetime | None = None
        self.uploads_ok = 0
        self.uploads_failed = 0

        #: Donation mode as PVOutput reports it: from the hourly limit in every
        #: answer (300 = donation), or False after a donation error.
        self.donation_active: bool | None = None
        #: PVOutput's own error text when it refused donation-only values.
        self.donation_error: str | None = None
        self.donation_checked: datetime | None = None

        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unsub_schedule: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._last_logged_error: str | None = None
        self._peak: tuple[float, datetime] | None = None
        self._peak_day: date | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def live(self) -> bool:
        return self.settings.upload_mode != PVOUTPUT_MODE_DAILY

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start the schedule. Returns the unsubscriber for entry.async_on_unload."""
        self._schedule()
        if not self.live:
            self._unsubs.append(
                self.runtime.inverter.async_add_listener(self._async_track_peak)
            )
        # One getsystem.jsp at start: donation status (from the hourly limit)
        # and, in auto mode, a Status Interval changed on pvoutput.org.
        self.entry.async_create_background_task(
            self.hass,
            self._async_check_account(),
            f"{self.entry.domain} pvoutput account check",
        )
        _LOGGER.debug(
            "PVOutput uploads enabled for system %s: %s",
            self.settings.system_id,
            f"live every {self.interval} min"
            if self.live
            else f"daily at {self.settings.daily_time}",
        )

        @callback
        def _stop() -> None:
            if self._unsub_schedule is not None:
                self._unsub_schedule()
                self._unsub_schedule = None
            for unsub in self._unsubs:
                unsub()
            self._unsubs.clear()
            self._listeners.clear()

        return _stop

    @callback
    def _schedule(self) -> None:
        if self._unsub_schedule is not None:
            self._unsub_schedule()
        if self.live:
            self._unsub_schedule = async_track_time_change(
                self.hass,
                self._async_on_tick,
                minute=list(range(0, 60, self.interval)),
                second=_LIVE_SECOND,
            )
        else:
            daily = self.settings.daily_time
            self._unsub_schedule = async_track_time_change(
                self.hass,
                self._async_on_tick,
                hour=daily.hour,
                minute=daily.minute,
                second=daily.second,
            )

    async def _async_check_account(self) -> None:
        try:
            system = await self.client.async_get_system()
        except PVOutputError as err:
            _LOGGER.debug("PVOutput: could not read the system profile: %s", err)
            return
        self._update_donation(system.rate_limit)
        if system.rate_limit is not None:
            self.rate_limit = system.rate_limit
        self._notify()
        if self.live and self.settings.interval_mode == PVOUTPUT_INTERVAL_AUTO:
            self._follow_interval(system.status_interval)

    @callback
    def _follow_interval(self, interval: int | None) -> None:
        if interval is None or interval == self.interval:
            return
        _LOGGER.info(
            "PVOutput Status Interval changed to %d min on pvoutput.org; following it",
            interval,
        )
        self.interval = interval
        self._schedule()
        # Remember it so the next start uses it straight away. Options updates
        # from here do not reload the entry.
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, CONF_PVOUTPUT_STATUS_INTERVAL: interval},
        )
        self._notify()

    # -- donation status ---------------------------------------------------------

    @callback
    def _update_donation(self, rate_limit: int | None) -> None:
        """Follow the hourly limit PVOutput reports (300 = donation mode)."""
        active = is_donation_limit(rate_limit)
        if active is None:
            return
        self.donation_checked = dt_util.utcnow()
        if active and self.donation_error is not None:
            _LOGGER.info("PVOutput: donation mode is active again")
        if active:
            self.donation_error = None
        elif self.donation_active is not False and self.settings.donation:
            _LOGGER.warning(
                "PVOutput: the account is not in donation mode (limit %d/hour); "
                "v7-v12 and battery values are not sent until it is",
                rate_limit,
            )
        self.donation_active = active

    @property
    def withhold_donation_params(self) -> bool:
        """Donation is configured but PVOutput says it is not active."""
        return self.settings.donation and (
            self.donation_error is not None or self.donation_active is False
        )

    @property
    def donation_status(self) -> str | None:
        """Shown state: PVOutput's error text, "Active", "Not active" or None."""
        if self.donation_error:
            return self.donation_error
        if self.donation_active is None:
            return None
        return "Active" if self.donation_active else "Not active"

    # -- listeners (diagnostic sensor) ----------------------------------------

    @callback
    def add_listener(self, listener: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    # -- peak tracking (daily mode) --------------------------------------------

    @callback
    def _async_track_peak(self) -> None:
        now = dt_util.now()
        if self._peak_day != now.date():
            self._peak_day = now.date()
            self._peak = None
        resolved = resolve_source(self.runtime, "v2", "pv_power")
        if resolved is None:
            return
        power = float(resolved[1])
        if self._peak is None or power > self._peak[0]:
            self._peak = (power, dt_util.utcnow())

    # -- uploads ---------------------------------------------------------------

    @callback
    def _async_on_tick(self, now: datetime) -> None:
        self.entry.async_create_background_task(
            self.hass, self.async_upload(), f"{self.entry.domain} pvoutput upload"
        )

    async def async_upload(self, *, manual: bool = False) -> PVOutputResult:
        """Send one status / daily output. Never raises; returns the result."""
        async with self._lock:
            try:
                result = await self._async_upload(manual=manual)
            except Exception as err:  # last line of defence: never propagate
                _LOGGER.exception("PVOutput: unexpected error during upload")
                result = PVOutputResult(
                    ok=False,
                    service="addstatus.jsp" if self.live else "addoutput.jsp",
                    attempted=dt_util.utcnow(),
                    message=f"Unexpected error: {err}",
                )
                self.uploads_failed += 1
            self.last_result = result
            self._notify()
            return result

    async def _async_upload(self, *, manual: bool) -> PVOutputResult:
        service = "addstatus.jsp" if self.live else "addoutput.jsp"
        attempted = dt_util.utcnow()

        if (
            not manual
            and self.blocked_until is not None
            and attempted < self.blocked_until
        ):
            _LOGGER.debug(
                "PVOutput: rate limited until %s, skipping this upload",
                self.blocked_until.isoformat(),
            )
            return PVOutputResult(
                ok=False,
                service=service,
                attempted=attempted,
                status=403,
                message=f"Skipped: rate limited until {self.blocked_until.isoformat()}",
            )

        sources: dict[str, str] = {}
        local = dt_util.now()
        if self.live:
            payload, sources = build_status_payload(
                self.runtime,
                self.settings,
                self.hass,
                include_donation=not self.withhold_donation_params,
            )
            if not any(p in payload for p in ("v1", "v2", "v3", "v4")):
                # PVOutput requires at least one of v1-v4.
                self.uploads_failed += 1
                self._log_failure(
                    "PVOutput: no generation or consumption values available "
                    "(inverter offline?); status not sent"
                )
                return PVOutputResult(
                    ok=False,
                    service=service,
                    attempted=attempted,
                    message="No v1-v4 values available",
                    payload=dict(payload),
                )
            # Timestamp of the slot (floored to the interval) in local time.
            slot = local.replace(
                minute=local.minute - local.minute % self.interval,
                second=0,
                microsecond=0,
            )
            stamp = {"d": slot.strftime("%Y%m%d"), "t": slot.strftime("%H:%M")}
            sent: dict[str, Any] = {**stamp, **payload}
            try:
                response = await self.client.async_send_status(payload, slot)
            except PVOutputRateLimitError as err:
                return self._rate_limited(service, attempted, err, sent, sources)
            except PVOutputError as err:
                if not (err.donation_required and has_donation_params(payload)):
                    return self._failed(service, attempted, err, sent, sources)
                # Donation lapsed: keep PVOutput's message for the status
                # sensor and send the standard values on their own straight
                # away, so the slot is not lost.
                self._donation_refused(err)
                payload, sources = strip_donation_params(payload, sources)
                sent = {**stamp, **payload}
                try:
                    response = await self.client.async_send_status(payload, slot)
                except PVOutputRateLimitError as retry_err:
                    return self._rate_limited(
                        service, attempted, retry_err, sent, sources
                    )
                except PVOutputError as retry_err:
                    return self._failed(service, attempted, retry_err, sent, sources)
            return self._succeeded(service, attempted, response, sent, sources)
        else:
            peak = self._peak if self._peak_day == local.date() else None
            out = build_output_payload(self.runtime, peak)
            if "g" not in out and "c" not in out:
                self.uploads_failed += 1
                self._log_failure(
                    "PVOutput: no daily generation or consumption available; "
                    "daily output not sent"
                )
                return PVOutputResult(
                    ok=False,
                    service=service,
                    attempted=attempted,
                    message="No daily generation or consumption available",
                    payload=dict(out),
                )
            daily_sent: dict[str, Any] = {"d": local.strftime("%Y%m%d"), **out}
            try:
                response = await self.client.async_send_daily_output(out, local.date())
            except PVOutputRateLimitError as err:
                return self._rate_limited(service, attempted, err, daily_sent, sources)
            except PVOutputError as err:
                return self._failed(service, attempted, err, daily_sent, sources)
            return self._succeeded(service, attempted, response, daily_sent, sources)

    def _succeeded(
        self,
        service: str,
        attempted: datetime,
        response: PVOutputResponse,
        sent: dict[str, Any],
        sources: dict[str, str],
    ) -> PVOutputResult:
        self.blocked_until = None
        self.rate_limit = response.rate_limit
        self.rate_remaining = response.rate_remaining
        self._update_donation(response.rate_limit)
        self.last_success = attempted
        self.uploads_ok += 1
        if self._last_logged_error is not None:
            _LOGGER.info("PVOutput: uploads working again")
            self._last_logged_error = None
        _LOGGER.debug("PVOutput %s ok: %s", service, sent)
        return PVOutputResult(
            ok=True,
            service=service,
            attempted=attempted,
            status=response.status,
            message=response.text or "OK",
            payload=sent,
            sources=sources,
        )

    def _rate_limited(
        self,
        service: str,
        attempted: datetime,
        err: PVOutputRateLimitError,
        sent: dict[str, Any],
        sources: dict[str, str],
    ) -> PVOutputResult:
        self.blocked_until = err.reset or (
            dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
            + timedelta(hours=1)
        )
        return self._failed(service, attempted, err, sent, sources)

    def _donation_refused(self, err: PVOutputError) -> None:
        message = (err.text or str(err)).strip()
        if message != self.donation_error:
            _LOGGER.warning(
                "PVOutput refused donation-only values (%s); sending the "
                "standard values without them until donation mode is active",
                message,
            )
        self.donation_error = message
        self.donation_active = False
        self.donation_checked = dt_util.utcnow()

    def _failed(
        self,
        service: str,
        attempted: datetime,
        err: PVOutputError,
        sent: dict[str, Any],
        sources: dict[str, str],
    ) -> PVOutputResult:
        self.uploads_failed += 1
        self._log_failure(f"PVOutput {service} failed: {err}")
        return PVOutputResult(
            ok=False,
            service=service,
            attempted=attempted,
            status=err.status,
            message=str(err),
            payload=sent,
            sources=sources,
        )

    def _log_failure(self, message: str) -> None:
        """Warn once per distinct problem, then stay at debug level."""
        if message != self._last_logged_error:
            _LOGGER.warning(message)
            self._last_logged_error = message
        else:
            _LOGGER.debug(message)

    # -- diagnostics -------------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        s = self.settings
        return {
            "tier": s.tier,
            "upload_mode": s.upload_mode,
            "interval_mode": s.interval_mode,
            "interval_minutes": self.interval if self.live else None,
            "daily_time": s.daily_time.isoformat() if not self.live else None,
            "cumulative": s.cumulative,
            "send_values": s.send_values,
            "mapping": {p: s.mapping.get(p) for p in s.params},
            "battery_mapping": {p: s.mapping.get(p) for p in PVOUTPUT_BATTERY_PARAMS}
            if s.donation
            else None,
            "extended_entities": dict(s.extended) if s.donation else None,
            "donation_status": self.donation_status,
            "donation_active": self.donation_active,
            "donation_params_withheld": self.withhold_donation_params,
            "last_success": self.last_success.isoformat()
            if self.last_success
            else None,
            "last_result": self.last_result.as_dict() if self.last_result else None,
            "uploads_ok": self.uploads_ok,
            "uploads_failed": self.uploads_failed,
            "rate_limit": self.rate_limit,
            "rate_remaining": self.rate_remaining,
            "blocked_until": self.blocked_until.isoformat()
            if self.blocked_until
            else None,
        }
