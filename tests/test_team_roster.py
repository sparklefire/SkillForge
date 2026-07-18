from __future__ import annotations

import json
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from skillforge.contracts import ContractValidationError, validate_document
from skillforge.submission import _check_team_roster_private_state
from skillforge.team_roster import (
    EXPECTED_ROLES,
    TeamRosterError,
    _write_private_json,
    initialize_team_roster,
    verify_team_roster,
    verify_team_roster_document,
)


ROOT = Path(__file__).resolve().parents[1]


def _ready_document() -> dict:
    return {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": "2026-07-18T00:00:00+00:00",
        "status": "READY_FOR_CHECK",
        "members": [
            {
                "member_id": "M1",
                "name": "测试成员甲",
                "organization": "测试单位甲",
                "primary_contact": True,
                "registration_confirmed": True,
                "one_team_only_confirmed": True,
            },
            {
                "member_id": "M2",
                "name": "测试成员乙",
                "organization": "测试单位乙",
                "primary_contact": False,
                "registration_confirmed": True,
                "one_team_only_confirmed": True,
            },
        ],
        "role_assignments": [
            {"role_id": role_id, "member_id": "M1" if index % 2 == 0 else "M2"}
            for index, role_id in enumerate(sorted(EXPECTED_ROLES))
        ],
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": True,
            "git_tracked": False,
        },
    }


def _private_ready_roster(tmp_path: Path) -> tuple[Path, Path]:
    private = tmp_path / "private"
    path = private / "team_roster.json"
    initialize_team_roster(path, private_root=private)
    _write_private_json(_ready_document(), path, private_root=private)
    return private, path


def test_roster_template_is_private_empty_and_never_overwritten(tmp_path: Path) -> None:
    private = tmp_path / "private"
    path = private / "team_roster.json"
    initialize_team_roster(path, private_root=private)
    document = validate_document(
        json.loads(path.read_text(encoding="utf-8")),
        "team_roster.schema.json",
    )

    assert document["status"] == "PENDING_INPUT"
    assert document["members"] == []
    assert {item["role_id"] for item in document["role_assignments"]} == EXPECTED_ROLES
    assert all(item["member_id"] is None for item in document["role_assignments"])
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(TeamRosterError, match="不会覆盖"):
        initialize_team_roster(path, private_root=private)


def test_ready_roster_passes_without_copying_personal_data_to_qa(tmp_path: Path) -> None:
    private, path = _private_ready_roster(tmp_path)
    report = verify_team_roster(path, private_root=private)
    validate_document(report, "team_roster_qa.schema.json")

    assert report["status"] == "READY_FOR_HUMAN_CONFIRMATION"
    assert report["team_size"] == 2
    assert report["role_assignment_count"] == 6
    assert report["human_gate_status"] == "PENDING"
    serialized = json.dumps(report, ensure_ascii=False)
    for value in ("测试成员甲", "测试成员乙", "测试单位甲", "测试单位乙", "M1", "M2"):
        assert value not in serialized


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["members"].__setitem__(
            1, {**value["members"][1], "member_id": "M1"}
        ),
        lambda value: value["members"][0].__setitem__("primary_contact", False),
        lambda value: value["members"][1].__setitem__("primary_contact", True),
        lambda value: value["members"][0].__setitem__("registration_confirmed", False),
        lambda value: value["members"][0].__setitem__("one_team_only_confirmed", False),
        lambda value: value["members"][0].__setitem__("organization", " "),
        lambda value: value["role_assignments"].pop(),
        lambda value: value["role_assignments"][-1].__setitem__(
            "role_id", value["role_assignments"][0]["role_id"]
        ),
        lambda value: value["role_assignments"][0].__setitem__("member_id", "M5"),
    ],
)
def test_incomplete_or_inconsistent_roster_is_rejected(mutation) -> None:
    document = deepcopy(_ready_document())
    mutation(document)
    with pytest.raises(TeamRosterError):
        verify_team_roster_document(document, roster_sha256="1" * 64)


def test_roster_permission_drift_is_rejected(tmp_path: Path) -> None:
    private, path = _private_ready_roster(tmp_path)
    path.chmod(0o644)

    with pytest.raises(TeamRosterError, match="权限"):
        verify_team_roster(path, private_root=private)


def test_submission_check_is_safe_for_absent_draft_and_valid_roster(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    private = root / "outputs/submission"
    absent = _check_team_roster_private_state(root)
    assert absent["status"] == "PASSED"
    assert "ABSENT" in absent["details"][0]

    path = private / "team_roster.json"
    initialize_team_roster(path, private_root=private)
    draft = _check_team_roster_private_state(root)
    assert draft["status"] == "PASSED"
    assert "PENDING_INPUT" in draft["details"][0]
    assert "测试成员" not in json.dumps(draft, ensure_ascii=False)

    _write_private_json(_ready_document(), path, private_root=private)
    valid = _check_team_roster_private_state(root)
    assert valid["status"] == "PASSED"
    assert "成员=2" in valid["details"][0]
    assert "人工门禁=PENDING" in valid["details"][0]
    assert "测试成员" not in json.dumps(valid, ensure_ascii=False)


def test_qa_schema_cannot_claim_readiness_with_failed_check(tmp_path: Path) -> None:
    private, path = _private_ready_roster(tmp_path)
    report = verify_team_roster(path, private_root=private)
    invalid = deepcopy(report)
    invalid["checks"]["team_size_valid"] = False

    with pytest.raises(ContractValidationError):
        validate_document(invalid, "team_roster_qa.schema.json")


def test_team_roster_script_is_executable() -> None:
    script = ROOT / "scripts/check_team_roster.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
