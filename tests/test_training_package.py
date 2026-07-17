import json
import stat
from pathlib import Path

import pytest

from skillforge.checklist_sessions import ChecklistSessionStore
from skillforge.contracts import validate_document
from skillforge.creator import create_checklist, create_sop_views


ROOT = Path(__file__).resolve().parents[1]


def _gold_sop() -> dict:
    return json.loads((ROOT / "cases/n31/gold/gold_sop.json").read_text(encoding="utf-8"))


def _visual_review() -> dict:
    return json.loads(
        (ROOT / "cases/n31/evaluations/visual_sequence_review_v1.json").read_text(
            encoding="utf-8"
        )
    )


def test_creates_three_traceable_sop_views() -> None:
    views = create_sop_views(_gold_sop())
    validate_document(views, "sop_views.schema.json")
    assert set(views["views"]) == {"concise", "detailed", "evidence"}
    for view in views["views"].values():
        assert len(view["steps"]) == 13
        assert all(
            {"action", "reason", "completion_marker", "risks", "sources"}
            <= set(step)
            for step in view["steps"]
        )
    first = views["views"]["evidence"]["steps"][0]
    assert {item["source_type"] for item in first["evidence_details"]} == {
        "audio",
        "pdf",
        "video",
    }


def test_checklist_preserves_not_visible_visual_boundary() -> None:
    checklist = create_checklist(_gold_sop(), _visual_review())
    validate_document(checklist, "mobile_checklist.schema.json")
    assert checklist["interaction_mode"] == "ONE_STEP_PER_SCREEN"
    assert checklist["progress"] == {
        "total_items": 13,
        "completed_items": 0,
        "status": "NOT_STARTED",
    }
    s04 = next(item for item in checklist["items"] if item["step_id"] == "S04")
    assert s04["keyframe"]["visual_status"] == "NOT_VISIBLE"
    assert s04["keyframe"]["evidence_id"] == "E013"
    assert len(s04["evidence_details"]) == len(s04["evidence_ids"])


def test_checklist_session_records_completion_feedback_and_private_mode(
    tmp_path: Path,
) -> None:
    store = ChecklistSessionStore(tmp_path / "sessions")
    session = store.create(create_checklist(_gold_sop()))
    path = tmp_path / "sessions" / f"{session['session_id']}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    updated = store.update_item(
        session["session_id"],
        "S01",
        completed=True,
        feedback_category="STEP_BLOCKED",
        feedback_comment="导纸夹需要重新调节",
    )
    assert updated["status"] == "IN_PROGRESS"
    assert updated["progress"]["completed_items"] == 1
    assert updated["completion_log"][0]["action"] == "COMPLETED"
    assert updated["feedback_log"][0]["category"] == "STEP_BLOCKED"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_checklist_session_rejects_empty_or_oversized_feedback(tmp_path: Path) -> None:
    store = ChecklistSessionStore(tmp_path / "sessions")
    session = store.create(create_checklist(_gold_sop()))
    with pytest.raises(ValueError, match="不能为空"):
        store.update_item(
            session["session_id"],
            "S01",
            feedback_category="OTHER",
            feedback_comment=" ",
        )
    with pytest.raises(ValueError, match="500"):
        store.update_item(
            session["session_id"],
            "S01",
            feedback_category="OTHER",
            feedback_comment="问" * 501,
        )
    with pytest.raises(ValueError, match="必须为文本"):
        store.update_item(
            session["session_id"],
            "S01",
            feedback_category="OTHER",
            feedback_comment=123,  # type: ignore[arg-type]
        )
