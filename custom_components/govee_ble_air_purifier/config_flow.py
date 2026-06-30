"""Config flow for Govee BLE Air Purifier."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_PROFILE, DOMAIN
from .profiles import H7124_PROFILE, match_profile, normalize_ble_address


class GoveeBleAirPurifierConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Govee BLE Air Purifier."""

    VERSION = 1

    _discovered_device: bluetooth.BluetoothServiceInfoBleak | None = None

    async def async_step_bluetooth(
        self, discovery_info: bluetooth.BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle Bluetooth discovery."""

        profile = match_profile(discovery_info.name)
        if profile is None:
            return self.async_abort(reason="not_supported")

        unique_id = _unique_id_from_address(discovery_info.address)
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured(
            updates={CONF_ADDRESS: discovery_info.address}
        )
        self.context["title_placeholders"] = {
            CONF_NAME: discovery_info.name or profile.display_name
        }
        self._discovered_device = discovery_info
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm Bluetooth discovery."""

        assert self._discovered_device is not None
        if user_input is not None:
            profile = match_profile(self._discovered_device.name) or H7124_PROFILE
            name = self._discovered_device.name or profile.display_name
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ADDRESS: self._discovered_device.address,
                    CONF_NAME: name,
                    CONF_PROFILE: profile.key,
                },
            )
        self._set_confirm_only()
        return self.async_show_form(step_id="bluetooth_confirm")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle manual setup by BLE address and optional name."""

        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip()
            name = user_input.get(CONF_NAME) or H7124_PROFILE.display_name
            unique_id = _unique_id_from_address(address)
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured(updates={CONF_ADDRESS: address})
            return self.async_create_entry(
                title=name,
                data={
                    CONF_ADDRESS: address,
                    CONF_NAME: name,
                    CONF_PROFILE: H7124_PROFILE.key,
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): str,
                vol.Optional(CONF_NAME, default=H7124_PROFILE.display_name): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


def _unique_id_from_address(address: str) -> str:
    """Build the config-entry unique id from the BLE address."""

    return normalize_ble_address(address)
