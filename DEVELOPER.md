# Developer documentation

This document covers architecture, implementation details, and
non-obvious Home Assistant behavior. For installation and usage as an end
user, see [README.md](README.md). The Modbus data point list the register
map is based on is in [docs/83807301.pdf](docs/83807301.pdf).

## Dependencies and architecture notes

> **Modbus architecture:** the integration takes a `ModbusUnit` from Home
> Assistant's `modbus` integration
> (`homeassistant.components.modbus.async_get_unit`, `manifest.json`:
> `"dependencies": ["modbus"]`) instead of opening its own client. The
> unit comes from the `modbus-connection` library (tmodbus backend), which
> the `modbus` integration ships, so the manifest lists no requirements of
> its own. Minimum Home Assistant version is 2026.10.1: 2026.10 brings
> `ModbusUnit.require_timeout()` (modbus-connection 4.12), and 2026.10.1
> (4.12.4) fixes a request failing with "connection is not established"
> after the timeout was raised on a live link.

## File overview

- `config_flow.py` – UI-based setup (name, IP, port, device ID) + connection
  test, reconfigure flow, options flow (polling interval, timeout, room
  temperature source per heating circuit)
- `coordinator.py` – retries on the shared Modbus unit, periodic polling
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
- `diagnostics.py` – diagnostics download (see "Diagnostics download")

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
  `RegisterDef.named_values` is the exception: a raw value listed there is
  published as a named state instead of being left out. The error, warning
  and fault-free codes use it for `65535` ("Kein Fehlercode aktiv"), shown
  as `no_code` and translated per sensor ("No error", "No warning", "No
  error code") under the entity's `state` key, like an enum option; any
  other code stays a number.
- **Heating circuits** follow a pattern: values at `31<n>01 ...`, writable
  values at `41<n>03 ...` for circuit `<n>` = 1 ... 4. `_heating_circuit()`
  builds them, so circuits share translation keys and use the `{circuit}`
  placeholder (`RegisterDef.circuit` / `translation_key`).
- **Entity category and default.** `RegisterDef.entity_category` marks
  diagnostic values (fault/warning codes, status, operating hours,
  secondary temperatures) and rarely changed settings (config);
  `enabled_default=False` hides values not every installation has.
  Home Assistant applies `enabled_default` only when an entity is first
  registered, so changing it does not affect existing installations; the
  category applies on every start.
- **Adding a register:** add a `RegisterDef` to the matching list, then
  add the entity name to `strings.json` and `translations/de.json` (and
  `translations/en.json`, kept identical to `strings.json`). The test suite
  checks that every entity has a translation.
- **Registers beyond the official data point list.** `electrical_power_input`
  (`33126`) and `ELECTRICAL_STATISTICS_REGISTERS` (`36701 ... 36704`) are
  not in `83807301`; they come from community projects (see "Related
  projects and further reading" below). `electrical_power_input` is
  disabled by default since it is undocumented and not served by every
  controller. The electrical energy statistics count on the controllers
  seen so far, including the yearly total, and are enabled by default
  (except "yesterday", like the other statistics). The heating curve
  (`41x08`, `hcN_heating_curve`, slope x 0.01) is listed by
  Varitras/weishaupt_modbus and was read successfully on a BIBLOCK; it is
  read only (its limits are undocumented and the controller stores it in
  its EEPROM), diagnostic and disabled by default, and values outside
  0.01 ... 10.00 are treated as implausible. `fault_free_code`
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
- **Registers the controller does not have.** A register rejected with
  exception code 0x02 (illegal data address, `IllegalDataAddressError`)
  `CONSECUTIVE_FAILURES_BEFORE_DROP` times in a row is added to
  `_unsupported` and left out of the read plan until the entry reloads
  (enabling an entity reloads it, too). Its reading shows the outcome
  `unsupported`; the poll no longer splits its block into single reads
  every cycle. Other exception codes (e.g. device busy) keep the register
  polled.

## Connection, retries and timeout

The shared connection (`async_get_unit` in `__init__.py`) leaves retries
and recovery to its consumers; the coordinator handles them:

- **No retries, no reconnect within a request.** The library runs tmodbus
  with `retry_never` and `auto_reconnect=False`; a dropped link is only
  reopened by the *next* request. `async_with_retries()` (`coordinator.py`)
  wraps every request -- poll blocks, single reads, writes (including the
  PV heartbeat and the PV-surplus switch, which run concurrently with the
  poll):
  - `ModbusExceptionError` (the controller answered with an error code):
    final, no retry. The poll then falls back to single reads / function
    code 0x04 as described above.
  - `ModbusTimeoutError` / `ModbusDesyncError`: a failed answer, at most
    `MAX_FAILED_ANSWERS` (3). A timeout leaves the link open -- a peer that
    stopped answering would let every retry on it time out too -- so the
    link is dropped with `ModbusUnit.disconnect()` before the retry. A
    desync already drops it inside the library.
  - `ModbusConnectionError`: not a failed answer (the retry runs on a new
    link), at most `MAX_LOST_CONNECTIONS` (2) per request.
    `ClientClosedError` (the connection was closed for good) is final.
  - `RETRY_DELAY` (1 s) between attempts; each retry is logged at DEBUG
    with its reason and counters.
  Writes may be retried because every write of this integration sets an
  absolute value. A write that timed out may still have reached the
  controller, so a retry can cost one extra EEPROM cycle.
- **A poll that gets no answer stops at the first block** that exhausted
  its retries (`UpdateFailed`) instead of waiting out the same timeouts on
  every remaining block.
- **Short outages keep the last values.** `_async_update_data()` returns
  the previous data for up to `FAILED_POLLS_TOLERATED` (3) failed polls in
  a row before it lets `UpdateFailed` take the entities unavailable; the
  first refresh (no previous data) fails right away. This is on top of the
  per-register tolerance for rejected registers below.
- **Timeout.** `unit.require_timeout()` is set from the `timeout` option
  (3-10 s, default 5) before the first request: raising the timeout of a
  live link drops it, lowering it only applies to the next link. On one
  device the largest requirement of all consumers applies; the entry
  withdraws its own on unload (`require_timeout(None)`). Without a
  requirement the library uses 10 s. With 3 attempts, a short timeout keeps
  one unanswered request at roughly 3 x timeout + 2 s.
- **Idle connections.** Some devices close an idle TCP connection without
  a clean shutdown. The debug log then shows, on the first poll after a
  pause: timeout, timeout, "Connection lost", success. If that appears, the
  polling interval is above the device's idle limit (or a keepalive read is
  needed); the default 30 s has not shown it so far.
- **Config flow.** `async_get_temporary_unit()` shares the connection of an
  entry that already holds the device and otherwise closes it again. The
  connection only opens on the first request, so the test reads a real
  register (`CONNECTION_TEST_ADDRESS`).
- **Logs.** `manifest.json` lists the `modbus_connection` and `tmodbus`
  loggers, so "Enable debug logging" includes them. They are shared by every
  integration on Modbus: a tmodbus warning "Received unexpected response
  with Transaction ID ..." may belong to another integration (tell them
  apart by the response's length and content).
- **Tests.** `MockModbusUnit` from modbus-connection does not model
  reconnects or timeout semantics, so the retry logic is tested with
  `FakeUnit` (`tests/test_coordinator.py`), which raises scripted
  exception sequences. The simulator tests run over the real shared
  connection.

## Debug log

With debug logging enabled (integration page → *Enable debug logging*),
the coordinator logs what helps to improve the integration. None of its
lines contain the host, IP address or device name, so a log can be
attached to an issue; the `modbus_connection`/`tmodbus` library lines can
name the address, though.

| Line | Meaning |
|---|---|
| `Poll: 0.42 s, 18 requests (0 failed answers, 0 lost connections, 0 rejected), 33 registers: 32 published, 1 filtered, 0 rejected, 0 kept, 0 unsupported` | One per poll. *filtered*: read, but outside `valid_range` (no sensor, no setpoint request, ...). *rejected*: the controller refused the register. *kept*: refused, last value kept (see "Tolerating transient read failures"). *unsupported*: not on this controller, no longer polled (see "Registers the controller does not have"). |
| `Register outdoor_temp_2 (address 30002): raw 0x8000 = -32768, filtered (no sensor)` | Every polled register's raw word, once each time debug logging is switched on (and after setup). Tells a real `0.0` from a status word. Only registers of enabled entities are polled after setup. |
| `heat_pump_status: 19 (heating) -> 43 (oil_recirculation) after 9 min 10 s` | A change of the heat pump or system status, with how long the previous one lasted. |
| `Retrying read of ... after ModbusTimeoutError (failed answers 1/3, lost connections 0/2)` | A retry (see "Connection, retries and timeout"). |
| `Modbus connection lost (2 since setup)` | The link reported a drop (`on_connection_lost`). |
| `Poll failed after ... s` / `Keeping the last values (failed poll 1 of 3 tolerated)` | A poll without an answer, and whether the last values are kept. |
| `Writing dhw_normal_target_temp (address 42103): 55.0 (raw 550)` / `Wrote ...` / `Skipping write ...` / `... changed at the controller since the last poll ...` | The write path, including the fresh read before a skip. |

Logged as **warnings**, once per register and value (repeats are not
logged; the raw value dump shows them again), regardless of the log level
-- except during a poll of every register before entities subscribed
(setup), where registers of disabled entities and missing hardware are
expected to be odd and the finding goes to DEBUG:

- a status code not in `HEAT_PUMP_STATUS_MAP` -- worth adding to the map;
- a raw value outside `valid_range` that is not one of the
  `DOCUMENTED_SENTINELS` (`const.py`) -- an undocumented status word or a
  wrong range.

The counters (`coordinator.counters`, `connection_losses`,
`failed_polls_total`) and the last poll's `readings` (raw word, decoded
value, outcome per register) are kept on the coordinator for the
diagnostics download as well.

