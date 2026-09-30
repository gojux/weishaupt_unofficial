"""Entities and services against the simulated heat pump."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.weishaupt_unofficial.const import CONF_PV_SURPLUS_ENTITY_ID, DOMAIN
from custom_components.weishaupt_unofficial.number import PV_SETPOINT_HEARTBEAT_INTERVAL

from conftest import Simulator, entity_id


async def call(hass: HomeAssistant, domain: str, service: str, **data) -> None:
    await hass.services.async_call(domain, service, data, blocking=True)


async def test_sensor_states(hass: HomeAssistant, integration) -> None:
    def state(platform: str, key: str):
        return hass.states.get(entity_id(hass, platform, integration, key))

    assert state("sensor", "outdoor_temp_1").state == "5.5"
    assert state("sensor", "dhw_temp").state == "48.2"
    assert state("sensor", "hc1_flow_temp").state == "34.2"
    assert state("sensor", "energy_total_year").state == "3900"
    assert state("sensor", "heat_pump_status").state == "heating"
    assert state("sensor", "heat_pump_status").attributes["raw_status_code"] == 19
    assert state("sensor", "power_demand").state == "45"
    assert state("binary_sensor", "fault").state == "off"
    assert state("select", "system_mode").state == "automatic"
    assert state("number", "dhw_lowered_target_temp").state == "40.0"


async def test_entities_without_data_have_no_value(
    hass: HomeAssistant, integration
) -> None:
    registry = er.async_get(hass)
    # Sentinel value 'no sensor' and 'no code active' are not published.
    assert hass.states.get(entity_id(hass, "sensor", integration, "error_code")).state == "unknown"
    # Circuits 2 ... 4 and optional sensors are disabled by default.
    for platform, key in (
        ("climate", "heating_circuit_2"),
        ("sensor", "outdoor_temp_2"),
        ("sensor", "energy_cooling_today"),
        ("sensor", "electrical_power_input"),
        ("binary_sensor", "e_heater_1_active"),
    ):
        entry = registry.async_get(entity_id(hass, platform, integration, key))
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key


async def test_water_heater(
    hass: HomeAssistant, integration, simulator: Simulator
) -> None:
    water_heater = entity_id(hass, "water_heater", integration, "dhw_water_heater")
    state = hass.states.get(water_heater)
    assert state.state == "idle"
    assert state.attributes["current_temperature"] == 48.2
    assert state.attributes["temperature"] == 50.0

    await call(hass, "water_heater", "set_temperature", entity_id=water_heater, temperature=55)
    assert await simulator.read(42103) == 550

    await simulator.write(33101, 20)
    await hass.data[DOMAIN][integration.entry_id].async_refresh()
    assert hass.states.get(water_heater).state == "heating"


async def test_number_and_select(
    hass: HomeAssistant, integration, simulator: Simulator
) -> None:
    push = entity_id(hass, "number", integration, "dhw_push_duration")
    await call(hass, "number", "set_value", entity_id=push, value=30)
    assert await simulator.read(42102) == 30

    lowered = entity_id(hass, "number", integration, "dhw_lowered_target_temp")
    await call(hass, "number", "set_value", entity_id=lowered, value=38)
    assert await simulator.read(42104) == 380

    mode = entity_id(hass, "select", integration, "system_mode")
    await call(hass, "select", "select_option", entity_id=mode, option="heating")
    assert await simulator.read(40001) == 1

    pv_setpoint = entity_id(hass, "number", integration, "pv_power_setpoint")
    await call(hass, "number", "set_value", entity_id=pv_setpoint, value=2500)
    assert await simulator.read(40002) == 2500
    # Writing 0 disables the PV setpoint and returns the controller to its
    # own regulation.
    await call(hass, "number", "set_value", entity_id=pv_setpoint, value=0)
    assert await simulator.read(40002) == 0


async def test_pv_setpoint_heartbeat_keeps_re_sending_the_value(
    hass: HomeAssistant, integration, simulator: Simulator
) -> None:
    """The PV setpoint isn't saved by the controller, so the entity re-sends
    the value it was last set to on its own every
    PV_SETPOINT_HEARTBEAT_INTERVAL, independently of anything polling it."""
    pv_setpoint = entity_id(hass, "number", integration, "pv_power_setpoint")
    await call(hass, "number", "set_value", entity_id=pv_setpoint, value=1500)
    assert await simulator.read(40002) == 1500

    # Change it directly on the simulated controller, bypassing this
    # integration, to prove the heartbeat re-asserts the last commanded
    # value rather than just confirming whatever is currently read back.
    await simulator.write(40002, 0)

    async_fire_time_changed(
        hass, dt_util.utcnow() + PV_SETPOINT_HEARTBEAT_INTERVAL + timedelta(seconds=1)
    )
    await hass.async_block_till_done()

    assert await simulator.read(40002) == 1500


async def test_pv_setpoint_heartbeat_stays_silent_once_deactivated(
    hass: HomeAssistant, integration, simulator: Simulator
) -> None:
    pv_setpoint = entity_id(hass, "number", integration, "pv_power_setpoint")
    await call(hass, "number", "set_value", entity_id=pv_setpoint, value=0)

    coordinator = hass.data[DOMAIN][integration.entry_id]
    with patch.object(
        coordinator.client, "write_register", wraps=coordinator.client.write_register
    ) as write:
        async_fire_time_changed(
            hass, dt_util.utcnow() + PV_SETPOINT_HEARTBEAT_INTERVAL + timedelta(seconds=1)
        )
        await hass.async_block_till_done()
        assert write.call_count == 0


async def test_pv_surplus_follow_switch_not_created_without_a_source(
    hass: HomeAssistant, integration
) -> None:
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "switch", DOMAIN, f"{integration.entry_id}_pv_surplus_follow"
        )
        is None
    )


async def test_pv_surplus_follow_switch(
    hass: HomeAssistant, make_integration, simulator: Simulator
) -> None:
    """While the switch is on, the PV power setpoint follows the
    configured source entity (converting its unit to watts) and can't be
    set directly; turning the switch off hands control back."""
    hass.states.async_set(
        "sensor.pv_surplus",
        "1.5",
        {"unit_of_measurement": "kW", "device_class": "power"},
    )
    entry = await make_integration(
        options={CONF_PV_SURPLUS_ENTITY_ID: "sensor.pv_surplus"}
    )
    follow = entity_id(hass, "switch", entry, "pv_surplus_follow")
    pv_setpoint = entity_id(hass, "number", entry, "pv_power_setpoint")

    await call(hass, "switch", "turn_on", entity_id=follow)
    assert hass.states.get(follow).state == "on"
    assert await simulator.read(40002) == 1500  # 1.5 kW -> 1500 W

    state = hass.states.get(pv_setpoint)
    assert state.state == "1500"
    assert state.attributes["min"] == 1500
    assert state.attributes["max"] == 1500
    with pytest.raises(ServiceValidationError):
        await call(hass, "number", "set_value", entity_id=pv_setpoint, value=999)

    # A later change of the source is written through immediately.
    hass.states.async_set(
        "sensor.pv_surplus",
        "2.2",
        {"unit_of_measurement": "kW", "device_class": "power"},
    )
    await hass.async_block_till_done()
    assert await simulator.read(40002) == 2200

    await call(hass, "switch", "turn_off", entity_id=follow)
    assert hass.states.get(follow).state == "off"
    assert await simulator.read(40002) == 0

    # The number entity is settable again, with its normal range restored.
    assert hass.states.get(pv_setpoint).attributes["max"] == 65535
    await call(hass, "number", "set_value", entity_id=pv_setpoint, value=321)
    assert await simulator.read(40002) == 321


async def test_pv_surplus_follow_switch_handles_bad_source_values(
    hass: HomeAssistant, make_integration, simulator: Simulator
) -> None:
    """A negative source value is clamped to 0 (a valid reading, e.g.
    currently importing); an unavailable source is skipped so the last
    written value stays active instead of being replaced."""
    hass.states.async_set(
        "sensor.pv_surplus", "500", {"unit_of_measurement": "W", "device_class": "power"}
    )
    entry = await make_integration(
        options={CONF_PV_SURPLUS_ENTITY_ID: "sensor.pv_surplus"}
    )
    follow = entity_id(hass, "switch", entry, "pv_surplus_follow")
    await call(hass, "switch", "turn_on", entity_id=follow)
    assert await simulator.read(40002) == 500

    hass.states.async_set(
        "sensor.pv_surplus", "-300", {"unit_of_measurement": "W", "device_class": "power"}
    )
    await hass.async_block_till_done()
    assert await simulator.read(40002) == 0

    # Something the integration itself would never write, to prove the
    # unavailable state below is genuinely skipped rather than overwritten.
    await simulator.write(40002, 777)
    hass.states.async_set("sensor.pv_surplus", "unavailable")
    await hass.async_block_till_done()
    assert await simulator.read(40002) == 777


async def test_climate_modes_and_presets(
    hass: HomeAssistant, integration, simulator: Simulator
) -> None:
    climate = entity_id(hass, "climate", integration, "heating_circuit_1")
    state = hass.states.get(climate)
    assert state.state == "heat"
    assert state.attributes["preset_mode"] == "normal"
    assert state.attributes["temperature"] == 21.0
    assert state.attributes["preset_modes"] == ["comfort", "normal", "setback"]
    # No room temperature source is configured by default.
    assert state.attributes["current_temperature"] is None

    await call(hass, "climate", "set_preset_mode", entity_id=climate, preset_mode="comfort")
    assert await simulator.read(41103) == 1
    state = hass.states.get(climate)
    assert state.attributes["preset_mode"] == "comfort"
    assert state.attributes["temperature"] == 22.0

    await call(hass, "climate", "set_temperature", entity_id=climate, temperature=22.5)
    assert await simulator.read(41105) == 225

    await call(hass, "climate", "set_hvac_mode", entity_id=climate, hvac_mode="off")
    assert await simulator.read(41103) == 4
    state = hass.states.get(climate)
    assert state.state == "off"
    assert state.attributes["preset_modes"] is None

    # Switching back to heating restores the last preset.
    await call(hass, "climate", "set_hvac_mode", entity_id=climate, hvac_mode="heat")
    assert await simulator.read(41103) == 1

    await call(hass, "climate", "set_hvac_mode", entity_id=climate, hvac_mode="auto")
    assert await simulator.read(41103) == 0


async def test_target_temperature_is_only_settable_while_heating(
    hass: HomeAssistant, integration, simulator: Simulator
) -> None:
    climate = entity_id(hass, "climate", integration, "heating_circuit_1")
    await call(hass, "climate", "set_hvac_mode", entity_id=climate, hvac_mode="auto")
    with pytest.raises(ServiceValidationError):
        await call(hass, "climate", "set_temperature", entity_id=climate, temperature=22)
    # The setpoint requested by the controller stays visible.
    assert hass.states.get(climate).attributes["temperature"] == 21.0
    for address in (41105, 41106, 41107):
        assert await simulator.read(address) in (220, 210, 180)


async def test_unchanged_setpoint_is_not_written(
    hass: HomeAssistant, integration
) -> None:
    climate = entity_id(hass, "climate", integration, "heating_circuit_1")
    coordinator = hass.data[DOMAIN][integration.entry_id]
    with patch.object(
        coordinator.client, "write_register", wraps=coordinator.client.write_register
    ) as write:
        await call(hass, "climate", "set_temperature", entity_id=climate, temperature=21.0)
        await call(hass, "climate", "set_preset_mode", entity_id=climate, preset_mode="normal")
        assert write.call_count == 0
    state = hass.states.get(climate)
    assert state.attributes["temperature_change_pending"] is False
    assert state.attributes["mode_change_pending"] is False


async def test_standby_and_other_modes_map_to_hvac_modes(
    hass: HomeAssistant, integration, simulator: Simulator
) -> None:
    climate = entity_id(hass, "climate", integration, "heating_circuit_1")
    coordinator = hass.data[DOMAIN][integration.entry_id]
    for raw, hvac, preset in (
        (0, "auto", None),
        (1, "heat", "comfort"),
        (2, "heat", "normal"),
        (3, "heat", "setback"),
        (4, "off", None),
    ):
        await simulator.write(41103, raw)
        await coordinator.async_refresh()
        state = hass.states.get(climate)
        assert state.state == hvac, raw
        assert state.attributes["preset_mode"] == preset, raw
