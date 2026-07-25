"""Small Home Assistant stubs used by tests that run without Home Assistant."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest


class CoordinatorEntity:
    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator
        self.hass = None
        self._remove_callbacks: list[Any] = []
        self.state_writes = 0

    @property
    def available(self) -> bool:
        return getattr(self.coordinator, "last_update_success", True)

    async def async_added_to_hass(self) -> None:
        return None

    def async_on_remove(self, callback: Any) -> None:
        self._remove_callbacks.append(callback)

    def async_write_ha_state(self) -> None:
        self.state_writes += 1


class DeviceInfo(dict[str, Any]):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class HomeAssistantError(Exception):
    """Stub Home Assistant service error."""


class RestoreEntity:
    async def async_get_last_state(self) -> Any:
        return getattr(self, "_test_last_state", None)


def install_modules(
    monkeypatch: pytest.MonkeyPatch,
    module_attributes: dict[str, dict[str, Any]],
) -> dict[str, ModuleType]:
    """Install a linked module tree in sys.modules and return its modules."""
    names = set(module_attributes)
    for name in tuple(names):
        parts = name.split(".")
        names.update(".".join(parts[:index]) for index in range(1, len(parts)))

    modules = {name: ModuleType(name) for name in sorted(names)}
    for name, attributes in module_attributes.items():
        modules[name].__dict__.update(attributes)
    for name, module in modules.items():
        if "." in name:
            parent_name, child_name = name.rsplit(".", 1)
            setattr(modules[parent_name], child_name, module)
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return modules