## Diagnostics download

`diagnostics.py` (`async_get_config_entry_diagnostics`) returns:

- `versions` -- Home Assistant, the integration, `modbus-connection`,
  `tmodbus`;
- `entry` -- data and options with `TO_REDACT` removed: host, name and the
  chosen source entities (PV surplus, room temperature per circuit), since
  entity ids often contain room or device names;
- `connection` -- whether the link is up, connection drops and the request
  counters (`RequestCounters`);
- `polling` -- interval, last success, failed polls in a row and in total,
  registers read via function code 0x04 (`input_fallback_addresses`),
  registers rejected in a row and registers no longer polled because the
  controller does not have them (`unsupported_registers`);
- `status` -- current heat pump / system status code and seconds in it;
- `writes` -- values written since the last poll, PV setpoint state;
- `registers` -- every register of the map by address with the last
  poll's raw word, decoded value, published value, outcome and the meaning
  of a filtered status word; registers of disabled entities show as
  `not polled`.

`tests/test_diagnostics.py` checks that host, title and chosen entity ids
appear nowhere in the serialized download. Add any new free-text option
to `TO_REDACT`.

## Writing and the controller's EEPROM

Every parameter write is stored in the controller's EEPROM (100,000 write
cycles). `async_write_value()` therefore skips a write whose value equals
the current one. "Current" is the value last written since the register
was last polled (`_written`), else the polled value — a refresh requested
after a write can be delayed by the coordinator's debouncer, so the polled
data alone may be stale right after a write. Before a write is actually
dropped, `_controller_holds()` reads the register fresh: the value may
have been changed at the controller's panel since the last poll. Only if
the controller confirms the value is the write skipped; a fresh read that
fails or is rejected lets the write go ahead (a needless write costs one
EEPROM cycle, a wrongly skipped one loses the setpoint). A skipped write still calls
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

