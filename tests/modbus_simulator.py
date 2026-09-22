"""Modbus TCP simulator of a Weishaupt heat pump, used for tests and development.

The register values are listed with the addresses printed in the Weishaupt
data point list "Modbus TCP (WWP)" and deliberately do not import the
integration's register map, so tests also check the map against the list.
Like the real controller, it answers at most five consecutive registers per
request and rejects addresses it does not know. Heating circuits 3 and 4
are not installed.
"""
from __future__ import annotations

import asyncio
import logging
import os

from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

MAX_REGISTERS_PER_READ = 5

DEFAULT_VALUES: dict[int, int] = {
    # System
    30001: 55,        # outdoor temperature 1: 5.5 °C
    30002: -32768,    # outdoor temperature 2: no sensor
    30003: 65535,     # error: none
    30004: 65535,     # warning: none
    30005: 65535,
    30006: 19,        # system status: heating
    40001: 0,         # system operating mode: automatic
    40002: 0,         # PV power setpoint: inactive
    # Heating circuit 1: standby is 4, automatic 0, comfort/normal/setback 1..3
    31101: 210, 31102: 208, 31103: 65535, 31104: 350, 31105: 342,
    41103: 2, 41105: 220, 41106: 210, 41107: 180,
    41110: 350, 41111: 300, 41112: 150,
    # Heating circuit 2
    31201: -32768, 31202: 215, 31203: 65535, 31204: 320, 31205: 315,
    41203: 0, 41205: 220, 41206: 205, 41207: 170,
    41210: 350, 41211: 300, 41212: 150,
    # Domestic hot water
    32101: 500, 32102: 482,
    42102: 0, 42103: 500, 42104: 400,
    # Heat pump
    33101: 19, 33102: 1, 33103: 45, 33104: 358, 33105: 312,
    33108: -32768, 33109: -32768, 33110: 405, 33111: -32768,
    33126: 850,       # electrical power input, not in the official list
    # Second heat generator and electric heaters
    34101: 0, 34102: 12, 34104: 0, 34105: 1, 34106: 5, 34107: 7,
    # Inputs
    35101: 0, 35102: 0, 35103: -32768, 35104: -32768, 35105: -32768,
    35106: -32768, 35107: 0, 35108: 0,
    # Statistics: total, heating, hot water, cooling x today/yesterday/month/year
    36101: 12, 36102: 30, 36103: 410, 36104: 3900,
    36201: 9, 36202: 24, 36203: 350, 36204: 3100,
    36301: 3, 36302: 6, 36303: 60, 36304: 800,
    36401: 0, 36402: 0, 36403: 0, 36404: 0,
    # Electrical energy, not in the official list (see const.py)
    36701: 4, 36702: 11, 36703: 140, 36704: 1300,
}


async def _limit_request_size(
    function_code: int,
    start_address: int,
    address: int,
    count: int,
    current_registers: list[int],
    set_values: list[int] | list[bool] | None,
) -> ExcCodes | None:
    """Rejects requests for more registers than the controller answers."""
    if count > MAX_REGISTERS_PER_READ:
        return ExcCodes.ILLEGAL_ADDRESS
    return None


def _to_signed(word: int) -> int:
    word &= 0xFFFF
    return word - 0x10000 if word > 0x7FFF else word


def build_device(values: dict[int, int] | None = None) -> SimDevice:
    """Creates the device; the input and holding registers are shared.

    Values may be given signed or as unsigned 16-bit words. Addresses that
    are not listed are rejected as illegal, like on the real controller.
    """
    source = DEFAULT_VALUES if values is None else values
    return SimDevice(
        id=0,
        simdata=[
            SimData(address, values=_to_signed(value), datatype=DataType.REGISTERS)
            for address, value in sorted(source.items())
        ],
        action=_limit_request_size,
    )


def build_server(
    host: str = "0.0.0.0",
    port: int = 502,
    values: dict[int, int] | None = None,
) -> ModbusTcpServer:
    return ModbusTcpServer(build_device(values), address=(host, port))


async def run_server(
    host: str = "0.0.0.0", port: int = 502, values: dict[int, int] | None = None
) -> None:
    await build_server(host, port, values).serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_server(port=int(os.environ.get("SIMULATOR_PORT", "502"))))
