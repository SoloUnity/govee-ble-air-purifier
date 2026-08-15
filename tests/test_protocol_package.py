"""Validate the reusable protocol package and compatibility boundaries."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from custom_components.govee_ble_air_purifier import models as legacy_models
from custom_components.govee_ble_air_purifier import profiles as legacy_profiles
from custom_components.govee_ble_air_purifier import protocol as legacy_protocol
from custom_components.govee_ble_air_purifier.bluetooth import (
    framing as legacy_framing,
)
from custom_components.govee_ble_air_purifier.bluetooth import (
    govee_v1 as legacy_govee_v1,
)
from custom_components.govee_ble_air_purifier import (
    govee_ble_air_purifier_protocol as protocol_library,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SOURCE = (
    ROOT
    / "custom_components"
    / "govee_ble_air_purifier"
    / "govee_ble_air_purifier_protocol"
)


def test_protocol_source_has_no_home_assistant_or_integration_imports() -> None:
    """Keep the reusable source independent of its Home Assistant adapter."""

    forbidden_roots = {"custom_components", "homeassistant"}
    for path in sorted(PACKAGE_SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not ({alias.name.split(".", 1)[0] for alias in node.names} & forbidden_roots)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert node.module is not None
                assert node.module.split(".", 1)[0] not in forbidden_roots


def test_public_api_decodes_a_profile_status_frame() -> None:
    """Exercise the documented package-root API without integration helpers."""

    profile = protocol_library.get_profile("h7124")
    frame = protocol_library.build_frame(
        bytes.fromhex("aa 19 81 00 08 00 00 64")
    )

    assert profile.is_status_response(frame)
    assert protocol_library.decode_status(frame) == protocol_library.DecodedStatus(
        pm25=8,
        filter_life=100,
    )
    assert protocol_library.PROFILE_DIRECTORY.joinpath("h7129.json").is_file()
    assert protocol_library.MODEL_PROFILE_SCHEMA_PATH.is_file()


def test_legacy_imports_are_thin_identity_preserving_facades() -> None:
    """Preserve existing custom-component imports during package extraction."""

    assert legacy_models.PurifierState is protocol_library.PurifierState
    assert legacy_profiles.ModelProfile is protocol_library.ModelProfile
    assert legacy_profiles.get_profile is protocol_library.get_profile
    assert legacy_protocol.decode_status is protocol_library.decode_status
    assert legacy_framing.ProtocolError is protocol_library.ProtocolError
    assert legacy_framing.build_frame is protocol_library.build_frame
    assert legacy_govee_v1.encrypt_frame is protocol_library.encrypt_frame


def test_built_wheel_contains_typed_package_data_and_imports_in_isolation(
    tmp_path: Path,
) -> None:
    """Install a built wheel and prove it neither shadows nor omits its data."""

    wheels = sorted((ROOT / "dist").glob("*.whl"))
    if not wheels:
        pytest.skip("build the wheel before running the isolated wheel smoke test")
    wheel = wheels[-1]
    package_prefix = "govee_ble_air_purifier_protocol/"
    required = {
        f"{package_prefix}py.typed",
        f"{package_prefix}model_profiles/default.json",
        f"{package_prefix}model_profiles/h7124.json",
        f"{package_prefix}model_profiles/h7129.json",
        f"{package_prefix}schemas/model_profile_v5.schema.json",
    }
    with zipfile.ZipFile(wheel) as archive:
        assert required <= set(archive.namelist())
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in archive.namelist())

    installed = tmp_path / "installed"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    check = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                f"import sys; sys.path.insert(0, {str(installed)!r}); "
                "import json, govee_ble_air_purifier_protocol as p; "
                "print(json.dumps({'origin': p.__file__, "
                "'profile': str(p.PROFILE_DIRECTORY), "
                "'schema': str(p.MODEL_PROFILE_SCHEMA_PATH), "
                "'model': p.get_profile('h7124').model, "
                "'paths': sys.path}))"
            ),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(check.stdout)
    assert str(ROOT) not in result["origin"]
    assert str(installed) in result["origin"]
    assert str(installed) in result["profile"]
    assert str(installed) in result["schema"]
    resolved_paths = {Path(path).resolve() for path in result["paths"] if path}
    assert ROOT not in resolved_paths
    source_root = ROOT / "custom_components"
    assert not any(
        path == source_root or source_root in path.parents for path in resolved_paths
    )
    assert result["model"] == "H7124"
