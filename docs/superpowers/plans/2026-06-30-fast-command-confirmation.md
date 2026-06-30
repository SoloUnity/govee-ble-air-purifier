# Fast Command Confirmation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Home Assistant fan controls update quickly after BLE commands by waiting for command-specific confirmations instead of blocking on a full power/status poll.

**Architecture:** Add protocol helpers for command echoes and mode push notifications. Update the BLE client to subscribe, write, and wait briefly for command-specific confirmation frames. Update the coordinator to publish command-confirmed state immediately, while scheduling a later background refresh for sensor/state reconciliation.

**Tech Stack:** Python, Home Assistant `DataUpdateCoordinator`, BLE notifications via Bleak, pytest.

---

### Task 1: Protocol Confirmation Helpers

**Files:**
- Modify: `custom_components/govee_ble_air_purifier/protocol.py`
- Test: `tests/test_protocol.py`

- [ ] **Step 1: Write failing tests**

Add tests that verify `33 01` echoes, `3a 05` echoes, and `ee 05` mode pushes can be matched and decoded.

- [ ] **Step 2: Run protocol tests**

Run: `.venv/bin/python -m pytest tests/test_protocol.py -q`

Expected: FAIL because the helper functions do not exist.

- [ ] **Step 3: Implement helpers**

Add minimal functions to match `33 01`, exact command echoes, and decode `ee 05` mode push frames.

- [ ] **Step 4: Verify protocol tests**

Run: `.venv/bin/python -m pytest tests/test_protocol.py -q`

Expected: PASS.

### Task 2: BLE Command Confirmation

**Files:**
- Modify: `custom_components/govee_ble_air_purifier/client.py`
- Test: `tests/test_client.py`

- [ ] **Step 1: Write failing tests**

Add tests proving power and fan-mode commands start notifications, write the command, and wait for confirmation frames without running a full `aa01` + `aa19` state poll.

- [ ] **Step 2: Run client tests**

Run: `.venv/bin/python -m pytest tests/test_client.py -q`

Expected: FAIL because command methods currently write without notifications or confirmations.

- [ ] **Step 3: Implement client confirmation methods**

Change `async_set_power`, `async_set_fan_mode`, and `async_set_power_and_fan_mode` to return confirmed state/mode data after a short notification wait.

- [ ] **Step 4: Verify client tests**

Run: `.venv/bin/python -m pytest tests/test_client.py -q`

Expected: PASS.

### Task 3: Coordinator Immediate UI Update

**Files:**
- Modify: `custom_components/govee_ble_air_purifier/coordinator.py`
- Test: `tests/test_coordinator_logic.py`

- [ ] **Step 1: Write failing tests**

Add tests proving command methods update `coordinator.data` immediately from command confirmation and schedule a background refresh instead of awaiting a full refresh before returning.

- [ ] **Step 2: Run coordinator tests**

Run: `.venv/bin/python -m pytest tests/test_coordinator_logic.py -q`

Expected: FAIL because command methods currently await `async_request_refresh()`.

- [ ] **Step 3: Implement coordinator update path**

Merge confirmed command fields into existing `GoveeData`, call `async_set_updated_data()` when available, and schedule `async_request_refresh()` in the background.

- [ ] **Step 4: Verify coordinator tests**

Run: `.venv/bin/python -m pytest tests/test_coordinator_logic.py -q`

Expected: PASS.

### Task 4: Full Verification

**Files:**
- Verify all touched files.

- [ ] **Step 1: Run full tests**

Run: `.venv/bin/python -m pytest -q`

Expected: PASS.

- [ ] **Step 2: Run whitespace check**

Run: `git diff --check`

Expected: no output.

- [ ] **Step 3: Inspect diff**

Run: `git status --short` and `git diff --stat`

Expected: only intended protocol/client/coordinator/test/plan files changed.
