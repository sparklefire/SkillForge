from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

import skillforge.submission_closeout as closeout_module
from skillforge.contracts import validate_document
from skillforge.submission_closeout import (
    DEFAULT_OUTPUT,
    GATE_IDS,
    STAGE_ORDER,
    SubmissionCloseoutError,
    _overall_status,
    _preflight_stage,
    build_submission_closeout_status,
    verify_saved_closeout_status,
    write_closeout_status,
)
from skillforge.team_roster import initialize_team_roster, verify_team_roster
from skillforge.training_video_review import initialize_training_video_review


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def private_root(tmp_path: Path) -> Path:
    path = tmp_path / "submission"
    path.mkdir(mode=0o700)
    return path


def _stages(document: dict) -> dict[str, dict]:
    return {item["stage_id"]: item for item in document["stages"]}


def _write_json_600(path: Path, document: dict) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def test_current_empty_private_state_is_actionable_not_implementation_blocked(
    private_root: Path,
) -> None:
    report = build_submission_closeout_status(private_root=private_root)
    validate_document(report, "submission_closeout_status.schema.json")
    assert report["status"] == "TECHNICAL_READY_HUMAN_GATES_PENDING"
    assert report["implementation_goal_blocked"] is False
    assert report["formal_submission_ready"] is False
    assert report["submission_archived"] is False
    assert report["stage_count"] == 10
    assert report["completed_stage_count"] == 1
    assert report["human_gate_summary"] == {
        "confirmed": 0,
        "pending": 5,
        "total": 5,
        "store_state": "ABSENT",
        "valid": True,
        "issue_count": 0,
    }
    assert report["next_action"]["stage_id"] == "TRAINING_VIDEO_FULL_WATCH"
    assert tuple(item["stage_id"] for item in report["stages"]) == STAGE_ORDER
    stages = _stages(report)
    assert stages["TECHNICAL_RELEASE_BUNDLE"]["status"] == "COMPLETED"
    assert stages["OFFICIAL_RULES_VERIFIED"]["status"] == "AWAITING_EXTERNAL"
    assert stages["FINAL_CLEAN_PREFLIGHT"]["status"] == "WAITING_ON_DEPENDENCIES"
    assert report["data_policy"]["automatic_human_confirmations"] == 0
    assert report["data_policy"]["network_requests"] == 0


