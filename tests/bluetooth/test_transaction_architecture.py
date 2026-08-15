"""Architecture guards for the extracted BLE transaction boundary."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[2]
BLUETOOTH = ROOT / "custom_components" / "govee_ble_air_purifier" / "bluetooth"


def _tree(filename: str) -> ast.Module:
    return ast.parse((BLUETOOTH / filename).read_text(encoding="utf-8"))


def _client_method(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    client = next(
        node
        for node in _tree("client.py").body
        if isinstance(node, ast.ClassDef) and node.name == "GoveeBleClient"
    )
    return next(
        node
        for node in client.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def test_client_transaction_facade_stays_bounded() -> None:
    """Connection policy must not absorb transaction execution again."""

    method = _client_method("_async_write_and_wait_many")

    assert method.end_lineno is not None
    assert method.end_lineno - method.lineno + 1 <= 120


def test_client_connection_and_recovery_facades_stay_bounded() -> None:
    """Connection lifecycle policy must remain in its typed collaborator."""

    limits = {
        "_async_with_connection_unarbitrated": 15,
        "_schedule_notification_recovery": 10,
        "_async_drop_after_error": 10,
        "_handle_disconnect": 8,
        "_async_drop_connection": 10,
    }
    for name, limit in limits.items():
        method = _client_method(name)
        assert method.end_lineno is not None
        assert method.end_lineno - method.lineno + 1 <= limit


def test_connection_manager_has_no_transaction_semantics() -> None:
    """Lifecycle recovery cannot acquire command matching or replay policy."""

    tree = _tree("_connection.py")
    imported_modules: list[str] = []
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_modules.append(node.module or "")

    assert not any("_transactions" in module for module in imported_modules)
    assert names.isdisjoint(
        {"TransactionRunner", "ExchangePlan", "ExchangeRequest", "FrameMatcher"}
    )


def test_transaction_runner_has_no_ha_transport_or_client_dependency() -> None:
    """The runner consumes a typed session rather than integration internals."""

    forbidden: list[str] = []
    for node in ast.walk(_tree("_transactions.py")):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("homeassistant") or "transport" in alias.name:
                    forbidden.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.startswith("homeassistant") or "transport" in module:
                forbidden.append(module)
            forbidden.extend(
                alias.name for alias in node.names if alias.name == "GoveeBleClient"
            )

    assert forbidden == []
