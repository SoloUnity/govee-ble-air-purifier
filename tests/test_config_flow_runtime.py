import importlib
import sys
from dataclasses import replace
from enum import Enum
from types import ModuleType, SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier.const import (
    CONF_CUSTOM_AUTO_CONFIRMATION_DELAY,
    CONF_SHARE_BLUETOOTH_CONNECTION,
)
from custom_components.govee_ble_air_purifier.custom_auto.config import (
    CUSTOM_AUTO_DEFAULTS,
    MAX_UPSHIFT_CONFIRMATION_DELAY_SECONDS,
)
from custom_components.govee_ble_air_purifier.profiles import (
    get_profile,
    ModelSupportStatus,
)
from tests.helpers.ha_stubs import install_modules


MODULE_NAME = "custom_components.govee_ble_air_purifier.config_flow"


class _ConfigFlow:
    def __init__(self) -> None:
        self.context: dict[str, object] = {}

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

    def _async_current_ids(self, include_ignore: bool = True) -> set[str | None]:
        return set()

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
        assert not (callable(value) and not isinstance(value, type)), (
            f"custom callable schema value is not serializable: {value}"
        )


def _install_homeassistant_modules(
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_module: ModuleType,
) -> None:
    bluetooth_module.BluetoothServiceInfoBleak = getattr(
        bluetooth_module, "BluetoothServiceInfoBleak", object
    )
    modules = install_modules(
        monkeypatch,
        {
            "homeassistant.components": {},
            "homeassistant.config_entries": {
                "ConfigFlow": _ConfigFlow,
                "OptionsFlow": _OptionsFlow,
                "ConfigEntry": object,
            },
            "homeassistant.const": {"CONF_ADDRESS": "address", "CONF_NAME": "name"},
            "homeassistant.data_entry_flow": {
                "FlowResult": dict,
                "section": lambda schema, options: _Section(schema, options),
            },
            "homeassistant.helpers.selector": {
                "BooleanSelector": _BooleanSelector,
                "NumberSelector": _NumberSelector,
                "NumberSelectorConfig": _NumberSelectorConfig,
                "NumberSelectorMode": _NumberSelectorMode,
            },
            "voluptuous": {
                "All": _VoluptuousAll,
                "Coerce": _VoluptuousCoerce,
                "Invalid": _VoluptuousInvalid,
                "In": _VoluptuousIn,
                "Optional": lambda key, default=None: _VoluptuousMarker(key, default),
                "Required": lambda key, default=None: _VoluptuousMarker(key, default),
                "Range": _VoluptuousRange,
                "Schema": _VoluptuousSchema,
            },
        },
    )
    monkeypatch.setitem(
        sys.modules, "homeassistant.components.bluetooth", bluetooth_module
    )
    modules["homeassistant.components"].bluetooth = bluetooth_module


def _import_config_flow(
    monkeypatch: pytest.MonkeyPatch,
    bluetooth_module: ModuleType,
):
    _install_homeassistant_modules(monkeypatch, bluetooth_module)
    sys.modules.pop(MODULE_NAME, None)
    module = importlib.import_module(MODULE_NAME)

    async def successful_probe(*args: object, **kwargs: object) -> None:
        return None

    module.async_probe_device = successful_probe
    return module


def _schema_by_key(schema: _VoluptuousSchema) -> dict[str, tuple[object, object]]:
    return {marker.key: (marker, value) for marker, value in schema.schema.items()}


def _sectioned_values(config_flow, values: dict[str, int]) -> dict[str, object]:
    return {
        CONF_CUSTOM_AUTO_CONFIRMATION_DELAY: values[
            CONF_CUSTOM_AUTO_CONFIRMATION_DELAY
        ],
        **{
            section_key: {
                threshold_key: values[threshold_key],
                delay_key: values[delay_key],
            }
            for section_key, threshold_key, delay_key in config_flow.CUSTOM_AUTO_SECTIONS
        },
    }


def test_config_flow_version_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)

    assert config_flow.GoveeBleAirPurifierConfigFlow.VERSION == 1