def test_written_status_is_private_reproducible_and_detects_drift(
    private_root: Path,
) -> None:
    output = private_root / "submission_closeout_status.json"
    report = build_submission_closeout_status(private_root=private_root)
    write_closeout_status(report, output, private_root=private_root)
    assert stat.S_IMODE(private_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert verify_saved_closeout_status(
        output, private_root=private_root
    ) == report

    orphan = private_root / "training_video_review_qa.json"
    _write_json_600(orphan, {})
    with pytest.raises(SubmissionCloseoutError, match="不一致"):
        verify_saved_closeout_status(output, private_root=private_root)


def test_missing_or_partial_technical_bundle_is_not_false_success(
    private_root: Path, tmp_path: Path
) -> None:
    missing = build_submission_closeout_status(
        private_root=private_root,
        release_archive=tmp_path / "missing.zip",
        release_qa=tmp_path / "missing.json",
    )
    assert missing["status"] == "TECHNICAL_PACKAGE_PENDING"
    assert missing["next_action"]["stage_id"] == "TECHNICAL_RELEASE_BUNDLE"
    assert _stages(missing)["TECHNICAL_RELEASE_BUNDLE"]["status"] == "READY"

    partial_archive = tmp_path / "partial.zip"
    partial_archive.write_bytes(b"not-a-bundle")
    partial = build_submission_closeout_status(
        private_root=private_root,
        release_archive=partial_archive,
        release_qa=tmp_path / "missing.json",
    )
    assert partial["status"] == "NEEDS_REVIEW"
    assert _stages(partial)["TECHNICAL_RELEASE_BUNDLE"]["evidence_state"] == "PARTIAL"


def test_initialized_video_review_is_reported_as_draft_without_private_content(
    private_root: Path,
) -> None:
    initialize_training_video_review(
        private_root / "training_video_review.json",
        manifest_path=ROOT / "output/video/n31_training_video_manifest_v1.json",
        video_path=ROOT / "output/video/n31_training_video_v1.mp4",
        private_root=private_root,
    )
    report = build_submission_closeout_status(private_root=private_root)
    video = _stages(report)["TRAINING_VIDEO_FULL_WATCH"]
    assert video["status"] == "AWAITING_HUMAN"
    assert video["evidence_state"] == "DRAFT"
    assert video["next_command"] == "bash scripts/check_training_video_review.sh"
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(private_root) not in serialized
    assert "/Users/" not in serialized


def test_valid_team_qa_is_ready_for_confirmation_without_leaking_identity(
    private_root: Path,
) -> None:
    roster_path = private_root / "team_roster.json"
    initialize_team_roster(roster_path, private_root=private_root)
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    roster["status"] = "READY_FOR_CHECK"
    roster["members"] = [
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
    ]
    for index, assignment in enumerate(roster["role_assignments"]):
        assignment["member_id"] = "M1" if index % 2 == 0 else "M2"
    _write_json_600(roster_path, roster)
    qa = verify_team_roster(roster_path, private_root=private_root)
    _write_json_600(private_root / "team_roster_qa.json", qa)

    report = build_submission_closeout_status(private_root=private_root)
    team = _stages(report)["TEAM_ELIGIBILITY_CONFIRMED"]
    assert team["status"] == "READY_FOR_CONFIRMATION"
    assert team["evidence_state"] == "MACHINE_READY"
    serialized = json.dumps(report, ensure_ascii=False)
    for private_value in ("测试成员甲", "测试成员乙", "测试单位甲", "测试单位乙", "M1", "M2"):
        assert private_value not in serialized


def test_invalid_confirmation_store_is_safely_summarized(private_root: Path) -> None:
    store = private_root / "human_gate_confirmations.json"
    _write_json_600(store, {})
    report = build_submission_closeout_status(private_root=private_root)
    assert report["status"] == "NEEDS_REVIEW"
    assert report["human_gate_summary"]["store_state"] == "INVALID"
    assert report["human_gate_summary"]["valid"] is False
    assert report["human_gate_summary"]["issue_count"] >= 1
    assert report["next_action"]["stage_id"] == "TRAINING_VIDEO_FULL_WATCH"
    assert all(
        _stages(report)[gate_id]["status"] == "NEEDS_REVIEW"
        for gate_id in GATE_IDS
    )
    serialized = json.dumps(report, ensure_ascii=False)
    assert "gate_label" not in serialized
    assert '"issues"' not in serialized
    assert "reviewer" not in serialized
    assert str(private_root) not in serialized


def test_overall_state_machine_orders_post_gate_dependencies(private_root: Path) -> None:
    stages = deepcopy(
        build_submission_closeout_status(private_root=private_root)["stages"]
    )
    by_id = {item["stage_id"]: item for item in stages}
    for gate_id in GATE_IDS:
        by_id[gate_id]["status"] = "COMPLETED"
    assert _overall_status(stages) == "FINAL_PREFLIGHT_PENDING"
    by_id["FINAL_CLEAN_PREFLIGHT"]["status"] = "COMPLETED"
    assert _overall_status(stages) == "READY_FOR_UPLOAD"
    by_id["SUBMISSION_UPLOAD"]["status"] = "COMPLETED"
    assert _overall_status(stages) == "PUBLIC_LINK_QA_PENDING"
    by_id["PUBLIC_LINK_QA"]["status"] = "COMPLETED"
    assert _overall_status(stages) == "SUBMISSION_RECEIPT_PENDING"
    by_id["SUBMISSION_RECEIPT"]["status"] = "COMPLETED"
    assert _overall_status(stages) == "READY_FOR_ARCHIVE"


def test_early_publication_input_cannot_skip_human_gates_or_final_preflight(
    private_root: Path,
) -> None:
    publication = {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": "2026-07-19T00:00:00+08:00",
        "status": "READY_FOR_CHECK",
        "targets": [
            {
                "target_id": "PROJECT_PAGE",
                "expected_surface": "HTML",
                "public_url": "https://example.com/project",
            },
            {
                "target_id": "CODE_REPOSITORY",
                "expected_surface": "HTML",
                "public_url": "https://example.com/code",
            },
            {
                "target_id": "FINAL_RECORDING",
                "expected_surface": "HTML_OR_VIDEO",
                "public_url": "https://example.com/video",
            },
        ],
        "data_policy": {
            "private_local_state": True,
            "contains_credentials": False,
            "contains_personal_data": False,
        },
    }
    _write_json_600(private_root / "publication_links.json", publication)
    report = build_submission_closeout_status(private_root=private_root)
    stages = _stages(report)
    assert report["status"] == "TECHNICAL_READY_HUMAN_GATES_PENDING"
    assert stages["SUBMISSION_UPLOAD"]["status"] == "WAITING_ON_DEPENDENCIES"
    assert stages["PUBLIC_LINK_QA"]["status"] == "WAITING_ON_DEPENDENCIES"
    assert report["formal_submission_ready"] is False
    serialized = json.dumps(report, ensure_ascii=False)
    assert "example.com" not in serialized
    assert "https://" not in serialized


def test_nonfinal_preflight_copy_is_needs_review(private_root: Path) -> None:
    latest = json.loads(
        (ROOT / "outputs/submission/submission_preflight_latest.json").read_text(
            encoding="utf-8"
        )
    )
    _write_json_600(private_root / "submission_preflight_final.json", latest)
    report = build_submission_closeout_status(private_root=private_root)
    preflight = _stages(report)["FINAL_CLEAN_PREFLIGHT"]
    assert preflight["status"] == "NEEDS_REVIEW"
    assert preflight["evidence_state"] == "INVALID"
    assert report["status"] == "NEEDS_REVIEW"


def test_final_preflight_rejects_substituted_check_id(
    private_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = json.loads(
        (ROOT / "outputs/submission/submission_preflight_latest.json").read_text(
            encoding="utf-8"
        )
    )
    latest["status"] = "READY_FOR_SUBMISSION"
    latest["source_commit"] = "a" * 40
    latest["source_worktree_clean"] = True
    latest["pending_human_gates"] = []
    for check in latest["automatic_checks"]:
        check["status"] = "PASSED"
    latest["automatic_checks"][0]["check_id"] = "SUBSTITUTED_CHECK"
    _write_json_600(private_root / "submission_preflight_final.json", latest)
    monkeypatch.setattr(
        closeout_module,
        "_git_state",
        lambda _root: (True, "a" * 40, True),
    )

    stage = _preflight_stage(ROOT, private_root, dependencies_complete=True)
    assert stage["status"] == "NEEDS_REVIEW"
    assert stage["evidence_state"] == "INVALID"


def test_cli_output_is_safe_pending_signal_and_default_report_is_ignored() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "skillforge.submission_closeout"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "TECHNICAL_READY_HUMAN_GATES_PENDING"
    assert payload["implementation_goal_blocked"] is False
    assert payload["automatic_human_confirmations"] == 0
    assert payload["network_requests"] == 0
    assert "/Users/" not in result.stdout
    assert "https://" not in result.stdout
    assert DEFAULT_OUTPUT.is_file()
    assert stat.S_IMODE(DEFAULT_OUTPUT.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(DEFAULT_OUTPUT.stat().st_mode) == 0o600
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(DEFAULT_OUTPUT.relative_to(ROOT))],
        cwd=ROOT,
        check=False,
    )
    assert ignored.returncode == 0
    script = ROOT / "scripts/check_submission_closeout.sh"
    assert script.is_file() and script.stat().st_mode & 0o111
