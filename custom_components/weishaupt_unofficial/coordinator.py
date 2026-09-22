"""DataUpdateCoordinator: handles the Modbus connection, polling and writes."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from .const import (
    ALL_REGISTERS,
    CONSECUTIVE_FAILURES_BEFORE_DROP,
    MAX_REGISTERS_PER_READ,
    DataType,
    RegisterDef,
)

_LOGGER = logging.getLogger(__name__)


def decode_value(register: int, data_type: DataType) -> int:
    """Converts a raw 16-bit register into a signed or unsigned integer."""
    if data_type == "int16":
        return register - 65536 if register > 32767 else register
    if data_type == "uint16":
        return register
    raise ValueError(f"Unknown data_type: {data_type}")


def encode_value(value: int, data_type: DataType) -> int:
    """Converts an integer into a raw 16-bit register."""
    if data_type == "int16" and -32768 <= value <= 32767:
        return value & 0xFFFF
    if data_type == "uint16" and 0 <= value <= 65535:
        return value
    raise ValueError(f"Value {value} does not fit into {data_type}")


def plan_read_blocks(
    registers: list[RegisterDef], input_fallback: set[int] | frozenset[int] = frozenset()
) -> list[tuple[str, list[RegisterDef]]]:
    """Groups registers into blocks of consecutive addresses.

    The controller answers at most MAX_REGISTERS_PER_READ consecutive
    registers per request. Registers are only grouped if they are read with
    the same function code: holding registers that turned out to be readable
    as input registers only (`input_fallback`) are read as such.
    """
    def read_type(reg: RegisterDef) -> str:
        if reg.register_type == "holding" and reg.address not in input_fallback:
            return "holding"
        return "input"

    blocks: list[tuple[str, list[RegisterDef]]] = []
    for reg in sorted(registers, key=lambda r: (read_type(r), r.address)):
        reg_type = read_type(reg)
        if blocks:
            last_type, last_block = blocks[-1]
            if (
                last_type == reg_type
                and last_block[-1].address + 1 == reg.address
                and len(last_block) < MAX_REGISTERS_PER_READ
            ):
                last_block.append(reg)
                continue
        blocks.append((reg_type, [reg]))
    return blocks


class WeishauptModbusCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Periodically reads all needed registers and provides them to the entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        host: str,
        port: int,
        device_id: int,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.device_name = name
        self.host = host
        self.port = port
        self.device_id = device_id
        self.client = AsyncModbusTcpClient(host, port=port)
        # Addresses of holding registers that only answer as input registers.
        self._input_fallback: set[int] = set()
        # Keys of registers that already logged a read failure (so the
        # warning is only logged once per outage, not every poll).
        self._failed_keys: set[str] = set()
        # Consecutive read failures per register key, reset on a successful
        # read. See CONSECUTIVE_FAILURES_BEFORE_DROP.
        self._consecutive_failures: dict[str, int] = {}
        # Raw values written since the register was last polled. A refresh
        # requested after a write may be delayed, so this is the newest
        # known device value until the next poll.
        self._written: dict[str, int] = {}

    async def _ensure_connected(self) -> None:
        if not self.client.connected:
            await self.client.connect()
        if not self.client.connected:
            raise UpdateFailed(f"Could not connect to {self.host}:{self.port}")

    async def _read_raw(
        self, reg_type: str, address: int, count: int
    ) -> list[int] | None:
        """Reads consecutive registers, None if the controller rejects the request."""
        try:
            if reg_type == "holding":
                result = await self.client.read_holding_registers(
                    address, count=count, device_id=self.device_id
                )
            else:
                result = await self.client.read_input_registers(
                    address, count=count, device_id=self.device_id
                )
        except ModbusException as err:
            raise UpdateFailed(
                f"Modbus error while reading address {address}: {err}"
            ) from err
        if result.isError():
            return None
        return list(result.registers)

    async def _read_single(
        self, reg: RegisterDef, primary_tried: bool
    ) -> list[int] | None:
        """Reads one register. A holding register the controller does not
        serve as holding register is retried as input register, and read
        that way from then on. `primary_tried` says that the request with
        the register's regular function code already failed."""
        if reg.register_type == "holding" and reg.address not in self._input_fallback:
            if not primary_tried:
                raw = await self._read_raw("holding", reg.address, 1)
                if raw is not None:
                    return raw
            raw = await self._read_raw("input", reg.address, 1)
            if raw is not None:
                _LOGGER.info(
                    "Register %s (address %s) is only readable as input register",
                    reg.key,
                    reg.address,
                )
                self._input_fallback.add(reg.address)
            return raw
        if primary_tried:
            return None
        return await self._read_raw("input", reg.address, 1)

    def _store(self, data: dict[str, float], reg: RegisterDef, raw: int) -> None:
        self._written.pop(reg.key, None)
        # The read itself succeeded, whether or not the value ends up
        # published below -- a value outside valid_range is a legitimate
        # "not available" state (e.g. no sensor installed), not a transient
        # communication failure, so it must not carry forward a stale value
        # the way a read failure below does.
        self._consecutive_failures.pop(reg.key, None)
        value = decode_value(raw, reg.data_type)
        if reg.valid_range is not None and not (
            reg.valid_range[0] <= value <= reg.valid_range[1]
        ):
            return
        data[reg.key] = value if reg.scale == 1 else round(value * reg.scale, 6)

    async def _async_update_data(self) -> dict[str, float]:
        await self._ensure_connected()
        data: dict[str, float] = {}

        # Only poll registers a currently-subscribed entity actually needs.
        # Each CoordinatorEntity passes its register keys as `context` when
        # subscribing, so async_contexts() reflects exactly what is enabled
        # and added to hass -- disabled entities never subscribe, and
        # enabling one later picks its registers up on the next poll.
        #
        # An empty set (e.g. the very first refresh in __init__.py, which
        # runs before entities are set up) means "poll everything".
        needed_keys: set[str] = set()
        for context in self.async_contexts():
            needed_keys.update(context)

        registers_to_poll = (
            [reg for reg in ALL_REGISTERS if reg.key in needed_keys]
            if needed_keys
            else ALL_REGISTERS
        )

        # Registers of optional hardware (e.g. heating circuits 3 and 4) are
        # rejected by controllers without it, which is expected during the
        # initial full poll. Once entities subscribed, a rejected register
        # belongs to an enabled entity and is worth a warning.
        failure_level = logging.WARNING if needed_keys else logging.DEBUG

        for reg_type, block in plan_read_blocks(registers_to_poll, self._input_fallback):
            raw_values = await self._read_raw(reg_type, block[0].address, len(block))
            if raw_values is not None and len(raw_values) == len(block):
                for reg, raw in zip(block, raw_values):
                    self._store(data, reg, raw)
                continue

            # One unavailable register (e.g. a heating circuit that is not
            # installed) must not hide its neighbours: read them one by one.
            for reg in block:
                raw_single = await self._read_single(reg, primary_tried=len(block) == 1)
                if raw_single is None:
                    if reg.key not in self._failed_keys:
                        self._failed_keys.add(reg.key)
                        _LOGGER.log(
                            failure_level,
                            "Error reading %s (address %s), the controller "
                            "rejected the request",
                            reg.key,
                            reg.address,
                        )
                    failures = self._consecutive_failures.get(reg.key, 0) + 1
                    self._consecutive_failures[reg.key] = failures
                    # Fewer than CONSECUTIVE_FAILURES_BEFORE_DROP failures in
                    # a row: keep showing the last known value rather than
                    # letting a single transient error flap the entity to
                    # unavailable/unknown and back on the very next poll.
                    if failures < CONSECUTIVE_FAILURES_BEFORE_DROP:
                        previous_value = (self.data or {}).get(reg.key)
                        if previous_value is not None:
                            data[reg.key] = previous_value
                    continue
                self._failed_keys.discard(reg.key)
                self._store(data, reg, raw_single[0])

        return data

    async def async_write_value(self, reg: RegisterDef, value: float) -> None:
        """Writes a value to a holding register and refreshes the data afterwards.

        Every parameter write is stored in the controller's EEPROM, which
        only survives a limited number of write cycles. A write that would
        not change the value is therefore skipped -- unless the register
        says it isn't stored in the EEPROM in the first place
        (`skip_unchanged_write=False`, e.g. SYSTEM_PV_SETPOINT_REGISTER),
        in which case it must be re-sent to stay in effect.
        """
        if not reg.writable:
            raise ValueError(f"Register {reg.key} is not writable")

        raw_value = round(value / reg.scale)
        register = encode_value(raw_value, reg.data_type)

        if reg.skip_unchanged_write:
            current = self._written.get(reg.key)
            if current is None:
                polled = (self.data or {}).get(reg.key)
                current = round(polled / reg.scale) if polled is not None else None
            if current == raw_value:
                _LOGGER.debug("Skipping write of %s: value is already set", reg.key)
                # Lets entities confirm a pending change against the unchanged data.
                self.async_update_listeners()
                return

        await self._ensure_connected()
        try:
            result = await self.client.write_register(
                reg.address, register, device_id=self.device_id
            )
            if result.isError():
                raise UpdateFailed(f"Error writing {reg.key}: {result}")
            self._written[reg.key] = raw_value
        except ModbusException as err:
            raise UpdateFailed(f"Modbus error while writing {reg.key}: {err}") from err

        await self.async_request_refresh()

    async def async_close(self) -> None:
        self.client.close()
