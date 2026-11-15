"""Weishaupt Heat Pump Integration (unofficial)."""
from __future__ import annotations

from homeassistant.components.modbus import async_get_unit
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError

from modbus_connection import ModbusTcpParams

from .const import (
    CONF_DEVICE_ID,
    CONF_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .coordinator import WeishauptModbusCoordinator

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.WATER_HEATER,
    Platform.CLIMATE,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Sets up the integration from a config entry."""
    name = entry.data.get(CONF_NAME, entry.title)
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]
    device_id = entry.data[CONF_DEVICE_ID]
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    timeout = entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

    # A unit on Home Assistant's shared Modbus connection. Home Assistant
    # releases it when this entry unloads and closes the connection behind
    # the last entry holding it.
    try:
        unit = async_get_unit(hass, entry, ModbusTcpParams(host=host, port=port), device_id)
    except HomeAssistantError as err:
        # The device is already in use over different link settings.
        raise ConfigEntryError(str(err)) from err

    # Set before the first request: raising the timeout of a live link drops
    # it. Withdrawn on unload, as another entry may keep the connection.
    unit.require_timeout(timeout)
    entry.async_on_unload(lambda: unit.require_timeout(None))

    coordinator = WeishauptModbusCoordinator(hass, unit, name, scan_interval)
    entry.async_on_unload(unit.on_connection_lost(coordinator.handle_connection_lost))
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change (e.g. polling interval, timeout,
    or a heating circuit's current-temperature source)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unloads the integration. The Modbus unit is released by Home Assistant."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
