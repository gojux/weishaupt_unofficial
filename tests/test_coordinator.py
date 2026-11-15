"""Decoding, block planning and reading against the simulator."""
from __future__ import annotations

from collections.abc import Iterable
from unittest.mock import patch

import pytest
from homeassistant.components.modbus.connection import async_get_connection_info
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusDesyncError,
    ModbusTimeoutError,
)

from custom_components.weishaupt_unofficial.const import (
    ALL_REGISTERS,
    CONSECUTIVE_FAILURES_BEFORE_DROP,
    DOMAIN,
    FAILED_POLLS_TOLERATED,
    MAX_REGISTERS_PER_READ,
    RegisterDef,
)
from custom_components.weishaupt_unofficial.coordinator import (
    WeishauptModbusCoordinator,
    async_with_retries,
    decode_value,
    encode_value,
    plan_read_blocks,
)

from conftest import Simulator


class FakeUnit:
    """A ModbusUnit whose requests answer from scripted outcomes.

    Each request takes the next outcome: an exception instance is raised,
    anything else is returned (registers repeated to the requested count).
    The last outcome repeats once the script runs out.
    """

    def __init__(self, outcomes: Iterable = (), holding: Iterable = ()) -> None:
        self.input_outcomes = list(outcomes)
        self.holding_outcomes = list(holding) or [IllegalDataAddressError("no")]
        self.requests: list[tuple[str, int, int]] = []
        self.writes: list[tuple[int, int]] = []
        self.write_outcomes: list = [None]
        self.disconnects = 0

    @staticmethod
    def _next(outcomes: list):
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        self.requests.append(("input", address, count))
        return [self._next(self.input_outcomes)] * count

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.requests.append(("holding", address, count))
        return [self._next(self.holding_outcomes)] * count

    async def write_register(self, address: int, value: int) -> None:
        self.writes.append((address, value))
        self._next(self.write_outcomes)

    async def disconnect(self) -> None:
        self.disconnects += 1


def timeout() -> ModbusTimeoutError:
    return ModbusTimeoutError("timeout")


def lost() -> ModbusConnectionError:
    return ModbusConnectionError("Connection lost before response was received")


async def _read(unit: FakeUnit):
    return await async_with_retries(
        unit, lambda: unit.read_input_registers(30001, 1), "test read"
    )


async def test_retry_succeeds_after_timeouts_and_a_lost_connection() -> None:
    unit = FakeUnit([timeout(), timeout(), lost(), 55])
    assert await _read(unit) == [55]
    assert len(unit.requests) == 4
    # A timeout leaves the link open: it is dropped so the retry gets a new one.
    assert unit.disconnects == 2


async def test_retry_gives_up_after_three_failed_answers() -> None:
    unit = FakeUnit([timeout(), ModbusDesyncError("late answer"), timeout(), 55])
    with pytest.raises(ModbusTimeoutError):
        await _read(unit)
    assert len(unit.requests) == 3


async def test_retry_gives_up_after_three_lost_connections() -> None:
    unit = FakeUnit([lost(), lost(), lost(), 55])
    with pytest.raises(ModbusConnectionError):
        await _read(unit)
    assert len(unit.requests) == 3


async def test_retry_does_not_repeat_a_rejected_request() -> None:
    unit = FakeUnit([IllegalDataAddressError("no such register"), 55])
    with pytest.raises(IllegalDataAddressError):
        await _read(unit)
    assert len(unit.requests) == 1


async def test_write_is_retried_after_a_lost_connection(hass: HomeAssistant) -> None:
    unit = FakeUnit()
    unit.write_outcomes = [lost(), None]
    coordinator = WeishauptModbusCoordinator(hass, unit, "Heat Pump", 30)
    reg = next(r for r in ALL_REGISTERS if r.key == "dhw_normal_target_temp")
    with patch.object(coordinator, "async_request_refresh"):
        await coordinator.async_write_value(reg, 55.0)
    assert unit.writes == [(42103, 550), (42103, 550)]


