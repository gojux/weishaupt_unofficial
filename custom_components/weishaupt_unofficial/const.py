"""Constants and Modbus register map for the Weishaupt heat pump integration.

Register addresses are taken 1:1 from the Weishaupt data point list
"Modbus TCP (WWP)" (83807301): the number printed there is the address used
on the wire, without any offset. Read-only values (3xxxx) are input
registers; values that can be written (4xxxx) are holding registers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from homeassistant.components.climate import HVACMode, PRESET_COMFORT

DOMAIN = "weishaupt_unofficial"

CONF_DEVICE_ID = "device_id"
# Options-flow key for the optional PV-surplus source entity (see switch.py).
CONF_PV_SURPLUS_ENTITY_ID = "pv_surplus_entity_id"

DEFAULT_NAME = "Heat Pump"
DEFAULT_PORT = 502
DEFAULT_DEVICE_ID = 1
DEFAULT_SCAN_INTERVAL = 30  # seconds

# The controller answers at most this many consecutive registers per request.
MAX_REGISTERS_PER_READ = 5

# A register that fails to read for this many consecutive polls in a row is
# dropped (entities relying on it go unavailable/unknown); fewer failures
# than this keep showing the last known value instead, so a single
# transient Modbus error does not immediately flap an entity's state.
CONSECUTIVE_FAILURES_BEFORE_DROP = 4

RegisterType = Literal["holding", "input"]
DataType = Literal["int16", "uint16"]

# Register read to check that the controller answers (outdoor temperature 1).
CONNECTION_TEST_ADDRESS = 30001

# Raw value ranges the data point list documents for each register format --
# a raw value outside its register's range is not published (see
# RegisterDef.valid_range / WeishauptModbusCoordinator._store()). A sensor
# reports values like -32768 (no sensor) or -32767 (open circuit) outside
# its range instead of a measurement; a setpoint reports -32768 or 1 while
# no setpoint request is active; an error/warning code reports 65535 while
# nothing is active. Checking the documented range instead of enumerating
# every such raw value also covers codes the data point list does not name.
SENSOR_RAW_RANGE = (-500, 5000)      # "Format Sensor": -50.0 ... 500.0°C
SETPOINT_RAW_RANGE = (50, 5000)      # "FormatSollwert": 5.0 ... 500.0°C
CODE_RAW_RANGE = (0, 65534)          # "FormatFehlermeldung": error/warning code


@dataclass(frozen=True)
class RegisterDef:
    """Describes a single 16-bit Modbus register."""

    key: str                     # unique internal key, also the translation key
    address: int                 # Modbus register address as printed in the data point list
    register_type: RegisterType = "input"
    data_type: DataType = "int16"
    scale: float = 1.0           # raw_value * scale = displayed value
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = "measurement"
    writable: bool = False
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None
    precision: int | None = None  # suggested number of displayed decimals
    enabled_default: bool = True
    # Inclusive range of raw values considered available; a value outside
    # is not published. None means every raw value is accepted.
    valid_range: tuple[int, int] | None = None
    # Heating circuit number for registers whose name contains it; passed to
    # the translation as the {circuit} placeholder.
    circuit: int | None = None
    # Translation key if it differs from `key` (registers of several
    # heating circuits share one translation).
    translation_key: str | None = None
    # False for registers the controller does not store in its EEPROM (e.g.
    # SYSTEM_PV_SETPOINT_REGISTER): writing the same value again is not
    # skipped, since such a register must be re-sent to stay in effect
    # rather than being written once and left alone.
    skip_unchanged_write: bool = True


@dataclass(frozen=True)
class BinarySensorDef:
    """A digital register (0 / 1) exposed as a binary sensor."""

    register: RegisterDef
    device_class: str | None = None
    on_value: int = 1


def _temperature(
    key: str,
    address: int,
    *,
    enabled_default: bool = True,
    circuit: int | None = None,
    translation_key: str | None = None,
) -> RegisterDef:
    return RegisterDef(
        key=key,
        address=address,
        scale=0.1,
        precision=1,
        unit="°C",
        device_class="temperature",
        valid_range=SENSOR_RAW_RANGE,
        enabled_default=enabled_default,
        circuit=circuit,
        translation_key=translation_key,
    )


def _operating_hours(key: str, address: int) -> RegisterDef:
    return RegisterDef(
        key=key,
        address=address,
        data_type="uint16",
        unit="h",
        device_class="duration",
        state_class="total_increasing",
        enabled_default=False,
    )


# --------------------------------------------------------------------------
# System, heat pump and domestic hot water (DHW) values
# --------------------------------------------------------------------------

SYSTEM_STATUS_REGISTER = RegisterDef(
    key="system_status", address=30006, data_type="uint16", state_class=None,
    enabled_default=False,
)
HEAT_PUMP_STATUS_REGISTER = RegisterDef(
    key="heat_pump_status", address=33101, data_type="uint16", state_class=None
)
STATUS_REGISTERS: list[RegisterDef] = [
    SYSTEM_STATUS_REGISTER,
    HEAT_PUMP_STATUS_REGISTER,
]

DHW_TEMP_REGISTER = _temperature("dhw_temp", 32102)
DHW_ACTIVE_TARGET_TEMP_REGISTER = RegisterDef(
    key="dhw_active_target_temp",
    address=32101,
    scale=0.1,
    precision=1,
    unit="°C",
    device_class="temperature",
    valid_range=SETPOINT_RAW_RANGE,
)

# Normal setpoint: the target temperature of the water_heater entity.
DHW_NORMAL_TARGET_TEMP_REGISTER = RegisterDef(
    key="dhw_normal_target_temp",
    address=42103,
    register_type="holding",
    scale=0.1,
    unit="°C",
    device_class="temperature",
    writable=True,
    min_value=10.0,
    max_value=70.0,
    step=1.0,
    valid_range=SETPOINT_RAW_RANGE,
)
DHW_LOWERED_TARGET_TEMP_REGISTER = RegisterDef(
    key="dhw_lowered_target_temp",
    address=42104,
    register_type="holding",
    scale=0.1,
    unit="°C",
    device_class="temperature",
    writable=True,
    min_value=10.0,
    max_value=70.0,
    step=1.0,
    valid_range=SETPOINT_RAW_RANGE,
)
# Runtime of a one-off DHW charge; 0 = inactive, otherwise 5 ... 240 minutes.
DHW_PUSH_REGISTER = RegisterDef(
    key="dhw_push_duration",
    address=42102,
    register_type="holding",
    data_type="uint16",
    unit="min",
    state_class=None,
    writable=True,
    min_value=0,
    max_value=240,
    step=5,
)

# Power the heat pump may use for heating (e.g. surplus PV production), in
# watts. Unlike other writable registers, this one is not stored in the
# controller's EEPROM and has no write-cycle limit (data point list, 4.3
# "Datenpunkt für PV-Eigenstromnutzung") -- skip_unchanged_write=False keeps
# it out of the EEPROM-protecting write skip in async_write_value(), since
# it must be re-sent to stay in effect. Writing 0 disables it again and
# returns control to the controller's own regulation; an active PV setpoint
# overrides SG Ready.
SYSTEM_PV_SETPOINT_REGISTER = RegisterDef(
    key="pv_power_setpoint",
    address=40002,
    register_type="holding",
    data_type="uint16",
    unit="W",
    device_class="power",
    state_class=None,
    writable=True,
    min_value=0,
    max_value=65535,
    step=100,
    skip_unchanged_write=False,
)

NUMBER_REGISTERS: list[RegisterDef] = [
    DHW_LOWERED_TARGET_TEMP_REGISTER,
    DHW_PUSH_REGISTER,
    SYSTEM_PV_SETPOINT_REGISTER,
]

# Status codes (see HEAT_PUMP_STATUS_MAP) during which DHW is being heated,
# and the water_heater states derived from them.
DHW_ACTIVE_STATUS_CODES: tuple[int, ...] = (20, 21)
DHW_STATE_HEATING = "heating"
DHW_STATE_IDLE = "idle"

# System-wide operating mode (read and write).
SYSTEM_MODE_REGISTER = RegisterDef(
    key="system_mode",
    address=40001,
    register_type="holding",
    data_type="uint16",
    state_class=None,
    writable=True,
)
SYSTEM_MODE_MAP: dict[int, str] = {
    0: "automatic",
    1: "heating",
    2: "cooling",
    3: "summer",
    4: "standby",
    5: "second_generator",
}
SYSTEM_MODE_TO_RAW: dict[str, int] = {
    mode: raw for raw, mode in SYSTEM_MODE_MAP.items()
}

# --------------------------------------------------------------------------
# Heating circuits 1 ... 4
#
# Read-only values live at 31<n>01 ..., writable ones at 41<n>03 ... where
# <n> is the circuit number.
# --------------------------------------------------------------------------

# Room setpoint registers are written per operating mode; the range applies
# to all circuits.
COMFORT_SETPOINT_RANGE = (10.0, 30.0)
NORMAL_SETPOINT_RANGE = (10.0, 30.0)
SETBACK_SETPOINT_RANGE = (5.0, 20.0)

# Circuit operating mode register values.
HC_MODE_AUTOMATIC = 0
HC_MODE_COMFORT = 1
HC_MODE_NORMAL = 2
HC_MODE_SETBACK = 3
HC_MODE_STANDBY = 4

# Presets are only offered while the circuit is in HVACMode.HEAT.
PRESET_NORMAL = "normal"
PRESET_SETBACK = "setback"
DEFAULT_PRESET = PRESET_NORMAL

HC_MODE_TO_HVAC: dict[int, HVACMode] = {
    HC_MODE_AUTOMATIC: HVACMode.AUTO,
    HC_MODE_COMFORT: HVACMode.HEAT,
    HC_MODE_NORMAL: HVACMode.HEAT,
    HC_MODE_SETBACK: HVACMode.HEAT,
    HC_MODE_STANDBY: HVACMode.OFF,
}
HC_MODE_TO_PRESET: dict[int, str] = {
    HC_MODE_COMFORT: PRESET_COMFORT,
    HC_MODE_NORMAL: PRESET_NORMAL,
    HC_MODE_SETBACK: PRESET_SETBACK,
}
PRESET_TO_HC_MODE: dict[str, int] = {
    preset: raw for raw, preset in HC_MODE_TO_PRESET.items()
}
CLIMATE_PRESET_OPTIONS: list[str] = list(HC_MODE_TO_PRESET.values())


@dataclass(frozen=True)
class HeatingCircuit:
    """The registers belonging to one heating circuit."""

    key: str
    number: int
    mode_reg: RegisterDef
    room_target_reg: RegisterDef    # active room setpoint (read only)
    room_temp_reg: RegisterDef
    flow_target_reg: RegisterDef
    flow_temp_reg: RegisterDef
    comfort_reg: RegisterDef
    normal_reg: RegisterDef
    setback_reg: RegisterDef
    enabled_default: bool = True

    def setpoint_reg_for_preset(self, preset: str) -> RegisterDef:
        """The writable room setpoint register belonging to a preset."""
        return {
            PRESET_COMFORT: self.comfort_reg,
            PRESET_NORMAL: self.normal_reg,
            PRESET_SETBACK: self.setback_reg,
        }[preset]


def _room_setpoint(
    key: str, address: int, value_range: tuple[float, float], number: int
) -> RegisterDef:
    return RegisterDef(
        key=key,
        address=address,
        register_type="holding",
        scale=0.1,
        unit="°C",
        device_class="temperature",
        writable=True,
        min_value=value_range[0],
        max_value=value_range[1],
        step=0.5,
        valid_range=SETPOINT_RAW_RANGE,
        circuit=number,
    )


def _heating_circuit(number: int) -> HeatingCircuit:
    read_base = 31000 + 100 * number
    write_base = 41000 + 100 * number
    enabled = number == 1
    prefix = f"hc{number}"
    return HeatingCircuit(
        key=f"heating_circuit_{number}",
        number=number,
        mode_reg=RegisterDef(
            key=f"{prefix}_mode",
            address=write_base + 3,
            register_type="holding",
            data_type="uint16",
            state_class=None,
            writable=True,
            circuit=number,
        ),
        room_target_reg=RegisterDef(
            key=f"{prefix}_room_target_temp",
            address=read_base + 1,
            scale=0.1,
            valid_range=SETPOINT_RAW_RANGE,
            circuit=number,
        ),
        room_temp_reg=_temperature(
            f"{prefix}_room_temp",
            read_base + 2,
            enabled_default=enabled,
            circuit=number,
            translation_key="hc_room_temp",
        ),
        flow_target_reg=RegisterDef(
            key=f"{prefix}_flow_target_temp",
            address=read_base + 4,
            scale=0.1,
            precision=1,
            unit="°C",
            device_class="temperature",
            valid_range=SETPOINT_RAW_RANGE,
            enabled_default=enabled,
            circuit=number,
            translation_key="hc_flow_target_temp",
        ),
        flow_temp_reg=_temperature(
            f"{prefix}_flow_temp",
            read_base + 5,
            enabled_default=enabled,
            circuit=number,
            translation_key="hc_flow_temp",
        ),
        comfort_reg=_room_setpoint(
            f"{prefix}_comfort_setpoint", write_base + 5, COMFORT_SETPOINT_RANGE, number
        ),
        normal_reg=_room_setpoint(
            f"{prefix}_normal_setpoint", write_base + 6, NORMAL_SETPOINT_RANGE, number
        ),
        setback_reg=_room_setpoint(
            f"{prefix}_setback_setpoint", write_base + 7, SETBACK_SETPOINT_RANGE, number
        ),
        enabled_default=enabled,
    )


HEATING_CIRCUITS: list[HeatingCircuit] = [_heating_circuit(n) for n in range(1, 5)]

# --------------------------------------------------------------------------
# Sensors
# --------------------------------------------------------------------------

# Energy statistics: <category> x <period>, all uint16 in kWh. The counters
# restart at the beginning of their period, "yesterday" is a plain value.
STATISTICS_CATEGORIES: dict[str, int] = {
    "total": 1,
    "heating": 2,
    "dhw": 3,
    "cooling": 4,
}
STATISTICS_PERIODS: dict[str, int] = {
    "today": 1,
    "yesterday": 2,
    "month": 3,
    "year": 4,
}
STATISTICS_REGISTERS: list[RegisterDef] = [
    RegisterDef(
        key=f"energy_{category}_{period}",
        address=36000 + 100 * category_no + period_no,
        data_type="uint16",
        unit="kWh",
        device_class="energy",
        state_class=None if period == "yesterday" else "total_increasing",
        enabled_default=category != "cooling",
    )
    for category, category_no in STATISTICS_CATEGORIES.items()
    for period, period_no in STATISTICS_PERIODS.items()
]

# Electrical energy statistics at 36701 ... 36704 (today/yesterday/month/
# year), the same format as STATISTICS_REGISTERS but not in the official
# data point list (83807301); reported by a community Weishaupt monitoring
# project (https://www.ingmar-kaiser.de/blog/weishaupt/). Disabled by
# default since unconfirmed against the official document.
ELECTRICAL_STATISTICS_REGISTERS: list[RegisterDef] = [
    RegisterDef(
        key=f"energy_electrical_{period}",
        address=36700 + period_no,
        data_type="uint16",
        unit="kWh",
        device_class="energy",
        state_class=None if period == "yesterday" else "total_increasing",
        enabled_default=False,
    )
    for period, period_no in STATISTICS_PERIODS.items()
]

SENSOR_REGISTERS: list[RegisterDef] = [
    _temperature("outdoor_temp_1", 30001),
    _temperature("outdoor_temp_2", 30002, enabled_default=False),
    RegisterDef(
        key="error_code",
        address=30003,
        data_type="uint16",
        state_class=None,
        valid_range=CODE_RAW_RANGE,
    ),
    RegisterDef(
        key="warning_code",
        address=30004,
        data_type="uint16",
        state_class=None,
        valid_range=CODE_RAW_RANGE,
        enabled_default=False,
    ),
    RegisterDef(
        key="fault_free_code",
        address=30005,
        data_type="uint16",
        state_class=None,
        valid_range=CODE_RAW_RANGE,
        enabled_default=False,
    ),
    RegisterDef(
        key="power_demand",
        address=33103,
        data_type="uint16",
        unit="%",
    ),
    # Not in the official data point list (83807301); used by the evcc
    # project's Weishaupt charger (github.com/evcc-io/evcc,
    # charger/weishaupt.go) and described in
    # github.com/Varitras/weishaupt_modbus (pull request 4). Disabled by
    # default since it is undocumented.
    RegisterDef(
        key="electrical_power_input",
        address=33126,
        data_type="uint16",
        unit="W",
        device_class="power",
        enabled_default=False,
    ),
    _temperature("heat_pump_flow_temp", 33104),
    _temperature("heat_pump_return_temp", 33105),
    _temperature("hydraulic_separator_temp", 33108, enabled_default=False),
    _temperature("regenerative_temp", 33109, enabled_default=False),
    _temperature("buffer_temp", 33110),
    _temperature("total_flow_temp", 33111, enabled_default=False),
    DHW_TEMP_REGISTER,
    DHW_ACTIVE_TARGET_TEMP_REGISTER,
    *(
        reg
        for hc in HEATING_CIRCUITS
        for reg in (hc.room_temp_reg, hc.flow_target_reg, hc.flow_temp_reg)
    ),
    _operating_hours("second_generator_operating_hours", 34102),
    _operating_hours("e_heater_1_operating_hours", 34106),
    _operating_hours("e_heater_2_operating_hours", 34107),
    *STATISTICS_REGISTERS,
    *ELECTRICAL_STATISTICS_REGISTERS,
]

BINARY_SENSORS: list[BinarySensorDef] = [
    # 0 = fault active, 1 = operating without fault.
    BinarySensorDef(
        RegisterDef(
            key="fault", address=33102, data_type="uint16", state_class=None
        ),
        device_class="problem",
        on_value=0,
    ),
    BinarySensorDef(
        RegisterDef(
            key="second_generator_active",
            address=34101,
            data_type="uint16",
            state_class=None,
            enabled_default=False,
        ),
        device_class="running",
    ),
    BinarySensorDef(
        RegisterDef(
            key="e_heater_1_active",
            address=34104,
            data_type="uint16",
            state_class=None,
            enabled_default=False,
        ),
        device_class="running",
    ),
    BinarySensorDef(
        RegisterDef(
            key="e_heater_2_active",
            address=34105,
            data_type="uint16",
            state_class=None,
            enabled_default=False,
        ),
        device_class="running",
    ),
]

# --------------------------------------------------------------------------
# Operating status (heat pump 33101 / system 30006)
# --------------------------------------------------------------------------

# Raw status code -> stable, machine-readable key. The displayed text lives
# in strings.json / translations/*.json.
HEAT_PUMP_STATUS_MAP: dict[int, str] = {
    0: "undefined",
    1: "relay_test",
    2: "emergency_stop",
    3: "diagnosis",
    4: "manual_mode",
    5: "manual_heating",
    6: "manual_cooling",
    7: "manual_defrost",
    8: "defrost",
    9: "second_generator",
    10: "utility_lock",
    11: "sg_tariff",
    12: "sg_maximum",
    13: "tariff_charging",
    14: "increased_operation",
    15: "idle_time",
    16: "standby",
    17: "flushing",
    18: "frost_protection",
    19: "heating",
    20: "hot_water",
    21: "legionella_protection",
    22: "heating_cooling_switchover",
    23: "cooling",
    24: "passive_cooling",
    25: "summer_mode",
    26: "swimming_pool",
    27: "vacation",
    28: "screed_drying",
    29: "blocked",
    30: "lock_outdoor_temp",
    31: "lock_summer",
    32: "lock_winter",
    33: "operating_limit",
    34: "heating_circuit_lock",
    35: "readiness",
    36: "regenerative",
    37: "sgr3_heating",
    38: "sgr3_cooling",
    39: "sgr3_hot_water",
    40: "sgr4_heating",
    41: "sgr4_cooling",
    42: "sgr4_hot_water",
    43: "oil_recirculation",
}

# Fallback key for codes not in HEAT_PUMP_STATUS_MAP. Must be part of the
# enum sensor's options, since Home Assistant rejects other values.
HEAT_PUMP_STATUS_UNKNOWN = "unknown_status"

HEAT_PUMP_STATUS_OPTIONS: list[str] = [
    *HEAT_PUMP_STATUS_MAP.values(),
    HEAT_PUMP_STATUS_UNKNOWN,
]

# --------------------------------------------------------------------------
# Room temperature source (per heating circuit, set in the options flow)
#
#   - "none":   no current temperature is shown
#   - "modbus": the circuit's room temperature register
#   - "entity": mirror the state of an existing Home Assistant sensor
# --------------------------------------------------------------------------

CURRENT_TEMP_SOURCE_NONE = "none"
CURRENT_TEMP_SOURCE_MODBUS = "modbus"
CURRENT_TEMP_SOURCE_ENTITY = "entity"
CURRENT_TEMP_SOURCE_OPTIONS: list[str] = [
    CURRENT_TEMP_SOURCE_NONE,
    CURRENT_TEMP_SOURCE_MODBUS,
    CURRENT_TEMP_SOURCE_ENTITY,
]
DEFAULT_CURRENT_TEMP_SOURCE = CURRENT_TEMP_SOURCE_NONE


def current_temp_source_key(hc: HeatingCircuit) -> str:
    """Options-flow storage key for the chosen current-temperature source."""
    return f"{hc.key}_current_temp_source"


def current_temp_entity_key(hc: HeatingCircuit) -> str:
    """Options-flow storage key for the chosen Home Assistant source entity."""
    return f"{hc.key}_current_temp_entity_id"


def _unique_registers() -> list[RegisterDef]:
    registers: dict[str, RegisterDef] = {}
    circuit_registers = [
        reg
        for hc in HEATING_CIRCUITS
        for reg in (
            hc.mode_reg,
            hc.room_target_reg,
            hc.room_temp_reg,
            hc.flow_target_reg,
            hc.flow_temp_reg,
            hc.comfort_reg,
            hc.normal_reg,
            hc.setback_reg,
        )
    ]
    for reg in [
        *SENSOR_REGISTERS,
        *STATUS_REGISTERS,
        *NUMBER_REGISTERS,
        *circuit_registers,
        *(sensor.register for sensor in BINARY_SENSORS),
        DHW_NORMAL_TARGET_TEMP_REGISTER,
        SYSTEM_MODE_REGISTER,
    ]:
        registers[reg.key] = reg
    return list(registers.values())


# Every register the coordinator can poll, without duplicates.
ALL_REGISTERS: list[RegisterDef] = _unique_registers()
