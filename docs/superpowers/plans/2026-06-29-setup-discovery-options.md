# Setup Discovery Options Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve first-time setup so users can choose discovered purifiers by name/address/signal and configure BLE polling interval during setup or later.

**Architecture:** Keep Bluetooth discovery and setup choices inside `config_flow.py`, with pure helper functions for testability. Store polling interval seconds in config entry `options`; pass it into `GoveeCoordinator` at setup so the update interval is per entry instead of global-only.

**Tech Stack:** Home Assistant config flows/options flows, HA Bluetooth discovery cache, voluptuous schemas, existing pure pytest suite.

---

### Task 1: Config Constants And Pure Helpers

**Files:**
- Modify: `custom_components/govee_ble_air_purifier/const.py`
- Modify: `custom_components/govee_ble_air_purifier/config_flow.py`
- Test: `tests/test_config_flow_logic.py`

- [ ] Write failing tests for display labels, polling interval validation, and discovered-device filtering.
- [ ] Run `pytest tests/test_config_flow_logic.py -q` and confirm failures.
- [ ] Add `CONF_POLLING_INTERVAL`, default/min/max constants, and pure helpers in `config_flow.py`.
- [ ] Run `pytest tests/test_config_flow_logic.py -q` and confirm pass.

### Task 2: User Flow Discovery Picker

**Files:**
- Modify: `custom_components/govee_ble_air_purifier/config_flow.py`
- Modify: `custom_components/govee_ble_air_purifier/strings.json`
- Modify: `custom_components/govee_ble_air_purifier/translations/en.json`
- Test: `tests/test_config_flow_logic.py`

- [ ] Add tests proving discovered `GVH7124*` entries become selectable options with name, address, and RSSI signal text.
- [ ] Update manual user flow schema to include discovered device selector, manual address fallback, name, and polling interval seconds.
- [ ] Keep HA Bluetooth auto-discovery confirmation path intact, adding polling interval storage.
- [ ] Run focused tests.

### Task 3: Options Flow And Coordinator Interval

**Files:**
- Modify: `custom_components/govee_ble_air_purifier/config_flow.py`
- Modify: `custom_components/govee_ble_air_purifier/__init__.py`
- Modify: `custom_components/govee_ble_air_purifier/coordinator.py`
- Modify: `custom_components/govee_ble_air_purifier/strings.json`
- Modify: `custom_components/govee_ble_air_purifier/translations/en.json`
- Test: `tests/test_coordinator_logic.py`
- Test: `tests/test_config_flow_logic.py`

- [ ] Add tests for default and custom coordinator update intervals.
- [ ] Add tests for options schema bounds/defaults.
- [ ] Make `GoveeCoordinator` accept `polling_interval`.
- [ ] Add `OptionsFlow` for polling interval changes.
- [ ] Run focused tests.

### Task 4: Documentation And Verification

**Files:**
- Modify: `README.md`
- Test: `tests/test_hacs_packaging.py`

- [ ] Update README setup/polling descriptions.
- [ ] Extend packaging/docs tests for polling interval documentation.
- [ ] Run full verification: `pytest`, `compileall`, JSON checks, YAML parse, PNG sanity, `git diff --check`.

No commits are part of this plan unless explicitly requested.
