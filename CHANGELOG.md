# Changelog

## 0.1.5

**Scope decision: Sungrow EMS is read-only, permanently.** It is a Modbus TCP telemetry and energy-tracking integration. Write/control features (`select`, `number`, `switch`, services, VPP heartbeat) will not be added, because register writes behave differently across models and regional firmware and can affect inverters, batteries and EV chargers.

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
