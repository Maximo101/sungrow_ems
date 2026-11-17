"""Constants for the Sungrow EMS integration (read-only Modbus TCP telemetry)."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "sungrow_ems"
MANUFACTURER: Final = "Sungrow"

#: Appended to every entity unique_id so this integration can run beside the
#: original ``sungrow`` integration without registry collisions.
UNIQUE_ID_SUFFIX: Final = "_ems"

#: Device names. The inverter is named after its model as read from the
#: device type code (e.g. "SH10RS"); DEVICE_NAME_INVERTER is only used until
#: the model is known or when the code is not in the protocol's table.
#: Home Assistant shows entities as "<device name> <entity name>", e.g.
#: "SH10RS Daily PV generation" and "iHM Battery SoC".
DEVICE_NAME_INVERTER: Final = "Inverter"
DEVICE_NAME_IHM: Final = "iHM"

#: Entity ID prefix per device. Entity IDs are set from these rather than
#: derived from the device name, so they stay
#: ``sensor.sungrow_ems_inverter_*`` / ``sensor.sungrow_ems_ihomemanager_*``
#: (as in every earlier version) whatever the devices are called and
#: whichever area they are in. Existing installs keep the entity IDs stored
#: in the entity registry in any case.
ENTITY_ID_PREFIX_INVERTER: Final = "sungrow_ems_inverter"
ENTITY_ID_PREFIX_IHM: Final = "sungrow_ems_ihomemanager"

#: Platforms set up by __init__.py. Read-only: sensors only. There are no
#: control platforms (select, number, switch) or services at this stage.
PLATFORMS: Final[list[Platform]] = [Platform.BINARY_SENSOR, Platform.SENSOR]

# ---------------------------------------------------------------------------
# Config entry data: connection settings (config flow / reconfigure)
# ---------------------------------------------------------------------------
CONF_INVERTER_HOST: Final = "inverter_host"
CONF_INVERTER_PORT: Final = "inverter_port"
CONF_INVERTER_DEVICE_ID: Final = "inverter_device_id"

CONF_IHM_ENABLED: Final = "ihm_enabled"
CONF_IHM_HOST: Final = "ihm_host"
CONF_IHM_PORT: Final = "ihm_port"
CONF_IHM_DEVICE_ID: Final = "ihm_device_id"

DEFAULT_INVERTER_PORT: Final = 502
DEFAULT_INVERTER_DEVICE_ID: Final = 1

#: iHM listens on 502, 503 and 504, one TCP connection per port ([IHM] 1.2).
#: 503 by default; set 504 while another client still holds 503.
DEFAULT_IHM_PORT: Final = 503
#: [IHM] 1: "The iHM Communication Address is 247" (not changeable).
DEFAULT_IHM_DEVICE_ID: Final = 247

# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------
CONF_INVERTER_SCAN_INTERVAL: Final = "inverter_scan_interval"
CONF_IHM_SCAN_INTERVAL: Final = "ihm_scan_interval"
CONF_TIMEOUT: Final = "timeout"
CONF_RETRIES: Final = "retries"
CONF_IHM_REQUEST_INTERVAL: Final = "ihm_request_interval"
CONF_FAILURE_THRESHOLD: Final = "failure_threshold"
CONF_BATTERY_CAPACITY: Final = "battery_capacity"
CONF_DISCHARGE_EFFICIENCY: Final = "discharge_efficiency"
CONF_SPIKE_MAX_JUMP: Final = "spike_max_jump"
CONF_SPIKE_MAX_DAILY: Final = "spike_max_daily"
CONF_SPIKE_REBASELINE_COUNT: Final = "spike_rebaseline_count"

# Poll intervals, seconds. The iHM needs >= 1 s per request and does about
# 3 requests per fast poll, so 10 s is the realistic floor there.
DEFAULT_INVERTER_SCAN_INTERVAL: Final = 10
DEFAULT_IHM_SCAN_INTERVAL: Final = 10
MIN_SCAN_INTERVAL: Final = 5
MAX_SCAN_INTERVAL: Final = 300

#: Per-request response timeout, seconds.
DEFAULT_TIMEOUT: Final = 3.0
MIN_TIMEOUT: Final = 1.0
MAX_TIMEOUT: Final = 10.0

#: Extra attempts per request after the first (timeouts, dropped sockets,
#: busy/gateway exception codes).
DEFAULT_RETRIES: Final = 2
MAX_RETRIES: Final = 5

#: Minimum gap between iHM requests, seconds ([IHM] 1.2 item 6: 1000 ms).
DEFAULT_IHM_REQUEST_INTERVAL: Final = 1.0
MIN_IHM_REQUEST_INTERVAL: Final = 1.0
MAX_IHM_REQUEST_INTERVAL: Final = 5.0

#: The inverter (via WiNet-S) has no documented pacing requirement.
INVERTER_REQUEST_INTERVAL: Final = 0.0

#: Consecutive failed polls before a coordinator raises UpdateFailed and
#: entities go unavailable. Until then, last-known-good values are kept.
DEFAULT_FAILURE_THRESHOLD: Final = 6
MIN_FAILURE_THRESHOLD: Final = 1
MAX_FAILURE_THRESHOLD: Final = 60

# ---------------------------------------------------------------------------
# Energy model defaults (ported from configuration.yaml)
# ---------------------------------------------------------------------------
#: Option value. 0 = auto: use what the hardware reports (iHM 8147, then
#: inverter 13039, then inverter 5639). There is deliberately no hard-coded
#: capacity: if nothing reports one, Battery usable energy is unavailable.
DEFAULT_BATTERY_CAPACITY_OPTION: Final = 0.0
MAX_BATTERY_CAPACITY: Final = 200.0

#: DC-to-AC battery discharge efficiency used by Daily Consumption.
DEFAULT_DISCHARGE_EFFICIENCY: Final = 0.85

#: Grid Import/Export Safe: reject a single jump on a cumulative meter larger
#: than this (kWh).
DEFAULT_SPIKE_MAX_JUMP: Final = 50.0
#: Reject a daily total above this (kWh).
DEFAULT_SPIKE_MAX_DAILY: Final = 200.0
#: Accept a new baseline after this many consecutive, mutually consistent
#: readings (prevents the YAML version's permanent latch).
DEFAULT_SPIKE_REBASELINE_COUNT: Final = 3
MIN_SPIKE_REBASELINE_COUNT: Final = 2
MAX_SPIKE_REBASELINE_COUNT: Final = 30

#: Riemann integration (Total Battery Charge Cumulative): an interval longer
#: than this between samples is skipped rather than integrated, so a restart
#: or outage never adds "last power x downtime".
INTEGRATION_MAX_GAP_SECONDS: Final = 300.0

#: Battery time estimates average the battery power over this many seconds, so
#: a kettle or a passing cloud does not make the estimate jump on every poll.
BATTERY_POWER_AVERAGE_SECONDS: Final = 120.0
#: Below this average power (W) no time estimate is given (it would run to
#: hundreds of hours). Also the idle band for Battery status when the
#: inverter's power-flow bits (13001) are not available.
BATTERY_IDLE_POWER_W: Final = 50.0

# ---------------------------------------------------------------------------
# Hub internals
# ---------------------------------------------------------------------------
#: Seconds before retrying a register span that the device rejected.
UNSUPPORTED_RETRY_SECONDS: Final = 3600.0
#: Seconds before throwing away a learned block split and trying the full
#: block again (recovers from a transient rejection).
PLAN_RESET_SECONDS: Final = 6 * 3600.0
#: Ceiling for the exponential reconnect backoff, seconds.
RECONNECT_BACKOFF_MAX: Final = 60.0

# ---------------------------------------------------------------------------
# PVOutput.org uploader (optional, off by default). Stored in entry.options.
# This is the only part of the integration that talks to the internet; it
# only reads coordinator data and never touches the Modbus devices.
# ---------------------------------------------------------------------------
CONF_PVOUTPUT_ENABLED: Final = "pvoutput_enabled"
CONF_PVOUTPUT_API_KEY: Final = "pvoutput_api_key"
CONF_PVOUTPUT_SYSTEM_ID: Final = "pvoutput_system_id"
CONF_PVOUTPUT_TIER: Final = "pvoutput_tier"
CONF_PVOUTPUT_UPLOAD_MODE: Final = "pvoutput_upload_mode"
CONF_PVOUTPUT_INTERVAL_MODE: Final = "pvoutput_interval_mode"
#: Status Interval read from the PVOutput system profile (getsystem.jsp).
CONF_PVOUTPUT_STATUS_INTERVAL: Final = "pvoutput_status_interval"
CONF_PVOUTPUT_DAILY_TIME: Final = "pvoutput_daily_time"
#: Send v1 as the lifetime PV counter with c1=2 instead of today's total.
CONF_PVOUTPUT_CUMULATIVE: Final = "pvoutput_cumulative"
#: Source mapping per PVOutput parameter: "pvoutput_v1" ... "pvoutput_v12",
#: "pvoutput_b1" ... "pvoutput_b5".
CONF_PVOUTPUT_V_PREFIX: Final = "pvoutput_"
#: Live status sends either power (v2 + v4, recommended) or energy (v1 + v3).
CONF_PVOUTPUT_SEND_VALUES: Final = "pvoutput_send_values"

PVOUTPUT_TIER_FREE: Final = "free"
PVOUTPUT_TIER_DONATION: Final = "donation"
PVOUTPUT_MODE_LIVE: Final = "live"
PVOUTPUT_MODE_DAILY: Final = "daily"
PVOUTPUT_INTERVAL_AUTO: Final = "auto"
PVOUTPUT_INTERVALS: Final[tuple[int, ...]] = (5, 10, 15)
PVOUTPUT_SEND_POWER: Final = "power"
PVOUTPUT_SEND_ENERGY: Final = "energy"

DEFAULT_PVOUTPUT_TIER: Final = PVOUTPUT_TIER_FREE
DEFAULT_PVOUTPUT_UPLOAD_MODE: Final = PVOUTPUT_MODE_LIVE
DEFAULT_PVOUTPUT_STATUS_INTERVAL: Final = 5
DEFAULT_PVOUTPUT_DAILY_TIME: Final = "23:55:00"
DEFAULT_PVOUTPUT_SEND_VALUES: Final = PVOUTPUT_SEND_POWER

#: Standard parameters (every account) and extended ones (donation only).
PVOUTPUT_STANDARD_PARAMS: Final[tuple[str, ...]] = ("v1", "v2", "v3", "v4", "v5", "v6")
#: Standard parameters sent per "send values" choice. PVOutput derives the
#: other pair itself, so only one of v1+v3 / v2+v4 is sent.
PVOUTPUT_POWER_PARAMS: Final[tuple[str, ...]] = ("v2", "v4", "v5", "v6")
PVOUTPUT_ENERGY_PARAMS: Final[tuple[str, ...]] = ("v1", "v3", "v5", "v6")
#: Donation only. v7-v12 are fed from any Home Assistant entity the user
#: picks (none by default); b1-b5 are battery values (auto by default).
PVOUTPUT_EXTENDED_PARAMS: Final[tuple[str, ...]] = (
    "v7",
    "v8",
    "v9",
    "v10",
    "v11",
    "v12",
)
PVOUTPUT_BATTERY_PARAMS: Final[tuple[str, ...]] = ("b1", "b2", "b3", "b4", "b5")
#: Mapping choices besides a source key.
PVOUTPUT_SOURCE_AUTO: Final = "auto"
PVOUTPUT_SOURCE_NONE: Final = "none"

#: X-Rate-Limit-Limit at or above this means the account is in donation mode
#: (60 requests/hour free, 300 donation).
PVOUTPUT_DONATION_RATE_LIMIT: Final = 300

PVOUTPUT_BASE_URL: Final = "https://pvoutput.org/service/r2"
PVOUTPUT_TIMEOUT: Final = 15.0

SERVICE_PVOUTPUT_PUSH_NOW: Final = "pvoutput_push_now"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
