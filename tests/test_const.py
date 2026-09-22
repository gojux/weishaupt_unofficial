"""Consistency checks of the register map, independent of a running device."""
from __future__ import annotations

import json
from pathlib import Path

from custom_components.weishaupt_unofficial.const import (
    ALL_REGISTERS,
    BINARY_SENSORS,
    HEAT_PUMP_STATUS_MAP,
    HEATING_CIRCUITS,
    NUMBER_REGISTERS,
    SENSOR_REGISTERS,
    STATUS_REGISTERS,
    SYSTEM_MODE_MAP,
)
from modbus_simulator import DEFAULT_VALUES

COMPONENT = Path(__file__).parent.parent / "custom_components" / "weishaupt_unofficial"


def test_keys_and_addresses_are_unique() -> None:
    keys = [reg.key for reg in ALL_REGISTERS]
    addresses = [reg.address for reg in ALL_REGISTERS]
    assert len(keys) == len(set(keys))
    assert len(addresses) == len(set(addresses))


def test_addresses_match_the_data_point_list() -> None:
    by_key = {reg.key: reg.address for reg in ALL_REGISTERS}
    assert by_key["outdoor_temp_1"] == 30001
    assert by_key["dhw_temp"] == 32102
    assert by_key["heat_pump_status"] == 33101
    assert by_key["system_mode"] == 40001
    assert by_key["dhw_push_duration"] == 42102
    assert by_key["dhw_normal_target_temp"] == 42103
    assert by_key["hc1_room_target_temp"] == 31101
    assert by_key["hc1_room_temp"] == 31102
    assert by_key["hc1_flow_target_temp"] == 31104
    assert by_key["hc1_flow_temp"] == 31105
    assert by_key["hc1_mode"] == 41103
    assert by_key["hc1_comfort_setpoint"] == 41105
    assert by_key["hc1_normal_setpoint"] == 41106
    assert by_key["hc1_setback_setpoint"] == 41107
    assert by_key["hc4_mode"] == 41403
    assert by_key["hc4_flow_temp"] == 31405
    assert by_key["energy_total_today"] == 36101
    assert by_key["energy_heating_year"] == 36204
    assert by_key["energy_dhw_month"] == 36303
    assert by_key["energy_cooling_yesterday"] == 36402
    assert by_key["pv_power_setpoint"] == 40002
    assert by_key["electrical_power_input"] == 33126
    assert by_key["fault_free_code"] == 30005
    assert by_key["energy_electrical_today"] == 36701
    assert by_key["energy_electrical_year"] == 36704


def test_valid_range_covers_every_sentinel_the_data_point_list_documents() -> None:
    """The documented sentinel values must fall outside their register's
    valid_range, so the range check filters them the same way an explicit
    list of sentinels would -- see const.py for why a range is preferred."""
    sensor_sentinels = (-32768, -32767, -32766, -32758, -32757)
    setpoint_sentinels = (-32768, 1)
    for reg in ALL_REGISTERS:
        if reg.valid_range is None:
            continue
        low, high = reg.valid_range
        sentinels = setpoint_sentinels if low == 50 else sensor_sentinels
        assert all(not (low <= s <= high) for s in sentinels), reg.key


def test_writable_registers_are_holding_registers() -> None:
    for reg in ALL_REGISTERS:
        assert (reg.register_type == "holding") == (reg.address >= 40000), reg.key
        if reg.writable:
            assert reg.address >= 40000, reg.key


def test_simulator_serves_every_register_of_installed_circuits() -> None:
    missing = {
        reg.key
        for reg in ALL_REGISTERS
        if reg.address not in DEFAULT_VALUES
        and not (reg.circuit is not None and reg.circuit > 2)
    }
    assert not missing


def test_status_map_covers_all_documented_codes() -> None:
    assert sorted(HEAT_PUMP_STATUS_MAP) == list(range(44))
    assert len(set(HEAT_PUMP_STATUS_MAP.values())) == 44


def _translation_keys(platform: str) -> set[str]:
    strings = json.loads((COMPONENT / "strings.json").read_text())
    return set(strings["entity"][platform])


def test_every_entity_has_a_translation() -> None:
    sensor_keys = {reg.translation_key or reg.key for reg in SENSOR_REGISTERS}
    sensor_keys |= {reg.key for reg in STATUS_REGISTERS}
    assert sensor_keys <= _translation_keys("sensor")
    assert {b.register.key for b in BINARY_SENSORS} <= _translation_keys("binary_sensor")
    assert {reg.key for reg in NUMBER_REGISTERS} <= _translation_keys("number")
    assert "system_mode" in _translation_keys("select")
    assert "dhw" in _translation_keys("water_heater")
    assert "heating_circuit" in _translation_keys("climate")


def test_translation_files_are_in_sync() -> None:
    def structure(node):
        if isinstance(node, dict):
            return {key: structure(value) for key, value in node.items()}
        return None

    strings = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())
    german = json.loads((COMPONENT / "translations" / "de.json").read_text())
    assert strings == english
    assert structure(german) == structure(english)


def test_status_and_mode_states_are_translated() -> None:
    strings = json.loads((COMPONENT / "strings.json").read_text())
    sensors = strings["entity"]["sensor"]
    expected = set(HEAT_PUMP_STATUS_MAP.values()) | {"unknown_status"}
    assert set(sensors["heat_pump_status"]["state"]) == expected
    assert set(sensors["system_status"]["state"]) == expected
    assert set(strings["entity"]["select"]["system_mode"]["state"]) == set(
        SYSTEM_MODE_MAP.values()
    )


def test_options_sections_exist_for_every_heating_circuit() -> None:
    strings = json.loads((COMPONENT / "strings.json").read_text())
    sections = strings["options"]["step"]["init"]["sections"]
    for hc in HEATING_CIRCUITS:
        assert hc.key in sections
