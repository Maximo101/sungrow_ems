# Sungrow EMS for Home Assistant

<p align="center">
  <img src="https://raw.githubusercontent.com/Maximo101/sungrow_ems/main/custom_components/sungrow_ems/brand/logo.png" alt="Sungrow EMS Logo" width="200">
</p>

[![GitHub release (pre-release)](https://img.shields.io/github/v/release/Maximo101/sungrow_ems?include_prereleases&label=pre-release)](https://github.com/Maximo101/sungrow_ems/releases)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/docs/faq/custom_repositories)
![Status: Beta](https://img.shields.io/badge/status-beta-yellow)
![Read-only](https://img.shields.io/badge/Modbus-read--only-blue)

A **100 % local, read-only** Home Assistant integration for Sungrow hybrid inverters and the Sungrow **iHomeManager** (iHM), over Modbus TCP. It gives you fast telemetry and ready-made energy tracking with no iSolarCloud account, no cloud polling and no YAML.

> [!WARNING]
> **Beta / pre-release.** Tested on one system so far (see [Tested hardware](#tested-hardware)). Please report what works and what doesn't on yours.

## Read-only

Sungrow EMS is currently a **telemetry and energy-tracking** integration. It **does not write** to your inverter, battery, iHomeManager or EV charger, and there are **no plans to add write/control features at this stage**.

- The only Modbus function codes it can send are **FC03** and **FC04** (read holding / input registers). There is no write code in the repository, and the test suite fails if any is introduced.
- There are no `select`, `number`, `switch` or `button` entities and no services.
- Settings such as EMS mode, SOC limits, export limit and EV charger mode are shown as **read-only diagnostic sensors**.

Why: register writes behave differently across models and regional firmware, and a wrong write can affect an inverter, a battery or a charger. Schedules and limits set in the iSolarCloud app cover day-to-day control, so the integration doesn't need to write. This may be revisited in future, but nothing is planned.

## Key metrics

A selection of the 192 entities (some are disabled by default). Entity IDs assume the default device names. iHomeManager entities are tagged "(iHM)" in their names; the tag is not part of the entity ID.

| Metric | Entity | Unit | Source |
|---|---|---|---|
| Solar production now | `sensor.sungrow_ems_inverter_pv_dc_power` | W | Inverter |
| Solar production today / lifetime | `…inverter_daily_pv_generation`, `…inverter_total_pv_generation` | kWh | Inverter |
| MPPT 1–4 voltage and current | `…inverter_mppt1_voltage`, `…inverter_mppt1_current`, … | V, A | Inverter |
| House load now | `sensor.sungrow_ems_ihomemanager_load_power` | W | iHM |
| Grid power now (+ import, − export) | `sensor.sungrow_ems_ihomemanager_grid_active_power` | W | iHM |
| Grid import / export now (split) | `…ihomemanager_active_import_from_grid`, `…ihomemanager_active_export_to_grid` | W | Calculated |
| Grid import / export lifetime (spike-filtered) | `…ihomemanager_grid_import_safe`, `…ihomemanager_grid_export_safe` | kWh | Calculated |
| Grid import / export today | `…ihomemanager_daily_import`, `…ihomemanager_daily_export` | kWh | Calculated |
| Battery state of charge | `sensor.sungrow_ems_inverter_battery_soc` | % | Inverter |
| Battery status (Charging / Discharging / Idle) | `sensor.sungrow_ems_inverter_battery_status` | text | Calculated |
| Battery usable energy | `sensor.sungrow_ems_inverter_battery_usable_energy` | kWh | Calculated |
| Time until fully charged / discharged | `…inverter_time_until_fully_charged`, `…inverter_time_until_fully_discharged` | min | Calculated |
| Battery power (charging / discharging split) | `…inverter_battery_charging_power`, `…inverter_battery_discharging_power` | W | Calculated |
| Battery charge / discharge today | `…inverter_daily_battery_charge`, `…inverter_daily_battery_discharge` | kWh | Inverter |
| Battery health, temperature, voltage | `…inverter_battery_health`, `…inverter_battery_temperature`, `…inverter_battery_voltage` | %, °C, V | Inverter |
| Consumption today | `sensor.sungrow_ems_inverter_daily_consumption` | kWh | Calculated |
| Self-sufficiency today | `…inverter_self_sufficient`, `…inverter_self_sufficient_percentage` | kWh, % | Calculated |
| Where today's PV went (battery / home / grid) | `…inverter_battery_charged_from_pv_percentage`, `…inverter_daily_consumption_from_pv_percentage`, `…inverter_daily_export_to_grid_percentage` | % | Calculated |
| EV charger power and status | `…ihomemanager_ev_charger_power`, `…ihomemanager_ev_charger_status` | W, text | iHM |
| System status (decoded power flow) | `sensor.sungrow_ems_inverter_system_status` | text | Calculated |
| Inverter state and temperature | `…inverter_running_state`, `…inverter_temperature` | text, °C | Inverter |
| Grid voltage and frequency | `…ihomemanager_grid_voltage_a`, `…ihomemanager_grid_frequency` | V, Hz | iHM |
| Off-grid, inverter fault, EV charger fault | `binary_sensor.sungrow_ems_inverter_off_grid_backup_mode`, `…inverter_inverter_fault`, `…ihomemanager_ev_charger_fault` | on/off | Calculated |
| Connection (on = being polled) | `binary_sensor.sungrow_ems_inverter_connection`, `…ihomemanager_connection` | on/off | Diagnostic |
| Last successful poll, with counters (disabled by default) | `…inverter_last_successful_poll`, `…ihomemanager_last_successful_poll` | timestamp | Diagnostic |

### Energy dashboard

| Energy dashboard slot | Entity |
|---|---|
| Grid consumption | `sensor.sungrow_ems_ihomemanager_grid_import_safe` |
| Return to grid | `sensor.sungrow_ems_ihomemanager_grid_export_safe` |
| Solar production | `sensor.sungrow_ems_inverter_total_pv_generation` |
| Battery: energy going in | `sensor.sungrow_ems_inverter_total_battery_charge_cumulative` |
| Battery: energy coming out | `sensor.sungrow_ems_inverter_total_battery_discharge` |

## Features

- **Fully local Modbus TCP.** Reads the inverter (via WiNet-S / LAN) and the iHomeManager directly. Nothing leaves your network.
- **Two independent coordinators**, one per device, so a slow or offline iHM never stalls inverter data (and vice versa).
- **Fast polling.** 10 s default for both devices (5 s minimum). The iHM's mandatory 1 s gap between requests is respected automatically.
- **Efficient block reads.** 161 registers in 17 batched blocks. Live values are read every poll; settings are read once a minute and identity once an hour. If a device rejects part of a block, the hub splits it and remembers the working split.
- **Resilient.** Last-known-good values are kept through short dropouts. Entities go unavailable only after a configurable number of failed polls (default 6). Reconnects use exponential backoff, and sockets are always released on unload, reload and failed setup.
- **Sentinel filtering.** Sungrow's "not available" values (`0xFFFF`, `0x7FFF`, …) and out-of-range readings are dropped instead of appearing as spikes.
- **Native replacements for common YAML templates:** spike-filtered grid totals, midnight-reset daily meters, daily consumption, self-sufficiency, PV split percentages (always sum to 100), battery status, usable energy and time-to-full / time-to-empty estimates, split import/export and charge/discharge power, and a decoded System Status sensor.
- **Blueprints** for battery SoC alerts, grid outage / restore alerts and EV charger fault alerts.
- **Diagnostics.** Per-device poll status sensors, plus a redacted *Download diagnostics* report (hosts and serial number removed).
- **Runs beside other Sungrow integrations.** Unique IDs carry an `_ems` suffix and devices have their own names.

## Tested hardware

| Component | Model / firmware | Connection |
|---|---|---|
| Hybrid inverter | SH10RS (10 kW single-phase), WiNet-S | Modbus TCP 502, unit ID 1 |
| Battery | Sungrow SBR192 × 2 (38.4 kWh) | via inverter |
| Gateway | iHomeManager, firmware SV930.001.00.P014 | Modbus TCP 503, unit ID 247 |
| EV charger | Sungrow AC22E-01 | via iHomeManager |

Tested on Home Assistant 2026.10.4. The test suite (87 tests) runs against a simulated inverter and iHomeManager.

Other SH-series hybrids that implement the *Communication Protocol of Residential Hybrid Inverter* (V1.1.x) should work, since the register map follows the protocol, but they haven't been tested. Three-phase (RT) models will show some phase B/C entities as unavailable for now. **Reports from other models are very welcome.**

The iHomeManager is optional. Without it you still get every inverter sensor, but grid-meter totals, the EV charger and the iHM-based derived sensors won't be available.

## Requirements

| Device | Port | Unit (slave) ID | Notes |
|---|---|---|---|
| SH-RS hybrid inverter (WiNet-S / WiNet-S2 or LAN) | **502** | **1** | Modbus TCP must be enabled on the dongle |
| iHomeManager | **503** (or 504) | **247** | Fixed by the iHM protocol; one TCP connection per port |

- **Inverter:** Modbus TCP must be enabled on the WiNet-S / WiNet-S2 (or LAN) interface.
- **iHomeManager (optional):** Modbus TCP must be enabled on the iHM during local commissioning (by your installer or via local access). The iHM listens on ports **502, 503 and 504** and allows **only one TCP connection per port**. If another tool (SunGather, another integration) already holds 503, use 504.
- A fixed IP (DHCP reservation) for both devices is strongly recommended.
- Home Assistant **2026.10 or newer**.

## Installation

### Marketplace on Home Assistant (formerly HACS - custom repository)

You can add a custom respository.
1. In Settings go to 'Marketplace', open the menu (⋮) → **Custom repositories**.
2. Add from Link `https://github.com/Maximo101/sungrow_ems` with category **Integration**.
3. Find **Sungrow EMS** in HACS and download it. Pre-releases are listed when you enable **Show beta versions** on the integration's page in HACS.
4. Restart Home Assistant.

<p align="center">
  <img src="https://raw.githubusercontent.com/Maximo101/sungrow_ems/main/custom_components/sungrow_ems/brand/marketplace.png" alt="Home Assistant">
</p>

### Manual

1. Download the source code from the [latest release](https://github.com/Maximo101/sungrow_ems/releases).
2. Copy the `custom_components/sungrow_ems/` folder into your `config/custom_components/` folder (so `config/custom_components/sungrow_ems/manifest.json` exists).
3. Restart Home Assistant.

## Configuration

**Settings → Devices & services → Add integration → Sungrow EMS**

| Field | Default | Notes |
|---|---|---|
| Inverter host | — | IP of the WiNet-S / LAN interface |
| Inverter port | 502 | |
| Inverter unit ID | 1 | |
| Use iHomeManager | on | Turn off if you don't have one |
| iHomeManager host | — | Required when the iHM is enabled |
| iHomeManager port | 503 | Use 504 if 503 is taken |
| iHomeManager unit ID | 247 | Fixed by the iHM protocol; leave as is |

The integration reads the inverter serial number during setup and uses it as the entry's unique ID, so the same inverter can't be added twice. Hosts and ports can be changed later with **Reconfigure**.

<p align="center">
  <img src="https://raw.githubusercontent.com/Maximo101/sungrow_ems/main/custom_components/sungrow_ems/brand/ha1.png" alt="Sungrow EMS">
</p>

### Options

**Settings → Devices & services → Sungrow EMS → Configure**

| Option | Default | What it does |
|---|---|---|
| Inverter poll interval | 10 s | 5–300 s |
| iHomeManager poll interval | 10 s | Keep ≥ 10 s; each poll is about 3 paced requests |
| Request timeout | 3 s | Per Modbus request |
| Retries per request | 2 | Extra attempts after a timeout or busy reply |
| Minimum gap between iHM requests | 1.0 s | Protocol minimum is 1.0 s |
| Failed polls before unavailable | 6 | Until then, last good values are held |
| Battery capacity override | 0 (auto) | Rated capacity. 0 = read from the hardware (iHM 8147 → inverter 13039 → 5639) |
| Battery discharge efficiency | 0.85 | DC→AC factor used in Daily consumption |
| Spike filter: largest jump | 50 kWh | Grid Import/Export Safe ignore bigger single steps |
| Spike filter: largest daily total | 200 kWh | Daily values above this are treated as corrupt |
| Spike filter: readings to re-baseline | 3 | Consistent readings needed to accept a new baseline |

Saving changed options reloads the integration.

## Entities

Two devices are created: **Sungrow EMS Inverter** and **Sungrow EMS iHomeManager**. Some rarely useful or unreliable sensors are disabled by default; enable them from the device page if you need them. Holding-register readbacks (settings) are diagnostic entities.

### Battery sensors

Nothing here uses a fixed capacity; every figure comes from live readings.

- **Battery status** follows the inverter's own power-flow flags (register 13001), with the sign of the battery power as the fallback.
- **Battery usable energy** is the energy you can draw before discharging stops. The SoC the inverter reports runs from 0 % at *Min SoC* to 100 % at *Max SoC*, so the capacity behind it is `rated capacity × (Max SoC − Min SoC)`. While the grid is up, the *Backup reserve SoC* is held back and is not counted; in off-grid (backup) mode it is. The attributes show the parts: `usable_capacity_kwh`, `stored_energy_kwh` (everything above Min SoC), `reserved_energy_kwh`, `energy_to_full_kwh` and `discharge_floor`.
- **Time until fully charged / fully discharged** divide the energy still to go by the battery power averaged over two minutes. "Fully discharged" means the point where discharging stops: the backup reserve on grid, Min SoC off grid. Each shows *Unknown* while the battery is going the other way or is idle. Charging slows near 100 %, so the last part of a charge takes longer than the estimate.

The SoC scaling between Min SoC and Max SoC is how Sungrow hybrids are observed to behave; it is not stated in the protocol document. If your system behaves differently, please open an issue.

## Blueprints

Three automation blueprints are in [`blueprints/automation/sungrow_ems/`](blueprints/automation/sungrow_ems). They are not installed with the integration: import the ones you want.

| Blueprint | What it does | Import |
|---|---|---|
| Battery SoC alert | Notifies when the SoC stays below or above a level. One automation per level. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMaximo101%2Fsungrow_ems%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fsungrow_ems%2Fbattery_soc_alert.yaml) |
| Grid outage and restore alert | Notifies when the system goes off grid and when the grid is back. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMaximo101%2Fsungrow_ems%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fsungrow_ems%2Fgrid_outage_alert.yaml) |
| EV charger fault alert | Notifies when the EV charger reports a fault, with the fault code. | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMaximo101%2Fsungrow_ems%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fsungrow_ems%2Fev_charger_fault_alert.yaml) |

Or go to **Settings → Automations & scenes → Blueprints → Import blueprint** and paste the file's GitHub address. Each blueprint asks for the entity to watch and the notify action to call (for example `notify.mobile_app_my_phone`), and can run extra actions of your own.

## Running beside the original integration

You can run Sungrow EMS next to the cloud/hybrid integration it grew out of while you validate the numbers:

1. Leave the existing integration on iHM port **503**.
2. Add Sungrow EMS with iHM port **504**.
3. Compare history, then remove the old integration (and stop SunGather or any other poller) once you're happy.

Because the iHM only accepts one connection per port, two pollers on the same port will fight and both will see timeouts.

## Known limitations

- **Inverter total battery charge (13041)** returns an invalid high word on the SH10RS and is rejected; use *Total battery charge cumulative* (integrated from battery power) instead.
- **Inverter meter registers** (13029, 13036–13038, 13045–13047) read 0 when the iHM does the metering, so they're disabled by default. Use the iHM meter sensors.
- **Settings sensors update once a minute** (EMS mode, EV charger mode, limits), not every poll.
- **WiNet-S caching (suspected):** the inverter sometimes repeats identical values for about 2 minutes while polls still succeed. The poll status sensors expose `seconds_since_value_change` to help confirm this.
- **EV charger power (iHM 8594)** isn't in the current iHM protocol document and is marked unverified.
- **iHM 8049 / 8050** (charger enable / grid draw) read "Disable" in every charger mode tested; their meaning on current firmware is still unclear.
- The iHM export-limit registers (8028 / 8031) are a separate Modbus limitation layer and don't reflect the export limit set in the iSolarCloud app. Their sensors are disabled by default.
- **Grid state (13030)** is a reserved register in the current inverter protocol and reads "Not reported" on current firmware; it is disabled by default. Use *Off-grid (backup) mode* instead.
- Three-phase (RT) models are untested.

## Troubleshooting

- **Cannot connect during setup:** check the IP and port, make sure Modbus TCP is enabled on the WiNet-S, and that nothing else holds the iHM port you chose.
- **Entities flicker unavailable:** raise *Failed polls before unavailable* or the timeout in Options.
- **Debug logging:**

  ```yaml
  logger:
    default: warning
    logs:
      custom_components.sungrow_ems: debug
      pymodbus: info
  ```

When opening an issue, please attach the diagnostics download (**Settings → Devices & services → Sungrow EMS → ⋮ → Download diagnostics**) and include your inverter model, iHM firmware and Home Assistant version.

## Development

```bash
pip install -r requirements_test.txt
pytest
ruff check . && ruff format --check .
```

## Credits

Sungrow EMS started life as a fork of **[sungrow-solarcloud-haos-integration](https://github.com/Cyber40014/sungrow-solarcloud-haos-integration)** by **[@Cyber40014](https://github.com/Cyber40014)**. Huge thanks for the original integration: its entity keys (`mb_*`, `ihm_*`) and register selection are the foundation this project was built on. Since then the code has been rewritten as a local-only Modbus integration, with the cloud API removed and the coordinators, Modbus hub, register map and derived sensors redone from scratch.

Also thanks to:

- **[SunGather](https://github.com/bohdan-s/SunGather)** by [@bohdan-s](https://github.com/bohdan-s), whose community register map was cross-checked for registers not covered by Sungrow's documents (those are flagged as unverified in `registers.py`).
- **Sungrow**, for publishing the *Communication Protocol of Residential Hybrid Inverter* (V1.1.4) and the *iHM Communication Protocol* (V1.0.2), which every register here is cross-referenced against.
- The **[pymodbus](https://github.com/pymodbus-dev/pymodbus)** project.

## Disclaimer

This is an independent community project. It is **not affiliated with, endorsed by or supported by Sungrow**. "Sungrow", "iSolarCloud", "iHomeManager" and "WiNet" are trademarks of their respective owners.
