"""Select entity for the system-wide operating mode."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SYSTEM_MODE_MAP,
    SYSTEM_MODE_REGISTER,
    SYSTEM_MODE_TO_RAW,
)
from .coordinator import WeishauptModbusCoordinator
from .entity import WeishauptRegisterEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WeishauptModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WeishauptSystemModeSelect(coordinator, entry)])


class WeishauptSystemModeSelect(WeishauptRegisterEntity, SelectEntity):
    """The system operating mode (automatic, heating, cooling, ...)."""

    _attr_options = list(SYSTEM_MODE_MAP.values())

    def __init__(
        self, coordinator: WeishauptModbusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, SYSTEM_MODE_REGISTER)

    @property
    def current_option(self) -> str | None:
        raw = self.register_value
        if raw is None:
            return None
        return SYSTEM_MODE_MAP.get(int(raw))

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_write_value(
            SYSTEM_MODE_REGISTER, SYSTEM_MODE_TO_RAW[option]
        )
