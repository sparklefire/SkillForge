from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from skillforge.contracts import validate_document
from skillforge.release_manifest import (
    DEFAULT_CONFIG,
    DEFAULT_MANIFEST,
    DEFAULT_RUNBOOK,
    EXPECTED_PUBLICATION_STATUS,
    ReleaseManifestError,
    build_release_manifest,
    validate_release_manifest_document,
    verify_release_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict:
    return json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))


def test_tracked_release_manifest_matches_all_frozen_artifacts() -> None:
    manifest = verify_release_manifest()
    validate_document(manifest, "release_manifest.schema.json")

    assert manifest["artifact_count"] == 18
    assert len(manifest["artifacts"]) == 18
    assert len({item["artifact_id"] for item in manifest["artifacts"]}) == 18
    assert all(item["status"] == "FROZEN_MACHINE_VERIFIED" for item in manifest["artifacts"])
    assert all(item["responsible_role"] != item["final_checker_role"] for item in manifest["artifacts"])


def test_release_roles_keep_identity_and_urls_out_of_git() -> None:
    config = validate_document(_config(), "release_roles.schema.json")
    manifest = verify_release_manifest()

    assert config["identity_assignment"]["status"] == "PENDING_TEAM_ROSTER"
    assert config["identity_assignment"]["contains_personal_data"] is False
    assert {item["target_id"]: item["status"] for item in config["publication_targets"]} == EXPECTED_PUBLICATION_STATUS
    assert all(item["public_url"] is None for item in manifest["publication_targets"])
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert "/Users/" not in serialized
    assert "/home/" not in serialized
    assert "https://" not in serialized


def test_freeze_digest_is_stable_across_generation_time() -> None:
    first = build_release_manifest(generated_at="2026-07-18T00:00:00+00:00")
    second = build_release_manifest(generated_at="2026-07-18T01:00:00+00:00")

    assert first["generated_at"] != second["generated_at"]
    assert first["technical_freeze_digest"] == second["technical_freeze_digest"]


def test_manifest_detects_artifact_hash_drift() -> None:
    manifest = deepcopy(verify_release_manifest())
    manifest["artifacts"][0]["sha256"] = "0" * 64

    with pytest.raises(ReleaseManifestError, match="不一致"):
        validate_release_manifest_document(manifest)


@pytest.mark.parametrize("failure", ["duplicate", "same_reviewer"])
def test_role_config_rejects_unsafe_assignment(
    tmp_path: Path,
    failure: str,
) -> None:
    config = _config()
    if failure == "duplicate":
        config["artifact_assignments"][-1] = deepcopy(
            config["artifact_assignments"][0]
        )
    else:
        config["artifact_assignments"][0]["final_checker_role"] = config[
            "artifact_assignments"
        ][0]["responsible_role"]
    path = tmp_path / "release_roles.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ReleaseManifestError):
        build_release_manifest(config_path=path, runbook_path=DEFAULT_RUNBOOK)


def test_release_manifest_script_is_executable() -> None:
    script = ROOT / "scripts/build_release_manifest.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
    assert DEFAULT_MANIFEST.is_file()
