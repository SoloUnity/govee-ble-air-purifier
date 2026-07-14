"""Config flow for Govee BLE Air Purifier."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_CUSTOM_AUTO_DELAY_20,
    CONF_CUSTOM_AUTO_DELAY_40,
    CONF_CUSTOM_AUTO_DELAY_60,
    CONF_CUSTOM_AUTO_DELAY_80,
    CONF_CUSTOM_AUTO_DOWN_20,
    CONF_CUSTOM_AUTO_DOWN_40,
    CONF_CUSTOM_AUTO_DOWN_60,
    CONF_CUSTOM_AUTO_DOWN_80,
    CONF_CUSTOM_AUTO_UP_100,
    CONF_CUSTOM_AUTO_UP_40,
    CONF_CUSTOM_AUTO_UP_60,
    CONF_CUSTOM_AUTO_UP_80,
    CONF_DISCOVERED_DEVICE,
    CONF_POLLING_INTERVAL,
    CONF_PROFILE,
    CONF_USE_CUSTOM_AUTO,
    DEFAULT_POLLING_INTERVAL_SECONDS,
    DOMAIN,
    MAX_POLLING_INTERVAL_SECONDS,
    MIN_POLLING_INTERVAL_SECONDS,
)
from .controller import (
    CUSTOM_AUTO_DEFAULTS,
    CUSTOM_AUTO_OPTION_KEYS,
    CustomAutoConfig,
    parse_custom_auto_values,
    validate_custom_auto_values,
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

    def __init__(self) -> None:
        super().__init__()
        self._pending_entry: dict[str, Any] | None = None
        self._pending_options: dict[str, Any] | None = None

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
            self._pending_entry = {
                "title": name,
                "data": {
                    CONF_ADDRESS: address,
                    CONF_NAME: name,
                    CONF_PROFILE: profile.key,
                },
            }
            self._pending_options = {
                CONF_POLLING_INTERVAL: polling_interval,
                CONF_USE_CUSTOM_AUTO: user_input.get(CONF_USE_CUSTOM_AUTO, False)
                is True,
            }
            if self._pending_options[CONF_USE_CUSTOM_AUTO]:
                return await self.async_step_custom_auto()
            return self._create_pending_entry()

        return self.async_show_form(
            step_id="user", data_schema=_user_schema(discovered_options), errors=errors
        )

    async def async_step_custom_auto(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure integration-managed automatic speed rules."""

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                values = parse_custom_auto_values(user_input)
                validate_custom_auto_values(values)
            except ValueError as err:
                error = str(err)
                errors["base"] = (
                    error
                    if error
                    in {
                        "up_thresholds_not_ascending",
                        "down_thresholds_not_ascending",
                        "down_threshold_above_up",
                    }
                    else "invalid_custom_auto_value"
                )
            else:
                if self._pending_options is None:
                    return self.async_abort(reason="unknown")
                self._pending_options.update(values)
                return self._create_pending_entry()

        return self.async_show_form(
            step_id="custom_auto",
            data_schema=_custom_auto_schema(user_input or CUSTOM_AUTO_DEFAULTS),
            errors=errors,
        )

    def _create_pending_entry(self) -> FlowResult:
        """Create the setup entry after all requested forms are complete."""

        if self._pending_entry is None or self._pending_options is None:
            return self.async_abort(reason="unknown")
        return self.async_create_entry(
            **self._pending_entry, options=self._pending_options
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
        self._pending_options: dict[str, Any] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options."""

        if user_input is not None:
            polling_interval = validate_polling_interval_seconds(
                user_input[CONF_POLLING_INTERVAL]
            )
            use_custom_auto = user_input.get(CONF_USE_CUSTOM_AUTO, False) is True
            self._pending_options = {
                **dict(self._config_entry.options),
                CONF_POLLING_INTERVAL: polling_interval,
                CONF_USE_CUSTOM_AUTO: use_custom_auto,
            }
            if use_custom_auto:
                return await self.async_step_custom_auto()
            return self.async_create_entry(
                title="",
                data=self._pending_options,
            )

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(
                polling_default=polling_interval_from_options(
                    self._config_entry.options
                ),
                custom_auto_default=CustomAutoConfig.from_options(
                    self._config_entry.options
                ).enabled,
            ),
        )

    async def async_step_custom_auto(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure integration-managed automatic speed rules."""

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                values = parse_custom_auto_values(user_input)
                validate_custom_auto_values(values)
            except ValueError as err:
                error = str(err)
                errors["base"] = (
                    error
                    if error
                    in {
                        "up_thresholds_not_ascending",
                        "down_thresholds_not_ascending",
                        "down_threshold_above_up",
                    }
                    else "invalid_custom_auto_value"
                )
            else:
                if self._pending_options is None:
                    self._pending_options = dict(self._config_entry.options)
                    self._pending_options[CONF_USE_CUSTOM_AUTO] = True
                self._pending_options.update(values)
                return self.async_create_entry(title="", data=self._pending_options)

        defaults = CustomAutoConfig.from_options(self._config_entry.options).as_options()
        return self.async_show_form(
            step_id="custom_auto",
            data_schema=_custom_auto_schema(user_input or defaults),
            errors=errors,
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
            ): _polling_interval_schema_value(),
            vol.Required(CONF_USE_CUSTOM_AUTO, default=False): BooleanSelector(),
        }
    )


