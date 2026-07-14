"""Config flow for Govee BLE Air Purifier."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

try:
    from homeassistant.data_entry_flow import section as data_entry_section
except ImportError:  # Home Assistant before 2024.8
    data_entry_section = None

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
    DEFAULT_POLLING_INTERVAL_SECONDS,
    DOMAIN,
    LEGACY_CONF_USE_CUSTOM_AUTO,
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
from .profiles import (
    H7124_PROFILE,
    canonicalize_ble_address,
    get_profile,
    match_profile,
    normalize_ble_address,
)
from .setup_helpers import (
    MANUAL_DEVICE_VALUE,
    DiscoveredDeviceOption,
    build_discovered_device_options,
    polling_interval_from_options,
    validate_polling_interval_seconds,
)

SECTION_EXCELLENT_GOOD = "excellent_good"
SECTION_GOOD_FAIR = "good_fair"
SECTION_FAIR_BAD = "fair_bad"
SECTION_BAD_POOR = "bad_poor"

CUSTOM_AUTO_SECTIONS = (
    (
        SECTION_EXCELLENT_GOOD,
        CONF_CUSTOM_AUTO_UP_40,
        CONF_CUSTOM_AUTO_DOWN_20,
        CONF_CUSTOM_AUTO_DELAY_20,
    ),
    (
        SECTION_GOOD_FAIR,
        CONF_CUSTOM_AUTO_UP_60,
        CONF_CUSTOM_AUTO_DOWN_40,
        CONF_CUSTOM_AUTO_DELAY_40,
    ),
    (
        SECTION_FAIR_BAD,
        CONF_CUSTOM_AUTO_UP_80,
        CONF_CUSTOM_AUTO_DOWN_60,
        CONF_CUSTOM_AUTO_DELAY_60,
    ),
    (
        SECTION_BAD_POOR,
        CONF_CUSTOM_AUTO_UP_100,
        CONF_CUSTOM_AUTO_DOWN_80,
        CONF_CUSTOM_AUTO_DELAY_80,
    ),
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
                entered_address = user_input.get(CONF_ADDRESS, "").strip()
                if not entered_address:
                    errors[CONF_ADDRESS] = "address_required"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=_user_schema(discovered_options),
                        errors=errors,
                    )
                try:
                    address = canonicalize_ble_address(entered_address)
                except ValueError:
                    errors[CONF_ADDRESS] = "invalid_address"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=_user_schema(discovered_options),
                        errors=errors,
                    )
                service_info = _cached_service_info(self.hass, address)
                if service_info is None:
                    errors[CONF_ADDRESS] = "device_not_found"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=_user_schema(discovered_options),
                        errors=errors,
                    )
                profile = match_profile(getattr(service_info, "name", None))
                if profile is None:
                    errors[CONF_ADDRESS] = "unsupported_device"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=_user_schema(discovered_options),
                        errors=errors,
                    )
                name = user_input.get(CONF_NAME) or profile.display_name
            else:
                option = discovered_by_value.get(selected_device)
                if option is None:
                    errors[CONF_DISCOVERED_DEVICE] = "unsupported_device"
                    return self.async_show_form(
                        step_id="user",
                        data_schema=_user_schema(discovered_options),
                        errors=errors,
                    )
                address = canonicalize_ble_address(option.value)
                profile = get_profile(option.profile_key)
                name = user_input.get(CONF_NAME) or option.name

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
            }
            return await self.async_step_custom_auto()

        return self.async_show_form(
            step_id="user", data_schema=_user_schema(discovered_options), errors=errors
        )

    async def async_step_custom_auto(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Configure integration-managed automatic speed rules."""

        errors: dict[str, str] = {}
        defaults: Mapping[str, Any] = CUSTOM_AUTO_DEFAULTS
        submitted_values: dict[str, int] | None = None
        if user_input is not None:
            try:
                submitted_values = _parse_custom_auto_form(user_input)
                validate_custom_auto_values(submitted_values)
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
                if submitted_values is not None:
                    defaults = submitted_values
            else:
                if self._pending_options is None:
                    return self.async_abort(reason="unknown")
                self._pending_options.update(submitted_values)
                return self._create_pending_entry()

        return self.async_show_form(
            step_id="custom_auto",
            data_schema=_custom_auto_schema(defaults),
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

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage options."""

        errors: dict[str, str] = {}
        defaults = CustomAutoConfig.from_options(
            self._config_entry.options
        ).as_options()
        polling_default = polling_interval_from_options(self._config_entry.options)
        submitted_values: dict[str, int] | None = None
        if user_input is not None:
            polling_default = validate_polling_interval_seconds(
                user_input[CONF_POLLING_INTERVAL]
            )
            try:
                submitted_values = _parse_custom_auto_form(user_input)
                validate_custom_auto_values(submitted_values)
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
                options = {
                    key: value
                    for key, value in self._config_entry.options.items()
                    if key != LEGACY_CONF_USE_CUSTOM_AUTO
                }
                options[CONF_POLLING_INTERVAL] = polling_default
                options.update(submitted_values)
                return self.async_create_entry(title="", data=options)
            if submitted_values is not None:
                defaults = submitted_values

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(
                polling_default=polling_default,
                custom_auto_defaults=defaults,
            ),
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


def _cached_service_info(hass: Any, address: str) -> Any | None:
    """Return cached advertisement evidence for an address, including history."""

    normalized = normalize_ble_address(address)
    for service_info in bluetooth.async_discovered_service_info(
        hass, connectable=True
    ):
        candidate_address = getattr(service_info, "address", "")
        try:
            canonicalize_ble_address(candidate_address)
        except ValueError:
            continue
        if normalize_ble_address(candidate_address) == normalized:
            return service_info
    if async_last_service_info := getattr(bluetooth, "async_last_service_info", None):
        return async_last_service_info(hass, address, connectable=True)
    return None


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
        }
    )


def _options_schema(
    *,
    polling_default: int = DEFAULT_POLLING_INTERVAL_SECONDS,
    custom_auto_defaults: Mapping[str, Any],
) -> vol.Schema:
    """Build the complete options form."""

    return vol.Schema(
        {
            vol.Required(
                CONF_POLLING_INTERVAL,
                default=polling_default,
            ): _polling_interval_schema_value(),
            **_custom_auto_sections(custom_auto_defaults),
        }
    )


def _custom_auto_schema(defaults: Any) -> vol.Schema:
    """Build the custom-auto threshold and delay form."""

    return vol.Schema(_custom_auto_sections(defaults))


def _custom_auto_sections(defaults: Mapping[str, Any]) -> dict[Any, Any]:
    """Build expanded boundary sections for the five air-quality levels."""

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
    if data_entry_section is None:
        fields: dict[Any, Any] = {}
        for _, up_key, down_key, delay_key in CUSTOM_AUTO_SECTIONS:
            fields[vol.Required(up_key, default=values[up_key])] = pm_selector
            fields[vol.Required(down_key, default=values[down_key])] = pm_selector
            fields[vol.Required(delay_key, default=values[delay_key])] = delay_selector
        return fields
    return {
        vol.Required(section_key): data_entry_section(
            vol.Schema(
                {
                    vol.Required(up_key, default=values[up_key]): pm_selector,
                    vol.Required(down_key, default=values[down_key]): pm_selector,
                    vol.Required(delay_key, default=values[delay_key]): delay_selector,
                }
            ),
            {"collapsed": False},
        )
        for section_key, up_key, down_key, delay_key in CUSTOM_AUTO_SECTIONS
    }


def _parse_custom_auto_form(values: Mapping[str, Any]) -> dict[str, int]:
    """Flatten sectioned form input into the existing config-entry option keys."""

    flattened: dict[str, Any] = {}
    has_sections = False
    for section_key, up_key, down_key, delay_key in CUSTOM_AUTO_SECTIONS:
        section_values = values.get(section_key)
        if not isinstance(section_values, Mapping):
            continue
        has_sections = True
        for key in (up_key, down_key, delay_key):
            if key in section_values:
                flattened[key] = section_values[key]
    return parse_custom_auto_values(flattened if has_sections else values)


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
