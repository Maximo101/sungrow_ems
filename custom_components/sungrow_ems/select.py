"""Select entities (control, opt-in): iHM energy management mode, EV charger mode.

Only created when "Enable control" is on and an iHomeManager is configured.
Writes go through the controller's SafeWriter.

Energy management mode (iHM 8024): the app shows AI mode, Self-consumption,
Time plan and Forced mode. Only Self-consumption (1) and Compulsory (5, the
app's Forced mode) have documented codes. Any other code read back is shown
as unknown (the raw code is in the attributes) and cannot be chosen here.

EV charger mode (iHM 8048): Fast / ECO & fast / ECO & economic were read on
site for each app mode. Time plan (0xA7) is listed so it can be shown, but
choosing it needs "Allow unverified writes": it is not known whether writing
it restores the schedule saved in the app.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .controller import SungrowController
from .coordinator import SungrowEmsConfigEntry, SungrowEmsCoordinator
from .entity import SungrowEmsEntity
from .profiles import IHM_APP_EMS_MODES, IHM_EV_CHARGER_MODES

PARALLEL_UPDATES = 1

EMS_MODE_KEY = "ihm_ems_mode_control"
EV_CHARGER_MODE_KEY = "ihm_charger_mode_control"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SungrowEmsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    runtime = entry.runtime_data
    controller = runtime.controller
    if controller is None or runtime.ihm is None:
        return
    async_add_entities(
        [
            CodeSelect(
                runtime.ihm,
                controller,
                key=EMS_MODE_KEY,
                name="Energy management mode",
                read_key="ihm_ems_mode",
                write_key="ihm_ems_mode",
                labels=IHM_APP_EMS_MODES,
                icon="mdi:home-lightning-bolt",
            ),
            CodeSelect(
                runtime.ihm,
                controller,
                key=EV_CHARGER_MODE_KEY,
                name="EV charger mode",
                read_key="ihm_charger_mode",
                write_key="ihm_charger_mode",
                labels=IHM_EV_CHARGER_MODES,
                icon="mdi:ev-station",
            ),
        ]
    )


class CodeSelect(SungrowEmsEntity, SelectEntity):
    """Select backed by an enum holding register."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SungrowEmsCoordinator,
        controller: SungrowController,
        *,
        key: str,
        name: str,
        read_key: str,
        write_key: str,
        labels: Mapping[int, str],
        icon: str,
    ) -> None:
        super().__init__(coordinator, key, name)
        self._controller = controller
        self._read_key = read_key
        self._write_key = write_key
        self._labels = dict(labels)
        self._codes = {label: code for code, label in labels.items()}
        self._attr_options = list(labels.values())
        self._attr_icon = icon

    def _raw(self) -> int | None:
        data = self.coordinator.data
        if data is None:
            return None
        raw = data.raw.get(self._read_key)
        return int(raw) if raw is not None else None

    @property
    def current_option(self) -> str | None:
        raw = self._raw()
        return self._labels.get(raw) if raw is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        raw = self._raw()
        return {"raw_code": raw, "raw_hex": f"0x{raw:02X}" if raw is not None else None}

    async def async_select_option(self, option: str) -> None:
        code = self._codes.get(option)
        if code is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="write_refused",
                translation_placeholders={"error": f"unknown option {option}"},
            )
        if self._write_key == "ihm_ems_mode" and self._controller.force_charge.active:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="force_charge_invalid",
                translation_placeholders={
                    "error": "a forced charge controls the EMS mode; cancel it first"
                },
            )
        await self._controller.async_write(self._write_key, code)
