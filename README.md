# Weishaupt Heat Pump Integration (unofficial)

![Weishaupt Heat Pump Integration (unofficial)](images/logo.png)

[GitHub repository](https://github.com/gojux/weishaupt_unofficial) — please
report issues or feature requests there.

Custom component for Home Assistant that connects to a Weishaupt heat pump
via **Modbus TCP** and exposes it as regular Home Assistant entities —
sensors, a domestic hot water (DHW) control, a system operating mode
selector and climate entities for up to 4 room heating circuits.

It uses the Modbus TCP data points of the Weishaupt air/water heat pumps
AEROBLOCK (WAB), BIBLOCK (WBB) and SPLITBLOCK (WSB) and the brine/water heat
pump GEOBLOCK (WGB).

Setup is done entirely through the Home Assistant UI — no YAML required.

## Disclaimer

This is an **unofficial**, community-developed integration and is **not
affiliated with, endorsed by, or supported by Weishaupt**. "Weishaupt" and
any related trademarks are the property of their respective owner.

This software is provided **"as is", without warranty of any kind**, and
is used **entirely at your own risk**. The author(s) assume **no
liability** for any damage, data loss, equipment malfunction, or other
harm resulting from the use of this integration, including but not
limited to unintended interaction with your heat pump's Modbus
interface.

For architecture details and implementation notes, see
[DEVELOPER.md](DEVELOPER.md).

## Requirements

This integration requires `pymodbus` version `3.11.0` or newer, which Home
Assistant installs automatically.

Not every data point exists on every installation — the hydraulic scheme of
your system determines which sensors and heating circuits are present, and
a data point can require a newer controller software version. Entities
whose value the controller does not provide show `unknown`.

## Installation

### Via HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=gojux&repository=weishaupt_unofficial&category=integration)

Requires [HACS](https://hacs.xyz) to be installed. Clicking the button
above adds this repository to HACS on your Home Assistant instance;
confirm the dialog there, then install the integration from HACS and
restart Home Assistant.

### Manual

1. Copy `custom_components/weishaupt_unofficial` into the
   `config/custom_components/` directory of your Home Assistant instance.
2. Restart Home Assistant.

### Enable Modbus TCP on the heat pump

On the system device (Systemgerät) open **Settings → Modbus TCP** and set:

| Setting | Value |
|---|---|
| Access | On |
| Network / network mask | matching your home network |
| TCP port | 502 |
| Slave address | 1 |

All controllers use slave address 1; they are told apart by their IP
address, so add one integration entry per controller.

### Setup

1. **Settings → Devices & Services → Add Integration** → search for
   "Weishaupt Heat Pump Integration (unofficial)".
2. Enter in the dialog:
   - **Name** for this heat pump (used as the device name in HA)
   - **IP address** of the controller
   - **Port** (default: 502)
   - **Modbus device ID** (default: 1)
   - optional: polling interval in seconds

Home Assistant tests the connection directly during setup. If it fails,
an error message is shown in the dialog.

You can change the IP address, port, name, or device ID later via the
entry's three-dot menu → **Reconfigure**.

## What you get

- **Sensors** — outdoor temperature, heat pump flow and return
  temperature, buffer temperature, power demand, per heating circuit room,
  flow and flow target temperature, domestic hot water temperature, and a
  translated status sensor (shows the controller's operating status as
  readable text in your HA language, e.g. "Heating" / "Defrost" /
  "Legionella protection"). Operating hours of the second heat generator
  and electric heaters as well as error, warning and fault-free codes are
  available too. An **electrical power input** sensor and **electrical
  energy statistics** are available disabled by default — they aren't in
  the official data point list this integration is otherwise based on, see
  [DEVELOPER.md](DEVELOPER.md) for where they come from.
- **Fault** binary sensor, plus binary sensors for the second heat
  generator and the electric heaters.
- **Energy statistics** — energy today, yesterday, this month and this
  year for total, heating, hot water and cooling.
- **Domestic hot water (DHW)** — a `water_heater` entity, plus number
  entities for the lowered setpoint and a one-off DHW charge ("push").
- **System operating mode** — a `select` entity: Automatic, Heating,
  Cooling, Summer, Standby, Second heat generator.
- **PV power setpoint** — a number entity to let the heat pump use up to a
  given power (in watts), e.g. from photovoltaic surplus. Set it back to
  `0` to return control to the heat pump's own regulation. It overrides
  the SG Ready function; since the controller doesn't save it, this entity
  automatically re-sends the current value every 30 seconds on its own for
  as long as it isn't `0`, so it stays active without any extra
  automation. An automation is only needed to change it when the actual
  available power changes. Mind the response time of the heating system.
- **Room heating circuits 1–4** — `climate` entities. Only heating circuit
  1 is enabled by default; enable further circuits and less common sensors
  under **Settings → Devices & Services → this integration → Entities**.

## Using the heating circuits (climate entities)

Each circuit has 5 operating modes, mapped onto Home Assistant's climate
controls as follows:

| Controller mode | Home Assistant |
|---|---|
| Standby | **Off** |
| Automatic | **Auto** |
| Comfort / Normal / Setback | **Heat**, with the mode selected via the **preset** dropdown |

- **Setting a target temperature** works in **Heat**: it changes the room
  setpoint of the active preset (comfort, normal or setback each have their
  own). In **Auto** and **Off** the room setpoint currently requested by
  the controller is shown, but can't be changed.
- **Switching to Heat** restores the preset that was last active.
- After changing a mode or temperature, the display updates immediately
  and the `mode_change_pending` / `temperature_change_pending` attribute
  (visible under "Attributes" in the entity's more-info dialog) shows
  `true` until the controller reports the new value.

### Room temperature source (per circuit)

By default, a heating circuit's current room temperature isn't shown
(only the target temperature). You can change this per circuit under
the entry's three-dot menu → **Configure**:

- **Don't show a current temperature** (default)
- **Read from heat pump** — uses the room sensor of that circuit, if one
  is installed
- **Use a Home Assistant entity** — pick any existing temperature sensor
  (e.g. a separate smart thermostat in that room) to use instead

## Using domestic hot water

The `water_heater` entity shows the tank temperature and lets you change
the **normal** target temperature. Its state is *Heating* while the heat
pump is heating hot water (including legionella protection) and *Idle*
otherwise. The controller offers no operating modes for hot water.

- **Lowered target temperature** — number entity for the setpoint used
  during the lowered time windows.
- **Push duration** — number entity for a one-off charge: `0` = off,
  otherwise 5 to 240 minutes in steps of 5.

## Options

Available under the entry's three-dot menu → **Configure**:

- **Polling interval** (seconds) — how often the integration reads data
  from the controller.
- **Room temperature source** — see above, configurable per heating
  circuit.

Changing any option takes effect immediately, without needing to remove
and re-add the integration.

## Protecting the controller's memory

The controller stores every changed parameter (operating modes and
setpoints) in its EEPROM, which supports a limited number of write cycles
(100,000 over its lifetime). This integration therefore never writes a
value that is already set, but **automations should not change setpoints or
modes continuously** — for example, don't adjust a setpoint every minute
to follow photovoltaic production. The **PV power setpoint** is the
exception: the controller does not save it to the EEPROM, so it is always
written, and following photovoltaic production with it is exactly what
it's for.

## Troubleshooting

**Some text still shows in English after switching Home Assistant to a
different language, or vice versa.** Custom integration translations are
cached by Home Assistant. Try, in order:

1. **Settings → System → Restart** (not just reloading the integration).
2. A hard refresh in your browser (Ctrl/Cmd+Shift+R), or fully closing
   and reopening the mobile app.

**Connection fails during setup.** Double-check the IP address, port,
and that Modbus TCP is enabled on the system device and reachable from
your Home Assistant instance (same network / no firewall blocking the
port).

**A sensor shows `unknown`.** The controller reports "no sensor" for it,
or the data point isn't available on your installation or controller
software version.

**A heating circuit shows an operating mode that differs from what you
set.** The heat pump ignores some settings during automatic operating
states such as defrosting or a utility lock. The status sensor shows the
current operating state.

**A sensor's value never changes, or an entity briefly goes unavailable
and comes back.** Community users have reported registers (e.g. the yearly
energy counters) that stay at their factory value regardless of runtime,
and values that intermittently fail to read. This integration keeps
showing a register's last known value for a few consecutive failed polls
rather than immediately going unavailable, so a single transient Modbus
error doesn't cause flapping; a value that never changes despite the heat
pump clearly having run is most likely a controller/firmware limitation, not
something this integration can work around. Consider asking your installer
about a controller software update.

## Known limitations

- The **SG Ready** inputs, the digital and analog inputs (H1, DE), room
  humidity and the constant flow temperature setpoints are not exposed by
  this integration.
- The **energy statistics** are exposed as reported by the controller
  (whole kWh, restarting at the beginning of each period). Community users
  have reported the yearly counters staying at zero on some controllers,
  and the reported thermal values not matching an external heat meter.
- The allowed setpoint ranges (comfort and normal 10–30 °C, setback
  5–20 °C, hot water 10–70 °C) are fixed defaults of this integration; the
  controller applies its own limits.

## Acknowledgements

This integration was developed with AI assistance.

## License

[MIT](LICENSE)
