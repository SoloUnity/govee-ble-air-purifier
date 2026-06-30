import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "govee_ble_air_purifier"


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
    assert manifest["documentation"].startswith("https://github.com/")
    assert manifest["issue_tracker"].startswith("https://github.com/")
    assert manifest["codeowners"]
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "local_polling"
    assert manifest["integration_type"] == "device"
    assert manifest["dependencies"] == ["bluetooth_adapters"]
    assert "bleak-retry-connector" in manifest["requirements"][0]
    assert manifest["bluetooth"] == [{"local_name": "GVH7124*", "connectable": True}]


def test_validation_workflow_runs_hacs_and_hassfest() -> None:
    workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
        encoding="utf-8"
    )

    assert "hacs/action@main" in workflow
    assert 'category: "integration"' in workflow
    assert "home-assistant/actions/hassfest@master" in workflow
    assert "secrets." not in workflow


def test_readme_documents_hacs_and_manual_installation() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "HACS" in readme
    assert "Custom repositories" in readme
    assert "category" in readme
    assert "Integration" in readme
    assert "Restart Home Assistant" in readme
    assert "custom_components/govee_ble_air_purifier" in readme
    assert "update" in readme.lower()
    assert "issue tracker" in readme.lower()
