from __future__ import annotations

import hashlib
import json
import shutil
import stat
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from skillforge import final_rehearsal as rehearsal_module
from skillforge.contracts import ContractValidationError, validate_document
from skillforge.final_rehearsal import (
    DEFAULT_POLICY,
    DEFAULT_RUNBOOK,
    FinalRehearsalError,
    _write_private_json,
    final_rehearsal_qa_issue,
    initialize_final_rehearsal,
    load_policy,
    verify_final_rehearsal,
    verify_final_rehearsal_document,
)
from skillforge.submission import _check_final_rehearsal_private_state


ROOT = Path(__file__).resolve().parents[1]
BOUNDARIES = [0, 20000, 40000, 70000, 110000, 140000, 160000, 178000]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ready_rehearsal_document(runbook_path: Path = DEFAULT_RUNBOOK) -> dict:
    runbook = json.loads(runbook_path.read_text(encoding="utf-8"))
    return {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": "2026-07-19T01:00:00+00:00",
        "status": "READY_FOR_CHECK",
        "performed_at": "2026-07-19T00:30:00+00:00",
        "run_number": 1,
        "timer_source": "STOPWATCH",
        "total_duration_ms": BOUNDARIES[-1],
        "segments": [
            {
                "phase": segment["phase"],
                "planned_start_ms": segment["start_ms"],
                "planned_end_ms": segment["end_ms"],
                "actual_start_ms": BOUNDARIES[index],
                "actual_end_ms": BOUNDARIES[index + 1],
                "script_completed": True,
                "operator_action_completed": True,
                "proof_points_verified": True,
                "fallback_ready": True,
            }
            for index, segment in enumerate(runbook["segments"])
        ],
        "completion": {
            "full_sequence_completed": True,
            "no_unrecovered_failure": True,
            "no_sensitive_material_shown": True,
        },
        "notes": "私有彩排备注不得复制到QA报告",
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": False,
            "contains_credentials": False,
            "git_tracked": False,
        },
    }


def _private_ready_rehearsal(
    tmp_path: Path,
    *,
    runbook_path: Path = DEFAULT_RUNBOOK,
    policy_path: Path = DEFAULT_POLICY,
) -> tuple[Path, Path, Path, dict]:
    private = tmp_path / "private"
    record = private / "final_stage_rehearsal.json"
    report_path = private / "final_stage_rehearsal_qa.json"
    initialize_final_rehearsal(
        record,
        runbook_path=runbook_path,
        private_root=private,
    )
    _write_private_json(
        ready_rehearsal_document(runbook_path),
        record,
        private_root=private,
    )
    report = verify_final_rehearsal(
        record,
        runbook_path=runbook_path,
        policy_path=policy_path,
        private_root=private,
    )
    _write_private_json(report, report_path, private_root=private)
    return private, record, report_path, report


def test_policy_is_internal_and_not_an_official_video_rule() -> None:
    policy = load_policy()

    assert policy["policy_basis"] == "INTERNAL_STAGE_REHEARSAL_TARGET_NOT_OFFICIAL_RULE"
    assert policy["official_video_requirements_verified"] is False
    assert policy["duration"] == {"minimum_ms": 175000, "maximum_ms": 180000}


def test_template_is_private_complete_and_never_overwritten(tmp_path: Path) -> None:
    private = tmp_path / "private"
    record = private / "final_stage_rehearsal.json"
    initialize_final_rehearsal(record, private_root=private)
    document = validate_document(
        json.loads(record.read_text(encoding="utf-8")),
        "final_rehearsal_record.schema.json",
    )

    assert document["status"] == "PENDING_INPUT"
    assert len(document["segments"]) == 7
    assert document["segments"][0]["phase"] == "PROBLEM"
    assert document["segments"][-1]["planned_end_ms"] == 180000
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    with pytest.raises(FinalRehearsalError, match="不会覆盖"):
        initialize_final_rehearsal(record, private_root=private)


