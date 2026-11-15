"""Diagnostics download: what the controller answered, not what identifies the home.

The download is meant to be attached to a public issue. It leaves out the
host, the entry's name/title and the chosen source entities (entity ids
often contain room or device names); it keeps everything that helps to
understand the controller's behavior: raw register values, request
statistics and the integration's settings.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import (
    ALL_REGISTERS,
    CONF_PV_SURPLUS_ENTITY_ID,
    DOMAIN,
    HEAT_PUMP_STATUS_MAP,
    HEATING_CIRCUITS,
    current_temp_entity_key,
)
from .coordinator import WeishauptModbusCoordinator

# Free text the user typed or chose: may name the family, the address or a
# room. Redacted wherever it appears in the entry's data or options.
TO_REDACT = {
    CONF_HOST,
    CONF_NAME,
    CONF_PV_SURPLUS_ENTITY_ID,
    *(current_temp_entity_key(hc) for hc in HEATING_CIRCUITS),
}

LIBRARIES = ("modbus-connection", "tmodbus")


def _library_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _registers(coordinator: WeishauptModbusCoordinator) -> list[dict[str, Any]]:
    """Every register of the map with what the last poll got for it."""
    data = coordinator.data or {}
    registers: dict[str, dict[str, Any]] = {}
    for reg in sorted(ALL_REGISTERS, key=lambda r: r.address):
        if reg.key in registers:
            continue
        reading = coordinator.readings.get(reg.key)
        if reading is None:
            # Disabled entities do not subscribe, so their registers are
            # not polled.
            registers[reg.key] = {
                "address": reg.address,
                "key": reg.key,
                "outcome": "not polled",
            }
            continue
        registers[reg.key] = {
            "address": reg.address,
            "key": reg.key,
            "raw": None if reading.raw is None else f"0x{reading.raw:04X}",
            "value": reading.value,
            "published": data.get(reg.key),
            "outcome": reading.outcome,
            "detail": reading.detail,
        }
    return list(registers.values())


def _status(coordinator: WeishauptModbusCoordinator) -> dict[str, Any]:
    return {
        key: {
            "code": code,
            "name": None if code is None else HEAT_PUMP_STATUS_MAP.get(code, "unknown"),
            "seconds": round(seconds),
        }
        for key, (code, seconds) in coordinator.status_durations().items()
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Diagnostics for a config entry."""
    coordinator: WeishauptModbusCoordinator = hass.data[DOMAIN][entry.entry_id]
    integration = await async_get_integration(hass, DOMAIN)
    counters = coordinator.counters
    last_poll = coordinator.last_successful_poll

    return {
        "versions": {
            "home_assistant": HA_VERSION,
            "integration": str(integration.version),
            **{name: _library_version(name) for name in LIBRARIES},
        },
        "entry": {
            "version": entry.version,
            "minor_version": entry.minor_version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "connection": {
            "connected": coordinator.unit.connected,
            "connection_losses": coordinator.connection_losses,
            "requests": counters.requests,
            "failed_answers": counters.failed_answers,
            "lost_connections": counters.lost_connections,
            "rejected": counters.rejected,
        },
        "polling": {
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "last_update_success": coordinator.last_update_success,
            "last_successful_poll": last_poll.isoformat() if last_poll else None,
            "failed_polls_in_a_row": coordinator.failed_polls_in_a_row,
            "failed_polls_total": coordinator.failed_polls_total,
            "input_fallback_addresses": coordinator.input_fallback_addresses,
            "consecutive_failures": coordinator.consecutive_failures,
            "unsupported_registers": coordinator.unsupported_registers,
        },
        "status": _status(coordinator),
        "writes": {
            "pending": coordinator.pending_writes,
            "pv_setpoint_last_value": coordinator.pv_setpoint_last_value,
            "pv_surplus_follow_enabled": coordinator.pv_surplus_follow_enabled,
        },
        "registers": _registers(coordinator),
    }
