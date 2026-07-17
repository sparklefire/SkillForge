import copy
import json
from pathlib import Path

from skillforge.selective_rebuild import build_selective_rebuild_report


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _report() -> dict:
    return build_selective_rebuild_report(
        _read("cases/n31/demo_bundle/before_sop.json"),
        _read("cases/n31/demo_bundle/after_sop.json"),
        _read("cases/n31/demo_bundle/revision_audit.json"),
        _read("cases/n31/gold/gold_sop.json"),
        _read("cases/n31/training_video_storyboard.json"),
    )


def test_n31_revision_is_bounded_to_affected_units() -> None:
    report = _report()
    assert report["status"] == "PASSED"
    assert [item["step_id"] for item in report["affected_steps"]] == [
        "S07",
        "S08",
        "S09",
        "S10",
        "S11",
        "S12",
        "S13",
    ]
    assert report["summary"] == {
        "affected_step_count": 7,
        "content_changed_step_count": 3,
        "position_changed_step_count": 7,
        "rebuild_artifact_count": 6,
        "skipped_artifact_count": 0,
        "quiz_question_count": 1,
        "video_scene_count": 7,
        "whole_artifact_count": 1,
    }
    plans = {item["artifact_type"]: item for item in report["artifact_plans"]}
    assert plans["TRAINING_QUIZ"]["units"] == ["Q02"]
    assert plans["TRAINING_QUIZ"]["unchanged_unit_count"] == 4
    assert plans["TRAINING_VIDEO"]["units"] == [
        "V07",
        "V08",
        "V09",
        "V10",
        "V11",
        "V12",
        "V13",
    ]
    assert plans["TRAINING_VIDEO"]["unchanged_unit_count"] == 8
    assert plans["A4_POSTER"]["scope"] == "WHOLE_ARTIFACT"
    assert all(report["verification"].values())
    assert report["data_policy"]["external_model_calls"] == 0


def test_rejects_audit_that_does_not_reproduce_after() -> None:
    audit = _read("cases/n31/demo_bundle/revision_audit.json")
    audit = copy.deepcopy(audit)
    audit["changes"] = audit["changes"][:-1]
    try:
        build_selective_rebuild_report(
            _read("cases/n31/demo_bundle/before_sop.json"),
            _read("cases/n31/demo_bundle/after_sop.json"),
            audit,
            _read("cases/n31/gold/gold_sop.json"),
            _read("cases/n31/training_video_storyboard.json"),
        )
    except ValueError as exc:
        assert "不能精确重放" in str(exc)
    else:
        raise AssertionError("不完整审计不应通过")


def test_source_bindings_are_deterministic() -> None:
    first = _report()
    second = _report()
    assert first == second
    assert all(len(value) == 64 for value in first["source_bindings"].values())
