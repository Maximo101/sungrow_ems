"""Sungrow EMS: local Modbus TCP telemetry for Sungrow hybrid inverters and the iHomeManager.

Read-only by default: the integration polls input and holding registers
(FC04/FC03).

Control is opt-in ("Enable control" in the options, off by default). Then a
small, allowlisted set of settings can be written through one guarded path
(``writer.SafeWriter``): Backup reserve, iHM energy management mode, EV
charger mode, AC22 charge current (unverified), and a forced charge to a
target SoC that restores every setting afterwards (``force_charge.py``).
With control off there are no control entities and the control actions
refuse to run.

Optional: an uploader that sends the values already polled to PVOutput.org
(off by default; see pvoutput.py), with a ``pvoutput_push_now`` action to
send one upload on demand.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_METHOD,
    ATTR_PERCENT,
    ATTR_POWER_KW,
    ATTR_TARGET_SOC,
    ATTR_TIMEOUT_MINUTES,
    CONF_AC22_UNIT,
    CONF_CONTROL_ALLOW_UNVERIFIED,
    CONF_CONTROL_ENABLED,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_FAILURE_THRESHOLD,
    CONF_FORCE_CHARGE_TIMEOUT,
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
    CONF_PVOUTPUT_ENABLED,
    CONF_RETRIES,
    CONF_TIMEOUT,
    DATA_THREE_PHASE_ENABLED,
    DEFAULT_AC22_UNIT,
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_FORCE_CHARGE_TIMEOUT,
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
    MAX_FORCE_CHARGE_TIMEOUT,
    MIN_FORCE_CHARGE_TIMEOUT,
    MIN_IHM_REQUEST_INTERVAL,
    PLAN_RESET_SECONDS,
    PLATFORMS,
    RECONNECT_BACKOFF_MAX,
    SERVICE_FORCE_CHARGE_CANCEL,
    SERVICE_FORCE_CHARGE_START,
    SERVICE_PVOUTPUT_PUSH_NOW,
    SERVICE_SET_BACKUP_RESERVE,
    UNIQUE_ID_SUFFIX,
    UNSUPPORTED_RETRY_SECONDS,
)
from .controller import SungrowController
from .coordinator import (
    SungrowEmsConfigEntry,
    SungrowEmsCoordinator,
    SungrowEmsRuntimeData,
    get_option,
)
from .derived import DerivedEngine
from .force_charge import Method
from .modbus import SungrowModbusHub
from .profiles import THREE_PHASE_KEYS
from .pvoutput import PVOutputSettings, PVOutputUploader
from .registers import Device

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

_PUSH_NOW_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})
_SET_RESERVE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_PERCENT): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
    }
)
_FORCE_START_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_TARGET_SOC): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=100)
        ),
        vol.Optional(ATTR_POWER_KW): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=50)
        ),
        vol.Optional(ATTR_TIMEOUT_MINUTES): vol.All(
            vol.Coerce(int),
            vol.Range(min=MIN_FORCE_CHARGE_TIMEOUT, max=MAX_FORCE_CHARGE_TIMEOUT),
        ),
        vol.Optional(ATTR_METHOD): vol.In([str(m) for m in Method]),
    }
)
_FORCE_CANCEL_SCHEMA = vol.Schema({vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string})

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

#: Disabled by default from 0.1.10 when an iHM is configured: they need a meter
#: wired to the inverter (protocol note 1). Only the ones that used to be on
#: by default are touched; the rest of METER_DEPENDENT_KEYS was already off,
#: so if they are on now the user turned them on. Re-enabling is respected.
_DISABLED_IN_1_3_WITH_IHM = (
    "mb_total_export_from_pv",
    "mb_daily_direct_consumption",
    "mb_total_direct_consumption",
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


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions (once, not per entry)."""

    async def _async_push_now(call: ServiceCall) -> ServiceResponse:
        wanted: str | None = call.data.get(ATTR_CONFIG_ENTRY_ID)
        entries: list[SungrowEmsConfigEntry] = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
            and (wanted is None or entry.entry_id == wanted)
            and entry.runtime_data.pvoutput is not None
        ]
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="pvoutput_not_enabled",
            )
        results: dict[str, Any] = {}
        failures: list[str] = []
        for entry in entries:
            uploader = entry.runtime_data.pvoutput
            assert uploader is not None
            result = await uploader.async_upload(manual=True)
            results[entry.entry_id] = result.as_dict()
            if not result.ok:
                failures.append(f"{entry.title}: {result.message}")
        if failures and not call.return_response:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="pvoutput_push_failed",
                translation_placeholders={"error": "; ".join(failures)},
            )
        return {"results": results} if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        SERVICE_PVOUTPUT_PUSH_NOW,
        _async_push_now,
        schema=_PUSH_NOW_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    def _controllers(call: ServiceCall) -> list[SungrowController]:
        wanted: str | None = call.data.get(ATTR_CONFIG_ENTRY_ID)
        found = [
            entry.runtime_data.controller
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.state is ConfigEntryState.LOADED
            and (wanted is None or entry.entry_id == wanted)
            and entry.runtime_data.controller is not None
        ]
        if not found:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="control_not_enabled"
            )
        if len(found) > 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="control_ambiguous"
            )
        return [c for c in found if c is not None]

    async def _async_set_reserve(call: ServiceCall) -> None:
        controller = _controllers(call)[0]
        await controller.async_write(
            "backup_reserve_soc",
            int(call.data[ATTR_PERCENT]),
            bounds=controller.reserve_bounds(),
        )

    async def _async_force_start(call: ServiceCall) -> ServiceResponse:
        controller = _controllers(call)[0]
        session = await controller.force_charge.async_start(
            float(call.data[ATTR_TARGET_SOC]),
            power_kw=call.data.get(ATTR_POWER_KW),
            timeout_minutes=call.data.get(ATTR_TIMEOUT_MINUTES),
            method=call.data.get(ATTR_METHOD),
        )
        if not call.return_response:
            return None
        return {
            "method": session.method,
            "target_soc": session.target_soc,
            "power_kw": session.power_kw,
            "deadline": session.deadline,
            "previous_settings": dict(session.previous),
        }

    async def _async_force_cancel(call: ServiceCall) -> None:
        await _controllers(call)[0].force_charge.async_cancel()

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_BACKUP_RESERVE,
        _async_set_reserve,
        schema=_SET_RESERVE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_CHARGE_START,
        _async_force_start,
        schema=_FORCE_START_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_CHARGE_CANCEL,
        _async_force_cancel,
        schema=_FORCE_CANCEL_SCHEMA,
    )
    return True


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

    controllers: list[SungrowController] = []

    async def _async_close() -> None:
        # One callback, in order: on-unload callbacks run as concurrent tasks,
        # and a forced charge must be ended while the sockets are still open.
        for controller in controllers:
            try:
                await controller.async_shutdown()
            except Exception:
                _LOGGER.exception("Could not end the forced charge on unload")
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

    if get_option(entry, CONF_PVOUTPUT_ENABLED, False):
        # Optional cloud upload. It only reads the snapshots above; a PVOutput
        # problem is logged and never affects the coordinators or entities.
        runtime.pvoutput = PVOutputUploader(
            hass, entry, runtime, PVOutputSettings.from_options(entry.options)
        )
        entry.async_on_unload(runtime.pvoutput.async_start())

    _enable_three_phase_entities_once(hass, entry, inverter)

    if get_option(entry, CONF_CONTROL_ENABLED, False):
        runtime.controller = SungrowController(
            hass,
            entry,
            runtime,
            allow_unverified=bool(
                get_option(entry, CONF_CONTROL_ALLOW_UNVERIFIED, False)
            ),
            ac22_unit=int(get_option(entry, CONF_AC22_UNIT, DEFAULT_AC22_UNIT)),
            force_charge_timeout=int(
                get_option(
                    entry, CONF_FORCE_CHARGE_TIMEOUT, DEFAULT_FORCE_CHARGE_TIMEOUT
                )
            ),
        )
        # Restores the settings of a forced charge that was running when
        # Home Assistant stopped.
        await runtime.controller.async_setup()
        controllers.append(runtime.controller)
    _remove_stale_control_entities(hass, entry, runtime)

    async def _async_on_stop(_event: Event) -> None:
        # Entries are not unloaded on shutdown. End a forced charge first
        # (the iHM's Compulsory mode has no heartbeat), then free the iHM's
        # TCP slot.
        if runtime.controller is not None:
            try:
                await runtime.controller.async_shutdown()
            except Exception:
                _LOGGER.exception("Could not end the forced charge on shutdown")
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
        _disable_entities(hass, entry, _DISABLED_IN_1_2)
        hass.config_entries.async_update_entry(entry, title=title, minor_version=2)
        _LOGGER.debug("Migrated %s to version 1.2", entry.title)
    if entry.version == 1 and entry.minor_version < 3:
        # 1.3: behind an iHM, disable the inverter's meter-based counters that
        # were still enabled by default (they read 0 or a frozen total).
        if entry.data.get(CONF_IHM_ENABLED) and entry.data.get(CONF_IHM_HOST):
            _disable_entities(hass, entry, _DISABLED_IN_1_3_WITH_IHM)
        hass.config_entries.async_update_entry(entry, minor_version=3)
        _LOGGER.debug("Migrated %s to version 1.3", entry.title)
    if entry.version == 1 and entry.minor_version < 4:
        # 1.4 (0.2.0): Daily consumption is an energy balance without a
        # discharge efficiency factor, so the option is retired.
        options = {
            k: v for k, v in entry.options.items() if k != CONF_DISCHARGE_EFFICIENCY
        }
        hass.config_entries.async_update_entry(entry, options=options, minor_version=4)
        _LOGGER.debug("Migrated %s to version 1.4", entry.title)
    return True


