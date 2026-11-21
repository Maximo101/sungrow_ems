"""The README must describe entities that really exist and say control is opt-in."""

from __future__ import annotations

from pathlib import Path
import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

README = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")


async def test_readme_entities_exist(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entity_ids = {
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }
    full = set(re.findall(r"`((?:binary_)?sensor\.sungrow_ems_\w+)`", README))
    short = set(re.findall(r"`…((?:inverter|ihomemanager)_\w+)`", README))
    assert full and short
    assert full <= entity_ids, sorted(full - entity_ids)
    suffixes = {e.split(".sungrow_ems_", 1)[1] for e in entity_ids}
    assert short <= suffixes, sorted(short - suffixes)


def test_readme_states_the_scope_and_keeps_its_images() -> None:
    assert "## Read-only by default\n" in README
    assert "does not write" in README
    assert "## Control (opt-in, advanced)\n" in README
    assert "Phase 2" not in README
    for image in ("logo.png", "marketplace.png", "ha1.png"):
        assert f"brand/{image}" in README
        assert (
            Path(__file__).parent.parent / "custom_components/sungrow_ems/brand" / image
        ).is_file()
