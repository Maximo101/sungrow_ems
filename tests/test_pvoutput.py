"""Optional PVOutput.org uploader: flows, payloads, scheduling and isolation."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.sungrow_ems.const import (
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
    DOMAIN,
    PVOUTPUT_BASE_URL,
)
from custom_components.sungrow_ems.derived import format_hours_minutes
from custom_components.sungrow_ems.pvoutput import option_key, parse_getsystem

from .conftest import ENTRY_DATA, SERIAL, FakeDevice

GETSYSTEM = f"{PVOUTPUT_BASE_URL}/getsystem.jsp"
ADDSTATUS = f"{PVOUTPUT_BASE_URL}/addstatus.jsp"
ADDOUTPUT = f"{PVOUTPUT_BASE_URL}/addoutput.jsp"
#: Status Interval is field 16 of the first section.
SYSTEM_CSV = (
    "My Roof,13200,3000,24,550,Brand,1,10000,Sungrow,N,20,No,20230101,-37.8,145.0,10;;0"
)
LAST_UPLOAD = "sensor.sungrow_ems_inverter_pvoutput_last_upload"
DONATION_STATUS = "sensor.sungrow_ems_inverter_pvoutput_donation_status"
FREE_LIMIT = {"X-Rate-Limit-Limit": "60", "X-Rate-Limit-Remaining": "59"}
DONATION_LIMIT = {"X-Rate-Limit-Limit": "300", "X-Rate-Limit-Remaining": "299"}
DONATION_ERROR = "Bad request 400: Donation mode required for extended data"

PV_OPTIONS: dict[str, Any] = {
    CONF_PVOUTPUT_ENABLED: True,
    CONF_PVOUTPUT_API_KEY: "key123",
    CONF_PVOUTPUT_SYSTEM_ID: "4242",
    CONF_PVOUTPUT_TIER: "free",
    CONF_PVOUTPUT_UPLOAD_MODE: "live",
    CONF_PVOUTPUT_INTERVAL_MODE: "5",
    CONF_PVOUTPUT_STATUS_INTERVAL: 5,
    CONF_PVOUTPUT_CUMULATIVE: False,
}


@pytest.fixture(autouse=True)
def getsystem_free(aioclient_mock: AiohttpClientMocker) -> None:
    """The uploader reads getsystem.jsp once at start (donation status)."""
    aioclient_mock.get(GETSYSTEM, text=SYSTEM_CSV, headers=FREE_LIMIT)


def _calls(aioclient_mock: AiohttpClientMocker, url: str) -> list[tuple]:
    return [c for c in aioclient_mock.mock_calls if str(c[1]) == url]


def _form(call: tuple) -> dict[str, str]:
    """Body of a recorded aioclient_mock call as a dict."""
    data = call[2]
    if isinstance(data, dict):
        return {k: str(v) for k, v in data.items()}
    return dict(parse_qsl(data.decode() if isinstance(data, bytes) else data))


async def _setup(hass: HomeAssistant, options: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(ENTRY_DATA),
        options=options,
        unique_id=SERIAL,
        version=1,
        minor_version=2,
        title="Sungrow SH10RS",
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
def seeded(inverter: FakeDevice, ihm: FakeDevice) -> None:
    inverter.set("mb_daily_pv_generation", 254)  # 25.4 kWh
    inverter.set("mb_total_pv_generation", 123456)  # 12345.6 kWh
    inverter.set("mb_inverter_temperature", 412)  # 41.2 °C
    inverter.set("mb_phase_a_voltage", 2431)  # 243.1 V
    ihm.set("ihm_total_load_power", 156)  # 1.56 kW
    ihm.set("ihm_meter_voltage_a", 2445)  # 244.5 V
    inverter.set("mb_total_battery_discharge", 52345)  # 5234.5 kWh


# -- parsing --------------------------------------------------------------------


def test_parse_getsystem() -> None:
    system = parse_getsystem(SYSTEM_CSV)
    assert (system.name, system.status_interval) == ("My Roof", 10)
    assert parse_getsystem("short,csv").status_interval is None
    assert parse_getsystem("").status_interval is None


# -- config flow ------------------------------------------------------------------


async def test_setup_flow_with_pvoutput_live_free(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, CONF_PVOUTPUT_ENABLED: True}
    )
    assert result["step_id"] == "pvoutput_account"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PVOUTPUT_API_KEY: " key123 ",
            CONF_PVOUTPUT_SYSTEM_ID: "4242",
            CONF_PVOUTPUT_TIER: "free",
            CONF_PVOUTPUT_UPLOAD_MODE: "live",
        },
    )
    headers = aioclient_mock.mock_calls[0][3]
    assert headers["X-Pvoutput-Apikey"] == "key123"
    assert headers["X-Pvoutput-SystemId"] == "4242"
    assert result["step_id"] == "pvoutput_live"
    assert result["description_placeholders"]["interval"] == "10"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PVOUTPUT_INTERVAL_MODE: "auto",
            CONF_PVOUTPUT_SEND_VALUES: "power",
            CONF_PVOUTPUT_CUMULATIVE: False,
        },
    )
    assert result["step_id"] == "pvoutput_mapping"
    # Free account, power mode: v2, v4, v5, v6 only (no v1/v3, v7+, b*).
    fields = {str(k) for k in result["data_schema"].schema}
    assert fields == {option_key(p) for p in ("v2", "v4", "v5", "v6")}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {option_key(p): "auto" for p in ("v2", "v4", "v5", "v6")}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_PVOUTPUT_ENABLED not in result["data"]
    options = result["result"].options
    assert options[CONF_PVOUTPUT_ENABLED] is True
    assert options[CONF_PVOUTPUT_STATUS_INTERVAL] == 10
    assert options[CONF_PVOUTPUT_INTERVAL_MODE] == "auto"
    assert options[CONF_PVOUTPUT_SEND_VALUES] == "power"


async def test_setup_flow_donation_not_active(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Donation chosen, but PVOutput reports the free 60/hour limit."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, CONF_PVOUTPUT_ENABLED: True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PVOUTPUT_API_KEY: "key123",
            CONF_PVOUTPUT_SYSTEM_ID: "4242",
            CONF_PVOUTPUT_TIER: "donation",
            CONF_PVOUTPUT_UPLOAD_MODE: "live",
        },
    )
    assert result["errors"] == {"base": "pvoutput_donation_not_active"}


async def test_setup_flow_donation_mapping(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSYSTEM, text=SYSTEM_CSV, headers=DONATION_LIMIT)
    hass.states.async_set("sensor.ws_outdoor_temperature", "21.4")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, CONF_PVOUTPUT_ENABLED: True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PVOUTPUT_API_KEY: "key123",
            CONF_PVOUTPUT_SYSTEM_ID: "4242",
            CONF_PVOUTPUT_TIER: "donation",
            CONF_PVOUTPUT_UPLOAD_MODE: "live",
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PVOUTPUT_INTERVAL_MODE: "5",
            CONF_PVOUTPUT_SEND_VALUES: "energy",
            CONF_PVOUTPUT_CUMULATIVE: False,
        },
    )
    assert result["step_id"] == "pvoutput_mapping"
    schema = {str(k): k for k in result["data_schema"].schema}
    # Energy mode: v1/v3 instead of v2/v4; donation adds b1-b5 and v7-v12.
    assert option_key("v1") in schema and option_key("v2") not in schema
    assert all(option_key(f"b{i}") in schema for i in range(1, 6))
    assert all(option_key(f"v{i}") in schema for i in range(7, 13))
    # Battery values default to auto, v7-v12 to nothing (do not send).
    assert schema[option_key("b1")].description["suggested_value"] == "auto"
    assert "suggested_value" not in (schema[option_key("v7")].description or {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **{option_key(p): "auto" for p in ("v1", "v3", "v5", "v6")},
            **{option_key(f"b{i}"): "auto" for i in range(1, 6)},
            option_key("v7"): "sensor.ws_outdoor_temperature",
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = result["result"].options
    assert options[option_key("v7")] == "sensor.ws_outdoor_temperature"
    assert option_key("v8") not in options
    assert options[CONF_PVOUTPUT_SEND_VALUES] == "energy"


async def test_setup_flow_invalid_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSYSTEM, status=401, text="Unauthorized 401: Invalid API Key")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, CONF_PVOUTPUT_ENABLED: True}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_PVOUTPUT_API_KEY: "bad",
            CONF_PVOUTPUT_SYSTEM_ID: "1",
            CONF_PVOUTPUT_TIER: "free",
            CONF_PVOUTPUT_UPLOAD_MODE: "live",
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_options_flow_enables_daily_donation(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, inverter: FakeDevice
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSYSTEM, text=SYSTEM_CSV, headers=DONATION_LIMIT)
    entry = await _setup(hass, {})
    assert entry.runtime_data.pvoutput is None
    result = await hass.config_entries.options.async_init(entry.entry_id)
    user_input = {
        str(key): key.description["suggested_value"]
        for key in result["data_schema"].schema
    }
    user_input[CONF_PVOUTPUT_ENABLED] = True
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    assert result["step_id"] == "pvoutput_account"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PVOUTPUT_API_KEY: "key123",
            CONF_PVOUTPUT_SYSTEM_ID: "4242",
            CONF_PVOUTPUT_TIER: "donation",
            CONF_PVOUTPUT_UPLOAD_MODE: "daily",
        },
    )
    assert result["step_id"] == "pvoutput_daily"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_PVOUTPUT_DAILY_TIME: "23:50:00"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.state is ConfigEntryState.LOADED
    uploader = entry.runtime_data.pvoutput
    assert uploader is not None and not uploader.live
    assert uploader.settings.daily_time.isoformat() == "23:50:00"
    assert hass.states.get(LAST_UPLOAD) is not None
    assert hass.states.get(DONATION_STATUS).state == "Active"


# -- uploads ------------------------------------------------------------------------


@pytest.mark.usefixtures("seeded")
async def test_push_now_sends_status_in_watts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(ADDSTATUS, text="OK 200: Added Status", headers=FREE_LIMIT)
    await _setup(hass, PV_OPTIONS)
    response = await hass.services.async_call(
        DOMAIN, "pvoutput_push_now", {}, blocking=True, return_response=True
    )
    body = _form(aioclient_mock.mock_calls[-1])
    # Power mode (default): v2 + v4 only, PVOutput derives the energy.
    assert "v1" not in body and "v3" not in body
    assert body["v2"] == "4200"
    assert body["v4"] == "1560"  # iHM kW -> W
    assert body["v5"] == "41.2"
    assert body["v6"] == "244.5"  # iHM meter preferred
    assert "c1" not in body
    assert len(body["d"]) == 8 and len(body["t"]) == 5
    assert not any(k in body for k in ("v7", "v8", "v12", "b1"))  # free tier
    (result,) = response["results"].values()
    assert result["ok"] is True
    state = hass.states.get(LAST_UPLOAD)
    assert state.state not in ("unknown", STATE_UNAVAILABLE)
    assert state.attributes["last_status"] == 200
    assert hass.states.get(DONATION_STATUS).state == "Not active"


@pytest.mark.usefixtures("seeded")
async def test_energy_mode_and_cumulative(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(ADDSTATUS, text="OK 200")
    await _setup(
        hass,
        {
            **PV_OPTIONS,
            CONF_PVOUTPUT_SEND_VALUES: "energy",
            CONF_PVOUTPUT_CUMULATIVE: True,
        },
    )
    await hass.services.async_call(DOMAIN, "pvoutput_push_now", {}, blocking=True)
    body = _form(aioclient_mock.mock_calls[-1])
    assert body["v1"] == "12345600"  # lifetime counter in Wh
    assert body["c1"] == "2"  # only v1 is lifetime
    assert "v2" not in body and "v4" not in body


@pytest.mark.usefixtures("seeded")
async def test_donation_entities_and_battery_values(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, inverter: FakeDevice
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSYSTEM, text=SYSTEM_CSV, headers=DONATION_LIMIT)
    aioclient_mock.post(ADDSTATUS, text="OK 200", headers=DONATION_LIMIT)
    inverter.set("mb_battery_power", -1500)  # + discharge register: charging
    hass.states.async_set("sensor.ws_outdoor_temperature", "21.4")
    hass.states.async_set("sensor.ws_solar_radiation", "unavailable")
    await _setup(
        hass,
        {
            **PV_OPTIONS,
            CONF_PVOUTPUT_TIER: "donation",
            option_key("v7"): "sensor.ws_outdoor_temperature",
            option_key("v8"): "sensor.ws_solar_radiation",
            option_key("v9"): "battery_soc",  # pre-0.1.9 source key: ignored
            option_key("b4"): "none",
        },
    )
    await hass.services.async_call(DOMAIN, "pvoutput_push_now", {}, blocking=True)
    body = _form(_calls(aioclient_mock, ADDSTATUS)[-1])
    assert body["v7"] == "21.4"
    assert "v8" not in body  # unavailable: left out, not 0
    assert "v9" not in body
    assert body["b1"] == "1500"  # + charging for PVOutput
    assert body["b2"] == "65.5"
    assert int(body["b3"]) > 0  # capacity x SoC window, not a fixed value
    assert "b4" not in body
    assert body["b5"] == "5234500"
    assert hass.states.get(DONATION_STATUS).state == "Active"


@pytest.mark.usefixtures("seeded")
async def test_battery_group_needs_b1(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSYSTEM, text=SYSTEM_CSV, headers=DONATION_LIMIT)
    aioclient_mock.post(ADDSTATUS, text="OK 200", headers=DONATION_LIMIT)
    await _setup(
        hass, {**PV_OPTIONS, CONF_PVOUTPUT_TIER: "donation", option_key("b1"): "none"}
    )
    await hass.services.async_call(DOMAIN, "pvoutput_push_now", {}, blocking=True)
    body = _form(_calls(aioclient_mock, ADDSTATUS)[-1])
    assert not any(f"b{i}" in body for i in range(1, 7))


@pytest.mark.usefixtures("seeded")
async def test_zero_readings_are_left_out(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, inverter: FakeDevice
) -> None:
    """A sleeping inverter/meter reports 0: never sent as a real value."""
    inverter.set("mb_inverter_temperature", 0)
    inverter.set("mb_phase_a_voltage", 0)
    aioclient_mock.post(ADDSTATUS, text="OK 200")
    entry = await _setup(hass, PV_OPTIONS)
    entry.runtime_data.ihm.data.values.pop("ihm_meter_voltage_a", None)
    await hass.services.async_call(DOMAIN, "pvoutput_push_now", {}, blocking=True)
    body = _form(_calls(aioclient_mock, ADDSTATUS)[-1])
    assert "v5" not in body
    assert "v6" not in body
    assert body["v2"] == "4200"


@pytest.mark.usefixtures("seeded")
async def test_lapsed_donation_reports_error_and_still_uploads(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Donation expired: PVOutput's message is shown and the standard values
    are sent on their own straight away."""
    hass.states.async_set("sensor.ws_outdoor_temperature", "21.4")
    responses = iter(
        [
            {"status": 400, "text": DONATION_ERROR},
            {"status": 200, "text": "OK 200", "headers": FREE_LIMIT},
        ]
    )

    async def _addstatus(method: str, url: Any, data: Any) -> Any:
        from pytest_homeassistant_custom_component.test_util.aiohttp import (
            AiohttpClientMockResponse,
        )

        return AiohttpClientMockResponse(method, url, **next(responses))

    aioclient_mock.clear_requests()
    aioclient_mock.get(GETSYSTEM, text=SYSTEM_CSV)  # no limit header: unknown
    aioclient_mock.post(ADDSTATUS, side_effect=_addstatus)
    entry = await _setup(
        hass,
        {
            **PV_OPTIONS,
            CONF_PVOUTPUT_TIER: "donation",
            option_key("v7"): "sensor.ws_outdoor_temperature",
        },
    )
    assert hass.states.get(DONATION_STATUS).state == STATE_UNKNOWN
    response = await hass.services.async_call(
        DOMAIN, "pvoutput_push_now", {}, blocking=True, return_response=True
    )
    first, retry = (_form(c) for c in _calls(aioclient_mock, ADDSTATUS))
    assert first["v7"] == "21.4" and "b1" in first
    assert "v7" not in retry and "b1" not in retry and retry["v2"] == "4200"
    (result,) = response["results"].values()
    assert result["ok"] is True
    state = hass.states.get(DONATION_STATUS)
    assert state.state == DONATION_ERROR
    assert state.attributes["donation_values"] == "withheld"
    assert entry.runtime_data.pvoutput.withhold_donation_params


