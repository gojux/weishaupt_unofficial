"""End-to-end check of a working setup against a real Home Assistant instance.

Runs in the `e2e` service of docker-compose.yml, next to Home Assistant and
the Modbus simulator. Everything used here is synthetic: a throwaway owner
account created at runtime, the compose service name of the simulator, and
an invented device title. No host addresses, serial numbers or personal
names are used anywhere.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest
from pymodbus.client import ModbusTcpClient

HA_URL = os.environ.get("HA_URL", "http://homeassistant:8123").rstrip("/")
SIMULATOR_HOST = "modbus-simulator"
SIMULATOR_PORT = 502
CLIENT_ID = "http://e2e.invalid/"
DEVICE_TITLE = "E2E Test Heat Pump"
# Entity ids Home Assistant derives from DEVICE_TITLE and the entity names.
PREFIX = "e2e_test_heat_pump"
PV_SETPOINT_ADDRESS = 40002
PV_SOURCE_ENTITY = "sensor.e2e_pv_surplus"
TIMEOUT_SECONDS = 180


def _request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    form: bool = False,
) -> tuple[int, Any]:
    headers: dict[str, str] = {}
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        HA_URL + path, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            return err.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return err.code, raw.decode(errors="replace")


def _ha_is_up() -> bool:
    try:
        return _request("GET", "/api/")[0] in (200, 401)
    except OSError:
        return False


def _wait_until(check: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while not check():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out waiting for {what}")
        time.sleep(2)


def _state(token: str, entity_id: str) -> str | None:
    status, body = _request("GET", f"/api/states/{entity_id}", token=token)
    return body["state"] if status == 200 else None


def _wait_for_state(token: str, entity_id: str, expected: str) -> None:
    _wait_until(
        lambda: _state(token, entity_id) == expected,
        f"{entity_id} == {expected!r} (last: {_state(token, entity_id)!r})",
    )


def _read_register(address: int) -> int:
    client = ModbusTcpClient(SIMULATOR_HOST, port=SIMULATOR_PORT)
    assert client.connect(), "cannot reach the Modbus simulator"
    try:
        result = client.read_holding_registers(address, count=1, device_id=1)
        assert not result.isError(), f"cannot read register {address}: {result}"
        return result.registers[0]
    finally:
        client.close()


@pytest.fixture(scope="module")
def token() -> str:
    """Creates the throwaway owner account and returns an access token.

    The account only exists in this run's Home Assistant instance; the
    password is generated per run and never written anywhere.
    """
    _wait_until(_ha_is_up, "Home Assistant")
    _, steps = _request("GET", "/api/onboarding")
    if any(step["step"] == "user" and step["done"] for step in steps):
        pytest.fail(
            "Home Assistant is already onboarded; reset the stack with "
            "`docker compose down -v` first"
        )
    status, created = _request(
        "POST",
        "/api/onboarding/users",
        {
            "client_id": CLIENT_ID,
            "name": "E2E Test",
            "username": "e2e-test",
            "password": secrets.token_urlsafe(24),
            "language": "en",
        },
    )
    assert status == 200, created
    status, tokens = _request(
        "POST",
        "/auth/token",
        {
            "grant_type": "authorization_code",
            "code": created["auth_code"],
            "client_id": CLIENT_ID,
        },
        form=True,
    )
    assert status == 200, tokens
    return tokens["access_token"]


@pytest.fixture(scope="module")
def entry_id(token: str) -> str:
    """Sets up the integration through the config flow, like a user would."""
    status, flow = _request(
        "POST",
        "/api/config/config_entries/flow",
        {"handler": "weishaupt_unofficial"},
        token,
    )
    assert status == 200, flow
    status, result = _request(
        "POST",
        f"/api/config/config_entries/flow/{flow['flow_id']}",
        {
            "name": DEVICE_TITLE,
            "host": SIMULATOR_HOST,
            "port": SIMULATOR_PORT,
            "device_id": 1,
            "scan_interval": 30,
        },
        token,
    )
    assert status == 200 and result["type"] == "create_entry", result
    return result["result"]["entry_id"]


def test_setup_creates_entities_with_the_simulator_values(token: str, entry_id: str) -> None:
    _wait_for_state(token, f"sensor.{PREFIX}_outdoor_temperature_1", "5.5")
    _wait_for_state(token, f"sensor.{PREFIX}_hot_water_temperature", "48.2")
    _wait_for_state(token, f"binary_sensor.{PREFIX}_fault", "off")
    _wait_for_state(token, f"select.{PREFIX}_system_operating_mode", "automatic")


def test_setting_the_pv_setpoint_reaches_the_controller(token: str, entry_id: str) -> None:
    entity = f"number.{PREFIX}_pv_power_setpoint"
    _wait_until(lambda: _state(token, entity) is not None, entity)
    status, _ = _request(
        "POST",
        "/api/services/number/set_value",
        {"entity_id": entity, "value": 1234},
        token,
    )
    assert status == 200
    try:
        _wait_until(
            lambda: _read_register(PV_SETPOINT_ADDRESS) == 1234,
            "the controller to hold the PV setpoint 1234",
        )
    finally:
        _request(
            "POST",
            "/api/services/number/set_value",
            {"entity_id": entity, "value": 0},
            token,
        )


def test_pv_surplus_switch_follows_the_source_entity(token: str, entry_id: str) -> None:
    """Configures a PV-surplus source through the options flow, then checks
    that the switch writes its value (kW converted to W) and resets to 0."""
    status, _ = _request(
        "POST",
        f"/api/states/{PV_SOURCE_ENTITY}",
        {
            "state": "1.5",
            "attributes": {"unit_of_measurement": "kW", "device_class": "power"},
        },
        token,
    )
    assert status in (200, 201)

    status, flow = _request(
        "POST", "/api/config/config_entries/options/flow", {"handler": entry_id}, token
    )
    assert status == 200, flow
    user_input: dict[str, Any] = {
        "general": {"scan_interval": 30},
        "pv_surplus": {"pv_surplus_entity_id": PV_SOURCE_ENTITY},
    }
    for number in range(1, 5):
        user_input[f"heating_circuit_{number}"] = {}
    status, result = _request(
        "POST",
        f"/api/config/config_entries/options/flow/{flow['flow_id']}",
        user_input,
        token,
    )
    assert status == 200 and result["type"] == "create_entry", result

    switch = f"switch.{PREFIX}_follow_pv_surplus"
    _wait_until(lambda: _state(token, switch) is not None, switch)
    status, _ = _request(
        "POST", "/api/services/switch/turn_on", {"entity_id": switch}, token
    )
    assert status == 200
    try:
        _wait_until(
            lambda: _read_register(PV_SETPOINT_ADDRESS) == 1500,
            "the controller to hold 1500 W from the 1.5 kW source",
        )
    finally:
        _request("POST", "/api/services/switch/turn_off", {"entity_id": switch}, token)
    _wait_until(
        lambda: _read_register(PV_SETPOINT_ADDRESS) == 0,
        "the controller to be reset to 0 after switching off",
    )
    _wait_for_state(token, switch, "off")
