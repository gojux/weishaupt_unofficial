"""Water heater entity for the domestic hot water (DHW) control."""
from __future__ import annotations

from typing import Any

from homeassistant.components.water_heater import (
    ATTR_TEMPERATURE,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DHW_ACTIVE_STATUS_CODES,
    DHW_NORMAL_TARGET_TEMP_REGISTER,
    DHW_STATE_HEATING,
    DHW_STATE_IDLE,
    DHW_TEMP_REGISTER,
    DOMAIN,
    HEAT_PUMP_STATUS_REGISTER,
)
from .coordinator import WeishauptModbusCoordinator
from .entity import WeishauptEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WeishauptModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([WeishauptWaterHeater(coordinator, entry)])


class WeishauptWaterHeater(WeishauptEntity, WaterHeaterEntity):
    """The domestic hot water tank.

    The controller has no DHW operating mode register, so the entity offers
    no operation modes: it shows the tank temperature and lets the normal
    setpoint be changed. The state tells whether the heat pump is currently
    heating hot water. The lowered setpoint and the one-off charge ("push")
    are exposed as number entities.
    """

    _attr_translation_key = "dhw"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE

    def __init__(
        self, coordinator: WeishauptModbusCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(
            coordinator,
            entry,
            [
                DHW_TEMP_REGISTER.key,
                DHW_NORMAL_TARGET_TEMP_REGISTER.key,
                HEAT_PUMP_STATUS_REGISTER.key,
            ],
        )
        self._attr_unique_id = f"{entry.entry_id}_dhw_water_heater"

    @property
    def current_temperature(self) -> float | None:
        return self.coordinator.data.get(DHW_TEMP_REGISTER.key)

    @property
    def target_temperature(self) -> float | None:
        return self.coordinator.data.get(DHW_NORMAL_TARGET_TEMP_REGISTER.key)

    @property
    def min_temp(self) -> float:
        return DHW_NORMAL_TARGET_TEMP_REGISTER.min_value

    @property
    def max_temp(self) -> float:
        return DHW_NORMAL_TARGET_TEMP_REGISTER.max_value

    @property
    def target_temperature_step(self) -> float:
        return DHW_NORMAL_TARGET_TEMP_REGISTER.step

    @property
    def current_operation(self) -> str | None:
        status = self.coordinator.data.get(HEAT_PUMP_STATUS_REGISTER.key)
        if status is None:
            return None
        if int(status) in DHW_ACTIVE_STATUS_CODES:
            return DHW_STATE_HEATING
        return DHW_STATE_IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self.coordinator.async_write_value(
            DHW_NORMAL_TARGET_TEMP_REGISTER, temperature
        )
