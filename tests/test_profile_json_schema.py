"""Validate bundled model profiles against their published JSON Schema."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from custom_components.govee_ble_air_purifier.const import (
    MAX_POLLING_INTERVAL_SECONDS,
    MIN_POLLING_INTERVAL_SECONDS,
)
from custom_components.govee_ble_air_purifier.profiles import (
    MODEL_PROFILE_SCHEMA_PATH,
    PROFILE_DIRECTORY,
    PROFILE_SCHEMA_VERSION,
)


@pytest.fixture(scope="module")
def profile_schema() -> dict[str, object]:
    """Load and verify the developer-facing profile schema."""

    schema: dict[str, object] = json.loads(
        MODEL_PROFILE_SCHEMA_PATH.read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)
    return schema


@pytest.fixture(scope="module")
def h7124_profile() -> dict[str, object]:
    """Load one complete profile for negative schema tests."""

    return json.loads((PROFILE_DIRECTORY / "h7124.json").read_text(encoding="utf-8"))


def test_every_bundled_profile_matches_schema(
    profile_schema: dict[str, object],
) -> None:
    """Keep every shipped model definition structurally compatible with v5."""

    validator = Draft202012Validator(profile_schema)
    for path in sorted(PROFILE_DIRECTORY.glob("*.json")):
        profile = json.loads(path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(profile), key=lambda error: list(error.path))
        assert not errors, f"{path.name}: {errors}"


def test_schema_version_and_polling_bounds_match_runtime(
    profile_schema: dict[str, object],
) -> None:
    """Prevent duplicated structural constants from drifting apart."""

    properties = profile_schema["properties"]
    assert isinstance(properties, dict)
    assert properties["schema_version"] == {"const": PROFILE_SCHEMA_VERSION}
    assert properties["polling_interval_seconds"] == {
        "type": "integer",
        "minimum": MIN_POLLING_INTERVAL_SECONDS,
        "maximum": MAX_POLLING_INTERVAL_SECONDS,
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda profile: profile.update(schema_version=4), "5 was expected"),
        (lambda profile: profile.update(unexpected=True), "Additional properties"),
        (
            lambda profile: profile["commands"].update(power_on="33 01 01"),
            "does not match",
        ),
        (
            lambda profile: profile["custom_auto"].update(thresholds=[3, 5, 9]),
            "is too short",
        ),
    ],
)
def test_schema_rejects_invalid_profile_shapes(
    profile_schema: dict[str, object],
    h7124_profile: dict[str, object],
    mutation: object,
    message: str,
) -> None:
    """Guard the high-value structural constraints expressed by JSON Schema."""

    profile = copy.deepcopy(h7124_profile)
    assert callable(mutation)
    mutation(profile)

    with pytest.raises(ValidationError, match=message):
        Draft202012Validator(profile_schema).validate(profile)


def test_night_light_push_requires_night_light_capability(
    profile_schema: dict[str, object],
    h7124_profile: dict[str, object],
) -> None:
    """Keep the cross-capability requirement aligned with the runtime loader."""

    profile = copy.deepcopy(h7124_profile)
    del profile["night_light"]

    with pytest.raises(ValidationError, match="night_light"):
        Draft202012Validator(profile_schema).validate(profile)
