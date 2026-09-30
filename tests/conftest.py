"""Fixtures: a simulated heat pump and a config entry connected to it."""
from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pymodbus.client import AsyncModbusTcpClient
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.weishaupt_unofficial.const import DOMAIN
from modbus_simulator import build_server


@pytest.fixture(autouse=True)
def enable_sockets(socket_enabled: None) -> None:
    """The simulator is reached over a local TCP connection."""


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Makes Home Assistant load the integration from custom_components/."""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Simulator:
    """Handle to read and change the registers of the running simulator."""

    def __init__(self, port: int) -> None:
        self.port = port

    async def read(self, address: int) -> int:
        client = AsyncModbusTcpClient("127.0.0.1", port=self.port)
        await client.connect()
        try:
            result = await client.read_input_registers(address, count=1, device_id=1)
            assert not result.isError(), f"cannot read {address}: {result}"
            return result.registers[0]
        finally:
            client.close()

    async def write(self, address: int, value: int) -> None:
        client = AsyncModbusTcpClient("127.0.0.1", port=self.port)
        await client.connect()
        try:
            result = await client.write_register(address, value & 0xFFFF, device_id=1)
            assert not result.isError(), f"cannot write {address}: {result}"
        finally:
            client.close()


@pytest.fixture
async def simulator() -> AsyncGenerator[Simulator]:
    port = free_port()
    server = build_server("127.0.0.1", port)
    task = asyncio.create_task(server.serve_forever())
    for _ in range(50):
        client = AsyncModbusTcpClient("127.0.0.1", port=port)
        if await client.connect():
            client.close()
            break
        await asyncio.sleep(0.1)
    yield Simulator(port)
    await server.shutdown()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.fixture
def config_entry(simulator: Simulator) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Heat Pump",
        data={
            "name": "Heat Pump",
            "host": "127.0.0.1",
            "port": simulator.port,
            "device_id": 1,
        },
    )


@pytest.fixture
async def integration(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> AsyncGenerator[MockConfigEntry]:
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    yield config_entry
    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
async def make_integration(
    hass: HomeAssistant, simulator: Simulator
) -> AsyncGenerator[Callable[..., Any]]:
    """Factory for a config entry with custom options (e.g. an optional
    source entity), for tests that can't use the plain `integration`
    fixture. Every entry created through it is unloaded at the end of the
    test."""
    entries: list[MockConfigEntry] = []

    async def _make(options: dict | None = None) -> MockConfigEntry:
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="Heat Pump",
            data={
                "name": "Heat Pump",
                "host": "127.0.0.1",
                "port": simulator.port,
                "device_id": 1,
            },
            options=options or {},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entries.append(entry)
        return entry

    yield _make

    for entry in entries:
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def entity_id(
    hass: HomeAssistant, platform: str, entry: MockConfigEntry, unique_key: str
) -> str:
    """The entity id of the entity with the given unique key."""
    found = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{unique_key}"
    )
    assert found is not None, f"no {platform} entity {unique_key}"
    return found
