import importlib
import sys
from enum import Enum
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

    def async_abort(self, **kwargs: object) -> dict[str, object]:
        return {"type": "abort", **kwargs}


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


class _VoluptuousAll:
    def __init__(self, *validators: object) -> None:
        self.validators = validators

    def __call__(self, value: object) -> object:
        return value


class _VoluptuousCoerce:
    def __init__(self, value_type: type) -> None:
        self.type = value_type

    def __call__(self, value: object) -> object:
        return value


class _VoluptuousRange:
    def __init__(self, *, min: int, max: int) -> None:
        self.min = min
        self.max = max

    def __call__(self, value: object) -> object:
        return value


class _BooleanSelector:
    def __call__(self, value: object) -> object:
        return value


class _NumberSelectorMode(Enum):
    BOX = "box"


class _NumberSelectorConfig:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class _NumberSelector:
    def __init__(self, config: _NumberSelectorConfig) -> None:
        self.config = config

    def __call__(self, value: object) -> object:
        return value


class _Section:
    def __init__(self, schema: _VoluptuousSchema, options: dict[str, object]) -> None:
        self.schema = schema
        self.options = options


def _assert_schema_values_are_serializable(schema: object) -> None:
    for value in schema.schema.values():
        if isinstance(value, _Section):
            _assert_schema_values_are_serializable(value.schema)
            continue
        if isinstance(
            value,
            (_VoluptuousAll, _VoluptuousIn, _BooleanSelector, _NumberSelector),
        ):
            continue
        assert not (
            callable(value) and not isinstance(value, type)
        ), f"custom callable schema value is not serializable: {value}"


def _install_homeassistant_modules(
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_module: ModuleType,
    *,
    sections: bool = True,
) -> None:
    homeassistant_module = ModuleType("homeassistant")
    components_module = ModuleType("homeassistant.components")
    config_entries_module = ModuleType("homeassistant.config_entries")
    const_module = ModuleType("homeassistant.const")
    data_entry_flow_module = ModuleType("homeassistant.data_entry_flow")
    helpers_module = ModuleType("homeassistant.helpers")
    selector_module = ModuleType("homeassistant.helpers.selector")
    voluptuous_module = ModuleType("voluptuous")

    config_entries_module.ConfigFlow = _ConfigFlow
    config_entries_module.OptionsFlow = _OptionsFlow
    config_entries_module.ConfigEntry = object
    const_module.CONF_ADDRESS = "address"
    const_module.CONF_NAME = "name"
    data_entry_flow_module.FlowResult = dict
    if sections:
        data_entry_flow_module.section = lambda schema, options: _Section(
            schema, options
        )
    voluptuous_module.All = _VoluptuousAll
    voluptuous_module.Coerce = _VoluptuousCoerce
    voluptuous_module.Invalid = _VoluptuousInvalid
    voluptuous_module.In = _VoluptuousIn
    voluptuous_module.Optional = lambda key, default=None: _VoluptuousMarker(
        key, default
    )
    voluptuous_module.Required = lambda key, default=None: _VoluptuousMarker(
        key, default
    )
    voluptuous_module.Range = _VoluptuousRange
    voluptuous_module.Schema = _VoluptuousSchema
    selector_module.BooleanSelector = _BooleanSelector
    selector_module.NumberSelector = _NumberSelector
    selector_module.NumberSelectorConfig = _NumberSelectorConfig
    selector_module.NumberSelectorMode = _NumberSelectorMode

    homeassistant_module.config_entries = config_entries_module
    homeassistant_module.components = components_module
    homeassistant_module.helpers = helpers_module
    components_module.bluetooth = bluetooth_module
    helpers_module.selector = selector_module

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
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers_module)
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.selector", selector_module
    )
    monkeypatch.setitem(sys.modules, "voluptuous", voluptuous_module)


def _import_config_flow(
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_module: ModuleType,
    *,
    sections: bool = True,
):
    _install_homeassistant_modules(monkeypatch, bluetooth_module, sections=sections)
    sys.modules.pop(MODULE_NAME, None)
    return importlib.import_module(MODULE_NAME)


def _schema_by_key(schema: _VoluptuousSchema) -> dict[str, tuple[object, object]]:
    return {
        marker.key: (marker, value) for marker, value in schema.schema.items()
    }


def _sectioned_values(config_flow, values: dict[str, int]) -> dict[str, object]:
    return {
        section_key: {
            up_key: values[up_key],
            down_key: values[down_key],
            delay_key: values[delay_key],
        }
        for section_key, up_key, down_key, delay_key in config_flow.CUSTOM_AUTO_SECTIONS
    }


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


@pytest.mark.asyncio
async def test_config_flow_schemas_do_not_expose_custom_callable_validators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)

    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()
    user_result = await flow.async_step_user()
    _assert_schema_values_are_serializable(user_result["data_schema"])

    options_flow = config_flow.GoveeBleAirPurifierOptionsFlow(
        SimpleNamespace(options={})
    )
    options_result = await options_flow.async_step_init()
    _assert_schema_values_are_serializable(options_result["data_schema"])


