"""Config flow and options flow."""
from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.weishaupt_unofficial.const import DOMAIN

from conftest import Simulator, entity_id, free_port


async def test_user_flow_creates_entry(hass: HomeAssistant, simulator: Simulator) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Heat Pump",
            "host": "127.0.0.1",
            "port": simulator.port,
            "device_id": 1,
            "scan_interval": 30,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Heat Pump"
    await hass.async_block_till_done()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_user_flow_reports_unreachable_device(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "Heat Pump",
            "host": "127.0.0.1",
            "port": free_port(),
            "device_id": 1,
            "scan_interval": 30,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_options_flow_switches_room_temperature_source(
    hass: HomeAssistant, integration
) -> None:
    climate = entity_id(hass, "climate", integration, "heating_circuit_1")
    assert hass.states.get(climate).attributes["current_temperature"] is None

    result = await hass.config_entries.options.async_init(integration.entry_id)
    assert result["type"] is FlowResultType.FORM
    user_input = {"general": {"scan_interval": 30}}
    for number in range(1, 5):
        user_input[f"heating_circuit_{number}"] = {
            f"heating_circuit_{number}_current_temp_source": (
                "modbus" if number == 1 else "none"
            )
        }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert hass.states.get(climate).attributes["current_temperature"] == 20.8