def test_ready_record_passes_without_copying_notes_or_paths_to_qa(
    tmp_path: Path,
) -> None:
    _, record, _, report = _private_ready_rehearsal(tmp_path)
    validate_document(report, "final_rehearsal_qa.schema.json")

    assert report["status"] == "READY_FOR_HUMAN_CONFIRMATION"
    assert report["duration"]["actual_ms"] == 178000
    assert report["duration"]["headroom_ms"] == 2000
    assert all(report["checks"].values())
    serialized = json.dumps(report, ensure_ascii=False)
    assert "私有彩排备注" not in serialized
    assert str(record.resolve()) not in serialized


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["segments"][1].__setitem__("phase", "PROBLEM"),
        lambda value: value["segments"][1].__setitem__("planned_start_ms", 21000),
        lambda value: value["segments"][1].__setitem__("actual_start_ms", 21000),
        lambda value: value.__setitem__("total_duration_ms", 181000),
        lambda value: value["segments"][2].__setitem__("proof_points_verified", False),
        lambda value: value["completion"].__setitem__("no_sensitive_material_shown", False),
    ],
)
def test_incomplete_or_inconsistent_rehearsal_is_rejected(mutation) -> None:
    document = deepcopy(ready_rehearsal_document())
    mutation(document)
    runbook = json.loads(DEFAULT_RUNBOOK.read_text(encoding="utf-8"))
    policy = load_policy()

    with pytest.raises((FinalRehearsalError, ContractValidationError)):
        verify_final_rehearsal_document(
            document,
            record_sha256="1" * 64,
            record_bytes=100,
            runbook=runbook,
            runbook_sha256="2" * 64,
            policy=policy,
            policy_sha256="3" * 64,
        )


def test_permission_drift_is_rejected(tmp_path: Path) -> None:
    _, record, _, _ = _private_ready_rehearsal(tmp_path)
    record.chmod(0o644)

    with pytest.raises(FinalRehearsalError, match="权限"):
        verify_final_rehearsal(record, private_root=record.parent)


def test_qa_binding_rejects_changed_record_policy_and_url(tmp_path: Path) -> None:
    private, record, report_path, report = _private_ready_rehearsal(tmp_path)
    evidence = {
        "kind": "LOCAL_FILE",
        "locator": str(record.resolve()),
        "sha256": report["record_sha256"],
        "size_bytes": report["record_bytes"],
    }

    assert final_rehearsal_qa_issue(report_path, evidence) is None
    changed_timing = json.loads(record.read_text(encoding="utf-8"))
    changed_timing["segments"][-1]["actual_end_ms"] -= 1000
    changed_timing["total_duration_ms"] -= 1000
    _write_private_json(changed_timing, record, private_root=private)
    forged_report = deepcopy(report)
    forged_report["record_sha256"] = _sha256(record)
    forged_report["record_bytes"] = record.stat().st_size
    _write_private_json(forged_report, report_path, private_root=private)
    forged_evidence = {
        "kind": "LOCAL_FILE",
        "locator": str(record.resolve()),
        "sha256": _sha256(record),
        "size_bytes": record.stat().st_size,
    }
    assert (
        final_rehearsal_qa_issue(report_path, forged_evidence)
        == "FINAL_REHEARSAL_QA_STATE_CHANGED"
    )
    _write_private_json(ready_rehearsal_document(), record, private_root=private)
    _write_private_json(report, report_path, private_root=private)
    inconsistent = json.loads(record.read_text(encoding="utf-8"))
    inconsistent["segments"][1]["actual_start_ms"] += 1000
    _write_private_json(inconsistent, record, private_root=private)
    forged_report = deepcopy(report)
    forged_report["record_sha256"] = _sha256(record)
    forged_report["record_bytes"] = record.stat().st_size
    _write_private_json(forged_report, report_path, private_root=private)
    forged_evidence = {
        "kind": "LOCAL_FILE",
        "locator": str(record.resolve()),
        "sha256": _sha256(record),
        "size_bytes": record.stat().st_size,
    }
    assert (
        final_rehearsal_qa_issue(report_path, forged_evidence)
        == "FINAL_REHEARSAL_QA_INVALID"
    )
    _write_private_json(ready_rehearsal_document(), record, private_root=private)
    _write_private_json(report, report_path, private_root=private)
    assert (
        final_rehearsal_qa_issue(report_path, {"kind": "HTTPS_URL"})
        == "FINAL_REHEARSAL_REQUIRES_LOCAL_FILE"
    )
    record.chmod(0o644)
    assert (
        final_rehearsal_qa_issue(report_path, evidence)
        == "FINAL_REHEARSAL_RECORD_PERMISSIONS_UNSAFE"
    )
    record.chmod(0o600)
    changed = json.loads(record.read_text(encoding="utf-8"))
    changed["notes"] = "记录已变化"
    _write_private_json(changed, record, private_root=private)
    current = verify_final_rehearsal(record, private_root=private)
    assert (
        final_rehearsal_qa_issue(
            report_path,
            {
                "kind": "LOCAL_FILE",
                "locator": str(record.resolve()),
                "sha256": current["record_sha256"],
                "size_bytes": current["record_bytes"],
            },
        )
        == "FINAL_REHEARSAL_QA_RECORD_CHANGED"
    )


