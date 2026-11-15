"""Constants for the Sungrow EMS integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "sungrow_ems"
NAME: Final = "Sungrow EMS"
MANUFACTURER: Final = "Sungrow"

#: Appended to every entity unique_id so this integration can run beside the
#: original ``sungrow`` integration without registry collisions.
UNIQUE_ID_SUFFIX: Final = "_ems"

#: Device names. Distinct from the old integration's device names so new
#: entity IDs come out clean instead of getting a ``_2`` suffix.
DEVICE_NAME_INVERTER: Final = "Sungrow EMS Inverter"
DEVICE_NAME_IHM: Final = "Sungrow EMS iHomeManager"

#: Platforms set up by __init__.py. Phase 2 adds select, number and switch.
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
IHM_PORTS: Final[tuple[int, ...]] = (502, 503, 504)
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
#: capacity: if nothing reports one, Battery stored energy is unavailable.
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
# Coordinator data keys that are not registers
# ---------------------------------------------------------------------------
KEY_LAST_SUCCESS: Final = "last_success"
KEY_DATA_AGE: Final = "data_age"
KEY_CONSECUTIVE_FAILURES: Final = "consecutive_failures"
