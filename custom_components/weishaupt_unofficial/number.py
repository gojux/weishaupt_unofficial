"""Number entities for writable registers (DHW setpoint, DHW push, PV setpoint)."""
from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
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
    `PV_SETPOINT_HEARTBEAT_INTERVAL`, it re-writes
    `coordinator.pv_setpoint_last_value`, for as long as that value isn't
    `0` -- no automation needs to keep calling `number.set_value` just to
    keep a setpoint alive.

    `coordinator.pv_setpoint_last_value` is shared with
    `WeishauptPvSurplusFollowSwitch` (switch.py), which may write it
    instead of this entity while "follow" is enabled
    (`coordinator.pv_surplus_follow_enabled`) -- see DEVELOPER.md. While
    that is the case, this entity rejects direct writes and collapses its
    range to the current value, the same pattern
    `WeishauptRoomClimate.min_temp`/`max_temp` (climate.py) uses for a
    setpoint that currently can't be changed directly.
    """

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Seeded from the last polled value, so a setpoint that was already
        # active before a Home Assistant restart keeps being kept alive.
        # Only if nothing has set it yet -- the switch may have already
        # seeded or written it, depending on platform setup order.
        if self.coordinator.pv_setpoint_last_value is None:
            self.coordinator.pv_setpoint_last_value = self.register_value
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_heartbeat, PV_SETPOINT_HEARTBEAT_INTERVAL
            )
        )

    @property
    def native_min_value(self) -> float:
        default = self._reg.min_value if self._reg.min_value is not None else 0
        if self.coordinator.pv_surplus_follow_enabled:
            current = self.native_value
            return current if current is not None else default
        return default

    @property
    def native_max_value(self) -> float:
        default = self._reg.max_value if self._reg.max_value is not None else 100
        if self.coordinator.pv_surplus_follow_enabled:
            current = self.native_value
            return current if current is not None else default
        return default

    @property
    def extra_state_attributes(self) -> dict[str, bool]:
        return {
            "pv_surplus_follow_active": self.coordinator.pv_surplus_follow_enabled
        }

    async def async_set_native_value(self, value: float) -> None:
        if self.coordinator.pv_surplus_follow_enabled:
            raise ServiceValidationError(
                "Cannot set the PV power setpoint directly while the "
                "'Follow PV surplus' switch is on -- turn it off first."
            )
        self.coordinator.pv_setpoint_last_value = value
        await self.coordinator.async_write_value(self._reg, value)

    async def _async_heartbeat(self, now: datetime) -> None:
        value = self.coordinator.pv_setpoint_last_value
        if value:
            await self.coordinator.async_write_value(self._reg, value)