#: (platform, key) of every control entity.
_CONTROL_ENTITIES: tuple[tuple[str, str], ...] = (
    ("number", "backup_reserve_soc_control"),
    ("number", "ac22_charge_current_control"),
    ("select", "ihm_ems_mode_control"),
    ("select", "ihm_charger_mode_control"),
    ("sensor", "force_charge_state"),
)


def _remove_stale_control_entities(
    hass: HomeAssistant, entry: SungrowEmsConfigEntry, runtime: SungrowEmsRuntimeData
) -> None:
    """Drop control entities that this setup will not create (control turned
    off, no AC22 unit, no iHM) instead of leaving them orphaned."""
    controller = runtime.controller
    keep: set[str] = set()
    if controller is not None:
        keep = {"backup_reserve_soc_control", "force_charge_state"}
        if controller.has_ac22:
            keep.add("ac22_charge_current_control")
        if runtime.ihm is not None:
            keep |= {"ihm_ems_mode_control", "ihm_charger_mode_control"}
    registry = er.async_get(hass)
    for platform, key in _CONTROL_ENTITIES:
        if key in keep:
            continue
        entity_id = registry.async_get_entity_id(
            platform, DOMAIN, f"{entry.entry_id}_{key}{UNIQUE_ID_SUFFIX}"
        )
        if entity_id is not None:
            registry.async_remove(entity_id)


