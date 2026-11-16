"""The bundled blueprints must load, and every input they use must exist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    PLATFORM_SCHEMA,
)
from homeassistant.components.blueprint import models
from homeassistant.util import yaml as yaml_util
import pytest

BLUEPRINTS = sorted(
    (Path(__file__).parent.parent / "blueprints/automation/sungrow_ems").glob("*.yaml")
)


def test_three_blueprints_are_shipped() -> None:
    assert [p.name for p in BLUEPRINTS] == [
        "battery_soc_alert.yaml",
        "ev_charger_fault_alert.yaml",
        "grid_outage_alert.yaml",
    ]


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.stem)
def test_blueprint_is_valid(path: Path) -> None:
    data: Any = yaml_util.load_yaml_dict(str(path))
    blueprint = models.Blueprint(
        data,
        path=path.name,
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    assert blueprint.name.startswith("Sungrow EMS: ")
    used = yaml_util.extract_inputs(data)
    assert used, path.name
    assert used <= set(blueprint.inputs), sorted(used - set(blueprint.inputs))
    # Every input is used, and every input except the entity has a default.
    assert set(blueprint.inputs) <= used, sorted(set(blueprint.inputs) - used)
    for name, spec in blueprint.inputs.items():
        if not name.endswith("_sensor"):
            assert "default" in spec, name
    assert path.name in data["blueprint"]["source_url"]
    # Old entity IDs and the old integration must never appear.
    text = path.read_text(encoding="utf-8")
    assert "sungrow_inverter_modbus" not in text
    assert "mark_phone" not in text


@pytest.mark.parametrize("path", BLUEPRINTS, ids=lambda p: p.stem)
def test_blueprint_renders_to_a_valid_automation(path: Path) -> None:
    data: Any = yaml_util.load_yaml_dict(str(path))
    blueprint = models.Blueprint(
        data,
        path=path.name,
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    entity = "sensor.battery_soc" if "soc" in path.stem else "binary_sensor.some_sensor"
    inputs = {name: entity for name in blueprint.inputs if name.endswith("_sensor")}
    rendered = models.BlueprintInputs(
        blueprint, {"use_blueprint": {"path": path.name, "input": inputs}}
    )
    rendered.validate()
    config = rendered.async_substitute()
    config["alias"] = path.stem
    PLATFORM_SCHEMA(config)