## Following an external PV-surplus entity (`switch.py`)

Instead of an automation, the PV power setpoint can optionally be kept in
sync with an existing Home Assistant entity (e.g. a smart meter's PV
surplus sensor) automatically: `WeishauptPvSurplusFollowSwitch`
(`switch.py`) is only created when a source entity is configured
(`CONF_PV_SURPLUS_ENTITY_ID`, set in the options flow's `pv_surplus`
section). While on, it tracks the source entity with
`async_track_state_change_event()` -- the same mechanism
`WeishauptRoomClimate` uses for its `entity`-sourced room temperature
(`climate.py`) -- converts its value to watts via
`homeassistant.util.unit_conversion.PowerConverter` (so kW/MW sources work
without extra configuration) and writes it to
`SYSTEM_PV_SETPOINT_REGISTER`. A value the switch can't use right now
(`unknown`/`unavailable`/non-numeric) is skipped rather than written as
`0`, so a momentary gap in the source doesn't interrupt the setpoint --
the number entity's heartbeat (see above) keeps re-sending the last usable
value regardless. A negative value (e.g. currently importing) is clamped
to `0` and written, since it's a valid reading rather than a missing one.

**The switch and the number entity have no reference to each other**, but
share two plain attributes on the coordinator they both already hold a
reference to (`pv_setpoint_last_value`, `pv_surplus_follow_enabled` --
see `WeishauptModbusCoordinator.__init__`). Both entities update
`pv_setpoint_last_value` *before* writing, whichever of them writes last,
so there's no race between the switch writing `0` on "off" and the
heartbeat resending a now-stale value in between.

