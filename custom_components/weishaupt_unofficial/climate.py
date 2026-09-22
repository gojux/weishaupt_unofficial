"""Climate entities for the room heating circuits (1 ... 4)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .const import (
    CLIMATE_PRESET_OPTIONS,
    CURRENT_TEMP_SOURCE_ENTITY,
    CURRENT_TEMP_SOURCE_MODBUS,
    DEFAULT_CURRENT_TEMP_SOURCE,
    DEFAULT_PRESET,
    DOMAIN,
    HC_MODE_AUTOMATIC,
    HC_MODE_STANDBY,
    HC_MODE_TO_HVAC,
    HC_MODE_TO_PRESET,
    HEATING_CIRCUITS,
    NORMAL_SETPOINT_RANGE,
    PRESET_TO_HC_MODE,
    HeatingCircuit,
    RegisterDef,
    current_temp_entity_key,
    current_temp_source_key,
)
from .coordinator import WeishauptModbusCoordinator
from .entity import WeishauptEntity

_LOGGER = logging.getLogger(__name__)

# Custom state attribute used to persist WeishauptRoomClimate._last_preset
# across Home Assistant restarts via RestoreEntity. Deliberately NOT the
# built-in preset_mode attribute: that one reflects the live preset, which
# is None whenever the circuit is not in HVACMode.HEAT -- i.e. for a restart
# while the circuit is in standby or automatic mode.
ATTR_LAST_HEATING_PRESET = "last_heating_preset"

# Custom state attributes exposing whether a mode or temperature change has
# been written to the controller but is not yet confirmed by reading the
# register back (see the optimistic-update mechanism below).
ATTR_MODE_CHANGE_PENDING = "mode_change_pending"
ATTR_TEMPERATURE_CHANGE_PENDING = "temperature_change_pending"

# A change the controller does not confirm within this time is dropped and
# the entity shows the state the controller actually reports, e.g. if the
# write silently failed.
CHANGE_CONFIRMATION_TIMEOUT = timedelta(seconds=90)

# Half the setpoint register scale (0.1): two values within this distance are
# the same value once decoded from the register.
TEMPERATURE_CONFIRMATION_EPSILON = 0.05


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WeishauptModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        WeishauptRoomClimate(coordinator, entry, hc) for hc in HEATING_CIRCUITS
    )


class WeishauptRoomClimate(WeishauptEntity, RestoreEntity, ClimateEntity):
    """Climate entity representing a room heating circuit.

    The controller's five circuit operating modes map onto Home Assistant as:
      - Standby                     -> HVACMode.OFF
      - Automatic (time program)    -> HVACMode.AUTO
      - Comfort / Normal / Setback  -> HVACMode.HEAT, with the mode exposed
        as preset ("comfort" / "normal" / "setback").

    Each of the three fixed modes has its own room setpoint register, so the
    target temperature can be set while HVACMode.HEAT is active and is
    written to the register of the active preset. In AUTO and OFF the room
    setpoint currently requested by the controller is shown read-only.
    Presets are only offered in HVACMode.HEAT.
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_translation_key = "heating_circuit"
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]

    def __init__(
        self,
        coordinator: WeishauptModbusCoordinator,
        entry: ConfigEntry,
        heating_circuit: HeatingCircuit,
    ) -> None:
        self._hc = heating_circuit

        # Where current_temperature comes from -- configurable per circuit
        # via the options flow (config_flow.py), stored in entry.options.
        source_key = current_temp_source_key(heating_circuit)
        entity_key = current_temp_entity_key(heating_circuit)
        self._current_temp_source = entry.options.get(
            source_key, entry.data.get(source_key, DEFAULT_CURRENT_TEMP_SOURCE)
        )
        self._current_temp_entity_id: str | None = entry.options.get(
            entity_key, entry.data.get(entity_key)
        )

        context = [
            heating_circuit.mode_reg.key,
            heating_circuit.room_target_reg.key,
            heating_circuit.comfort_reg.key,
            heating_circuit.normal_reg.key,
            heating_circuit.setback_reg.key,
        ]
        # Only poll the room temperature if it is actually used.
        if self._current_temp_source == CURRENT_TEMP_SOURCE_MODBUS:
            context.append(heating_circuit.room_temp_reg.key)

        super().__init__(coordinator, entry, context)
        self._attr_unique_id = f"{entry.entry_id}_{heating_circuit.key}"
        self._attr_translation_placeholders = {"circuit": str(heating_circuit.number)}
        self._attr_entity_registry_enabled_default = heating_circuit.enabled_default

        # Mirrors the state of the source entity for CURRENT_TEMP_SOURCE_ENTITY,
        # kept up to date by a state listener rather than the polling interval.
        self._external_current_temp: float | None = None

        # The heating preset that was last active. Switching from standby
        # or automatic mode to HEAT restores it, instead of always falling
        # back to a fixed mode. Kept in sync with the controller in
        # _handle_coordinator_update(), so it stays correct even if the mode
        # was changed at the heat pump itself, and restored across restarts
        # (see async_added_to_hass()).
        self._last_preset: str = DEFAULT_PRESET

        # Optimistic-update state: the raw mode / temperature value that was
        # written but is not yet confirmed by the controller. The entity
        # shows it right away instead of the old value until the controller
        # reports it, or the confirmation times out.
        self._pending_mode_value: int | None = None
        self._pending_mode_since: datetime | None = None
        self._pending_temperature: float | None = None
        self._pending_temperature_since: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            restored_preset = last_state.attributes.get(ATTR_LAST_HEATING_PRESET)
            if restored_preset in PRESET_TO_HC_MODE:
                self._last_preset = restored_preset

        if (
            self._current_temp_source == CURRENT_TEMP_SOURCE_ENTITY
            and self._current_temp_entity_id
        ):
            self._update_external_current_temp(
                self.hass.states.get(self._current_temp_entity_id)
            )
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self._current_temp_entity_id],
                    self._handle_source_entity_change,
                )
            )

    @callback
    def _handle_source_entity_change(self, event: Event[EventStateChangedData]) -> None:
        self._update_external_current_temp(event.data["new_state"])
        self.async_write_ha_state()

    def _update_external_current_temp(self, state) -> None:
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            self._external_current_temp = None
            return
        try:
            self._external_current_temp = float(state.state)
        except (TypeError, ValueError):
            _LOGGER.warning(
                "%s: source entity %s has a non-numeric state (%r), ignoring",
                self._hc.key,
                self._current_temp_entity_id,
                state.state,
            )
            self._external_current_temp = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Sync the last-heating-preset cache with the controller and confirm
        pending mode / temperature changes once the controller reports them.

        Runs on every coordinator refresh, not only after writes made by
        Home Assistant.
        """
        raw_mode = self.coordinator.data.get(self._hc.mode_reg.key)
        if raw_mode is not None:
            preset = HC_MODE_TO_PRESET.get(int(raw_mode))
            if preset is not None:
                self._last_preset = preset
            if self._pending_mode_value == int(raw_mode):
                self._pending_mode_value = None
                self._pending_mode_since = None

        current_setpoint = self._reported_target_temperature()
        if (
            self._pending_temperature is not None
            and current_setpoint is not None
            and abs(current_setpoint - self._pending_temperature)
            < TEMPERATURE_CONFIRMATION_EPSILON
        ):
            self._pending_temperature = None
            self._pending_temperature_since = None

        self._expire_stale_pending_mode()
        self._expire_stale_pending_temperature()
        super()._handle_coordinator_update()

    def _expire_stale_pending_mode(self) -> None:
        if (
            self._pending_mode_value is not None
            and self._pending_mode_since is not None
            and dt_util.utcnow() - self._pending_mode_since > CHANGE_CONFIRMATION_TIMEOUT
        ):
            _LOGGER.warning(
                "%s: mode change to raw value %s was not confirmed by the "
                "controller within %s, showing its reported state instead",
                self._hc.key,
                self._pending_mode_value,
                CHANGE_CONFIRMATION_TIMEOUT,
            )
            self._pending_mode_value = None
            self._pending_mode_since = None

    def _expire_stale_pending_temperature(self) -> None:
        if (
            self._pending_temperature is not None
            and self._pending_temperature_since is not None
            and dt_util.utcnow() - self._pending_temperature_since
            > CHANGE_CONFIRMATION_TIMEOUT
        ):
            _LOGGER.warning(
                "%s: temperature change to %s was not confirmed by the "
                "controller within %s, showing its reported state instead",
                self._hc.key,
                self._pending_temperature,
                CHANGE_CONFIRMATION_TIMEOUT,
            )
            self._pending_temperature = None
            self._pending_temperature_since = None

    def _effective_raw_mode(self) -> int | None:
        """The raw mode to base hvac_mode / preset_mode on: the pending
        value while a change awaits confirmation, else the reported one."""
        self._expire_stale_pending_mode()
        if self._pending_mode_value is not None:
            return self._pending_mode_value
        raw_mode = self.coordinator.data.get(self._hc.mode_reg.key)
        return int(raw_mode) if raw_mode is not None else None

    async def _async_write_mode(self, raw_mode: int) -> None:
        """Writes a raw mode value, showing it optimistically right away."""
        self._pending_mode_value = raw_mode
        self._pending_mode_since = dt_util.utcnow()
        self.async_write_ha_state()
        await self.coordinator.async_write_value(self._hc.mode_reg, raw_mode)

    def _active_setpoint_register(self) -> RegisterDef | None:
        """The room setpoint register of the active preset, None while the
        circuit is in standby or automatic mode (no settable setpoint)."""
        preset = self.preset_mode
        if preset is None:
            return None
        return self._hc.setpoint_reg_for_preset(preset)

    def _reported_target_temperature(self) -> float | None:
        """The target temperature as reported by the controller."""
        reg = self._active_setpoint_register()
        if reg is None:
            reg = self._hc.room_target_reg
        return self.coordinator.data.get(reg.key)

    @property
    def current_temperature(self) -> float | None:
        """Current room temperature, from whichever source is configured."""
        if self._current_temp_source == CURRENT_TEMP_SOURCE_MODBUS:
            return self.coordinator.data.get(self._hc.room_temp_reg.key)
        if self._current_temp_source == CURRENT_TEMP_SOURCE_ENTITY:
            return self._external_current_temp
        return None

    @property
    def target_temperature(self) -> float | None:
        """The pending value right after a change, else the reported one."""
        self._expire_stale_pending_temperature()
        if self._pending_temperature is not None:
            return self._pending_temperature
        return self._reported_target_temperature()

    @property
    def min_temp(self) -> float:
        """Lower bound of the active preset's setpoint register.

        While the circuit has no settable setpoint (standby / automatic),
        the range collapses to the current target temperature, so the
        frontend control has nowhere to move to. Hiding
        ClimateEntityFeature.TARGET_TEMPERATURE instead would also hide the
        value itself, since Home Assistant only includes the target
        temperature in the state attributes while that feature is set.
        async_set_temperature() additionally rejects the call for direct
        service calls.
        """
        reg = self._active_setpoint_register()
        if reg is not None:
            return reg.min_value
        current = self.target_temperature
        return current if current is not None else NORMAL_SETPOINT_RANGE[0]

    @property
    def max_temp(self) -> float:
        """Upper bound, see min_temp."""
        reg = self._active_setpoint_register()
        if reg is not None:
            return reg.max_value
        current = self.target_temperature
        return current if current is not None else NORMAL_SETPOINT_RANGE[1]

    @property
    def target_temperature_step(self) -> float:
        reg = self._active_setpoint_register()
        return reg.step if reg is not None else self._hc.normal_reg.step

    @property
    def hvac_mode(self) -> HVACMode | None:
        raw_mode = self._effective_raw_mode()
        if raw_mode is None:
            return None
        return HC_MODE_TO_HVAC.get(raw_mode)

    @property
    def preset_modes(self) -> list[str] | None:
        """Presets are only offered in HVACMode.HEAT.

        Home Assistant checks a submitted preset against this list before
        calling async_set_preset_mode(), so returning None also rejects
        preset changes in the other modes with a clear error.
        """
        if self.hvac_mode != HVACMode.HEAT:
            return None
        return CLIMATE_PRESET_OPTIONS

    @property
    def preset_mode(self) -> str | None:
        if self.hvac_mode != HVACMode.HEAT:
            return None
        raw_mode = self._effective_raw_mode()
        return HC_MODE_TO_PRESET.get(raw_mode)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data.get(self._hc.mode_reg.key) is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, str | bool]:
        """The last heating preset (restored after a restart) and whether a
        change still awaits confirmation from the controller."""
        return {
            ATTR_LAST_HEATING_PRESET: self._last_preset,
            ATTR_MODE_CHANGE_PENDING: self._pending_mode_value is not None,
            ATTR_TEMPERATURE_CHANGE_PENDING: self._pending_temperature is not None,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Writes the target temperature to the setpoint register of the
        active preset. Only possible in HVACMode.HEAT; the error also makes
        the frontend revert its optimistic display of the attempted value."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        target_reg = self._active_setpoint_register()
        if target_reg is None:
            raise ServiceValidationError(
                f"Cannot set a target temperature for {self._hc.key} in mode "
                f"{self.hvac_mode!r} -- only supported in {HVACMode.HEAT!r}."
            )

        self._pending_temperature = temperature
        self._pending_temperature_since = dt_util.utcnow()
        self.async_write_ha_state()
        await self.coordinator.async_write_value(target_reg, temperature)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Writes standby (OFF), automatic (AUTO) or the last heating preset (HEAT)."""
        if hvac_mode == HVACMode.OFF:
            raw_mode = HC_MODE_STANDBY
        elif hvac_mode == HVACMode.AUTO:
            raw_mode = HC_MODE_AUTOMATIC
        elif hvac_mode == HVACMode.HEAT:
            raw_mode = PRESET_TO_HC_MODE[self._last_preset]
        else:
            raise ValueError(f"Unsupported HVAC mode: {hvac_mode}")
        await self._async_write_mode(raw_mode)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        raw_mode = PRESET_TO_HC_MODE.get(preset_mode)
        if raw_mode is None:
            raise ValueError(f"Unsupported preset mode: {preset_mode}")
        # Update the cache right away, so an immediate switch to standby
        # and back to HEAT already restores this preset.
        self._last_preset = preset_mode
        await self._async_write_mode(raw_mode)
