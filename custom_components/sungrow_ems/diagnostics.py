"""Diagnostics for Sungrow EMS (Settings > Devices & services > Download diagnostics)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_IHM_HOST, CONF_INVERTER_HOST
from .coordinator import SungrowEmsConfigEntry

TO_REDACT = {
    CONF_INVERTER_HOST,
    CONF_IHM_HOST,
    "host",
    "unique_id",
    "mb_serial_number",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SungrowEmsConfigEntry
) -> dict[str, Any]:
    """Entry settings, poll health and the latest decoded values, redacted."""
    runtime = entry.runtime_data
    coordinators: dict[str, Any] = {}
    for coordinator in runtime.coordinators:
        data = coordinator.data
        coordinators[str(coordinator.device)] = {
            **coordinator.diagnostics(),
            "values": dict(data.values) if data is not None else {},
            "raw": dict(data.raw) if data is not None else {},
        }
    payload = {
        "entry": {
            "version": f"{entry.version}.{entry.minor_version}",
            "unique_id": entry.unique_id,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "coordinators": coordinators,
        "derived": dict(runtime.derived.values) if runtime.derived else {},
    }
    return async_redact_data(payload, TO_REDACT)
