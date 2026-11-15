"""Sungrow EMS: local Modbus TCP telemetry for Sungrow hybrid inverters and the iHomeManager.

Read-only by design. The integration polls input and holding registers
(FC04/FC03) and never writes to the inverter, battery, iHomeManager or EV
charger. Control entities and services are permanently out of scope.
"""

from __future__ import annotations

import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
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
    CONF_TIMEOUT,
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_IHM_DEVICE_ID,
    DEFAULT_IHM_PORT,
    DEFAULT_IHM_REQUEST_INTERVAL,
    DEFAULT_IHM_SCAN_INTERVAL,
    DEFAULT_INVERTER_DEVICE_ID,
    DEFAULT_INVERTER_PORT,
    DEFAULT_INVERTER_SCAN_INTERVAL,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    DOMAIN,
    INVERTER_REQUEST_INTERVAL,
    MIN_IHM_REQUEST_INTERVAL,
    PLAN_RESET_SECONDS,
    PLATFORMS,
    RECONNECT_BACKOFF_MAX,
    UNIQUE_ID_SUFFIX,
    UNSUPPORTED_RETRY_SECONDS,
)
from .coordinator import (
    SungrowEmsConfigEntry,
    SungrowEmsCoordinator,
    SungrowEmsRuntimeData,
    get_option,
)
from .derived import DerivedEngine
from .modbus import SungrowModbusHub
from .registers import Device

_LOGGER = logging.getLogger(__name__)

#: Entities disabled by default from 0.1.1 after live validation (they read 0
#: or garbage on an SH10RS behind an iHM). The 1.1 -> 1.2 migration disables
#: them once on existing installs; re-enabling them afterwards is respected.
_DISABLED_IN_1_2 = (
    "mb_daily_import_energy",
    "mb_total_import_energy",
    "mb_daily_export_energy",
    "mb_total_export_energy",
    "mb_self_consumption_today",
    "mb_total_battery_charge",
)


def _build_hub(
    entry: SungrowEmsConfigEntry,
    *,
    name: str,
    host: str,
    port: int,
    device_id: int,
    min_request_interval: float,
) -> SungrowModbusHub:
    return SungrowModbusHub(
        host=host,
        port=int(port),
        device_id=int(device_id),
        name=name,
        timeout=float(get_option(entry, CONF_TIMEOUT, DEFAULT_TIMEOUT)),
        retries=int(get_option(entry, CONF_RETRIES, DEFAULT_RETRIES)),
        min_request_interval=min_request_interval,
        reconnect_backoff_max=RECONNECT_BACKOFF_MAX,
        unsupported_retry=UNSUPPORTED_RETRY_SECONDS,
        plan_reset=PLAN_RESET_SECONDS,
    )


async def _async_close_hubs(hubs: list[SungrowModbusHub]) -> None:
    for hub in hubs:
        try:
            await hub.async_close()
        except Exception:
            _LOGGER.debug("Error closing %s", hub.name, exc_info=True)


