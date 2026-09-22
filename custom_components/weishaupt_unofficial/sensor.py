"""Sensor entities for the Weishaupt heat pump."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    HEAT_PUMP_STATUS_MAP,
    HEAT_PUMP_STATUS_OPTIONS,
    HEAT_PUMP_STATUS_UNKNOWN,
    SENSOR_REGISTERS,
    RegisterDef,
    STATUS_REGISTERS,
)
from .coordinator import WeishauptModbusCoordinator
from .entity import WeishauptRegisterEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WeishauptModbusCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        WeishauptSensor(coordinator, entry, reg) for reg in SENSOR_REGISTERS
    ]
    entities.extend(
        WeishauptStatusSensor(coordinator, entry, reg) for reg in STATUS_REGISTERS
    )
    async_add_entities(entities)


class WeishauptSensor(WeishauptRegisterEntity, SensorEntity):
    """A single Modbus value exposed as a sensor."""

    def __init__(
        self,
        coordinator: WeishauptModbusCoordinator,
        entry: ConfigEntry,
        reg: RegisterDef,
    ) -> None:
        super().__init__(coordinator, entry, reg)
        self._attr_native_unit_of_measurement = reg.unit
        self._attr_device_class = reg.device_class
        self._attr_state_class = reg.state_class
        if reg.precision is not None:
            self._attr_suggested_display_precision = reg.precision

    @property
    def native_value(self) -> float | None:
        return self.register_value


class WeishauptStatusSensor(WeishauptRegisterEntity, SensorEntity):
    """Translates the raw operating status code into a localized text.

    Uses SensorDeviceClass.ENUM: Home Assistant requires a fixed `options`
    list of every possible state and rejects other values, which is why
    HEAT_PUMP_STATUS_UNKNOWN covers undocumented codes.

    The state is a stable, machine-readable key (e.g. "heating"); the
    displayed text is resolved via the translation key from strings.json /
    translations/*.json.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = HEAT_PUMP_STATUS_OPTIONS

    @property
    def native_value(self) -> str | None:
        raw_code = self.register_value
        if raw_code is None:
            return None
        return HEAT_PUMP_STATUS_MAP.get(int(raw_code), HEAT_PUMP_STATUS_UNKNOWN)

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        """The raw status code, e.g. for undocumented codes."""
        raw_code = self.register_value
        if raw_code is None:
            return None
        return {"raw_status_code": int(raw_code)}