async def test_short_outage_keeps_the_last_values(hass: HomeAssistant) -> None:
    """A poll that gets no answer keeps the previous data for up to
    FAILED_POLLS_TOLERATED polls in a row; the next one fails."""
    reg = next(r for r in ALL_REGISTERS if r.key == "outdoor_temp_1")
    unit = FakeUnit([55])
    coordinator = WeishauptModbusCoordinator(hass, unit, "Heat Pump", 30)
    with patch(
        "custom_components.weishaupt_unofficial.coordinator.ALL_REGISTERS", [reg]
    ):
        coordinator.data = await coordinator._async_update_data()
        unit.input_outcomes = [timeout()]
        for _ in range(FAILED_POLLS_TOLERATED):
            assert await coordinator._async_update_data() == {reg.key: 5.5}
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

        unit.input_outcomes = [60]
        coordinator.data = await coordinator._async_update_data()
        assert coordinator.data == {reg.key: 6.0}
        # The counter starts over after a good poll.
        unit.input_outcomes = [timeout()]
        assert await coordinator._async_update_data() == {reg.key: 6.0}


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
    unit = FakeUnit([2], holding=[IllegalDataAddressError("not a holding register")])
    coordinator = WeishauptModbusCoordinator(hass, unit, "Heat Pump", 30)
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
        coordinator.unit, "write_register", wraps=coordinator.unit.write_register
    ) as write:
        await coordinator.async_write_value(reg, 50.0)
        assert write.call_count == 0
        await coordinator.async_write_value(reg, 55.0)
        assert write.call_count == 1
    assert await simulator.read(42103) == 550


async def test_write_is_not_skipped_when_the_controller_changed(
    hass: HomeAssistant, integration, simulator: Simulator
) -> None:
    """The polled value may be stale: a setpoint changed at the controller's
    panel since the last poll must still be written back."""
    coordinator = hass.data[DOMAIN][integration.entry_id]
    reg: RegisterDef = next(r for r in ALL_REGISTERS if r.key == "dhw_normal_target_temp")
    assert coordinator.data[reg.key] == 50.0
    await simulator.write(42103, 520)  # changed at the panel, not yet polled
    with patch.object(
        coordinator.unit, "write_register", wraps=coordinator.unit.write_register
    ) as write:
        await coordinator.async_write_value(reg, 50.0)
        assert write.call_count == 1
    assert await simulator.read(42103) == 500


async def test_write_goes_ahead_when_the_fresh_read_fails(hass: HomeAssistant) -> None:
    """Without a confirmation from the controller the write is not skipped."""
    reg = next(r for r in ALL_REGISTERS if r.key == "dhw_normal_target_temp")
    unit = FakeUnit([timeout()], holding=[timeout()])
    coordinator = WeishauptModbusCoordinator(hass, unit, "Heat Pump", 30)
    coordinator.data = {reg.key: 50.0}
    with patch.object(coordinator, "async_request_refresh"):
        await coordinator.async_write_value(reg, 50.0)
    assert unit.writes == [(42103, 500)]


async def test_pv_setpoint_write_is_never_skipped(
    hass: HomeAssistant, integration, simulator: Simulator
) -> None:
    """Unlike other writable registers, the PV setpoint is not stored in
    the controller's EEPROM (skip_unchanged_write=False), so it must be
    re-sent every time even when the value does not change."""
    coordinator = hass.data[DOMAIN][integration.entry_id]
    reg: RegisterDef = next(r for r in ALL_REGISTERS if r.key == "pv_power_setpoint")
    with patch.object(
        coordinator.unit, "write_register", wraps=coordinator.unit.write_register
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
    unit = FakeUnit([55])
    coordinator = WeishauptModbusCoordinator(hass, unit, "Heat Pump", 30)
    with patch(
        "custom_components.weishaupt_unofficial.coordinator.ALL_REGISTERS", [reg]
    ):
        coordinator.data = await coordinator._async_update_data()
        assert coordinator.data[reg.key] == 5.5

        unit.input_outcomes = [IllegalDataAddressError("rejected")]
        for _ in range(CONSECUTIVE_FAILURES_BEFORE_DROP - 1):
            coordinator.data = await coordinator._async_update_data()
            assert coordinator.data[reg.key] == 5.5

        coordinator.data = await coordinator._async_update_data()
        assert reg.key not in coordinator.data


async def test_unreachable_device_fails_the_first_refresh(hass: HomeAssistant) -> None:
    unit = FakeUnit([lost()], holding=[lost()])
    coordinator = WeishauptModbusCoordinator(hass, unit, "Heat Pump", 30)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    # Gave up on the first block instead of waiting out every other one.
    assert len(unit.requests) == 3


async def test_uses_home_assistants_shared_connection(
    hass: HomeAssistant, config_entry
) -> None:
    """The unit comes from the modbus integration, which keeps the
    connection while the entry holds it and closes it when it unloads."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    [info] = async_get_connection_info(hass)
    assert info.units == {config_entry.entry_id: [1]}

    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert async_get_connection_info(hass) == []
