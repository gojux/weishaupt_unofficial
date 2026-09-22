"""Binary sensor entities for digital registers (fault, second heat generator)."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BINARY_SENSORS, DOMAIN, BinarySensorDef
from .coordinator import WeishauptModbusCoordinator
from .entity import WeishauptRegisterEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WeishauptModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WeishauptBinarySensor(coordinator, entry, definition)
        for definition in BINARY_SENSORS
    )


class WeishauptBinarySensor(WeishauptRegisterEntity, BinarySensorEntity):
    """A digital register: 0 = off, 1 = on, anything else is undefined."""

    def __init__(
        self,
        coordinator: WeishauptModbusCoordinator,
        entry: ConfigEntry,
        definition: BinarySensorDef,
    ) -> None:
        super().__init__(coordinator, entry, definition.register)
        self._on_value = definition.on_value
        self._attr_device_class = definition.device_class

    @property
    def is_on(self) -> bool | None:
        raw = self.register_value
        if raw not in (0, 1):
            return None
        return raw == self._on_value
