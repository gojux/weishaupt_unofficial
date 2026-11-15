"""DataUpdateCoordinator: polling and writes over Home Assistant's shared Modbus connection."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import TypeVar

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from modbus_connection import (
    ClientClosedError,
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusDesyncError,
    ModbusError,
    ModbusExceptionError,
    ModbusTimeoutError,
    ModbusUnit,
)

from . import const
from .const import (
    ALL_REGISTERS,
    CONSECUTIVE_FAILURES_BEFORE_DROP,
    DOCUMENTED_SENTINELS,
    FAILED_POLLS_TOLERATED,
    HEAT_PUMP_STATUS_MAP,
    MAX_FAILED_ANSWERS,
    MAX_LOST_CONNECTIONS,
    MAX_REGISTERS_PER_READ,
    STATUS_REGISTERS,
    DataType,
    RegisterDef,
)

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# No (valid) answer to a request: counts as a failed answer. A desync (a late
# answer carrying another request's transaction ID) already drops the link in
# the library; a timeout leaves it open.
FAILED_ANSWER_ERRORS = (ModbusTimeoutError, ModbusDesyncError)

# Outcomes of a register in a poll (RegisterReading.outcome).
OUTCOME_PUBLISHED = "published"
OUTCOME_FILTERED = "filtered"  # read, but outside valid_range
OUTCOME_REJECTED = "rejected"  # the controller rejected the request
OUTCOME_KEPT = "kept"  # rejected, last known value kept for now
OUTCOME_UNSUPPORTED = "unsupported"  # not on this controller, no longer polled


@dataclass
class RequestCounters:
    """Modbus request statistics, for the debug log and diagnostics."""

    requests: int = 0  # every attempt sent, retries included
    failed_answers: int = 0  # timeouts and desyncs
    lost_connections: int = 0  # requests that lost the link
    rejected: int = 0  # requests the controller answered with an error code

    def __sub__(self, other: RequestCounters) -> RequestCounters:
        return RequestCounters(
            *(
                getattr(self, field.name) - getattr(other, field.name)
                for field in dataclasses.fields(self)
            )
        )


@dataclass
class RegisterReading:
    """What the last poll got for one register."""

    address: int
    raw: int | None  # raw 16-bit word as received; None if not read
    value: int | None  # raw word decoded per data_type (signed or unsigned)
    outcome: str
    detail: str | None = None  # e.g. the meaning of a filtered sentinel


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


async def async_with_retries(
    unit: ModbusUnit,
    request: Callable[[], Awaitable[_T]],
    description: str,
    counters: RequestCounters | None = None,
) -> _T:
    """Runs a Modbus request, retrying it on a failed answer or a lost link.

    - ModbusExceptionError (the controller answered with an error code) is
      final and raised at once.
    - A timeout or desync counts as a failed answer; the request is given up
      after MAX_FAILED_ANSWERS of them. A timeout leaves the link open, and a
      peer that stopped answering would time out every retry on it, so the
      link is dropped first: the retry runs on a new one.
    - A lost connection does not count as a failed answer (the retry runs on
      a new link), but at most MAX_LOST_CONNECTIONS are retried.
    - The connection being closed for good (the entry unloads) is final.

    Only for requests that are safe to repeat: every write of this
    integration sets an absolute value. `counters`, if given, is updated
    with every attempt and its outcome.
    """
    counters = counters if counters is not None else RequestCounters()
    failed_answers = 0
    lost_connections = 0
    while True:
        counters.requests += 1
        try:
            return await request()
        except ClientClosedError:
            raise
        except ModbusExceptionError:
            counters.rejected += 1
            raise
        except FAILED_ANSWER_ERRORS as err:
            counters.failed_answers += 1
            failed_answers += 1
            if failed_answers >= MAX_FAILED_ANSWERS:
                raise
            reason = type(err).__name__
            if isinstance(err, ModbusTimeoutError):
                try:
                    await unit.disconnect()
                except ModbusError as disconnect_err:
                    _LOGGER.debug(
                        "Dropping the link after a timeout failed: %s",
                        type(disconnect_err).__name__,
                    )
        except ModbusConnectionError as err:
            counters.lost_connections += 1
            lost_connections += 1
            if lost_connections > MAX_LOST_CONNECTIONS:
                raise
            reason = type(err).__name__
        _LOGGER.debug(
            "Retrying %s after %s (failed answers %d/%d, lost connections %d/%d)",
            description,
            reason,
            failed_answers,
            MAX_FAILED_ANSWERS,
            lost_connections,
            MAX_LOST_CONNECTIONS,
        )
        # Looked up at call time so tests can shorten it.
        await asyncio.sleep(const.RETRY_DELAY)


def _format_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} h {minutes} min"
    if minutes:
        return f"{minutes} min {secs} s"
    return f"{secs} s"


def _status_name(code: int | None) -> str:
    if code is None:
        return "none"
    return f"{code} ({HEAT_PUMP_STATUS_MAP.get(code, 'unknown')})"


class WeishauptModbusCoordinator(DataUpdateCoordinator[dict[str, float]]):
    """Periodically reads all needed registers and provides them to the entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        unit: ModbusUnit,
        name: str,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=name,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.device_name = name
        # Unit on Home Assistant's shared Modbus connection (modbus integration).
        # The connection is opened on the first request, reopened on the
        # request after a drop, and closed by Home Assistant when the last
        # config entry holding it unloads.
        self.unit = unit
        # Addresses of holding registers that only answer as input registers.
        self._input_fallback: set[int] = set()
        # Keys of registers that already logged a read failure (so the
        # warning is only logged once per outage, not every poll).
        self._failed_keys: set[str] = set()
        # Consecutive read failures per register key, reset on a successful
        # read. See CONSECUTIVE_FAILURES_BEFORE_DROP.
        self._consecutive_failures: dict[str, int] = {}
        # Keys of registers this controller does not have: rejected as an
        # illegal data address CONSECUTIVE_FAILURES_BEFORE_DROP times in a
        # row. Not polled until the entry reloads (enabling an entity
        # reloads it, too).
        self._unsupported: set[str] = set()
        # Failed polls in a row since the last good one. See
        # FAILED_POLLS_TOLERATED.
        self._failed_polls = 0
        # Raw values written since the register was last polled. A refresh
        # requested after a write may be delayed, so this is the newest
        # known device value until the next poll.
        self._written: dict[str, int] = {}
        # --- Statistics for the debug log and diagnostics ---
        # Requests since setup (polls and writes).
        self.counters = RequestCounters()
        # Connection drops reported by the link (on_connection_lost).
        self.connection_losses = 0
        # Polls that failed (no answer) since setup, tolerated or not.
        self.failed_polls_total = 0
        # What the last poll got per register key (raw value and outcome).
        self.readings: dict[str, RegisterReading] = {}
        # When the last poll succeeded (UTC).
        self.last_successful_poll: datetime | None = None
        # Whether debug logging was on at the last poll: the raw values are
        # logged once each time it gets switched on (and after setup).
        self._debug_was_enabled = False
        # Status registers: last code and since when (monotonic seconds).
        self._status_since: dict[str, tuple[int | None, float]] = {}
        # (register key, raw value) already warned about as undocumented.
        self._warned_values: set[tuple[str, int]] = set()
        # True during a poll of every register (no entity subscribed yet):
        # registers of disabled entities and missing hardware are expected
        # to be odd then, so their findings go to DEBUG only.
        self._full_poll = False
        # Runtime state shared between WeishauptPvPowerSetpointNumber
        # (number.py) and WeishauptPvSurplusFollowSwitch (switch.py), which
        # have no direct reference to each other. `pv_setpoint_last_value`
        # is the value the number entity's heartbeat keeps re-sending,
        # updated synchronously by whichever of the two last wrote it, so
        # there is no race between the switch writing 0 on "off" and the
        # heartbeat resending a stale value. `pv_surplus_follow_enabled`
        # tells the number entity to reject direct writes and collapse its
        # range while the switch is on.
        self.pv_setpoint_last_value: float | None = None
        self.pv_surplus_follow_enabled: bool = False

    @property
    def failed_polls_in_a_row(self) -> int:
        """Failed polls in a row since the last good one."""
        return self._failed_polls

    @property
    def input_fallback_addresses(self) -> list[int]:
        """Holding registers that only answer as input registers."""
        return sorted(self._input_fallback)

    @property
    def consecutive_failures(self) -> dict[str, int]:
        """Registers rejected in the last polls, with the count in a row."""
        return dict(self._consecutive_failures)

    @property
    def unsupported_registers(self) -> list[str]:
        """Registers this controller does not have, no longer polled."""
        return sorted(self._unsupported)

    @property
    def pending_writes(self) -> dict[str, int]:
        """Raw values written since the register was last polled."""
        return dict(self._written)

    def status_durations(self) -> dict[str, tuple[int | None, float]]:
        """Status register key -> (current code, seconds in that status)."""
        now = time.monotonic()
        return {
            key: (code, now - since) for key, (code, since) in self._status_since.items()
        }

    @callback
    def handle_connection_lost(self) -> None:
        """Called by the link when the connection drops (on_connection_lost)."""
        self.connection_losses += 1
        _LOGGER.debug("Modbus connection lost (%d since setup)", self.connection_losses)

    async def _read_raw(
        self, reg_type: str, address: int, count: int
    ) -> list[int] | ModbusExceptionError:
        """Reads consecutive registers; the error if the controller rejects the request.

        A request that gets no answer after its retries fails the whole poll
        (UpdateFailed): the controller or the link is down, and walking the
        remaining blocks would only wait out the same timeouts again.
        """
        read = (
            self.unit.read_holding_registers
            if reg_type == "holding"
            else self.unit.read_input_registers
        )
        description = f"read of {count} {reg_type} register(s) at {address}"
        try:
            values = await async_with_retries(
                self.unit, partial(read, address, count), description, self.counters
            )
        except ModbusExceptionError as err:
            _LOGGER.debug("Controller rejected the %s: %s", description, err)
            return err
        except ModbusError as err:
            raise UpdateFailed(
                f"No answer to the {description}: {type(err).__name__}"
            ) from err
        return list(values)

    async def _read_single(
        self, reg: RegisterDef, primary_tried: bool
    ) -> list[int] | ModbusExceptionError | None:
        """Reads one register. A holding register the controller does not
        serve as holding register is retried as input register, and read
        that way from then on. `primary_tried` says that the request with
        the register's regular function code already failed (None: no
        further request was made)."""
        if reg.register_type == "holding" and reg.address not in self._input_fallback:
            if not primary_tried:
                raw = await self._read_raw("holding", reg.address, 1)
                if isinstance(raw, list):
                    return raw
            raw = await self._read_raw("input", reg.address, 1)
            if isinstance(raw, list):
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

    def _store(
        self,
        data: dict[str, float],
        readings: dict[str, RegisterReading],
        reg: RegisterDef,
        raw: int,
    ) -> None:
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
            meaning = DOCUMENTED_SENTINELS.get(reg.valid_range, {}).get(value)
            if (state := dict(reg.named_values).get(value)) is not None:
                data[reg.key] = state
                readings[reg.key] = RegisterReading(
                    reg.address, raw, value, OUTCOME_PUBLISHED, meaning
                )
                return
            if meaning is None:
                self._warn_once(
                    reg,
                    value,
                    "%s (address %s) reported %s, outside its documented range "
                    "%s and not a documented status value; not published",
                    reg.key,
                    reg.address,
                    value,
                    reg.valid_range,
                )
            readings[reg.key] = RegisterReading(
                reg.address, raw, value, OUTCOME_FILTERED, meaning or "undocumented value"
            )
            return
        data[reg.key] = value if reg.scale == 1 else round(value * reg.scale, 6)
        readings[reg.key] = RegisterReading(reg.address, raw, value, OUTCOME_PUBLISHED)

    def _warn_once(self, reg: RegisterDef, value: int, msg: str, *args: object) -> None:
        """Warns about an undocumented value once per register and value.

        Repeats are not logged; the raw value dump shows them again each
        time debug logging is switched on. During a full poll the finding
        goes to DEBUG and the warning stays due for when an entity uses
        the register.
        """
        if (reg.key, value) in self._warned_values:
            return
        if self._full_poll:
            _LOGGER.debug(msg, *args)
            return
        self._warned_values.add((reg.key, value))
        _LOGGER.warning(msg, *args)

    async def _async_update_data(self) -> dict[str, float]:
        """Polls the controller; a short outage keeps the last values.

        A poll that fails (no answer after the retries, connection lost)
        returns the previous data for up to FAILED_POLLS_TOLERATED polls in a
        row, so a brief outage does not flap every entity to unavailable.
        Without previous data (the first refresh) it fails right away.
        """
        started = time.monotonic()
        counters_before = dataclasses.replace(self.counters)
        try:
            data, readings = await self._async_poll()
        except UpdateFailed as err:
            self._failed_polls += 1
            self.failed_polls_total += 1
            _LOGGER.debug(
                "Poll failed after %.2f s, %s: %s",
                time.monotonic() - started,
                self._format_counters(self.counters - counters_before),
                err,
            )
            if self.data is not None and self._failed_polls <= FAILED_POLLS_TOLERATED:
                _LOGGER.debug(
                    "Keeping the last values (failed poll %d of %d tolerated)",
                    self._failed_polls,
                    FAILED_POLLS_TOLERATED,
                )
                return self.data
            raise
        self._failed_polls = 0
        self.readings = readings
        self.last_successful_poll = dt_util.utcnow()
        self._log_poll(time.monotonic() - started, self.counters - counters_before, readings)
        self._log_status_changes(data)
        return data

    @staticmethod
    def _format_counters(counters: RequestCounters) -> str:
        return (
            f"{counters.requests} requests ({counters.failed_answers} failed answers, "
            f"{counters.lost_connections} lost connections, {counters.rejected} rejected)"
        )

    def _log_poll(
        self,
        duration: float,
        counters: RequestCounters,
        readings: dict[str, RegisterReading],
    ) -> None:
        debug = _LOGGER.isEnabledFor(logging.DEBUG)
        log_raw_values = debug and not self._debug_was_enabled
        self._debug_was_enabled = debug
        if not debug:
            return
        outcomes = [reading.outcome for reading in readings.values()]
        _LOGGER.debug(
            "Poll: %.2f s, %s, %d registers: %d published, %d filtered, "
            "%d rejected, %d kept, %d unsupported",
            duration,
            self._format_counters(counters),
            len(readings),
            outcomes.count(OUTCOME_PUBLISHED),
            outcomes.count(OUTCOME_FILTERED),
            outcomes.count(OUTCOME_REJECTED),
            outcomes.count(OUTCOME_KEPT),
            outcomes.count(OUTCOME_UNSUPPORTED),
        )
        if not log_raw_values:
            return
        # Once each time debug logging is switched on: every polled
        # register's raw value, so a value like "0.0 degC" can be told apart
        # from a status word.
        for key, reading in sorted(readings.items(), key=lambda item: item[1].address):
            _LOGGER.debug(
                "Register %s (address %s): raw %s, %s%s",
                key,
                reading.address,
                "-" if reading.raw is None else f"0x{reading.raw:04X} = {reading.value}",
                reading.outcome,
                f" ({reading.detail})" if reading.detail else "",
            )

    def _log_status_changes(self, data: dict[str, float]) -> None:
        now = time.monotonic()
        for reg in STATUS_REGISTERS:
            raw = data.get(reg.key)
            code = int(raw) if raw is not None else None
            if code is not None and code not in HEAT_PUMP_STATUS_MAP:
                self._warn_once(
                    reg,
                    code,
                    "%s (address %s) reported the undocumented status code %s",
                    reg.key,
                    reg.address,
                    code,
                )
            previous = self._status_since.get(reg.key)
            if previous is None:
                self._status_since[reg.key] = (code, now)
                continue
            previous_code, since = previous
            if code == previous_code:
                continue
            self._status_since[reg.key] = (code, now)
            _LOGGER.debug(
                "%s: %s -> %s after %s",
                reg.key,
                _status_name(previous_code),
                _status_name(code),
                _format_duration(now - since),
            )

    async def _async_poll(
        self,
    ) -> tuple[dict[str, float], dict[str, RegisterReading]]:
        data: dict[str, float] = {}
        readings: dict[str, RegisterReading] = {}

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

        registers_to_poll = []
        for reg in ALL_REGISTERS:
            if needed_keys and reg.key not in needed_keys:
                continue
            if reg.key in self._unsupported:
                readings[reg.key] = RegisterReading(
                    reg.address, None, None, OUTCOME_UNSUPPORTED, "illegal data address"
                )
                continue
            registers_to_poll.append(reg)

        # Registers of optional hardware (e.g. heating circuits 3 and 4) are
        # rejected by controllers without it, which is expected during the
        # initial full poll. Once entities subscribed, a rejected register
        # belongs to an enabled entity and is worth a warning.
        self._full_poll = not needed_keys
        failure_level = logging.DEBUG if self._full_poll else logging.WARNING

        for reg_type, block in plan_read_blocks(registers_to_poll, self._input_fallback):
            raw_values = await self._read_raw(reg_type, block[0].address, len(block))
            if isinstance(raw_values, list) and len(raw_values) == len(block):
                for reg, raw in zip(block, raw_values):
                    self._store(data, readings, reg, raw)
                continue

            # One unavailable register (e.g. a heating circuit that is not
            # installed) must not hide its neighbours: read them one by one.
            for reg in block:
                raw_single = await self._read_single(reg, primary_tried=len(block) == 1)
                if not isinstance(raw_single, list):
                    # A single-register block was rejected by the block read.
                    error = raw_values if raw_single is None else raw_single
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
                    if failures >= CONSECUTIVE_FAILURES_BEFORE_DROP and isinstance(
                        error, IllegalDataAddressError
                    ):
                        self._unsupported.add(reg.key)
                        self._consecutive_failures.pop(reg.key)
                        _LOGGER.log(
                            failure_level,
                            "Register %s (address %s) does not exist on this "
                            "controller (illegal data address %d times in a row); "
                            "not polled until the integration is reloaded",
                            reg.key,
                            reg.address,
                            failures,
                        )
                        readings[reg.key] = RegisterReading(
                            reg.address, None, None, OUTCOME_UNSUPPORTED,
                            "illegal data address",
                        )
                        continue
                    outcome = OUTCOME_REJECTED
                    # Fewer than CONSECUTIVE_FAILURES_BEFORE_DROP failures in
                    # a row: keep showing the last known value rather than
                    # letting a single transient error flap the entity to
                    # unavailable/unknown and back on the very next poll.
                    if failures < CONSECUTIVE_FAILURES_BEFORE_DROP:
                        previous_value = (self.data or {}).get(reg.key)
                        if previous_value is not None:
                            data[reg.key] = previous_value
                            outcome = OUTCOME_KEPT
                    readings[reg.key] = RegisterReading(
                        reg.address, None, None, outcome, f"{failures} in a row"
                    )
                    continue
                self._failed_keys.discard(reg.key)
                self._store(data, readings, reg, raw_single[0])

        return data, readings

    async def _controller_holds(self, reg: RegisterDef, raw_value: int) -> bool:
        """Whether the controller holds `raw_value` in `reg` right now.

        Reads the register fresh (with retries and the input fallback). A
        register that cannot be read counts as "not held": a needless write
        costs one EEPROM cycle, a wrongly skipped one loses the setpoint.
        """
        try:
            raw = await self._read_single(reg, primary_tried=False)
        except UpdateFailed as err:
            _LOGGER.debug("Fresh read of %s before writing failed: %s", reg.key, err)
            return False
        if not isinstance(raw, list):
            _LOGGER.debug("Fresh read of %s before writing was rejected", reg.key)
            return False
        held = decode_value(raw[0], reg.data_type)
        if held != raw_value:
            _LOGGER.debug(
                "%s changed at the controller since the last poll (known %s, "
                "controller %s): writing %s",
                reg.key,
                raw_value,
                held,
                raw_value,
            )
            return False
        return True

    async def async_write_value(self, reg: RegisterDef, value: float) -> None:
        """Writes a value to a holding register and refreshes the data afterwards.

        Every parameter write is stored in the controller's EEPROM, which
        only survives a limited number of write cycles. A write that would
        not change the value is therefore skipped -- unless the register
        says it isn't stored in the EEPROM in the first place
        (`skip_unchanged_write=False`, e.g. SYSTEM_PV_SETPOINT_REGISTER),
        in which case it must be re-sent to stay in effect.

        The write is retried like a read (see async_with_retries); that is
        safe because it sets an absolute value.
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
            # The known value may be stale (e.g. changed at the controller's
            # panel since the last poll): confirm it with a fresh read before
            # dropping the write.
            if current == raw_value and await self._controller_holds(reg, raw_value):
                _LOGGER.debug("Skipping write of %s: value is already set", reg.key)
                # Lets entities confirm a pending change against the unchanged data.
                self.async_update_listeners()
                return

        _LOGGER.debug(
            "Writing %s (address %s): %s (raw %s)", reg.key, reg.address, value, raw_value
        )
        try:
            await async_with_retries(
                self.unit,
                partial(self.unit.write_register, reg.address, register),
                f"write of {reg.key} (address {reg.address})",
                self.counters,
            )
        except ModbusExceptionError as err:
            _LOGGER.debug("Controller rejected the write of %s: %s", reg.key, err)
            raise UpdateFailed(f"Error writing {reg.key}: {err}") from err
        except ModbusError as err:
            _LOGGER.debug("Write of %s failed: %s", reg.key, type(err).__name__)
            raise UpdateFailed(
                f"Modbus error while writing {reg.key}: {type(err).__name__}"
            ) from err
        self._written[reg.key] = raw_value
        _LOGGER.debug("Wrote %s", reg.key)

        await self.async_request_refresh()
