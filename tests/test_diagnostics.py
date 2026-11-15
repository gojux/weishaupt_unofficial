"""Diagnostics download."""
from __future__ import annotations

import json

from homeassistant.core import HomeAssistant

from custom_components.weishaupt_unofficial.const import CONF_PV_SURPLUS_ENTITY_ID, DOMAIN
from custom_components.weishaupt_unofficial.diagnostics import (
    async_get_config_entry_diagnostics,
)


async def test_diagnostics_hold_registers_and_statistics(
    hass: HomeAssistant, integration
) -> None:
    # The first refresh at setup polls everything; later polls only what
    # enabled entities need.
    await hass.data[DOMAIN][integration.entry_id].async_refresh()
    diagnostics = await async_get_config_entry_diagnostics(hass, integration)

    assert diagnostics["versions"]["home_assistant"]
    assert diagnostics["versions"]["modbus-connection"]
    assert diagnostics["entry"]["data"]["device_id"] == 1
    assert diagnostics["connection"]["requests"] > 0
    assert diagnostics["polling"]["last_update_success"] is True
    assert diagnostics["status"]["heat_pump_status"]["name"] == "heating"

    registers = {reg["key"]: reg for reg in diagnostics["registers"]}
    assert registers["outdoor_temp_1"] == {
        "address": 30001,
        "key": "outdoor_temp_1",
        "raw": "0x0037",
        "value": 55,
        "published": 5.5,
        "outcome": "published",
        "detail": None,
    }
    assert registers["error_code"]["detail"] == "no code active"
    # Disabled entities do not subscribe: their registers are not polled.
    assert registers["outdoor_temp_2"]["outcome"] == "not polled"
    # Serializable as the download is.
    json.dumps(diagnostics)


async def test_diagnostics_leave_out_what_identifies_the_home(
    hass: HomeAssistant, make_integration
) -> None:
    hass.states.async_set(
        "sensor.living_room_temperature", "21.0",
        {"unit_of_measurement": "°C", "device_class": "temperature"},
    )
    hass.states.async_set(
        "sensor.pv_surplus", "500", {"unit_of_measurement": "W", "device_class": "power"}
    )
    entry = await make_integration(
        {
            "heating_circuit_1_current_temp_source": "entity",
            "heating_circuit_1_current_temp_entity_id": "sensor.living_room_temperature",
            CONF_PV_SURPLUS_ENTITY_ID: "sensor.pv_surplus",
        }
    )

    text = json.dumps(await async_get_config_entry_diagnostics(hass, entry))

    for secret in (
        entry.data["host"],
        entry.title,
        "living_room",
        "sensor.pv_surplus",
    ):
        assert secret not in text, secret
    # The settings themselves stay visible.
    assert '"heating_circuit_1_current_temp_source": "entity"' in text
