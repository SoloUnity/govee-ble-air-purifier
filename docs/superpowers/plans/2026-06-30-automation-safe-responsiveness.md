# Automation-Safe Responsiveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve BLE responsiveness while preserving automation correctness and external-change reconciliation.

**Architecture:** Keep short-lived BLE connections as the default. The coordinator remains the state authority, polling reconciles physical/Govee-app changes, and HA-initiated commands publish confirmed state immediately while deferring non-critical refresh work.

**Tech Stack:** Home Assistant `DataUpdateCoordinator`, BLE via `bleak_retry_connector`, pytest.

---

### File Structure

- Modify `custom_components/govee_ble_air_purifier/client.py`: split poll timeout from command confirmation timeout.
- Modify `custom_components/govee_ble_air_purifier/coordinator.py`: coalesce scheduled refreshes, cancel pending refresh before commands, and keep command behavior automation-safe.
- Modify `tests/test_client.py`: verify poll timeout differs from command timeout.
- Modify `tests/test_coordinator_logic.py`: verify refresh coalescing and command-priority behavior.

### Task 1: Poll Timeout

**Files:**
- Modify: `custom_components/govee_ble_air_purifier/client.py`
- Test: `tests/test_client.py`

- [ ] **Step 1: Write failing test**

Add a test that confirms `async_get_state()` uses a shorter poll timeout by observing the timeout passed into `_async_write_and_wait_many()`.

- [ ] **Step 2: Implement minimal code**

Add `POLL_TIMEOUT = 5.0` and pass it from `async_get_state()` to `_async_write_and_wait_many()`.

- [ ] **Step 3: Verify**

Run: `.venv/bin/python -m pytest tests/test_client.py -q`

Expected: pass.

### Task 2: Coalesced Background Refresh

**Files:**
- Modify: `custom_components/govee_ble_air_purifier/coordinator.py`
- Test: `tests/test_coordinator_logic.py`

- [ ] **Step 1: Write failing tests**

Add tests proving two scheduled refresh calls cancel the first task and keep only the latest scheduled refresh.

- [ ] **Step 2: Implement minimal code**

Store the refresh task on the coordinator and cancel it before scheduling a new one. Clear the task when it completes or is cancelled.

- [ ] **Step 3: Verify**

Run: `.venv/bin/python -m pytest tests/test_coordinator_logic.py -q`

Expected: pass.

### Task 3: Command Priority Over Pending Refresh

**Files:**
- Modify: `custom_components/govee_ble_air_purifier/coordinator.py`
- Test: `tests/test_coordinator_logic.py`

- [ ] **Step 1: Write failing tests**

Add tests proving `async_set_power()` and `async_set_fan_mode()` cancel a pending background refresh before sending BLE commands.

- [ ] **Step 2: Implement minimal code**

Add `_cancel_background_refresh()` and call it at the start of command methods.

- [ ] **Step 3: Verify**

Run: `.venv/bin/python -m pytest tests/test_coordinator_logic.py -q`

Expected: pass.

### Task 4: Full Verification

**Files:**
- Verify entire repo.

- [ ] **Step 1: Run full test suite**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests pass.

- [ ] **Step 2: Compile and whitespace checks**

Run: `.venv/bin/python -m compileall custom_components tests`

Run: `git diff --check`

Expected: clean.

### Self-Review

- Spec coverage: covers shorter poll timeout, coalesced refreshes, command priority, and automation-safe behavior by avoiding broad no-op skipping or global command coalescing.
- Placeholder scan: no placeholders remain.
- Type consistency: all planned names align with existing coordinator/client structure.
