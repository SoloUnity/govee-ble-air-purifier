"""Config flow for Govee BLE Air Purifier."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_DISCOVERED_DEVICE,
    CONF_POLLING_INTERVAL,
    CONF_PROFILE,
    DEFAULT_POLLING_INTERVAL_SECONDS,
    DOMAIN,
)
from .profiles import H7124_PROFILE, get_profile, normalize_ble_address
from .setup_helpers import (
    MANUAL_DEVICE_VALUE,
    DiscoveredDeviceOption,
    build_discovered_device_options,
    polling_interval_from_options,
    validate_polling_interval_seconds,
)


class GoveeBleAirPurifierConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Govee BLE Air Purifier."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual setup by BLE address and optional name."""

        errors: dict[str, str] = {}
        if user_input is None:
            if request_active_scan := getattr(
                bluetooth, "async_request_active_scan", None
            ):
                await request_active_scan(self.hass)
        discovered_options = _discovered_device_options(self.hass)
        discovered_by_value = {option.value: option for option in discovered_options}
        if user_input is not None:
            selected_device = user_input.get(
                CONF_DISCOVERED_DEVICE, MANUAL_DEVICE_VALUE
            )
            if selected_device == MANUAL_DEVICE_VALUE:
                address = user_input.get(CONF_ADDRESS, "").strip()
                if not address:
                    errors[CONF_ADDRESS] = "address_required"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=_user_schema(discovered_options),
                        errors=errors,
                    )
                profile = H7124_PROFILE
                name = user_input.get(CONF_NAME) or profile.display_name
            else:
                option = discovered_by_value.get(selected_device)
                address = selected_device
                profile = get_profile(option.profile_key if option else None)
                name = user_input.get(CONF_NAME) or (
                    option.name if option else profile.display_name
                )

            polling_interval = validate_polling_interval_seconds(
                user_input.get(
                    CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL_SECONDS
                )
            )
            unique_id = _unique_id_from_address(address)
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured(updates={CONF_ADDRESS: address})
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ADDRESS: address,
                    CONF_NAME: name,
                    CONF_PROFILE: profile.key,
                },
                options={CONF_POLLING_INTERVAL: polling_interval},
            )

        return self.async_show_form(
            step_id="user", data_schema=_user_schema(discovered_options), errors=errors
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""

        return GoveeBleAirPurifierOptionsFlow(config_entry)


class GoveeBleAirPurifierOptionsFlow(config_entries.OptionsFlow):
    """Handle options for Govee BLE Air Purifier."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options."""

        if user_input is not None:
            polling_interval = validate_polling_interval_seconds(
                user_input[CONF_POLLING_INTERVAL]
            )
            return self.async_create_entry(
                title="",
                data={
                    **dict(self._config_entry.options),
                    CONF_POLLING_INTERVAL: polling_interval,
                },
            )

        return self.async_show_form(
            step_id="init",
            data_schema=_polling_interval_schema(
                default=polling_interval_from_options(self._config_entry.options)
            ),
        )


def _unique_id_from_address(address: str) -> str:
    """Build the config-entry unique id from the BLE address."""

    return normalize_ble_address(address)


def _discovered_device_options(hass: Any) -> tuple[DiscoveredDeviceOption, ...]:
    """Return supported purifier discoveries from Home Assistant's BLE cache."""

    return build_discovered_device_options(
        tuple(bluetooth.async_discovered_service_info(hass, connectable=True))
    )


def _user_schema(
    discovered_options: tuple[DiscoveredDeviceOption, ...],
) -> vol.Schema:
    """Build the setup schema with a discovered-device picker and manual fallback."""

    default_device = (
        discovered_options[0].value if discovered_options else MANUAL_DEVICE_VALUE
    )
    return vol.Schema(
        {
            vol.Optional(
                CONF_DISCOVERED_DEVICE,
                default=default_device,
            ): vol.In(_select_options(discovered_options)),
            vol.Optional(CONF_ADDRESS): str,
            vol.Optional(CONF_NAME, default=H7124_PROFILE.display_name): str,
            vol.Required(
                CONF_POLLING_INTERVAL,
                default=DEFAULT_POLLING_INTERVAL_SECONDS,
            ): _polling_interval_validator,
        }
    )


def _polling_interval_schema(
    *, default: int = DEFAULT_POLLING_INTERVAL_SECONDS
) -> vol.Schema:
    """Build a polling-interval-only schema."""

    return vol.Schema(
        {
            vol.Required(
                CONF_POLLING_INTERVAL,
                default=default,
            ): _polling_interval_validator,
        }
    )


def _select_options(
    discovered_options: tuple[DiscoveredDeviceOption, ...],
) -> dict[str, str]:
    """Build HA select options for discovered purifiers plus manual entry."""

    options = {MANUAL_DEVICE_VALUE: "Enter address manually"}
    options.update({option.value: option.label for option in discovered_options})
    return options


def _polling_interval_validator(value: object) -> int:
    """Voluptuous validator for the polling interval."""

    try:
        return validate_polling_interval_seconds(value)
    except ValueError as err:
        raise vol.Invalid(str(err)) from err