async def async_setup_entry(hass: HomeAssistant, entry: SungrowEmsConfigEntry) -> bool:
    """Set up Sungrow EMS from a config entry."""
    data = entry.data
    failure_threshold = int(
        get_option(entry, CONF_FAILURE_THRESHOLD, DEFAULT_FAILURE_THRESHOLD)
    )

    inverter_hub = _build_hub(
        entry,
        name="inverter",
        host=data[CONF_INVERTER_HOST],
        port=data.get(CONF_INVERTER_PORT, DEFAULT_INVERTER_PORT),
        device_id=data.get(CONF_INVERTER_DEVICE_ID, DEFAULT_INVERTER_DEVICE_ID),
        min_request_interval=INVERTER_REQUEST_INTERVAL,
    )
    hubs: list[SungrowModbusHub] = [inverter_hub]

    async def _async_close() -> None:
        await _async_close_hubs(hubs)

    # Runs on unload AND when setup fails at any later step, so the sockets
    # (the iHM allows one TCP connection per port) are always released.
    entry.async_on_unload(_async_close)

    inverter = SungrowEmsCoordinator(
        hass,
        entry,
        inverter_hub,
        device=Device.INVERTER,
        scan_interval=float(
            get_option(
                entry, CONF_INVERTER_SCAN_INTERVAL, DEFAULT_INVERTER_SCAN_INTERVAL
            )
        ),
        failure_threshold=failure_threshold,
    )

    ihm: SungrowEmsCoordinator | None = None
    if data.get(CONF_IHM_ENABLED) and data.get(CONF_IHM_HOST):
        ihm_hub = _build_hub(
            entry,
            name="ihm",
            host=data[CONF_IHM_HOST],
            port=data.get(CONF_IHM_PORT, DEFAULT_IHM_PORT),
            device_id=data.get(CONF_IHM_DEVICE_ID, DEFAULT_IHM_DEVICE_ID),
            min_request_interval=max(
                MIN_IHM_REQUEST_INTERVAL,
                float(
                    get_option(
                        entry, CONF_IHM_REQUEST_INTERVAL, DEFAULT_IHM_REQUEST_INTERVAL
                    )
                ),
            ),
        )
        hubs.append(ihm_hub)
        ihm = SungrowEmsCoordinator(
            hass,
            entry,
            ihm_hub,
            device=Device.IHM,
            scan_interval=float(
                get_option(entry, CONF_IHM_SCAN_INTERVAL, DEFAULT_IHM_SCAN_INTERVAL)
            ),
            failure_threshold=failure_threshold,
        )

    # The inverter is required: without it nothing useful can be shown.
    # Raises ConfigEntryNotReady (Home Assistant retries with backoff).
    await inverter.async_config_entry_first_refresh()

    if ihm is not None:
        # The iHM is optional at startup: its entities start unavailable and
        # recover on their own once it answers.
        await ihm.async_refresh()
        if not ihm.last_update_success:
            _LOGGER.warning(
                "iHomeManager at %s:%s did not answer during setup; "
                "its entities stay unavailable until it does (%s)",
                data.get(CONF_IHM_HOST),
                data.get(CONF_IHM_PORT, DEFAULT_IHM_PORT),
                ihm.last_exception,
            )

    runtime = SungrowEmsRuntimeData(inverter=inverter, ihm=ihm, hubs=hubs)
    runtime.derived = DerivedEngine(hass, entry, inverter, ihm)
    entry.runtime_data = runtime
    entry.async_on_unload(runtime.derived.async_start())

    async def _async_on_stop(_event: Event) -> None:
        # Entries are not unloaded on shutdown: free the iHM's TCP slot now.
        await _async_close_hubs(hubs)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_on_stop)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_migrate_entry(
    hass: HomeAssistant, entry: SungrowEmsConfigEntry
) -> bool:
    """Migrate config entries created by older versions."""
    if entry.version > 1:
        return False
    if entry.version == 1 and entry.minor_version < 2:
        # 1.2: entry title without the serial number; disable the entities
        # that proved useless on live hardware.
        title = entry.title
        if entry.unique_id and title.endswith(f" {entry.unique_id}"):
            title = title.removesuffix(f" {entry.unique_id}")
        registry = er.async_get(hass)
        for key in _DISABLED_IN_1_2:
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_{key}{UNIQUE_ID_SUFFIX}"
            )
            reg_entry = registry.async_get(entity_id) if entity_id else None
            if reg_entry is not None and reg_entry.disabled_by is None:
                registry.async_update_entity(
                    reg_entry.entity_id,
                    disabled_by=er.RegistryEntryDisabler.INTEGRATION,
                )
        hass.config_entries.async_update_entry(entry, title=title, minor_version=2)
        _LOGGER.debug("Migrated %s to version 1.2", entry.title)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SungrowEmsConfigEntry) -> bool:
    """Unload a config entry. The sockets are closed by the on-unload callback."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