**While `pv_surplus_follow_enabled` is set, `WeishauptPvPowerSetpointNumber`
rejects direct writes and collapses `native_min_value`/`native_max_value`
to its current value** -- the exact pattern `WeishauptRoomClimate.min_temp`/
`max_temp` uses for a setpoint that currently can't be changed directly
(see "Room heating circuits" below): the range collapses instead of hiding
the number's value or feature, and `async_set_native_value()` still raises
`ServiceValidationError` as a backstop for a direct service call.

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

Everything runs in docker compose:

```bash
docker compose run --rm tests        # test suite
docker compose up                    # Home Assistant on http://localhost:8123
docker compose down -v               # stop and discard the HA configuration
```

The minimum supported Home Assistant version is 2026.10.1 (`hacs.json`).
The Home Assistant versions are set through environment variables (see
the header of `docker-compose.yml`):

| Variable | Used by | Default |
|---|---|---|
| `TEST_HA_VERSION` | test suite | `2026.10.1` (the minimum) |
| `TEST_PHACC_VERSION` | test suite | `0.13.372` (matches 2026.10.1) |
| `HA_VERSION` | `docker compose up`, end-to-end test | `2026.11.1` |

```bash
TEST_HA_VERSION=2026.10.4 TEST_PHACC_VERSION=0.13.375 docker compose run --build --rm tests
HA_VERSION=2026.10.1 docker compose --profile e2e run --rm e2e
```

GitHub Actions runs the test suite on the minimum version and on the newest
version `pytest-homeassistant-custom-component` is available for
(`.github/workflows/tests.yaml`), and the end-to-end test on the minimum and
the newest release (`.github/workflows/e2e.yaml`). Each
`pytest-homeassistant-custom-component` release pins exactly one Home
Assistant version, so the two variables of the test suite must match.

- `tests/modbus_simulator.py` simulates a heat pump. It lists the register
  values with the addresses from the data point list — deliberately not
  derived from `const.py`, so the tests check the register map — answers at
  most 5 registers per request and rejects unknown addresses. Heating
  circuits 3 and 4 are not installed in it. `docker compose up` starts it
  as the `modbus-simulator` service: add the integration with host
  `modbus-simulator` and port 502.
- `tests/test_const.py` checks the register map and the translations,
  `tests/test_coordinator.py` reading and writing,
  `tests/test_entities.py` and `tests/test_config_flow.py` the entities and
  flows against the simulator.

### End-to-end test

`docker compose --profile e2e run --rm e2e` starts a fresh Home Assistant
(`HA_VERSION`) and the Modbus simulator, then runs `tests/e2e/` against them
through Home Assistant's REST API: it sets up the integration through the
config flow, checks the simulator values, writes the PV setpoint, and
configures and toggles the PV-surplus switch. The same run executes in
GitHub Actions (`.github/workflows/e2e.yaml`) against each Home Assistant
version of its matrix.

The test uses only synthetic data. The owner account is created per run
with a random password and exists only in that run's Home Assistant
instance. The device title, the simulator host (its compose service name)
and the PV source entity are invented. Do not add real addresses, serial
numbers, names or other details of a real installation to the repository,
its test data or its logs.

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
  — another Home Assistant integration for the same heat pumps.
  Independently arrived at the same setpoint
  sentinel handling (`1`/`-32768` = "no setpoint request"), and reported
  the yearly energy counters staying at zero on every controller tested.
  Also the source of the heating curve register (`41x08`) and of what
  `33126` (`electrical_power_input`) measures:
  the heat pump's own electrical draw, without circulation pump and
  controller (see its [pull request](https://github.com/Varitras/weishaupt_modbus/pull/4)).
- [Ingmar Kaiser's blog on reading a Weishaupt heat pump via Modbus](https://www.ingmar-kaiser.de/blog/weishaupt/)
  — a Telegraf/Grafana monitoring setup; contributed the
  `ELECTRICAL_STATISTICS_REGISTERS` (`36701 ... 36704`) and helped find
  the missing `fault_free_code` (`30005`).
- The Home Assistant community thread ["Weishaupt Heatpump integration via modbus"](https://community.home-assistant.io/t/weishaupt-heatpump-integration-via-modbus/436823)
  — user reports of statistics registers only reporting whole kWh (the
  WEM portal shows more precision), registers occasionally stuck at
  factory-default values, and newer controller firmware exposing more
  registers than older ones.
