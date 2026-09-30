"""Switch entity that keeps the PV power setpoint in sync with an external
PV-surplus entity (e.g. from a smart meter)."""
from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfPower
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.unit_conversion import PowerConverter

from .const import CONF_PV_SURPLUS_ENTITY_ID, DOMAIN, SYSTEM_PV_SETPOINT_REGISTER
from .coordinator import WeishauptModbusCoordinator
from .entity import WeishauptEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WeishauptModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    source_entity_id = entry.options.get(CONF_PV_SURPLUS_ENTITY_ID)
    if source_entity_id:
        async_add_entities(
            [WeishauptPvSurplusFollowSwitch(coordinator, entry, source_entity_id)]
        )


class WeishauptPvSurplusFollowSwitch(WeishauptEntity, RestoreEntity, SwitchEntity):
    """Keeps `SYSTEM_PV_SETPOINT_REGISTER` in sync with a source entity.

    While on, every state change of the configured source entity (e.g. a
    smart meter's PV surplus sensor) is converted to watts and written to
    the register. Its value is also kept in
    `coordinator.pv_setpoint_last_value`, which
    `WeishauptPvPowerSetpointNumber`'s heartbeat (number.py) re-sends every
    30 seconds independently of this entity -- the source doesn't need to
    push an update that often to keep the setpoint alive.

    Turning it off writes `0`, handing control back to the heat pump, and
    makes the PV power setpoint number entity directly settable again
    (`coordinator.pv_surplus_follow_enabled`).
    """

    _attr_translation_key = "pv_surplus_follow"

    def __init__(
        self,
        coordinator: WeishauptModbusCoordinator,
        entry: ConfigEntry,
        source_entity_id: str,
    ) -> None:
        super().__init__(coordinator, entry, [])
        self._source_entity_id = source_entity_id
        self._attr_unique_id = f"{entry.entry_id}_pv_surplus_follow"
        self._attr_is_on = False
        self._unsub_track: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == "on"
        if self._attr_is_on:
            self.coordinator.pv_surplus_follow_enabled = True
            self._start_following()
            await self._async_apply_current_source_value()

    async def async_will_remove_from_hass(self) -> None:
        self._stop_following()
        await super().async_will_remove_from_hass()

    @property
    def is_on(self) -> bool:
        return self._attr_is_on

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.coordinator.pv_surplus_follow_enabled = True
        # Notifies every entity right away (in particular
        # WeishauptPvPowerSetpointNumber, which collapses its range while
        # this flag is set) -- independent of the Modbus refresh below,
        # which is debounced and may not complete synchronously.
        self.coordinator.async_update_listeners()
        self._start_following()
        await self._async_apply_current_source_value()

    async def async_turn_off(self, **kwargs) -> None:
        self._stop_following()
        self._attr_is_on = False
        self.coordinator.pv_surplus_follow_enabled = False
        self.coordinator.pv_setpoint_last_value = 0
        self.coordinator.async_update_listeners()
        await self.coordinator.async_write_value(SYSTEM_PV_SETPOINT_REGISTER, 0)

    def _start_following(self) -> None:
        # Not registered via self.async_on_remove(): unlike a listener that
        # only ever needs to be torn down once when the entity is removed,
        # this one is also torn down and restarted independently of that
        # (every async_turn_off()/async_turn_on()), which async_on_remove()
        # doesn't support un-registering -- async_will_remove_from_hass()
        # below calls _stop_following() itself on removal instead.
        if self._unsub_track is None:
            self._unsub_track = async_track_state_change_event(
                self.hass, [self._source_entity_id], self._handle_source_change
            )

    def _stop_following(self) -> None:
        if self._unsub_track is not None:
            self._unsub_track()
            self._unsub_track = None

    @callback
    def _handle_source_change(self, event: Event[EventStateChangedData]) -> None:
        self.hass.async_create_task(
            self._async_write_from_state(event.data["new_state"])
        )

    async def _async_apply_current_source_value(self) -> None:
        await self._async_write_from_state(self.hass.states.get(self._source_entity_id))

    async def _async_write_from_state(self, state: State | None) -> None:
        watts = self._parse_watts(state)
        if watts is None:
            return
        self.coordinator.pv_setpoint_last_value = watts
        await self.coordinator.async_write_value(SYSTEM_PV_SETPOINT_REGISTER, watts)

    def _parse_watts(self, state: State | None) -> int | None:
        """The source entity's state converted to watts, clamped to >= 0.

        None means the state cannot be used right now (missing, unknown,
        unavailable, or not a number) -- the caller then leaves the
        setpoint untouched, so the last known value (re-sent by the number
        entity's heartbeat) stays active instead of being replaced by a
        momentary gap in the source's own data.
        """
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "PV surplus source %s has a non-numeric state (%r), ignoring",
                self._source_entity_id,
                state.state,
            )
            return None

        unit = state.attributes.get("unit_of_measurement") or UnitOfPower.WATT
        try:
            value = PowerConverter.convert(value, unit, UnitOfPower.WATT)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning(
                "PV surplus source %s has an unrecognized unit %r, assuming watts",
                self._source_entity_id,
                unit,
            )

        # A negative reading (e.g. currently importing rather than
        # exporting) is a valid state, not a missing one -- clamp it to 0
        # rather than skipping the write.
        return max(0, round(value))
