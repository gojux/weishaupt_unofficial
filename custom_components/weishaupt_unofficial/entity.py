"""Base classes shared by all entities of the integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, RegisterDef
from .coordinator import WeishauptModbusCoordinator


class WeishauptEntity(CoordinatorEntity[WeishauptModbusCoordinator]):
    """An entity of the heat pump device.

    `context` lists the register keys the entity reads; the coordinator only
    polls registers that at least one subscribed entity needs.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WeishauptModbusCoordinator,
        entry: ConfigEntry,
        context: list[str],
    ) -> None:
        super().__init__(coordinator, context=context)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Weishaupt (Modbus)",
            model="Heat Pump (Modbus TCP)",
        )


class WeishauptRegisterEntity(WeishauptEntity):
    """An entity showing a single register."""

    def __init__(
        self,
        coordinator: WeishauptModbusCoordinator,
        entry: ConfigEntry,
        reg: RegisterDef,
    ) -> None:
        super().__init__(coordinator, entry, [reg.key])
        self._reg = reg
        self._attr_unique_id = f"{entry.entry_id}_{reg.key}"
        self._attr_translation_key = reg.translation_key or reg.key
        if reg.circuit is not None:
            self._attr_translation_placeholders = {"circuit": str(reg.circuit)}
        self._attr_entity_registry_enabled_default = reg.enabled_default

    @property
    def register_value(self) -> float | None:
        """The decoded register value, None while it is not available."""
        return self.coordinator.data.get(self._reg.key)
