import json
import stat
from pathlib import Path

import pytest

from skillforge.evidence_locator import build_evidence_locator
from skillforge.review_sessions import SopReviewSessionStore, rebuild_step_artifacts


ROOT = Path(__file__).resolve().parents[1]


def _gold() -> dict:
    return json.loads(
        (ROOT / "cases/n31/gold/gold_sop.json").read_text(encoding="utf-8")
    )


def test_review_session_locks_confirms_and_persists_private_audit(tmp_path: Path) -> None:
    store = SopReviewSessionStore(tmp_path / "sessions")
    session = store.create(_gold())
    assert session["status"] == "IN_REVIEW"
    assert len(session["steps"]) == 13
    assert stat.S_IMODE((tmp_path / "sessions").stat().st_mode) == 0o700
    path = tmp_path / "sessions" / f"{session['session_id']}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    with pytest.raises(ValueError, match="先锁定"):
        store.set_step_state(session["session_id"], "S01", _gold(), confirmed=True)
    session = store.set_step_state(
        session["session_id"], "S01", _gold(), locked=True
    )
    session = store.set_step_state(
        session["session_id"], "S01", _gold(), confirmed=True
    )
    s01 = next(item for item in session["steps"] if item["step_id"] == "S01")
    assert s01["locked"] is True
    assert s01["confirmed"] is True
    assert [item["action"] for item in session["events"]] == ["LOCKED", "CONFIRMED"]
    assert all(item["actor"] == "OPERATOR" for item in session["events"])
    assert all(item["automatic"] is False for item in session["events"])

    with pytest.raises(ValueError, match="先撤回确认"):
        store.set_step_state(session["session_id"], "S01", _gold(), locked=False)
    session = store.set_step_state(
        session["session_id"], "S01", _gold(), confirmed=False
    )
    session = store.set_step_state(
        session["session_id"], "S01", _gold(), locked=False
    )
    assert [item["action"] for item in session["events"]][-2:] == [
        "REOPENED",
        "UNLOCKED",
    ]
    session = store.set_step_state(
        session["session_id"], "S02", _gold(), locked=True, confirmed=True
    )
    assert [item["action"] for item in session["events"]][-2:] == [
        "LOCKED",
        "CONFIRMED",
    ]
    session = store.set_step_state(
        session["session_id"], "S02", _gold(), locked=False, confirmed=False
    )
    assert [item["action"] for item in session["events"]][-2:] == [
        "REOPENED",
        "UNLOCKED",
    ]
    for item in session["steps"]:
        session = store.set_step_state(
            session["session_id"], item["step_id"], _gold(), locked=True
        )
        session = store.set_step_state(
            session["session_id"], item["step_id"], _gold(), confirmed=True
        )
    assert session["status"] == "COMPLETED"
    assert all(item["locked"] and item["confirmed"] for item in session["steps"])


def test_review_reorder_enforces_dependencies_and_locked_positions(tmp_path: Path) -> None:
    gold = _gold()
    store = SopReviewSessionStore(tmp_path / "sessions")
    session = store.create(gold)
    session = store.reorder(session["session_id"], "S11", 12, gold)
    assert [item["step_id"] for item in session["steps"]][10:13] == [
        "S12",
        "S11",
        "S13",
    ]
    assert session["events"][-1]["action"] == "REORDERED"

    with pytest.raises(ValueError, match="S08 必须先于 S09"):
        store.reorder(session["session_id"], "S09", 8, gold)

    locked = store.set_step_state(
        session["session_id"], "S12", gold, locked=True
    )
    with pytest.raises(ValueError, match="已锁定步骤 S12"):
        store.reorder(locked["session_id"], "S11", 11, gold)


def test_single_step_rebuild_returns_only_bounded_units(tmp_path: Path) -> None:
    gold = _gold()
    store = SopReviewSessionStore(tmp_path / "sessions")
    session = store.create(gold)
    result = rebuild_step_artifacts(store, session["session_id"], "S12", gold)
    assert result["artifact_type"] == "SINGLE_STEP_REBUILD"
    assert result["step_id"] == "S12"
    assert result["rebuild_number"] == 1
    assert result["scope"] == {
        "sop_view_units": 3,
        "checklist_units": 1,
        "quiz_question_ids": ["Q03"],
        "unchanged_step_count": 12,
        "external_model_calls": 0,
    }
    assert set(result["sop_views"]) == {"concise", "detailed", "evidence"}
    assert all(item["step_id"] == "S12" for item in result["sop_views"].values())
    assert result["checklist_item"]["step_id"] == "S12"
    assert [item["question_id"] for item in result["quiz_questions"]] == ["Q03"]
    assert result["data_policy"]["contains_raw_media"] is False
    session = store.get(session["session_id"])
    s12 = next(item for item in session["steps"] if item["step_id"] == "S12")
    assert s12["rebuild_count"] == 1
    assert session["events"][-1]["action"] == "STEP_REBUILT"

    store.set_step_state(session["session_id"], "S12", gold, locked=True)
    with pytest.raises(ValueError, match="不能重建"):
        rebuild_step_artifacts(store, session["session_id"], "S12", gold)


def test_review_session_rejects_changed_sop_binding(tmp_path: Path) -> None:
    gold = _gold()
    store = SopReviewSessionStore(tmp_path / "sessions")
    session = store.create(gold)
    changed = json.loads(json.dumps(gold, ensure_ascii=False))
    changed["steps"][0]["title"] += "（变化）"
    with pytest.raises(ValueError, match="SOP内容已经变化"):
        store.set_step_state(session["session_id"], "S01", changed, locked=True)


def test_evidence_locator_strips_private_keyframe_and_returns_safe_navigation() -> None:
    gold = _gold()
    video = build_evidence_locator(gold, "E001")
    assert video["navigation"]["kind"] == "VIDEO_TIME"
    assert video["navigation"]["safe_preview_url"].startswith(
        "/api/n31/checklist/previews/S"
    )
    assert video["navigation"]["raw_source_url"] is None
    assert "keyframe" not in video["locator"]
    assert video["data_policy"]["contains_absolute_paths"] is False

    audio = build_evidence_locator(gold, "E144")
    assert audio["navigation"]["kind"] == "AUDIO_TIME"
    assert audio["navigation"]["safe_preview_url"] is None
    assert audio["navigation"]["label"] == "40.7–68.7秒"

    pdf_id = next(
        item["evidence_id"]
        for item in gold["evidence_catalog"]
        if item["source_type"] == "pdf"
    )
    pdf = build_evidence_locator(gold, pdf_id)
    assert pdf["navigation"]["kind"] == "PDF_PAGE"
    assert pdf["navigation"]["label"].startswith("PDF第")
    assert pdf["navigation"]["raw_source_url"] is None

    with pytest.raises(KeyError):
        build_evidence_locator(gold, "E999")