def _options_schema(
    *,
    polling_default: int = DEFAULT_POLLING_INTERVAL_SECONDS,
    custom_auto_default: bool = False,
) -> vol.Schema:
    """Build the first options form."""

    return vol.Schema(
        {
            vol.Required(
                CONF_POLLING_INTERVAL,
                default=polling_default,
            ): _polling_interval_schema_value(),
            vol.Required(
                CONF_USE_CUSTOM_AUTO, default=custom_auto_default
            ): BooleanSelector(),
        }
    )


def _custom_auto_schema(defaults: Any) -> vol.Schema:
    """Build the custom-auto threshold and delay form."""

    values = {
        key: defaults.get(key, CUSTOM_AUTO_DEFAULTS[key])
        for key in CUSTOM_AUTO_OPTION_KEYS
    }
    pm_selector = NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=999,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="µg/m³",
        )
    )
    delay_selector = NumberSelector(
        NumberSelectorConfig(
            min=0,
            max=1440,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="min",
        )
    )
    return vol.Schema(
        {
            vol.Required(key, default=values[key]): pm_selector
            for key in (
                CONF_CUSTOM_AUTO_UP_40,
                CONF_CUSTOM_AUTO_UP_60,
                CONF_CUSTOM_AUTO_UP_80,
                CONF_CUSTOM_AUTO_UP_100,
                CONF_CUSTOM_AUTO_DOWN_80,
                CONF_CUSTOM_AUTO_DOWN_60,
                CONF_CUSTOM_AUTO_DOWN_40,
                CONF_CUSTOM_AUTO_DOWN_20,
            )
        }
        | {
            vol.Required(key, default=values[key]): delay_selector
            for key in (
                CONF_CUSTOM_AUTO_DELAY_80,
                CONF_CUSTOM_AUTO_DELAY_60,
                CONF_CUSTOM_AUTO_DELAY_40,
                CONF_CUSTOM_AUTO_DELAY_20,
            )
        }
    )


def _select_options(
    discovered_options: tuple[DiscoveredDeviceOption, ...],
) -> dict[str, str]:
    """Build HA select options for discovered purifiers plus manual entry."""

    options = {MANUAL_DEVICE_VALUE: "Enter address manually"}
    options.update({option.value: option.label for option in discovered_options})
    return options


def _polling_interval_schema_value() -> vol.All:
    """Return a serializer-safe polling interval validator for HA forms."""

    return vol.All(
        vol.Coerce(int),
        vol.Range(
            min=MIN_POLLING_INTERVAL_SECONDS,
            max=MAX_POLLING_INTERVAL_SECONDS,
        ),
    )