@pytest.mark.usefixtures("seeded")
async def test_failure_never_touches_the_sungrow_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(ADDSTATUS, status=500, text="boom")
    entry = await _setup(hass, PV_OPTIONS)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "pvoutput_push_now", {}, blocking=True)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.inverter.last_update_success
    assert hass.states.get("sensor.sungrow_ems_inverter_battery_soc").state == "65.5"
    state = hass.states.get(LAST_UPLOAD)
    assert state.state == "unknown"  # never succeeded
    assert state.attributes["last_status"] == 500


@pytest.mark.usefixtures("seeded")
async def test_schedule_is_aligned_and_rate_limit_pauses(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(
        ADDSTATUS, status=403, text="Forbidden 403: Exceeded number requests per hour"
    )
    entry = await _setup(hass, PV_OPTIONS)
    now = dt_util.utcnow()
    slot = now.replace(second=15, microsecond=0) + timedelta(minutes=5 - now.minute % 5)
    async_fire_time_changed(hass, slot)
    await hass.async_block_till_done()
    assert len(_calls(aioclient_mock, ADDSTATUS)) == 1
    uploader = entry.runtime_data.pvoutput
    assert uploader is not None and uploader.blocked_until is not None
    # Next slot inside the same hour: skipped, no HTTP call.
    if (slot + timedelta(minutes=5)) < uploader.blocked_until:
        async_fire_time_changed(hass, slot + timedelta(minutes=5))
        await hass.async_block_till_done()
        assert len(_calls(aioclient_mock, ADDSTATUS)) == 1


async def test_daily_output_payload(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    inverter: FakeDevice,
) -> None:
    inverter.set("mb_daily_pv_generation", 301)
    aioclient_mock.post(ADDOUTPUT, text="OK 200: Added Output")
    entry = await _setup(hass, {**PV_OPTIONS, CONF_PVOUTPUT_UPLOAD_MODE: "daily"})
    # A poll lets the uploader record the day's peak PV power.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()
    await hass.services.async_call(
        DOMAIN,
        "pvoutput_push_now",
        {"config_entry_id": entry.entry_id},
        blocking=True,
    )
    body = _form(aioclient_mock.mock_calls[-1])
    assert body["g"] == "30100"
    assert body["pp"] == "4200"
    assert len(body["pt"]) == 5
    assert "v1" not in body


async def test_disabled_means_no_entity_and_action_refuses(
    hass: HomeAssistant,
) -> None:
    entry = await _setup(hass, {})
    assert hass.states.get(LAST_UPLOAD) is None
    assert hass.states.get(DONATION_STATUS) is None
    for key in ("pvoutput_last_upload", "pvoutput_donation_status"):
        assert (
            er.async_get(hass).async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{key}_ems"
            )
            is None
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "pvoutput_push_now", {}, blocking=True)


def test_format_hours_minutes() -> None:
    assert format_hours_minutes(675) == "11h 15m"
    assert format_hours_minutes(6) == "0h 6m"
    assert format_hours_minutes(60) == "1h 0m"
    assert format_hours_minutes(None) is None
