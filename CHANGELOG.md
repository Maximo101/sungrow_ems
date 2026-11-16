# Changelog

## 0.1.6

### Added
- **Battery status** (Charging / Discharging / Idle), from the inverter's power-flow flags (13001).
- **Time until fully charged** and **Time until fully discharged**, in minutes, from the battery power averaged over two minutes.
- Three automation **blueprints** in `blueprints/automation/sungrow_ems/`: battery SoC alert, grid outage and restore alert, EV charger fault alert.

### Changed
- **Battery stored energy is replaced by Battery usable energy.** The old sensor was SoC × rated capacity. The new one uses the capacity between Min SoC (13059) and Max SoC (13058) and leaves out the Backup reserve SoC (13100) while the grid is up; off grid the reserve is counted. The old entity (`…inverter_battery_stored_energy`) is removed on upgrade and the new one is `…inverter_battery_usable_energy`; update dashboards and automations that used it. The former value is still there as the `stored_energy_kwh` attribute (now relative to Min SoC).
- *Feed-in limitation* and *Feed-in limitation ratio* (iHM 8028 / 8031) are disabled by default on new installs. Existing installs keep them enabled; disable them on the device page if you don't want them.
- *Grid state* (13030, reserved in the current protocol) shows "Not reported" instead of "Unknown (0)".

No other entity IDs or unique IDs changed.

## 0.1.5

**Scope decision: Sungrow EMS is read-only at this stage.** It is a Modbus TCP telemetry and energy-tracking integration. The write/control features previously planned for v0.2.0 (`select`, `number`, `switch`, services, VPP heartbeat) are withdrawn: register writes behave differently across models and regional firmware and can affect inverters, batteries and EV chargers. This may be revisited in future, but nothing is planned.

### Removed
- All write code: the hub's FC06/FC16 methods, read-back verification, the register encoder and the `writable` register flag.
- The Phase 2 roadmap from the README, release notes, UI strings and code comments.
- Compatibility shims for pymodbus older than 3.10 and for old Home Assistant device-registry lookups.

### Changed
- The hub can only call `read_input_registers` and `read_holding_registers`.
- iHomeManager settings (8024–8052) are read once a minute instead of every poll: one paced request fewer per iHM poll. The block is renamed `ihm_control` → `ihm_settings`.
- Sockets are released by an on-unload callback, so a setup that fails part-way can no longer hold the iHomeManager's single TCP slot.
- Options use `OptionsFlowWithReload`: one reload, and only when something changed.
- Sensor presets are a data-driven table shared by register and calculated sensors.
- Requires Home Assistant 2026.10+ and `pymodbus>=3.10.0`.

### Added
- *Download diagnostics* with hosts and serial number redacted.
- Test suite (`tests/`), including guards that fail if write code or control platforms are introduced.

No entity IDs or unique IDs changed.

## 0.1.4

First public beta. See `RELEASE_NOTES_v0.1.4.md`.
