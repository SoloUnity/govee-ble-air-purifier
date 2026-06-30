# Cloud-Style Fan Entity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Present the BLE purifier to Home Assistant like Govee cloud integrations: one `fan` entity for power/speed/presets plus two surfaced sensor entities.

**Architecture:** Replace the loaded `switch` and `select` platforms with a `fan` platform while keeping the sensor platform. The fan entity wraps existing coordinator methods, maps manual speed labels to HA fan percentages, and maps `Auto` to HA preset mode while keeping PM2.5 and filter-life as separate sensors with cloud-style measurement metadata.

**Tech Stack:** Home Assistant custom integration, `FanEntity`, `SensorEntity`, `DataUpdateCoordinator`, pytest.

---

### Task 1: Add Cloud-Style Fan Entity

**Files:**
- Create: `custom_components/govee_ble_air_purifier/fan.py`
- Modify: `custom_components/govee_ble_air_purifier/const.py`
- Test: `tests/test_fan_entity.py`

- [ ] **Step 1: Write failing tests**

Add tests that prove the integration exposes `fan` instead of `switch/select`, that the fan maps manual speeds to percentage, and that `Auto` maps to preset mode.

- [ ] **Step 2: Run the fan tests and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_fan_entity.py -q`

Expected before implementation: import or assertion failure because `fan.py` does not exist and `PLATFORMS` lacks `fan`.

- [ ] **Step 3: Implement minimal fan platform**

Create `GoveeAirPurifierFan` using `FanEntityFeature.TURN_ON`, `TURN_OFF`, `SET_SPEED`, and `PRESET_MODE`. Use existing coordinator methods: `async_set_power(True/False)` and `async_set_fan_mode(label)`.

- [ ] **Step 4: Replace loaded control platforms**

Change `PLATFORMS` to `['fan', 'sensor']`. Leave old `switch.py` and `select.py` files in place but unloaded to avoid unnecessary file churn.

- [ ] **Step 5: Run focused tests**

Run: `.venv/bin/python -m pytest tests/test_fan_entity.py tests/test_coordinator_logic.py tests/test_protocol.py -q`

Expected: all focused tests pass.

### Task 2: Surface Sensors Like Cloud Integrations

**Files:**
- Modify: `custom_components/govee_ble_air_purifier/sensor.py`
- Modify: `custom_components/govee_ble_air_purifier/strings.json`
- Modify: `custom_components/govee_ble_air_purifier/translations/en.json`
- Test: `tests/test_sensor_entities.py`

- [ ] **Step 1: Write failing tests**

Add tests that assert the sensor platform creates exactly two sensors: PM2.5 and Filter Life. PM2.5 must be non-diagnostic with PM2.5 device class, µg/m³ unit, and measurement state class. Filter Life must use `%`, measurement state class, and diagnostic category like the cloud integrations.

- [ ] **Step 2: Run the sensor tests and confirm failure**

Run: `.venv/bin/python -m pytest tests/test_sensor_entities.py -q`

Expected before implementation: failure because current sensors do not set measurement state class.

- [ ] **Step 3: Add cloud-style sensor metadata**

Use `SensorEntityDescription` or equivalent metadata so both sensors have stable keys, names, device classes/units, and `SensorStateClass.MEASUREMENT`.

- [ ] **Step 4: Run focused tests**

Run: `.venv/bin/python -m pytest tests/test_sensor_entities.py tests/test_hacs_packaging.py -q`

Expected: all focused tests pass.

### Task 3: Update Docs And Verify

**Files:**
- Modify: `README.md`
- Possibly modify tests that assert platform/docs expectations.

- [ ] **Step 1: Update README entity list**

Document one `fan` entity with power, manual speeds, and presets plus PM2.5/filter-life sensors.

- [ ] **Step 2: Run full verification**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests pass.

Run: `.venv/bin/python -m compileall custom_components tests`

Expected: all files compile.

Run: `.venv/bin/python -m json.tool custom_components/govee_ble_air_purifier/strings.json >/dev/null && .venv/bin/python -m json.tool custom_components/govee_ble_air_purifier/translations/en.json >/dev/null && git diff --check`

Expected: no output/errors.

### Self-Review

- The plan replaces old controls with one fan entity, matching cloud integration presentation.
- The plan keeps exactly two surfaced sensors: PM2.5 and filter life.
- The plan avoids committing because the user did not explicitly request a commit.
