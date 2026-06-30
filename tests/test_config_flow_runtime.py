import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest


MODULE_NAME = "custom_components.govee_ble_air_purifier.config_flow"


class _ConfigFlow:
    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__()

    def async_show_form(self, **kwargs: object) -> dict[str, object]:
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs: object) -> dict[str, object]:
        return {"type": "create_entry", **kwargs}

    async def async_set_unique_id(self, unique_id: str) -> None:
        self._unique_id = unique_id

    def _abort_if_unique_id_configured(self, **kwargs: object) -> None:
        return None


class _OptionsFlow:
    def async_show_form(self, **kwargs: object) -> dict[str, object]:
        return {"type": "form", **kwargs}

    def async_create_entry(self, **kwargs: object) -> dict[str, object]:
        return {"type": "create_entry", **kwargs}


class _VoluptuousMarker:
    def __init__(self, key: str, default: object | None = None) -> None:
        self.key = key
        self.default = default

    def __hash__(self) -> int:
        return hash((self.key, self.default))

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _VoluptuousMarker)
            and self.key == other.key
            and self.default == other.default
        )


class _VoluptuousSchema:
    def __init__(self, schema: object) -> None:
        self.schema = schema

    def __call__(self, value: object) -> object:
        return value


class _VoluptuousInvalid(Exception):
    pass


class _VoluptuousIn:
    def __init__(self, container: object) -> None:
        self.container = container

    def __call__(self, value: object) -> object:
        return value


def _install_homeassistant_modules(
    monkeypatch: pytest.MonkeyPatch, bluetooth_module: ModuleType
) -> None:
    homeassistant_module = ModuleType("homeassistant")
    components_module = ModuleType("homeassistant.components")
    config_entries_module = ModuleType("homeassistant.config_entries")
    const_module = ModuleType("homeassistant.const")
    data_entry_flow_module = ModuleType("homeassistant.data_entry_flow")
    voluptuous_module = ModuleType("voluptuous")

    config_entries_module.ConfigFlow = _ConfigFlow
    config_entries_module.OptionsFlow = _OptionsFlow
    config_entries_module.ConfigEntry = object
    const_module.CONF_ADDRESS = "address"
    const_module.CONF_NAME = "name"
    data_entry_flow_module.FlowResult = dict
    voluptuous_module.Invalid = _VoluptuousInvalid
    voluptuous_module.In = _VoluptuousIn
    voluptuous_module.Optional = lambda key, default=None: _VoluptuousMarker(
        key, default
    )
    voluptuous_module.Required = lambda key, default=None: _VoluptuousMarker(
        key, default
    )
    voluptuous_module.Schema = _VoluptuousSchema

    homeassistant_module.config_entries = config_entries_module
    homeassistant_module.components = components_module
    components_module.bluetooth = bluetooth_module

    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant_module)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components_module)
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.bluetooth", bluetooth_module
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.config_entries", config_entries_module
    )
    monkeypatch.setitem(sys.modules, "homeassistant.const", const_module)
    monkeypatch.setitem(
        sys.modules, "homeassistant.data_entry_flow", data_entry_flow_module
    )
    monkeypatch.setitem(sys.modules, "voluptuous", voluptuous_module)


def _import_config_flow(monkeypatch: pytest.MonkeyPatch, bluetooth_module: ModuleType):
    _install_homeassistant_modules(monkeypatch, bluetooth_module)
    sys.modules.pop(MODULE_NAME, None)
    return importlib.import_module(MODULE_NAME)


@pytest.mark.asyncio
async def test_user_step_renders_when_active_scan_api_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)

    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()
    result = await flow.async_step_user()

    assert result["type"] == "form"
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_user_step_renders_without_selector_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: (
        SimpleNamespace(name="GVH7124", address="AA:BB:CC:DD:EE:01", rssi=-50),
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)

    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()
    result = await flow.async_step_user()

    assert result["type"] == "form"
    assert result["step_id"] == "user"