@pytest.mark.asyncio
async def test_bluetooth_discovery_requires_confirmation_before_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()
    discovery = SimpleNamespace(
        address="AA:BB:CC:DD:EE:24",
        name="GVH7124BEDROOM",
        connectable=True,
    )

    result = await flow.async_step_bluetooth(discovery)

    assert result["type"] == "form"
    assert result["step_id"] == "bluetooth_confirm"
    assert result["description_placeholders"] == {"name": "GVH7124BEDROOM"}
    assert flow._unique_id == "aabbccddee24"
    assert flow._pending_entry["data"] == {
        "address": "AA:BB:CC:DD:EE:24",
        "name": "GVH7124BEDROOM",
        "profile": "h7124",
    }

    result = await flow.async_step_bluetooth_confirm({})

    assert result["type"] == "form"
    assert result["step_id"] == "polling"


@pytest.mark.asyncio
async def test_bluetooth_discovery_preserves_model_support_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    await flow.async_step_bluetooth(
        SimpleNamespace(
            address="AA:BB:CC:DD:EE:29",
            name="ihoment_H7129_6A7D",
            connectable=True,
        )
    )
    result = await flow.async_step_bluetooth_confirm({})

    assert result["step_id"] == "support_read_verified"
    assert result["description_placeholders"] == {"model": "H7129"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("discovery", "reason"),
    [
        (
            SimpleNamespace(
                address="AA:BB:CC:DD:EE:01",
                name="GVH7119OTHER",
                connectable=True,
            ),
            "not_supported",
        ),
        (
            SimpleNamespace(
                address="AA:BB:CC:DD:EE:02",
                name="GVH7124BEDROOM",
                connectable=False,
            ),
            "not_connectable",
        ),
        (
            SimpleNamespace(
                address="not-an-address",
                name="GVH7124BEDROOM",
                connectable=True,
            ),
            "not_supported",
        ),
    ],
)
async def test_bluetooth_discovery_rejects_unusable_devices(
    monkeypatch: pytest.MonkeyPatch,
    discovery: SimpleNamespace,
    reason: str,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    result = await flow.async_step_bluetooth(discovery)

    assert result == {"type": "abort", "reason": reason}


@pytest.mark.asyncio
async def test_bluetooth_discovery_runs_unique_id_duplicate_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()
    duplicate_checks: list[dict[str, object]] = []
    flow._abort_if_unique_id_configured = lambda **kwargs: duplicate_checks.append(
        kwargs
    )

    await flow.async_step_bluetooth(
        SimpleNamespace(
            address="AA:BB:CC:DD:EE:24",
            name="GVH7124BEDROOM",
            connectable=True,
        )
    )

    assert flow._unique_id == "aabbccddee24"
    assert duplicate_checks == [
        {"updates": {"address": "AA:BB:CC:DD:EE:24"}}
    ]


@pytest.mark.asyncio
async def test_entry_creation_waits_for_successful_read_only_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = "AA:BB:CC:DD:EE:24"
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: (
        SimpleNamespace(name="GVH7124BEDROOM", address=address, rssi=-45),
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    probes: list[tuple[object, str, str]] = []

    async def record_probe(hass: object, target: str, profile: object) -> None:
        probes.append((hass, target, profile.key))

    monkeypatch.setattr(config_flow, "async_probe_device", record_probe)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    await flow.async_step_user({"discovered_device": address})
    await flow.async_step_polling({"polling_interval": 15})
    result = await flow.async_step_custom_auto(
        _sectioned_values(config_flow, dict(CUSTOM_AUTO_DEFAULTS))
    )

    assert result["type"] == "create_entry"
    assert probes == [(flow.hass, address, "h7124")]


@pytest.mark.asyncio
async def test_failed_probe_shows_translated_retry_and_never_creates_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = "AA:BB:CC:DD:EE:24"
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: (
        SimpleNamespace(name="GVH7124BEDROOM", address=address, rssi=-45),
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    attempts = 0

    async def fail_then_succeed(*args: object, **kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise config_flow.SetupProbeError("cannot_connect")

    monkeypatch.setattr(config_flow, "async_probe_device", fail_then_succeed)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()
    await flow.async_step_user({"discovered_device": address})
    await flow.async_step_polling({"polling_interval": 15})

    failure = await flow.async_step_custom_auto(
        _sectioned_values(config_flow, dict(CUSTOM_AUTO_DEFAULTS))
    )

    assert failure["type"] == "form"
    assert failure["step_id"] == "probe"
    assert failure["errors"] == {"base": "cannot_connect"}

    success = await flow.async_step_probe({})
    assert success["type"] == "create_entry"
    assert attempts == 2


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
async def test_user_step_orders_manual_address_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    result = await flow.async_step_user()

    assert list(_schema_by_key(result["data_schema"])) == [
        "discovered_device",
        "name",
        "address",
    ]


@pytest.mark.asyncio
async def test_user_step_hides_configured_devices_but_preserves_duplicate_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_address = "AA:BB:CC:DD:EE:01"
    available_address = "AA:BB:CC:DD:EE:02"
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: (
        SimpleNamespace(name="GVH7124CONFIGURED", address=configured_address, rssi=-40),
        SimpleNamespace(name="GVH7124AVAILABLE", address=available_address, rssi=-50),
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()
    flow._async_current_ids = lambda include_ignore=True: {"aabbccddee01"}

    result = await flow.async_step_user()
    fields = _schema_by_key(result["data_schema"])
    marker, selector = fields["discovered_device"]

    assert marker.default == available_address
    assert configured_address not in selector.container
    assert available_address in selector.container

    duplicate_checks: list[dict[str, object]] = []
    flow._abort_if_unique_id_configured = lambda **kwargs: duplicate_checks.append(
        kwargs
    )
    submitted = await flow.async_step_user(
        {
            "discovered_device": configured_address,
        }
    )

    assert submitted["step_id"] == "polling"
    submitted = await flow.async_step_polling({"polling_interval": 15})
    assert submitted["step_id"] == "custom_auto"
    assert duplicate_checks == [{"updates": {"address": configured_address}}]


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
    _assert_schema_values_are_serializable(config_flow._polling_schema(10))

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
    bluetooth_module.async_last_service_info = lambda *args, **kwargs: SimpleNamespace(
        name="GVH7124BEDROOM", address="AA:BB:CC:DD:EE:FF"
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    result = await flow.async_step_user(
        {
            "discovered_device": "__manual__",
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Bedroom",
        }
    )
    polling_fields = _schema_by_key(result["data_schema"])

    assert result["step_id"] == "polling"
    assert polling_fields["polling_interval"][0].default == 10
    assert polling_fields[CONF_SHARE_BLUETOOTH_CONNECTION][0].default is False
    assert isinstance(
        polling_fields[CONF_SHARE_BLUETOOTH_CONNECTION][1], _BooleanSelector
    )

    result = await flow.async_step_polling({"polling_interval": 15})
    fields = _schema_by_key(result["data_schema"])

    assert result["step_id"] == "custom_auto"
    assert list(fields) == [
        CONF_CUSTOM_AUTO_CONFIRMATION_DELAY,
        "excellent_good",
        "good_fair",
        "fair_bad",
        "bad_poor",
    ]
    _, confirmation_selector = fields[CONF_CUSTOM_AUTO_CONFIRMATION_DELAY]
    assert isinstance(confirmation_selector, _NumberSelector)
    assert confirmation_selector.config.mode is _NumberSelectorMode.BOX
    assert confirmation_selector.config.min == 0
    assert confirmation_selector.config.max == MAX_UPSHIFT_CONFIRMATION_DELAY_SECONDS
    assert confirmation_selector.config.step == 1
    assert confirmation_selector.config.unit_of_measurement == "s"
    for key in list(fields)[1:]:
        section_value = fields[key][1]
        assert isinstance(section_value, _Section)
        assert section_value.options == {"collapsed": False}
        section_fields = _schema_by_key(section_value.schema)
        assert len(section_fields) == 2
        for key, (_, selector) in section_fields.items():
            assert isinstance(selector, _NumberSelector)
            assert selector.config.mode is _NumberSelectorMode.BOX
            assert selector.config.min == 0
            assert selector.config.max == (1440 if "delay" in key else 999)
            assert selector.config.step == 1


@pytest.mark.asyncio
async def test_setup_stores_custom_confirmation_delay_and_reports_cross_field_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    bluetooth_module.async_last_service_info = lambda *args, **kwargs: SimpleNamespace(
        name="GVH7124BEDROOM", address="AA:BB:CC:DD:EE:FF"
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()
    await flow.async_step_user(
        {
            "discovered_device": "__manual__",
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Bedroom",
        }
    )
    await flow.async_step_polling({"polling_interval": 15})
    invalid_flat = {
        **CUSTOM_AUTO_DEFAULTS,
        "custom_auto_threshold_60": 3,
    }
    invalid = _sectioned_values(config_flow, invalid_flat)

    error_result = await flow.async_step_custom_auto(invalid)
    assert error_result["errors"] == {"base": "thresholds_not_ascending"}

    submitted = {
        **CUSTOM_AUTO_DEFAULTS,
        CONF_CUSTOM_AUTO_CONFIRMATION_DELAY: 7,
    }
    result = await flow.async_step_custom_auto(
        _sectioned_values(config_flow, submitted)
    )
    assert result["type"] == "create_entry"
    assert result["options"] == {
        "polling_interval": 15,
        CONF_SHARE_BLUETOOTH_CONNECTION: False,
        **submitted,
    }


@pytest.mark.asyncio
async def test_discovered_family_model_persists_exact_profile_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = "AA:BB:CC:DD:EE:09"
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: (
        SimpleNamespace(name="ihoment_H7129_6A7D", address=address, rssi=-45),
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    result = await flow.async_step_user(
        {
            "discovered_device": address,
        }
    )

    assert result["step_id"] == "support_read_verified"
    assert result["description_placeholders"] == {"model": "H7129"}
    result = await flow.async_step_support_read_verified({})
    assert result["step_id"] == "polling"
    fields = _schema_by_key(result["data_schema"])
    assert fields["polling_interval"][0].default == 3
    result = await flow.async_step_polling({"polling_interval": 15})
    assert result["step_id"] == "custom_auto"
    assert flow._pending_entry == {
        "title": "ihoment_H7129_6A7D",
        "data": {
            "address": address,
            "name": "ihoment_H7129_6A7D",
            "profile": "h7129",
        },
    }
    fields = _schema_by_key(result["data_schema"])
    displayed_thresholds = []
    for section_key in list(fields)[1:]:
        section_fields = _schema_by_key(fields[section_key][1].schema)
        threshold_key = next(key for key in section_fields if "threshold" in key)
        displayed_thresholds.append(section_fields[threshold_key][0].default)
    assert displayed_thresholds == [7, 9, 13, 19]


@pytest.mark.asyncio
async def test_manual_family_model_uses_model_specific_default_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = "AA:BB:CC:DD:EE:19"
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    bluetooth_module.async_last_service_info = lambda *args, **kwargs: SimpleNamespace(
        name="ihoment_H7129_6B51", address=address
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    result = await flow.async_step_user(
        {
            "discovered_device": "__manual__",
            "address": address,
        }
    )

    assert result["step_id"] == "support_read_verified"
    assert result["description_placeholders"] == {"model": "H7129"}
    result = await flow.async_step_support_read_verified({})
    assert result["step_id"] == "polling"
    assert _schema_by_key(result["data_schema"])["polling_interval"][0].default == 3
    result = await flow.async_step_polling({"polling_interval": 15})
    assert result["step_id"] == "custom_auto"
    assert flow._pending_entry["title"] == "Govee H7129 Air Purifier"
    assert flow._pending_entry["data"]["profile"] == "h7129"


@pytest.mark.asyncio
async def test_profile_without_custom_auto_modes_skips_policy_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = "AA:BB:CC:DD:EE:06"
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: (
        SimpleNamespace(name="GVH7126LIVING", address=address, rssi=-45),
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    profile = replace(
        get_profile("h7126"),
        fan_mode_commands={"Low": get_profile("h7126").fan_mode_commands["Low"]},
    )
    monkeypatch.setattr(config_flow, "get_profile", lambda key: profile)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    result = await flow.async_step_user(
        {
            "discovered_device": address,
        }
    )

    assert result["step_id"] == "support_fallback"
    assert result["description_placeholders"] == {"model": "H7126"}
    result = await flow.async_step_support_fallback({})
    assert result["step_id"] == "polling"
    result = await flow.async_step_polling(
        {
            "polling_interval": 15,
            CONF_SHARE_BLUETOOTH_CONNECTION: True,
        }
    )
    assert result["type"] == "create_entry"
    assert result["data"]["profile"] == "h7126"
    assert result["options"] == {
        "polling_interval": 15,
        CONF_SHARE_BLUETOOTH_CONNECTION: True,
    }


@pytest.mark.asyncio
async def test_experimental_profile_requires_translated_support_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = "AA:BB:CC:DD:EE:06"
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: (
        SimpleNamespace(name="GVH7126LIVING", address=address, rssi=-45),
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    experimental = replace(
        get_profile("h7126"), support_status=ModelSupportStatus.EXPERIMENTAL
    )
    monkeypatch.setattr(config_flow, "get_profile", lambda key: experimental)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    result = await flow.async_step_user({"discovered_device": address})

    assert result["step_id"] == "support_experimental"
    assert result["description_placeholders"] == {"model": "H7126"}
    result = await flow.async_step_support_experimental({})
    assert result["step_id"] == "polling"


@pytest.mark.asyncio
async def test_options_hide_custom_auto_for_profile_without_required_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    profile = replace(
        get_profile("h7126"),
        fan_mode_commands={"Low": get_profile("h7126").fan_mode_commands["Low"]},
    )
    monkeypatch.setattr(config_flow, "get_profile", lambda key: profile)
    options_flow = config_flow.GoveeBleAirPurifierOptionsFlow(
        SimpleNamespace(data={"profile": "h7126"}, options={})
    )

    result = await options_flow.async_step_init()

    assert list(_schema_by_key(result["data_schema"])) == [
        "polling_interval",
        CONF_SHARE_BLUETOOTH_CONNECTION,
    ]
    saved = await options_flow.async_step_init({"polling_interval": 30})
    assert saved["data"] == {
        "polling_interval": 30,
        CONF_SHARE_BLUETOOTH_CONNECTION: False,
    }


@pytest.mark.asyncio
async def test_manual_setup_rejects_malformed_address_before_unique_id_update(
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
            "address": "AA:BB:CC:DD:EE:FFG",
        }
    )

    assert result["errors"] == {"address": "invalid_address"}
    assert not hasattr(flow, "_unique_id")


@pytest.mark.asyncio
async def test_manual_setup_requires_cached_profile_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    bluetooth_module.async_last_service_info = lambda *args, **kwargs: None
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    result = await flow.async_step_user(
        {
            "discovered_device": "__manual__",
            "address": "AA:BB:CC:DD:EE:FF",
        }
    )

    assert result["errors"] == {"address": "device_not_found"}
    assert not hasattr(flow, "_unique_id")


@pytest.mark.asyncio
async def test_manual_setup_rejects_cached_unsupported_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    bluetooth_module.async_last_service_info = lambda *args, **kwargs: SimpleNamespace(
        name="Other device", address="AA:BB:CC:DD:EE:FF"
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    result = await flow.async_step_user(
        {
            "discovered_device": "__manual__",
            "address": "AA:BB:CC:DD:EE:FF",
        }
    )

    assert result["errors"] == {"address": "unsupported_device"}


@pytest.mark.asyncio
async def test_manual_setup_accepts_historical_h7124_with_uuid_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    address = "A1B2C3D4-E5F6-47A8-9012-123456789ABC"
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    bluetooth_module.async_last_service_info = lambda *args, **kwargs: SimpleNamespace(
        name="GVH7124ABCD", address=address
    )
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    flow = config_flow.GoveeBleAirPurifierConfigFlow()
    flow.hass = object()

    result = await flow.async_step_user(
        {
            "discovered_device": "__manual__",
            "address": address,
            "name": "Bedroom",
        }
    )

    assert result["step_id"] == "polling"
    assert flow._unique_id == "a1b2c3d4e5f647a89012123456789abc"
    assert flow._pending_entry["data"]["address"] == address


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
        **CUSTOM_AUTO_DEFAULTS,
    }
    options_flow = config_flow.GoveeBleAirPurifierOptionsFlow(
        SimpleNamespace(options=existing)
    )

    changed = {
        **CUSTOM_AUTO_DEFAULTS,
        CONF_CUSTOM_AUTO_CONFIRMATION_DELAY: 0,
        "custom_auto_delay_20": 12,
    }
    saved = await options_flow.async_step_init(
        {
            "polling_interval": 60,
            **_sectioned_values(config_flow, changed),
        }
    )
    assert saved["data"][CONF_CUSTOM_AUTO_CONFIRMATION_DELAY] == 0
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
        CONF_SHARE_BLUETOOTH_CONNECTION,
        CONF_CUSTOM_AUTO_CONFIRMATION_DELAY,
        "excellent_good",
        "good_fair",
        "fair_bad",
        "bad_poor",
    ]
    displayed_defaults = {
        CONF_CUSTOM_AUTO_CONFIRMATION_DELAY: fields[
            CONF_CUSTOM_AUTO_CONFIRMATION_DELAY
        ][0].default
    }
    for section_key in list(fields)[3:]:
        section_value = fields[section_key][1]
        displayed_defaults.update(
            {
                key: marker.default
                for key, (marker, _) in _schema_by_key(section_value.schema).items()
            }
        )
    assert displayed_defaults == CUSTOM_AUTO_DEFAULTS
    assert fields[CONF_SHARE_BLUETOOTH_CONNECTION][0].default is False


@pytest.mark.asyncio
async def test_options_enable_and_preserve_shared_connection_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    options_flow = config_flow.GoveeBleAirPurifierOptionsFlow(
        SimpleNamespace(
            options={
                "polling_interval": 10,
                CONF_SHARE_BLUETOOTH_CONNECTION: True,
            }
        )
    )

    form = await options_flow.async_step_init()
    fields = _schema_by_key(form["data_schema"])
    assert fields[CONF_SHARE_BLUETOOTH_CONNECTION][0].default is True

    saved = await options_flow.async_step_init(
        {
            "polling_interval": 10,
            CONF_SHARE_BLUETOOTH_CONNECTION: True,
            **_sectioned_values(config_flow, CUSTOM_AUTO_DEFAULTS),
        }
    )
    assert saved["data"][CONF_SHARE_BLUETOOTH_CONNECTION] is True


@pytest.mark.asyncio
async def test_h7129_options_use_profile_threshold_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_discovered_service_info = lambda *args, **kwargs: ()
    config_flow = _import_config_flow(monkeypatch, bluetooth_module)
    options_flow = config_flow.GoveeBleAirPurifierOptionsFlow(
        SimpleNamespace(data={"profile": "h7129"}, options={})
    )

    form = await options_flow.async_step_init()
    fields = _schema_by_key(form["data_schema"])
    displayed_thresholds = []
    for section_key in list(fields)[3:]:
        section_fields = _schema_by_key(fields[section_key][1].schema)
        threshold_key = next(key for key in section_fields if "threshold" in key)
        displayed_thresholds.append(section_fields[threshold_key][0].default)

    assert displayed_thresholds == [7, 9, 13, 19]
    assert fields["polling_interval"][0].default == 3


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
        **CUSTOM_AUTO_DEFAULTS,
        "custom_auto_threshold_60": 3,
    }

    result = await options_flow.async_step_init(
        {
            "polling_interval": 15,
            **_sectioned_values(config_flow, invalid),
        }
    )

    assert result["step_id"] == "init"
    assert result["errors"] == {"base": "thresholds_not_ascending"}
