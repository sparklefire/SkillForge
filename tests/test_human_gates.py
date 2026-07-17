from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from skillforge.contracts import validate_document
from skillforge.human_gates import HumanGateError, HumanGateStore
from skillforge.submission import build_submission_preflight


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "cases/n31/pitch_runbook.json"
GATE_IDS = [
    "TRAINING_VIDEO_FULL_WATCH",
    "FINAL_STAGE_REHEARSAL",
    "FINAL_RECORDING_REVIEW",
    "TEAM_ELIGIBILITY_CONFIRMED",
    "OFFICIAL_RULES_VERIFIED",
]


def _copied_runbook(tmp_path: Path) -> Path:
    path = tmp_path / "pitch_runbook.json"
    path.write_bytes(RUNBOOK.read_bytes())
    return path


def test_confirmation_is_private_hash_bound_and_revocable(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    evidence = tmp_path / "review-note.txt"
    evidence.write_text("完整观看并核对旁白节奏", encoding="utf-8")
    store_path = tmp_path / "private" / "human_gate_confirmations.json"
    store = HumanGateStore(store_path, runbook_path=runbook)

    result = store.confirm(
        GATE_IDS[0],
        reviewer="仅私有审核名",
        evidence_file=evidence,
        note="已从头到尾观看",
    )

    assert result["valid"] is True
    assert result["summary"] == {"passed": 1, "pending": 4, "total": 5}
    assert stat.S_IMODE(store_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store_path.stat().st_mode) == 0o600
    document = json.loads(store_path.read_text(encoding="utf-8"))
    validate_document(document, "human_gate_confirmations.schema.json")
    assert document["confirmations"][0]["evidence"]["sha256"]
    assert "完整观看并核对旁白节奏" not in store_path.read_text(encoding="utf-8")
    serialized_status = json.dumps(result, ensure_ascii=False)
    assert str(evidence.resolve()) not in serialized_status
    assert "仅私有审核名" not in serialized_status

    revoked = store.revoke(
        GATE_IDS[0],
        reviewer="仅私有审核名",
        note="需要重新观看修订版",
    )
    assert revoked["summary"] == {"passed": 0, "pending": 5, "total": 5}
    assert json.loads(store_path.read_text(encoding="utf-8"))["history"][-1]["action"] == "REVOKED"


def test_changed_evidence_invalidates_confirmation(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    evidence = tmp_path / "rehearsal.txt"
    evidence.write_text("180秒", encoding="utf-8")
    store = HumanGateStore(tmp_path / "private" / "state.json", runbook_path=runbook)
    store.confirm(GATE_IDS[1], reviewer="审核人", evidence_file=evidence)

    evidence.write_text("181秒", encoding="utf-8")
    audit = store.audit()

    assert audit["valid"] is False
    assert audit["store_state"] == "INVALID"
    assert audit["confirmed_gate_ids"] == []
    assert audit["issues"] == [f"EVIDENCE_HASH_CHANGED:{GATE_IDS[1]}"]


def test_missing_evidence_invalidates_confirmation(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    evidence = tmp_path / "recording.mp4"
    evidence.write_bytes(b"safe test recording")
    store = HumanGateStore(tmp_path / "private" / "state.json", runbook_path=runbook)
    store.confirm(GATE_IDS[2], reviewer="审核人", evidence_file=evidence)

    evidence.unlink()
    audit = store.audit()

    assert audit["valid"] is False
    assert audit["issues"] == [f"EVIDENCE_FILE_MISSING:{GATE_IDS[2]}"]


def test_duplicate_confirmation_requires_explicit_replace(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    evidence = tmp_path / "team.txt"
    evidence.write_text("team evidence", encoding="utf-8")
    store_path = tmp_path / "private" / "state.json"
    store = HumanGateStore(store_path, runbook_path=runbook)
    store.confirm(GATE_IDS[3], reviewer="审核人", evidence_file=evidence)

    with pytest.raises(HumanGateError, match="--replace"):
        store.confirm(GATE_IDS[3], reviewer="审核人", evidence_file=evidence)
    with pytest.raises(HumanGateError, match="未知人工门禁"):
        store.confirm("UNKNOWN_GATE", reviewer="审核人", evidence_file=evidence)
    replaced = store.confirm(
        GATE_IDS[3],
        reviewer="复核人",
        evidence_file=evidence,
        note="复核报名信息",
        replace=True,
    )

    assert replaced["summary"]["passed"] == 1
    document = json.loads(store_path.read_text(encoding="utf-8"))
    assert len(document["confirmations"]) == 1
    assert [item["action"] for item in document["history"]] == [
        "CONFIRMED",
        "CONFIRMED",
    ]


def test_changed_runbook_makes_all_private_confirmations_stale(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    evidence = tmp_path / "recording.txt"
    evidence.write_text("recording sha evidence", encoding="utf-8")
    store = HumanGateStore(tmp_path / "private" / "state.json", runbook_path=runbook)
    store.confirm(GATE_IDS[2], reviewer="审核人", evidence_file=evidence)

    payload = json.loads(runbook.read_text(encoding="utf-8"))
    payload["human_gates"][2]["label"] += "（修订）"
    runbook.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    stale = store.audit()

    assert stale["valid"] is False
    assert stale["store_state"] == "STALE"
    assert stale["issues"] == ["RUNBOOK_HASH_CHANGED"]
    reset = store.reset_stale(reviewer="审核人", note="运行单已冻结新版本")
    assert reset["valid"] is True
    assert reset["summary"] == {"passed": 0, "pending": 5, "total": 5}


def test_insecure_store_mode_is_rejected(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    evidence = tmp_path / "team.txt"
    evidence.write_text("team check", encoding="utf-8")
    store_path = tmp_path / "private" / "state.json"
    store = HumanGateStore(store_path, runbook_path=runbook)
    store.confirm(GATE_IDS[3], reviewer="审核人", evidence_file=evidence)
    store_path.chmod(0o644)

    audit = store.audit()

    assert audit["valid"] is False
    assert "STORE_MODE_NOT_600" in audit["issues"]
    with pytest.raises(HumanGateError, match="权限不安全"):
        store.confirm(
            GATE_IDS[4],
            reviewer="审核人",
            evidence_url="https://example.com/rules",
        )


def test_custom_store_does_not_change_broad_existing_directory(tmp_path: Path) -> None:
    runbook = _copied_runbook(tmp_path)
    evidence = tmp_path / "rules.txt"
    evidence.write_text("rules evidence", encoding="utf-8")
    broad = tmp_path / "shared"
    broad.mkdir(mode=0o755)
    broad.chmod(0o755)
    store = HumanGateStore(broad / "state.json", runbook_path=runbook)

    with pytest.raises(HumanGateError, match="目录权限必须为700"):
        store.confirm(GATE_IDS[4], reviewer="审核人", evidence_file=evidence)

    assert stat.S_IMODE(broad.stat().st_mode) == 0o755
    assert not (broad / "state.json").exists()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/rules",
        "https://user:pass@example.com/rules",
        "https://example.com/rules?token=secret",
        "https://example.com/rules#private",
    ],
)
def test_unsafe_evidence_url_is_rejected(tmp_path: Path, url: str) -> None:
    store = HumanGateStore(
        tmp_path / "private" / "state.json",
        runbook_path=_copied_runbook(tmp_path),
    )
    with pytest.raises(HumanGateError, match="证据网址"):
        store.confirm(GATE_IDS[4], reviewer="审核人", evidence_url=url)


def test_valid_private_confirmations_remove_only_human_gates_from_preflight(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("explicit human confirmation", encoding="utf-8")
    store_path = tmp_path / "private" / "human_gate_confirmations.json"
    store = HumanGateStore(store_path, runbook_path=RUNBOOK)
    for gate_id in GATE_IDS:
        store.confirm(gate_id, reviewer="测试审核人", evidence_file=evidence)

    report = build_submission_preflight(
        root=ROOT,
        run_tests=False,
        allow_dirty=True,
        allow_missing_git=True,
        confirmations_path=store_path,
    )
    checks = {item["check_id"]: item for item in report["automatic_checks"]}

    assert report["pending_human_gates"] == []
    assert report["status"] == "DEVELOPMENT_CHECK"
    assert checks["HUMAN_GATE_CONFIRMATIONS"]["status"] == "PASSED"
    assert "人工门禁有效=5/5" in checks["HUMAN_GATE_CONFIRMATIONS"]["details"][0]
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(evidence.resolve()) not in serialized
    assert "测试审核人" not in serialized


def test_human_gate_script_is_executable() -> None:
    script = ROOT / "scripts/manage_human_gates.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