def test_submission_state_is_safe_for_absent_ready_and_stale_record(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    (root / "cases/n31").mkdir(parents=True)
    (root / "config").mkdir()
    shutil.copy2(DEFAULT_RUNBOOK, root / "cases/n31/pitch_runbook.json")
    shutil.copy2(DEFAULT_POLICY, root / "config/final_rehearsal_policy.json")
    absent = _check_final_rehearsal_private_state(root)
    assert absent["status"] == "PASSED"
    assert "ABSENT" in absent["details"][0]

    private = root / "outputs/submission"
    record = private / "final_stage_rehearsal.json"
    report_path = private / "final_stage_rehearsal_qa.json"
    initialize_final_rehearsal(
        record,
        runbook_path=root / "cases/n31/pitch_runbook.json",
        private_root=private,
    )
    _write_private_json(
        ready_rehearsal_document(root / "cases/n31/pitch_runbook.json"),
        record,
        private_root=private,
    )
    report = verify_final_rehearsal(
        record,
        runbook_path=root / "cases/n31/pitch_runbook.json",
        policy_path=root / "config/final_rehearsal_policy.json",
        private_root=private,
    )
    _write_private_json(report, report_path, private_root=private)

    ready = _check_final_rehearsal_private_state(root)
    assert ready["status"] == "PASSED"
    assert "总时长=178000毫秒" in ready["details"][0]
    changed = json.loads(record.read_text(encoding="utf-8"))
    changed["notes"] = "changed"
    _write_private_json(changed, record, private_root=private)
    stale = _check_final_rehearsal_private_state(root)
    assert stale["status"] == "FAILED"
    assert str(record.resolve()) not in json.dumps(stale, ensure_ascii=False)


def test_ready_schema_cannot_claim_success_with_failed_check(tmp_path: Path) -> None:
    _, _, _, report = _private_ready_rehearsal(tmp_path)
    invalid = deepcopy(report)
    invalid["checks"]["all_fallbacks_ready"] = False

    with pytest.raises(ContractValidationError):
        validate_document(invalid, "final_rehearsal_qa.schema.json")


def test_final_rehearsal_script_is_executable() -> None:
    script = ROOT / "scripts/check_final_rehearsal.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
def test_final_rehearsal_error_prints_actionable_hints(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*args: object, **kwargs: object) -> object:
        raise FinalRehearsalError("彩排记录不存在；请先使用--init")

    monkeypatch.setattr(rehearsal_module, "verify_final_rehearsal", _boom)
    monkeypatch.setattr(sys, "argv", ["check_final_rehearsal"])
    exit_code = rehearsal_module.main()
    captured = capsys.readouterr()
    assert exit_code == 1
    payload = json.loads(captured.out)
    assert payload["status"] == "ERROR"
    assert "--init" in captured.err
    assert "final-rehearsal" in captured.err


def test_final_rehearsal_dynamic_failure_uses_prefix_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*args: object, **kwargs: object) -> object:
        raise FinalRehearsalError("彩排计时或完整性检查未通过：DURATION")

    monkeypatch.setattr(rehearsal_module, "verify_final_rehearsal", _boom)
    monkeypatch.setattr(sys, "argv", ["check_final_rehearsal"])
    exit_code = rehearsal_module.main()
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "READY_FOR_CHECK" in captured.err
