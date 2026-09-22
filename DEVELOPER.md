# Developer documentation

This document covers architecture, implementation details, and
non-obvious Home Assistant behavior. For installation and usage as an end
user, see [README.md](README.md). The Modbus data point list the register
map is based on is in [docs/83807301.pdf](docs/83807301.pdf).

## Dependencies and architecture notes

> **pymodbus `device_id`:** pymodbus renamed the `slave` parameter to
> `device_id` in version 3.10.0. This integration uses `device_id`
> throughout, and `manifest.json` requires `pymodbus>=3.11.0`.

> **Modbus architecture:** Home Assistant is working on a shared-connection
> `modbus_connection` integration, but as of its July 16, 2026 update the
> approach is not recommended for new device integrations yet. This
> integration keeps using `pymodbus` directly via its own
> `AsyncModbusTcpClient` connection.

## File overview

- `config_flow.py` – UI-based setup (name, IP, port, device ID) + connection
  test, reconfigure flow, options flow (polling interval, room temperature
  source per heating circuit)
- `coordinator.py` – central Modbus connection, periodic polling
  (`DataUpdateCoordinator`), block reading, writes
- `const.py` – register map and all mappings (see below)
- `entity.py` – base classes: device info, unique id, translation key
- `sensor.py` – sensors from `SENSOR_REGISTERS`, and `WeishauptStatusSensor`
  (device_class `enum`) for the operating status
- `binary_sensor.py` – digital registers from `BINARY_SENSORS`
- `number.py` – writable registers from `NUMBER_REGISTERS`
- `select.py` – the system operating mode
- `water_heater.py` – domestic hot water
- `climate.py` – up to 4 room heating circuits

## The register map (`const.py`)

The addresses are the numbers printed in the data point list, used as they
are (no offset). Values that can only be read (3xxxx) are input registers
(function code 0x04), values that can be written (4xxxx) are holding
registers (written with 0x06). Only 16-bit values occur.

```python
RegisterDef(
    key="outdoor_temp_1",            # unique key, also the translation key
    address=30001,
    data_type="int16",               # int16 / uint16
    scale=0.1,                       # raw_value * scale = published value
    unit="°C",
    valid_range=SENSOR_RAW_RANGE,    # raw values outside this mean "no value"
)
```