def _enable_three_phase_entities_once(
    hass: HomeAssistant, entry: SungrowEmsConfigEntry, inverter: SungrowEmsCoordinator
) -> None:
    """Enable the phase B/C sensors once when a three-phase inverter appears.

    On a single-phase model they read 0 and were created disabled. After a
    swap to a three-phase model (SH10RS -> SH20T) they carry data, so they are
    enabled once. A flag in the entry data makes sure a user who disables
    them again is respected.
    """
    if not inverter.profile.three_phase or entry.data.get(DATA_THREE_PHASE_ENABLED):
        return
    registry = er.async_get(hass)
    for key in THREE_PHASE_KEYS:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{key}{UNIQUE_ID_SUFFIX}"
        )
        reg_entry = registry.async_get(entity_id) if entity_id else None
        if (
            reg_entry is not None
            and reg_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        ):
            registry.async_update_entity(reg_entry.entity_id, disabled_by=None)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, DATA_THREE_PHASE_ENABLED: True}
    )
    _LOGGER.info(
        "Three-phase inverter (%s): phase B/C sensors enabled", inverter.profile.model
    )


def _disable_entities(
    hass: HomeAssistant, entry: SungrowEmsConfigEntry, keys: tuple[str, ...]
) -> None:
    """Disable these register sensors once, if they exist and are enabled."""
    registry = er.async_get(hass)
    for key in keys:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_{key}{UNIQUE_ID_SUFFIX}"
        )
        reg_entry = registry.async_get(entity_id) if entity_id else None
        if reg_entry is not None and reg_entry.disabled_by is None:
            registry.async_update_entity(
                reg_entry.entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )


async def async_unload_entry(hass: HomeAssistant, entry: SungrowEmsConfigEntry) -> bool:
    """Unload a config entry. The sockets are closed by the on-unload callback."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
