"""Number entities for writable registers (DHW setpoint, DHW push, PV setpoint)."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, NUMBER_REGISTERS, SYSTEM_PV_SETPOINT_REGISTER, RegisterDef
from .coordinator import WeishauptModbusCoordinator
from .entity import WeishauptRegisterEntity

# How often WeishauptPvPowerSetpointNumber re-sends the value it was last
# set to, for as long as it isn't 0 -- see that class for why.
PV_SETPOINT_HEARTBEAT_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WeishauptModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        _build_number(coordinator, entry, reg)
        for reg in NUMBER_REGISTERS
        if reg.writable
    )


def _build_number(
    coordinator: WeishauptModbusCoordinator, entry: ConfigEntry, reg: RegisterDef
) -> WeishauptNumber:
    if reg is SYSTEM_PV_SETPOINT_REGISTER:
        return WeishauptPvPowerSetpointNumber(coordinator, entry, reg)
    return WeishauptNumber(coordinator, entry, reg)


class WeishauptNumber(WeishauptRegisterEntity, NumberEntity):
    """A writable Modbus value exposed as a number entity."""

    def __init__(
        self,
        coordinator: WeishauptModbusCoordinator,
        entry: ConfigEntry,
        reg: RegisterDef,
    ) -> None:
        super().__init__(coordinator, entry, reg)
        self._attr_native_unit_of_measurement = reg.unit
        self._attr_device_class = reg.device_class
        self._attr_native_min_value = reg.min_value if reg.min_value is not None else 0
        self._attr_native_max_value = reg.max_value if reg.max_value is not None else 100
        self._attr_native_step = reg.step or 1

    @property
    def native_value(self) -> float | None:
        return self.register_value

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_write_value(self._reg, value)


class WeishauptPvPowerSetpointNumber(WeishauptNumber):
    """The PV power setpoint (`SYSTEM_PV_SETPOINT_REGISTER`).

    The controller does not store this value and applies it only for a
    limited time (see DEVELOPER.md), so it needs to be re-sent
    periodically to stay in effect. This entity does that on its own,
    independently of the polling interval: every
    `PV_SETPOINT_HEARTBEAT_INTERVAL`, it re-writes the value it was last
    set to, for as long as that value isn't `0` -- no automation needs to
    keep calling `number.set_value` just to keep a setpoint alive.
    """

    def __init__(
        self,
        coordinator: WeishauptModbusCoordinator,
        entry: ConfigEntry,
        reg: RegisterDef,
    ) -> None:
        super().__init__(coordinator, entry, reg)
        # Seeded from the last polled value in async_added_to_hass(), so a
        # setpoint that was already active before a Home Assistant restart
        # keeps being kept alive instead of silently expiring because this
        # entity forgot about it.
        self._last_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._last_value = self.register_value
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_heartbeat, PV_SETPOINT_HEARTBEAT_INTERVAL
            )
        )

    async def async_set_native_value(self, value: float) -> None:
        self._last_value = value
        await self.coordinator.async_write_value(self._reg, value)

    async def _async_heartbeat(self, now: datetime) -> None:
        if self._last_value:
            await self.coordinator.async_write_value(self._reg, self._last_value)
