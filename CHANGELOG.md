# Changelog

## 0.1.10

### Fixed
- **PV split behind an iHomeManager.** *Daily export to grid %*, *Daily consumption from PV* and its % used the inverter's PV export counter (13005), which reads 0 when the grid meter is on the iHM, so today's export was never counted as PV. With an iHM the iHM daily export is now used; 13005 only on systems without one. *Daily consumption* and *Self sufficient* are unchanged.
- **Registers marked "not supported" after a brief rejection.** When the inverter (or the WiNet-S in front of it) rejected a whole table for a moment, every register in it was marked unsupported for an hour and its last value kept showing. A register that has been read before is now only marked unsupported after 10 rejections in a row; until then its block counts as a failed read (retried on the next poll, unavailable after *failure threshold* polls).
- The hub's *last error* in diagnostics no longer wraps each "waiting before reconnecting" notice inside the next one, and now has a time (`last_error_at`).

### Changed
- **With an iHomeManager, the inverter's meter-based registers are disabled by default** (protocol note 1: they need a meter wired to the inverter): meter power 5601–5607, PV export 13005/13006, load and export power 13008/13010, direct consumption 13017/13018, self-consumption 13029, import 13036/13037 and export 13045/13046. Existing installs: *Total PV export*, *Daily direct consumption* and *Total direct consumption* are disabled once on update; the others were already off, and anything you turned on yourself is left on. Without an iHM nothing changes.

### Added
- Diagnostics: age, refresh interval and fail streak for each read block, and a count of temporary rejections per hub.

## 0.1.9

### Added
- **PVOutput donation mode: battery values b1–b5.** Auto by default: b1 battery power (+ charging / − discharging), b2 SoC, b3 usable battery size (reported capacity × Max–Min SoC), b4 lifetime charge, b5 lifetime discharge. Each can be changed or set to *Do not send*. PVOutput needs b1, so the group is left out when b1 has no value.
- **PVOutput v7–v12 from any Home Assistant entity** (for example a weather station). Empty by default (*do not send*). Unavailable or non-numeric states are left out of that upload instead of being sent as 0.
- **PVOutput donation status** sensor (diagnostic): *Active* / *Not active* from the hourly limit PVOutput reports (300 = donation), or PVOutput's own error message when it refuses donation-only values. While donation is configured but not active, uploads carry on with the standard values only.
- The setup checks the chosen account type against PVOutput and says so when a key is not in donation mode.

- **Inverter power limitation readback** (protocol V1.1.17, disabled by default, diagnostic): *Feed-in limitation ratio* (13088), *Active power limitation* (13089) and *Active power limit ratio* (13090). They come from the existing settings read, so no extra Modbus requests.

### Changed
- **Register map checked against inverter protocol V1.1.17** (was V1.1.4), for the SH8.0-10RS.
- **Inverter settings (holding registers) are read every 5 minutes instead of every minute**, as the protocol asks for read-write registers forwarded through WiNet-S. Values stay shown between reads; a failed read is retried on the next poll. Changes made in iSolarCloud to Min SoC, Max SoC or Backup reserve can take up to 5 minutes to reach the battery sensors. iHomeManager settings are still read every minute.
- **Running state** recognises *Microgrid operation* (0x0014), *Derating running* (0x0080) and *Communication fault* (0x2500) instead of showing *Unknown*.
- **Inverter Protocol version** shows a version such as *V1.1.7* instead of a hex code.
- **DRM state** shows *DRM0*–*DRM8* instead of a number.
- **Model** knows the device codes added up to V1.1.17 (SH-RL, MG-RL, SH-CX, MG-TL) and uses Sungrow's names for SH-T models.
- *Meter active power* (5601) and *Charge/discharge power (inverter)* (13052) are now documented by Sungrow and no longer marked unverified.
- **PVOutput sends either power (v2 + v4, default) or energy (v1 + v3)**, not both, as PVOutput recommends. Chosen on the live status step.
- PVOutput values that mean "no data" are left out: inverter temperature of 0 °C (asleep), grid voltage below 90 V, battery SoC of 0 %.
- v7–v12 no longer take Sungrow EMS values from a list; earlier mappings there are dropped (pick the entities instead).
- **Time until fully charged / discharged show hours and minutes** (e.g. *7h 27m*). They are now text sensors, with `minutes` and `finishes_at` attributes; Home Assistant may offer to delete their old long-term statistics.
- **iHomeManager Device type is now Model** and shows *iHomeManager* (register 8000, fixed code 0x072A) instead of the raw code. The entity ID is unchanged.

### Fixed
- *Battery current (13021)* and *Battery power (13022)* dropped readings of −0.1 A / −1 W. Protocol V1.1.17 confirms these registers are signed (S16) on the SH8.0-10RS, so 0xFFFF is −1, not "no data".

### Removed
- **Inverter Protocol number** (4950–4951, disabled by default). Sungrow made these registers *Reserved* in protocol V1.1.7. The entity is removed from the registry on setup.

Unchanged on purpose: firmware information (13250–13294), channel 2 meter (13200–13207), DO configuration (13001), PV power limitation (13018), MPPT 5–10 (5117–5133) and load rated power (33274) are not supported on the SH3.0-10RS, so they are not read.

No other entity IDs or unique IDs changed.

## 0.1.8

### Added
- **Connection** binary sensor for each device (connectivity, diagnostic). It turns off when the device's entities go unavailable and back on when polling recovers. It only changes on a real connect or disconnect, so it adds nothing to the Activity log or the database while things are running normally.

### Changed
- **Last successful poll is disabled by default on new installs.** It changes on every poll (every 10 s), which filled the Activity log and the database. Existing installs keep it as it is: disable it on each device page if you don't want it. Poll counters are also in *Download diagnostics*.

No entity IDs or unique IDs changed.

## 0.1.7

### Changed
- **iHomeManager entities are tagged "(iHM)" in their names**, for example *Battery SoC (iHM)*, so values that both devices report can be told apart on dashboards. Entity IDs and unique IDs are unchanged.
- **Device pages show the address each device is polled on**, for example *SH10RS @ <host>* and *iHomeManager @ <host>*.

### Fixed
- *Battery charging power* showed "-0 W" while the battery was idle.

No entity IDs or unique IDs changed.

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