- **Invalid values.** Sensors report `-32768` (no sensor), `-32767` (open
  circuit), `-32766` (short circuit) and `-32758`/`-32757` (digital
  status) instead of a temperature; setpoints report `-32768` and `1` when
  no setpoint request is active; error codes report `65535` when nothing is
  active. Rather than enumerating each such raw value, `RegisterDef.valid_range`
  holds the inclusive range the data point list documents for the
  register's format (`SENSOR_RAW_RANGE`, `SETPOINT_RAW_RANGE`,
  `CODE_RAW_RANGE`) -- a value outside it is left out of the coordinator's
  data, and entities show `unknown`. A range also covers raw values the
  list doesn't name explicitly, which a fixed list of sentinels wouldn't.
  The [evcc](https://github.com/evcc-io/evcc) project's Weishaupt charger
  (`charger/weishaupt.go`) takes the same approach for sensors, but reuses
  the sensor range for a setpoint register too, which would wrongly accept
  a setpoint's `1` ("no setpoint request") as a valid 0.1°C -- this
  integration keeps the two ranges separate for that reason.
- **Heating circuits** follow a pattern: values at `31<n>01 ...`, writable
  values at `41<n>03 ...` for circuit `<n>` = 1 ... 4. `_heating_circuit()`
  builds them, so circuits share translation keys and use the `{circuit}`
  placeholder (`RegisterDef.circuit` / `translation_key`).
- **Adding a register:** add a `RegisterDef` to the matching list, then
  add the entity name to `strings.json` and `translations/de.json` (and
  `translations/en.json`, kept identical to `strings.json`). The test suite
  checks that every entity has a translation.
- **Registers beyond the official data point list.** `electrical_power_input`
  (`33126`) and `ELECTRICAL_STATISTICS_REGISTERS` (`36701 ... 36704`) are
  not in `83807301`; they come from community projects (see "Related
  projects and further reading" below) and are disabled by default since
  they are unconfirmed against the official document. `fault_free_code`
  (`30005`), by contrast, *is* in the official list (section 3.2,
  "Fehlerfrei", same `FormatFehlermeldung` as `error_code`/`warning_code`)
  -- it was simply missed when this register map was first built from the
  PDF, found later by cross-checking a community blog post that also reads
  it.

## Reading: blocks, selective polling, fallback

- **Blocks.** The controller answers at most 5 consecutive registers per
  request. `plan_read_blocks()` groups the registers to poll into blocks of
  consecutive addresses (maximum `MAX_REGISTERS_PER_READ`) with the same
  function code. If a block is rejected (e.g. one register belongs to
  hardware that is not installed), its registers are read one by one, so a
  missing data point never hides its neighbours.
- **Holding or input.** The data point list's configuration section names
  function code 0x04 for reading, but that turns out to describe a
  read-block-size setting of the GLT software it documents, not a
  restriction on 4xxxx registers in general: evcc's Weishaupt charger reads
  and writes `40002` (`SollwertPV`) with plain holding-register requests
  (`ReadHoldingRegisters`/`WriteSingleRegister`), confirming 4xxxx
  registers answer to function code 0x03 like any standard holding
  register. This integration reads them with 0x03 first and, only if the
  controller rejects that, falls back to 0x04 from then on
  (`_input_fallback`) -- kept as a safety net for a controller or firmware
  version that behaves differently, even though it is expected to never
  trigger in practice.
- **Selective polling (`async_contexts()`).** Every `CoordinatorEntity`
  passes the register keys it reads as its `context` when subscribing.
  `_async_update_data()` only polls registers whose key appears in
  `async_contexts()` — what a currently enabled, added-to-hass entity
  needs. Disabled entities never subscribe, so their registers are
  skipped without any bookkeeping; enabling one starts polling on the next
  cycle. With no context yet (the very first refresh in `__init__.py`,
  before entities exist), everything is polled.
- **Tolerating transient read failures.** A register whose read fails
  (block or single, e.g. a dropped connection) is not dropped from the
  data on the spot: `_consecutive_failures` counts failures per register
  key, and `_async_update_data()` carries the last known value forward
  from `self.data` for up to `CONSECUTIVE_FAILURES_BEFORE_DROP - 1` polls
  in a row before finally leaving the key out (entities go
  `unknown`/`unavailable` only then). A
  successful read (`_store()`), whether or not the value passes
  `valid_range`, resets the counter -- a value legitimately filtered out by
  `valid_range` (e.g. "no sensor installed") is not a communication
  failure and must not linger with a stale value. Community reports (see
  below) of registers or entities briefly, spuriously going unavailable
  were the reason for this; [Varitras/weishaupt_modbus](https://github.com/Varitras/weishaupt_modbus)
  uses the same 4-failures threshold.

## Writing and the controller's EEPROM

Every parameter write is stored in the controller's EEPROM (100,000 write
cycles). `async_write_value()` therefore skips a write whose value equals
the current one. "Current" is the value last written since the register
was last polled (`_written`), else the polled value — a refresh requested
after a write can be delayed by the coordinator's debouncer, so the polled
data alone may be stale right after a write. A skipped write still calls
`async_update_listeners()`, so entities can resolve a pending optimistic
change.

**`SYSTEM_PV_SETPOINT_REGISTER` (`SollwertPV`, `40002`) is the one
exception.** The data point list explicitly says it isn't stored in the
EEPROM and has no write-cycle limit (section 4.3, "Datenpunkt für
PV-Eigenstromnutzung") -- writing `0` deactivates it again and returns
control to the heat pump's own regulation. Since it must be re-sent to
stay in effect rather than being set once, its `RegisterDef` sets
`skip_unchanged_write=False`, which makes `async_write_value()` write it
every time regardless of the current value. Its number entity,
`WeishauptPvPowerSetpointNumber` (`number.py`), additionally re-sends the
value it was last set to every `PV_SETPOINT_HEARTBEAT_INTERVAL` (30
seconds) via `async_track_time_interval`, for as long as that value isn't
`0` -- independently of the polling interval, and without needing an
automation to keep calling `number.set_value` just to keep a setpoint
alive. An automation is still needed to *change* the setpoint when the
actual available power changes.

## A note on entity naming and `translation_key`

If both `_attr_name` and `_attr_translation_key` are set on an entity, HA
always uses `_attr_name` and never resolves the translation. So don't set
`_attr_name` alongside `translation_key`; the translation key plus the
matching `entity.<domain>.<key>.name` entry in `strings.json` must be the
*only* source of the name.

## Room temperature source (per heating circuit)

Each heating circuit's `current_temperature` can come from one of three
sources, configurable **individually per circuit** via the options
(three-dot menu → "Configure"):

- **None** (default) -- no current temperature is shown.
- **Read from heat pump (Modbus)** -- reads the circuit's room temperature
  register (`HeatingCircuit.room_temp_reg`, `31<n>02`).
- **Home Assistant entity** -- mirrors the state of an existing `sensor`
  entity with `device_class: temperature`. Updates live via
  `async_track_state_change_event`, independent of the Modbus polling
  interval. Non-numeric or `unavailable`/`unknown` states are treated as
  "no current temperature".

Changing any of these options reloads the config entry automatically
(same mechanism as changing `scan_interval`).

## Options flow: sections and the "can't clear an entity" trap

The options form is grouped into collapsible **sections**
(`homeassistant.data_entry_flow.section`): "General" and one per heating
circuit. Home Assistant nests submitted data by section, so
`async_step_init()` flattens it back into the flat key structure the rest
of the integration expects (`current_temp_source_key()` /
`current_temp_entity_key()` in `const.py`) before saving.

- **Never use `vol.Optional(key, default=...)` for a field the user
  should be able to clear** (e.g. the entity picker's "X" button). With
  `default=`, voluptuous re-inserts the default whenever the field is
  missing from the submitted data — which is what happens when a field is
  cleared. Use `description={"suggested_value": ...}` instead.
- **`add_suggested_values_to_schema()` does not recurse into
  `section()`-wrapped fields** (see
  [home-assistant/frontend#22419](https://github.com/home-assistant/frontend/issues/22419)).
  Set `description={"suggested_value": ...}` directly on the
  `vol.Optional(...)` marker inside the section's schema.

## Room heating circuits: HVAC mode vs. preset mode

The controller has 5 operating modes per circuit: Standby, Automatic,
Comfort, Normal, Setback. Home Assistant's `HVACMode` is a **closed enum**,
so this integration maps:

- Standby → `HVACMode.OFF`, Automatic → `HVACMode.AUTO`.
- Comfort / Normal / Setback → `HVACMode.HEAT`, with the specific mode
  exposed as **`preset_mode`** (`"comfort"` / `"normal"` / `"setback"`).
  Selecting a preset writes the corresponding raw code directly to the
  mode register — no need to call `set_hvac_mode` first.

**Each of the three heating modes has its own room setpoint register**
(`41<n>05` / `06` / `07`, readable and writable). In `HEAT`,
`target_temperature` reads the register of the active preset and
`async_set_temperature()` writes to it. In `AUTO` and `OFF` the room
setpoint currently requested by the controller (`31<n>01`) is shown.

**Setting a temperature is only possible in `HVACMode.HEAT`.** This is
enforced via a **collapsed `min_temp`/`max_temp` range**, not by hiding the
`TARGET_TEMPERATURE` feature:

- `min_temp`/`max_temp`/`target_temperature_step` are `@property`s reading
  from `_active_setpoint_register()`. While that returns a register they
  reflect its real min/max/step. While it returns `None` (Off/Auto),
  `min_temp` and `max_temp` both collapse to the **current**
  `target_temperature`, so the frontend's stepper has nowhere to move to,
  without hiding the value itself.
- `async_set_temperature()` still raises `ServiceValidationError` in that
  case, as a backstop for direct service calls.

Hiding `TARGET_TEMPERATURE` from `supported_features` dynamically doesn't
work: `ClimateEntity.state_attributes` only includes the target
temperature when that feature bit is set, so the controller's setpoint
would disappear from Off/Auto.

**Presets are only offered while in HEAT.** `preset_modes` is a `@property`
returning `CLIMATE_PRESET_OPTIONS` in `HEAT` and `None` otherwise;
`preset_mode` mirrors that. Home Assistant validates a submitted preset
against `preset_modes` *before* `async_set_preset_mode()` is called, so
returning `None` also rejects preset changes in Off/Auto with a clear error.

**Preset icons** are defined in `icons.json` under
`entity.climate.heating_circuit.state_attributes.preset_mode`. Once an
entity defines *any* custom icon mapping for that attribute, it takes over
entirely — every preset value needs an explicit icon there, and unlisted
values fall through to your own `default`.

**Remembering the last heating preset (`self._last_preset`):** switching
`OFF`/`AUTO` → `HEAT` needs to pick *some* preset to write. Each
`WeishauptRoomClimate` keeps `self._last_preset`, updated

- optimistically in `async_set_preset_mode()`,
- in `_handle_coordinator_update()` on every refresh, so it also follows a
  mode changed at the heat pump's own panel,
- from a custom `last_heating_preset` state attribute via `RestoreEntity`.
  The built-in `preset_mode` attribute can't be used for this: it is `None`
  whenever the circuit isn't in `HEAT`, i.e. in exactly the case where a
  restart needs the restored value.

## Optimistic mode/temperature updates with pending confirmation

`WeishauptRoomClimate` shows a written mode or temperature immediately
instead of waiting for the next poll:

- `async_set_hvac_mode()` / `async_set_preset_mode()` /
  `async_set_temperature()` stash the value they write in
  `_pending_mode_value` / `_pending_temperature` and call
  `async_write_ha_state()` before awaiting the write.
- `hvac_mode` / `preset_mode` / `target_temperature` read the pending value
  while one is set, else the polled register value.
- `_handle_coordinator_update()` clears the pending value as soon as the
  polled register matches (temperatures within
  `TEMPERATURE_CONFIRMATION_EPSILON`, half the register scale).
- A change the controller doesn't confirm within
  `CHANGE_CONFIRMATION_TIMEOUT` (90 s) is dropped with a warning, so the
  entity falls back to the reported state after a silently failed write.
- The `mode_change_pending` / `temperature_change_pending` attributes
  expose whether a change is still awaiting confirmation.

## Domestic hot water

The controller has no operating mode register for hot water, only the
normal setpoint (`42103`), the lowered setpoint (`42104`), a push duration
(`42102`), the active setpoint (`32101`) and the tank temperature
(`32102`). `WeishauptWaterHeater` therefore supports only
`TARGET_TEMPERATURE`, reading and writing the normal setpoint. Its state
(`heating` / `idle`) is derived from the heat pump status
(`DHW_ACTIVE_STATUS_CODES`); `WaterHeaterEntity.state` returns
`current_operation` without checking it against an operation list.

## Tests and development environment

Everything runs in docker compose, on Home Assistant 2026.9.3:

```bash
docker compose run --rm tests        # test suite
docker compose up                    # Home Assistant on http://localhost:8123
docker compose down -v               # stop and discard the HA configuration
```

- `tests/modbus_simulator.py` simulates a heat pump. It lists the register
  values with the addresses from the data point list — deliberately not
  derived from `const.py`, so the tests check the register map — answers at
  most 5 registers per request and rejects unknown addresses. Heating
  circuits 3 and 4 are not installed in it. `docker compose up` starts it
  as the `modbus-simulator` service: add the integration with host
  `modbus-simulator` and port 502.
- The tests use `pytest-homeassistant-custom-component`; the version in
  `tests/Dockerfile` must match the Home Assistant version.
- `tests/test_const.py` checks the register map and the translations,
  `tests/test_coordinator.py` reading and writing,
  `tests/test_entities.py` and `tests/test_config_flow.py` the entities and
  flows against the simulator.

**Custom translation caching (custom integrations only):** if you change
`strings.json`/`translations/*.json` and the UI still shows the old text
after a reload, do a **full Home Assistant restart** and a hard browser
refresh (Ctrl/Cmd+Shift+R).

## Related projects and further reading

Besides the official data point list (`docs/83807301.pdf`), these were
useful while building and cross-checking this integration's register map:

- [evcc](https://github.com/evcc-io/evcc) — its Weishaupt charger
  (`charger/weishaupt.go`) confirmed that 4xxxx registers are plain
  holding registers and contributed the `electrical_power_input` (`33126`)
  register.
- [Varitras/weishaupt_modbus](https://github.com/Varitras/weishaupt_modbus)
  (a fork of [OStrama/weishaupt_modbus](https://github.com/OStrama/weishaupt_modbus))
  — another Home Assistant integration for the same heat pumps, built on
  Home Assistant's own `modbus` integration rather than a dedicated
  connection like this one. Independently arrived at the same setpoint
  sentinel handling (`1`/`-32768` = "no setpoint request"), and reported
  the yearly energy counters staying at zero on every controller tested.
- [Ingmar Kaiser's blog on reading a Weishaupt heat pump via Modbus](https://www.ingmar-kaiser.de/blog/weishaupt/)
  — a Telegraf/Grafana monitoring setup; contributed the
  `ELECTRICAL_STATISTICS_REGISTERS` (`36701 ... 36704`) and helped find
  the missing `fault_free_code` (`30005`).
- The Home Assistant community thread ["Weishaupt Heatpump integration via modbus"](https://community.home-assistant.io/t/weishaupt-heatpump-integration-via-modbus/436823)
  — user reports of statistics registers only reporting whole kWh (the
  WEM portal shows more precision), registers occasionally stuck at
  factory-default values, and newer controller firmware exposing more
  registers than older ones.
