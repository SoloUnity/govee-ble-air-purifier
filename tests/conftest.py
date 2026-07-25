"""Collection-time support for the fast suite without Home Assistant installed."""

from __future__ import annotations

import importlib.util
from types import ModuleType
import sys
from typing import Any


if importlib.util.find_spec("homeassistant") is None:

    class UpdateFailed(Exception):
        """Minimal stand-in for Home Assistant's coordinator update error."""

    class DataUpdateCoordinator:
        """Minimal production-shaped coordinator used by the fast test suite."""

        def __init__(
            self,
            hass: Any,
            logger: Any,
            *,
            name: str,
            update_interval: Any = None,
        ) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = None
            self.last_update_success = True

        def async_set_updated_data(self, data: Any) -> None:
            self.data = data
            self.last_update_success = True

        async def async_request_refresh(self) -> None:
            try:
                data = await self._async_update_data()
            except Exception:
                self.last_update_success = False
                raise
            self.async_set_updated_data(data)

        async def async_config_entry_first_refresh(self) -> None:
            await self.async_request_refresh()

        async def async_shutdown(self) -> None:
            return None

    homeassistant = ModuleType("homeassistant")
    homeassistant.__path__ = []
    homeassistant._FAST_TEST_STUB = True
    helpers = ModuleType("homeassistant.helpers")
    helpers.__path__ = []
    update_coordinator = ModuleType("homeassistant.helpers.update_coordinator")
    update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    update_coordinator.UpdateFailed = UpdateFailed
    homeassistant.helpers = helpers
    helpers.update_coordinator = update_coordinator
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator
