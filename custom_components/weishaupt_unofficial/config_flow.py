"""Config Flow: UI-based setup with name, IP address, port and device ID."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.modbus import async_get_temporary_unit
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, section
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from modbus_connection import ModbusError, ModbusExceptionError, ModbusTcpParams

from .const import (
    CONF_DEVICE_ID,
    CONF_PV_SURPLUS_ENTITY_ID,
    CONF_TIMEOUT,
    CURRENT_TEMP_SOURCE_OPTIONS,
    DEFAULT_CURRENT_TEMP_SOURCE,
    DEFAULT_DEVICE_ID,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    HEATING_CIRCUITS,
    CONNECTION_TEST_ADDRESS,
    MAX_TIMEOUT,
    MIN_TIMEOUT,
    current_temp_entity_key,
    current_temp_source_key,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Required(CONF_DEVICE_ID, default=DEFAULT_DEVICE_ID): vol.Coerce(int),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.Coerce(int),
    }
)

# Same fields as STEP_USER_DATA_SCHEMA minus scan_interval, which stays the
# sole responsibility of the options flow (WeishauptUnofficialOptionsFlow) to
# avoid two different places editing the same value.
STEP_RECONFIGURE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Required(CONF_DEVICE_ID, default=DEFAULT_DEVICE_ID): vol.Coerce(int),
    }
)


async def test_connection(
    hass: HomeAssistant, host: str, port: int, device_id: int
) -> bool:
    """Checks whether a Modbus device is reachable with the given settings.

    Uses a temporary unit on Home Assistant's shared Modbus connection: if a
    config entry already holds the connection to this device, the test
    shares it; otherwise the connection is closed again afterwards. The
    connection only opens on the first request, so a real register is read.
    """
    try:
        async with async_get_temporary_unit(
            hass, ModbusTcpParams(host=host, port=port), device_id
        ) as unit:
            # Reading the first outdoor temperature is enough to test
            # connectivity.
            await unit.read_input_registers(CONNECTION_TEST_ADDRESS, 1)
    except ModbusExceptionError:
        # An error code returned by the device (e.g. "IllegalDataAddress")
        # still counts as "device reachable" -- only an actual communication
        # failure does not.
        return True
    except ModbusError as err:
        _LOGGER.debug("Connection test failed: %s", type(err).__name__)
        return False
    except HomeAssistantError as err:
        # The device is already in use over different link settings.
        _LOGGER.warning("Connection test failed: %s", err)
        return False
    return True


class WeishauptUnofficialConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config Flow for the Weishaupt unofficial integration."""

    VERSION = 1
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_NAME]
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            device_id = user_input[CONF_DEVICE_ID]

            # Prevent setting up the same controller twice
            unique_id = f"{host}:{port}:{device_id}"
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            connection_ok = await test_connection(self.hass, host, port, device_id)
            if not connection_ok:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=name,
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Let the user change name, IP address, port and device ID after setup.

        Reachable via the entry's three-dot menu -> "Reconfigure". Doesn't
        touch `scan_interval` or the heating-circuit temperature-source
        settings, which are handled by the options flow below.
        """
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            name = user_input[CONF_NAME]
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            device_id = user_input[CONF_DEVICE_ID]

            connection_ok = await test_connection(self.hass, host, port, device_id)
            if not connection_ok:
                errors["base"] = "cannot_connect"
            else:
                # The unique_id is derived from host/port/device_id, so it can
                # change here -- check for conflicts with *other* entries only
                # (the entry being reconfigured itself is excluded).
                new_unique_id = f"{host}:{port}:{device_id}"
                conflict = next(
                    (
                        entry
                        for entry in self._async_current_entries()
                        if entry.entry_id != reconfigure_entry.entry_id
                        and entry.unique_id == new_unique_id
                    ),
                    None,
                )
                if conflict is not None:
                    errors["base"] = "already_configured"
                else:
                    return self.async_update_reload_and_abort(
                        reconfigure_entry,
                        title=name,
                        unique_id=new_unique_id,
                        data={**reconfigure_entry.data, **user_input},
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_RECONFIGURE_DATA_SCHEMA, reconfigure_entry.data
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "WeishauptUnofficialOptionsFlow":
        return WeishauptUnofficialOptionsFlow()


class WeishauptUnofficialOptionsFlow(config_entries.OptionsFlow):
    """Allows changing the polling interval and, per heating circuit, where
    its `current_temperature` comes from (Modbus / none / a HA entity).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            # user_input is nested by section, e.g.:
            # {"general": {"scan_interval": 30}, "heating_circuit_1": {...}, ...}
            # Flatten it back into the flat structure the rest of the
            # integration expects (const.py's key helpers, __init__.py,
            # climate.py all read entry.options with flat keys like
            # "heating_circuit_1_current_temp_source" directly, with no
            # knowledge of the section grouping, which exists purely for
            # display).
            flat_data: dict[str, Any] = {}
            for section_values in user_input.values():
                flat_data.update(section_values)
            return self.async_create_entry(title="", data=flat_data)

        # Pre-fill fields via `description={"suggested_value": ...}`, NOT
        # via vol.Optional's `default=` (which re-inserts itself whenever a
        # field is missing from the submitted data -- exactly what happens
        # when the entity picker's "X" clears a field, making it impossible
        # to actually clear a previously selected value) and NOT via
        # add_suggested_values_to_schema() (doesn't recurse into
        # section()-wrapped fields -- see
        # https://github.com/home-assistant/frontend/issues/22419).
        current_scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

        current_timeout = self.config_entry.options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

        general_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    description={"suggested_value": current_scan_interval},
                ): vol.Coerce(int),
                vol.Optional(
                    CONF_TIMEOUT,
                    description={"suggested_value": current_timeout},
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_TIMEOUT, max=MAX_TIMEOUT)),
            }
        )
        schema_dict: dict[Any, Any] = {
            vol.Required("general"): section(general_schema, {"collapsed": False}),
        }

        # Optional source entity for WeishauptPvSurplusFollowSwitch
        # (switch.py); same "suggested_value" pattern as the current-temp
        # entity fields above -- see the note there on why NOT to use
        # vol.Optional's default=.
        current_pv_surplus_entity = self.config_entry.options.get(
            CONF_PV_SURPLUS_ENTITY_ID
        )
        pv_surplus_field_kwargs: dict[str, Any] = {}
        if current_pv_surplus_entity:
            pv_surplus_field_kwargs["description"] = {
                "suggested_value": current_pv_surplus_entity
            }
        pv_surplus_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PV_SURPLUS_ENTITY_ID, **pv_surplus_field_kwargs
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="power")
                ),
            }
        )
        schema_dict[vol.Required("pv_surplus")] = section(
            pv_surplus_schema, {"collapsed": False}
        )

        for hc in HEATING_CIRCUITS:
            source_key = current_temp_source_key(hc)
            entity_key = current_temp_entity_key(hc)

            current_source = self.config_entry.options.get(
                source_key, DEFAULT_CURRENT_TEMP_SOURCE
            )
            current_entity_id = self.config_entry.options.get(entity_key)

            entity_field_kwargs: dict[str, Any] = {}
            if current_entity_id:
                entity_field_kwargs["description"] = {
                    "suggested_value": current_entity_id
                }

            hc_schema = vol.Schema(
                {
                    vol.Optional(
                        source_key,
                        description={"suggested_value": current_source},
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=CURRENT_TEMP_SOURCE_OPTIONS,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key="current_temp_source",
                        )
                    ),
                    # Always shown, but only relevant when the source above
                    # is set to "entity" -- see WeishauptRoomClimate in climate.py.
                    vol.Optional(
                        entity_key, **entity_field_kwargs
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            domain="sensor", device_class="temperature"
                        )
                    ),
                }
            )
            schema_dict[vol.Required(hc.key)] = section(
                hc_schema, {"collapsed": False}
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
        )