@pytest.mark.asyncio
async def test_setup_and_options_do_not_expose_custom_auto_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)

    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()
    setup = await flow.async_step_user()
    options = await config_flow.GoveeBleAirPurifierOptionsFlow(
        SimpleNamespace(options={})
    ).async_step_init()

    assert "use_custom_auto" not in _schema_by_key(setup["data_schema"])
    assert "use_custom_auto" not in _schema_by_key(options["data_schema"])


@pytest.mark.asyncio
async def test_custom_auto_form_uses_bounded_box_number_selectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    result = await flow.async_step_user(
        {
            "discovered_device": "__manual__",
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Bedroom",
            "polling_interval": 15,
        }
    )
    fields = _schema_by_key(result["data_schema"])

    assert result["step_id"] == "custom_auto"
    assert list(fields) == [
        "excellent_good",
        "good_fair",
        "fair_bad",
        "bad_poor",
    ]
    for _, section_value in fields.values():
        assert isinstance(section_value, _Section)
        assert section_value.options == {"collapsed": False}
        section_fields = _schema_by_key(section_value.schema)
        assert len(section_fields) == 3
        for key, (_, selector) in section_fields.items():
            assert isinstance(selector, _NumberSelector)
            assert selector.config.mode is _NumberSelectorMode.BOX
            assert selector.config.min == 0
            assert selector.config.max == (1440 if "delay" in key else 999)
            assert selector.config.step == 1


@pytest.mark.asyncio
async def test_setup_stores_defaults_and_reports_cross_field_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()
    await flow.async_step_user(
        {
            "discovered_device": "__manual__",
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Bedroom",
            "polling_interval": 15,
        }
    )
    invalid_flat = {
        **config_flow.CUSTOM_AUTO_DEFAULTS,
        "custom_auto_up_60": 3,
    }
    invalid = _sectioned_values(config_flow, invalid_flat)

    error_result = await flow.async_step_custom_auto(invalid)
    assert error_result["errors"] == {"base": "up_thresholds_not_ascending"}

    result = await flow.async_step_custom_auto(
        _sectioned_values(config_flow, config_flow.CUSTOM_AUTO_DEFAULTS)
    )
    assert result["type"] == "create_entry"
    assert result["options"] == {
        "polling_interval": 15,
        **config_flow.CUSTOM_AUTO_DEFAULTS,
    }


@pytest.mark.asyncio
async def test_options_always_edit_rules_and_remove_legacy_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    existing = {
        "polling_interval": 30,
        "use_custom_auto": True,
        **config_flow.CUSTOM_AUTO_DEFAULTS,
    }
    options_flow = config_flow.GoveeBleAirPurifierOptionsFlow(
        SimpleNamespace(options=existing)
    )

    changed = {
        **config_flow.CUSTOM_AUTO_DEFAULTS,
        "custom_auto_delay_20": 12,
    }
    saved = await options_flow.async_step_init(
        {
            "polling_interval": 60,
            **_sectioned_values(config_flow, changed),
        }
    )
    assert saved["data"]["custom_auto_delay_20"] == 12
    assert saved["data"]["polling_interval"] == 60
    assert "use_custom_auto" not in saved["data"]


@pytest.mark.asyncio
async def test_options_show_all_custom_auto_sections_on_initial_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    options_flow = config_flow.GoveeBleAirPurifierOptionsFlow(
        SimpleNamespace(options={})
    )

    form = await options_flow.async_step_init()
    fields = _schema_by_key(form["data_schema"])

    assert form["step_id"] == "init"
    assert list(fields) == [
        "polling_interval",
        "excellent_good",
        "good_fair",
        "fair_bad",
        "bad_poor",
    ]
    displayed_defaults = {}
    for section_key in list(fields)[1:]:
        section_value = fields[section_key][1]
        displayed_defaults.update(
            {
                key: marker.default
                for key, (marker, _) in _schema_by_key(section_value.schema).items()
            }
        )
    assert displayed_defaults == config_flow.CUSTOM_AUTO_DEFAULTS


@pytest.mark.asyncio
async def test_options_report_boundary_errors_without_leaving_initial_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    options_flow = config_flow.GoveeBleAirPurifierOptionsFlow(
        SimpleNamespace(options={})
    )
    invalid = {
        **config_flow.CUSTOM_AUTO_DEFAULTS,
        "custom_auto_up_60": 3,
    }

    result = await options_flow.async_step_init(
        {
            "polling_interval": 15,
            **_sectioned_values(config_flow, invalid),
        }
    )

    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "up_thresholds_not_ascending"}


@pytest.mark.asyncio
async def test_options_show_flat_expanded_fields_before_section_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(
        monkeypatch, bluetooth_module, sections=False
    )
    options_flow = config_flow.GoveeBleAirPurifierOptionsFlow(
        SimpleNamespace(options={})
    )

    form = await options_flow.async_step_init()
    fields = _schema_by_key(form["data_schema"])

    assert form["step_id"] == "init"
    assert list(fields)[0] == "polling_interval"
    assert len(fields) == 13
    assert "custom_auto_up_40" in fields
    assert "custom_auto_delay_80" in fields
