import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "govee_ble_air_purifier"
REPOSITORY_URL = "https://github.com/SoloUnity/govee-ble-air-purifier"
ISSUE_TRACKER_URL = f"{REPOSITORY_URL}/issues"
PLACEHOLDER_REPOSITORY = "custom-components"
PLACEHOLDER_CODEOWNER = "@custom-components"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_hacs_repository_metadata_exists_and_uses_component_layout() -> None:
    hacs = _read_json(ROOT / "hacs.json")

    assert hacs["name"] == "Govee BLE Air Purifier"
    assert "homeassistant" in hacs
    assert "hacs" in hacs
    assert "content_in_root" not in hacs
    assert (ROOT / "custom_components" / DOMAIN / "manifest.json").is_file()


def test_integration_manifest_has_hacs_required_metadata() -> None:
    manifest = _read_json(ROOT / "custom_components" / DOMAIN / "manifest.json")

    assert manifest["domain"] == DOMAIN
    assert manifest["name"] == "Govee BLE Air Purifier"
    assert manifest["version"]
    assert manifest["documentation"] == REPOSITORY_URL
    assert manifest["issue_tracker"] == ISSUE_TRACKER_URL
    assert manifest["codeowners"] == ["@SoloUnity"]
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_polling"
    assert manifest["integration_type"] == "device"
    assert manifest["dependencies"] == ["bluetooth_adapters"]
    assert "bleak-retry-connector" in manifest["requirements"][0]
    assert "bluetooth" not in manifest


def test_config_flow_is_manual_only() -> None:
    config_flow = (
        ROOT / "custom_components" / DOMAIN / "config_flow.py"
    ).read_text(encoding="utf-8")

    assert "async_step_user" in config_flow
    assert "async_request_active_scan" in config_flow
    assert "async_step_bluetooth" not in config_flow
    assert "async_step_bluetooth_confirm" not in config_flow


def test_integration_manifest_keys_match_hassfest_order() -> None:
    manifest = _read_json(ROOT / "custom_components" / DOMAIN / "manifest.json")
    keys = list(manifest)

    assert keys[:2] == ["domain", "name"]
    assert keys[2:] == sorted(keys[2:])


def test_integration_provides_hacs_brand_icon() -> None:
    icon = ROOT / "custom_components" / DOMAIN / "brand" / "icon.png"

    assert icon.is_file()
    assert icon.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_validation_workflow_runs_hacs_and_hassfest() -> None:
    workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
        encoding="utf-8"
    )

    assert "hacs/action@main" in workflow
    assert workflow.index("actions/checkout@v4") < workflow.index("hacs/action@main")
    assert 'category: "integration"' in workflow
    assert "home-assistant/actions/hassfest@master" in workflow
    assert "secrets." not in workflow


def test_hacs_packaging_metadata_does_not_use_placeholders() -> None:
    paths = [
        ROOT / "custom_components" / DOMAIN / "manifest.json",
        ROOT / "README.md",
    ]

    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert PLACEHOLDER_REPOSITORY not in text
        assert PLACEHOLDER_CODEOWNER not in text
