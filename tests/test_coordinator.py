"""Decoding, block planning and reading against the simulator."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.weishaupt_unofficial.const import (
    ALL_REGISTERS,
    CONSECUTIVE_FAILURES_BEFORE_DROP,
    DOMAIN,
    MAX_REGISTERS_PER_READ,
    RegisterDef,
)
from custom_components.weishaupt_unofficial.coordinator import (
    WeishauptModbusCoordinator,
    decode_value,
    encode_value,
    plan_read_blocks,
)

from conftest import Simulator


def test_decode_and_encode() -> None:
    assert decode_value(0xFFFF, "int16") == -1
    assert decode_value(0x8000, "int16") == -32768
    assert decode_value(0xFFFF, "uint16") == 65535
    assert encode_value(-1, "int16") == 0xFFFF
    assert encode_value(65535, "uint16") == 65535
    with pytest.raises(ValueError):
        encode_value(70000, "uint16")
    with pytest.raises(ValueError):
        encode_value(-1, "uint16")
    with pytest.raises(ValueError):
        encode_value(40000, "int16")


def test_blocks_are_consecutive_and_at_most_five_long() -> None:
    blocks = plan_read_blocks(ALL_REGISTERS)
    assert sum(len(block) for _, block in blocks) == len(ALL_REGISTERS)
    for reg_type, block in blocks:
        assert len(block) <= MAX_REGISTERS_PER_READ
        assert [reg.address for reg in block] == list(
            range(block[0].address, block[0].address + len(block))
        )
        assert all(
            (reg.register_type == "holding") == (reg_type == "holding") for reg in block
        )
    # 20 statistics registers (16 official + 4 electrical) collapse into 5 requests
    statistics = [b for _, b in blocks if b[0].key.startswith("energy_")]
    assert len(statistics) == 5 and all(len(b) == 4 for b in statistics)


def test_input_fallback_registers_are_read_as_input() -> None:
    mode = next(reg for reg in ALL_REGISTERS if reg.key == "hc1_mode")
    assert plan_read_blocks([mode])[0][0] == "holding"
    assert plan_read_blocks([mode], {mode.address})[0][0] == "input"


async def test_first_refresh_reads_valid_values_only(
    hass: HomeAssistant, integration
) -> None:
    coordinator = hass.data[DOMAIN][integration.entry_id]
    data = coordinator.data
    assert data["outdoor_temp_1"] == 5.5
    assert data["dhw_temp"] == 48.2
    assert data["hc1_mode"] == 2
    assert data["energy_total_year"] == 3900
    assert data["heat_pump_status"] == 19
    # sensor not installed / no value / no code active
    assert "outdoor_temp_2" not in data
    assert "hc2_room_target_temp" not in data
    assert "error_code" not in data
    # heating circuits 3 and 4 are not installed
    assert not any(key.startswith(("hc3_", "hc4_")) for key in data)


async def test_only_needed_registers_are_polled(
    hass: HomeAssistant, integration
) -> None:
    coordinator = hass.data[DOMAIN][integration.entry_id]
    await coordinator.async_refresh()
    # Disabled entities do not subscribe: their registers are skipped.
    assert "outdoor_temp_2" not in coordinator.data
    assert "energy_cooling_today" not in coordinator.data
    assert "energy_total_today" in coordinator.data


async def test_holding_registers_fall_back_to_input(hass: HomeAssistant) -> None:
    class FakeClient:
        connected = True

        async def read_holding_registers(self, address, count, device_id):
            return SimpleNamespace(isError=lambda: True)

        async def read_input_registers(self, address, count, device_id):
            return SimpleNamespace(isError=lambda: False, registers=[2] * count)

    coordinator = WeishauptModbusCoordinator(hass, "Heat Pump", "host", 502, 1, 30)
    coordinator.client = FakeClient()
    mode = next(reg for reg in ALL_REGISTERS if reg.key == "hc1_mode")
    with patch(
        "custom_components.weishaupt_unofficial.coordinator.ALL_REGISTERS", [mode]
    ):
        data = await coordinator._async_update_data()
    assert data == {"hc1_mode": 2}
    assert mode.address in coordinator._input_fallback


async def test_write_is_skipped_when_value_is_unchanged(
    hass: HomeAssistant, integration, simulator: Simulator
) -> None:
    coordinator = hass.data[DOMAIN][integration.entry_id]
    reg: RegisterDef = next(r for r in ALL_REGISTERS if r.key == "dhw_normal_target_temp")
    with patch.object(
        coordinator.client, "write_register", wraps=coordinator.client.write_register
    ) as write:
        await coordinator.async_write_value(reg, 50.0)
        assert write.call_count == 0
        await coordinator.async_write_value(reg, 55.0)
        assert write.call_count == 1
    assert await simulator.read(42103) == 550


async def test_pv_setpoint_write_is_never_skipped(
    hass: HomeAssistant, integration, simulator: Simulator
) -> None:
    """Unlike other writable registers, the PV setpoint is not stored in
    the controller's EEPROM (skip_unchanged_write=False), so it must be
    re-sent every time even when the value does not change."""
    coordinator = hass.data[DOMAIN][integration.entry_id]
    reg: RegisterDef = next(r for r in ALL_REGISTERS if r.key == "pv_power_setpoint")
    with patch.object(
        coordinator.client, "write_register", wraps=coordinator.client.write_register
    ) as write:
        await coordinator.async_write_value(reg, 2000)
        await coordinator.async_write_value(reg, 2000)
        assert write.call_count == 2
    assert await simulator.read(40002) == 2000


async def test_write_rejects_read_only_register(
    hass: HomeAssistant, integration
) -> None:
    coordinator = hass.data[DOMAIN][integration.entry_id]
    reg = next(r for r in ALL_REGISTERS if r.key == "outdoor_temp_1")
    with pytest.raises(ValueError):
        await coordinator.async_write_value(reg, 1.0)


async def test_transient_failures_keep_the_last_known_value(hass: HomeAssistant) -> None:
    """A register that fails to read keeps showing its last known value for
    up to CONSECUTIVE_FAILURES_BEFORE_DROP - 1 consecutive polls -- a single
    transient Modbus error must not immediately flap an entity to
    unavailable/unknown. Once the failures reach that count, it is dropped."""
    reg = next(r for r in ALL_REGISTERS if r.key == "outdoor_temp_1")

    class FlakyClient:
        connected = True
        fail = False

        async def read_input_registers(self, address, count, device_id):
            if self.fail:
                return SimpleNamespace(isError=lambda: True)
            return SimpleNamespace(isError=lambda: False, registers=[55])

    coordinator = WeishauptModbusCoordinator(hass, "Heat Pump", "host", 502, 1, 30)
    coordinator.client = FlakyClient()
    with patch(
        "custom_components.weishaupt_unofficial.coordinator.ALL_REGISTERS", [reg]
    ):
        coordinator.data = await coordinator._async_update_data()
        assert coordinator.data[reg.key] == 5.5

        coordinator.client.fail = True
        for _ in range(CONSECUTIVE_FAILURES_BEFORE_DROP - 1):
            coordinator.data = await coordinator._async_update_data()
            assert coordinator.data[reg.key] == 5.5

        coordinator.data = await coordinator._async_update_data()
        assert reg.key not in coordinator.data


async def test_unreachable_device_fails_the_refresh(hass: HomeAssistant) -> None:
    coordinator = WeishauptModbusCoordinator(hass, "Heat Pump", "127.0.0.1", 1, 1, 30)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
