# Manual-Only Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Home Assistant from creating unsolicited Bluetooth discovery setup flows while preserving user-initiated HACS install plus Add Integration setup.

**Architecture:** Remove the manifest Bluetooth matcher and `async_step_bluetooth`/`async_step_bluetooth_confirm` config-flow entry points. Keep `async_step_user` and its BLE cache picker so users can manually add the integration and choose a cached purifier or enter a BLE address.

**Tech Stack:** Home Assistant custom integration config flow, manifest metadata, HACS packaging tests, pytest.

---

### Task 1: Lock Manual-Only Discovery Behavior With Tests

**Files:**
- Modify: `tests/test_hacs_packaging.py`
- Test: `tests/test_hacs_packaging.py`

- [ ] **Step 1: Write the failing manifest behavior test**

Replace the Bluetooth assertion in `test_integration_manifest_has_hacs_required_metadata` with assertions that the integration has no manifest `bluetooth` matcher but still depends on Bluetooth support:

```python
    assert manifest["dependencies"] == ["bluetooth_adapters"]
    assert "bleak-retry-connector" in manifest["requirements"][0]
    assert "bluetooth" not in manifest
```

Add a config-flow source check:

```python
def test_config_flow_is_manual_only() -> None:
    config_flow = (
        ROOT / "custom_components" / DOMAIN / "config_flow.py"
    ).read_text(encoding="utf-8")

    assert "async_step_user" in config_flow
    assert "async_step_bluetooth" not in config_flow
    assert "async_step_bluetooth_confirm" not in config_flow
```

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_hacs_packaging.py::test_integration_manifest_has_hacs_required_metadata tests/test_hacs_packaging.py::test_config_flow_is_manual_only -q`

Expected: failure because `manifest.json` still has `bluetooth`, and `config_flow.py` still defines `async_step_bluetooth`.

### Task 2: Remove Automatic Bluetooth Discovery Entry Points

**Files:**
- Modify: `custom_components/govee_ble_air_purifier/manifest.json`
- Modify: `custom_components/govee_ble_air_purifier/config_flow.py`
- Modify: `custom_components/govee_ble_air_purifier/strings.json`
- Modify: `custom_components/govee_ble_air_purifier/translations/en.json`
- Test: `tests/test_hacs_packaging.py`

- [ ] **Step 1: Remove manifest Bluetooth matcher**

Remove this key from `manifest.json`:

```json
  "bluetooth": [
    {
      "local_name": "GVH7124*",
      "connectable": true
    }
  ],
```

Keep `dependencies: ["bluetooth_adapters"]`, because the integration still uses Home Assistant Bluetooth APIs after the user creates a config entry.

- [ ] **Step 2: Remove automatic Bluetooth config-flow methods**

Delete `GoveeBleAirPurifierConfigFlow.async_step_bluetooth` and `GoveeBleAirPurifierConfigFlow.async_step_bluetooth_confirm` from `config_flow.py`.

Delete the unused `_discovered_device` class attribute.

Keep the top-level `from homeassistant.components import bluetooth` import because `_discovered_device_options()` still reads the BLE cache for the manual setup picker.

- [ ] **Step 3: Remove unused Bluetooth-confirm translations**

Delete the `bluetooth_confirm` step from both `strings.json` and `translations/en.json`.

Keep the `user` step text, because manual setup still offers cached purifier choices and manual BLE address entry.

- [ ] **Step 4: Run focused tests to verify behavior**

Run: `.venv/bin/python -m pytest tests/test_hacs_packaging.py tests/test_config_flow_logic.py -q`

Expected: all focused packaging/setup helper tests pass.

### Task 3: Update User-Facing Setup Documentation

**Files:**
- Modify: `README.md`
- Test: `tests/test_hacs_packaging.py`

- [ ] **Step 1: Make setup docs explicitly manual-only**

Update setup/install wording so it says HACS only installs the integration, and the user must go to Settings > Devices & services > Add Integration to add `Govee BLE Air Purifier` manually.

Use wording equivalent to:

```markdown
After HACS installs the files and Home Assistant restarts, Home Assistant will not create an automatic discovered-device prompt. Add the integration yourself from Settings > Devices & services > Add Integration, then choose a cached BLE device or enter the BLE address manually.
```

- [ ] **Step 2: Keep HACS install documentation compliant**

Ensure README still contains:

```text
HACS
Custom repositories
https://github.com/SoloUnity/govee-ble-air-purifier
Integration
Restart Home Assistant
custom_components/govee_ble_air_purifier
5 to 300 seconds
```

- [ ] **Step 3: Run full verification**

Run:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall custom_components tests
.venv/bin/python -m json.tool hacs.json >/dev/null
.venv/bin/python -m json.tool custom_components/govee_ble_air_purifier/manifest.json >/dev/null
.venv/bin/python -m json.tool custom_components/govee_ble_air_purifier/strings.json >/dev/null
.venv/bin/python -m json.tool custom_components/govee_ble_air_purifier/translations/en.json >/dev/null
ruby -e 'require "yaml"; data = YAML.load_file(".github/workflows/validate.yml"); abort "missing Validate" unless data["name"] == "Validate"; abort "missing checkout" unless data.dig("jobs", "validate-hacs", "steps").any? { |step| step["uses"] == "actions/checkout@v4" }; abort "missing hacs action" unless data.dig("jobs", "validate-hacs", "steps").any? { |step| step["uses"] == "hacs/action@main" }; abort "missing hassfest" unless data.dig("jobs", "validate-hassfest")'
ruby -e 'data = File.binread("custom_components/govee_ble_air_purifier/brand/icon.png"); abort "bad png signature" unless data.start_with?("\x89PNG\r\n\x1a\n".b); width, height = data.byteslice(16, 8).unpack("NN"); abort "bad dimensions #{width}x#{height}" unless width == 256 && height == 256'
git diff --check
```

Expected: all commands exit 0.

No commit is part of this plan unless the user explicitly asks for one.

---

## Self-Review

- Spec coverage: The plan removes both Home Assistant autodiscovery triggers, preserves manual Add Integration setup, keeps cached BLE device selection, and updates docs/tests.
- Placeholder scan: No TBD/TODO placeholders remain.
- Type consistency: File paths, method names, config keys, and test names match the current repository structure.
