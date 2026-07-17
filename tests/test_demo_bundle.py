import json
import shutil
from pathlib import Path

import pytest

from scripts.build_n31_demo_bundle import build_bundle
from skillforge.contracts import validate_document


ROOT = Path(__file__).resolve().parents[1]


def test_builds_asset_free_gold_bundle(tmp_path) -> None:
    source = ROOT / "cases/n31/demo_bundle"
    output = tmp_path / "bundle"
    manifest = build_bundle(
        source,
        output,
        grounding_gate=(
            ROOT / "cases/n31/evaluations/deterministic_grounding_gate_v1.json"
        ),
        semantic_review=ROOT / "cases/n31/evaluations/semantic_review_v1.json",
        selective_rebuild=ROOT / "cases/n31/evaluations/selective_rebuild_v1.json",
    )
    assert manifest["gold_status"] == "GOLD"
    assert manifest["metrics_status"] == "FINAL"
    assert manifest["contains_raw_media"] is False
    assert manifest["contains_credentials"] is False
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["after"]["severe_error_count"] == 0
    before = json.loads((output / "before_sop.json").read_text(encoding="utf-8"))
    assert "evidence_catalog" not in before
    views = json.loads((output / "sop_views.json").read_text(encoding="utf-8"))
    assert views["artifact_type"] == "SOP_VIEWS"
    checklist = json.loads((output / "checklist.json").read_text(encoding="utf-8"))
    assert checklist["interaction_mode"] == "ONE_STEP_PER_SCREEN"
    quiz = json.loads((output / "quiz.json").read_text(encoding="utf-8"))
    validate_document(quiz, "training_quiz.schema.json")
    assert quiz["coverage"]["category_count"] == 5
    grounding_gate = json.loads(
        (output / "grounding_gate.json").read_text(encoding="utf-8")
    )
    validate_document(grounding_gate, "grounding_gate_report.schema.json")
    assert grounding_gate["summary"]["residual_conflict_count"] == 0
    semantic_review = json.loads(
        (output / "semantic_review.json").read_text(encoding="utf-8")
    )
    validate_document(semantic_review, "semantic_review_report.schema.json")
    assert semantic_review["summary"]["step_count"] == 13
    assert semantic_review["summary"]["automatic_gold_changes"] == 0
    selective = json.loads(
        (output / "selective_rebuild.json").read_text(encoding="utf-8")
    )
    validate_document(selective, "selective_rebuild_report.schema.json")
    assert selective["status"] == "PASSED"
    assert selective["summary"]["quiz_question_count"] == 1
    assert selective["summary"]["video_scene_count"] == 7
    workflow = json.loads((output / "workflow.json").read_text(encoding="utf-8"))
    validate_document(workflow, "workflow_run.schema.json")
    assert workflow["state"] == "COMPLETED"
    assert workflow["stage_attempts"]["VERIFYING"] == 2


def test_rejects_stale_optional_artifact_before_publication(tmp_path) -> None:
    source = tmp_path / "source"
    shutil.copytree(ROOT / "cases/n31/demo_bundle", source)
    checklist_path = source / "checklist.json"
    checklist = json.loads(checklist_path.read_text(encoding="utf-8"))
    checklist.pop("artifact_type")
    checklist_path.write_text(json.dumps(checklist), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact_type"):
        build_bundle(source, tmp_path / "output")


def test_rejects_selective_report_bound_to_different_audit(tmp_path) -> None:
    source = tmp_path / "source"
    # Use the tracked, schema-gated fixture instead of a developer-specific
    # ignored rehearsal cache, which may legitimately be from an older run.
    shutil.copytree(ROOT / "cases/n31/demo_bundle", source)
    audit_path = source / "revision_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["changes"][0]["reason"] += "（被篡改）"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="revision_audit_sha256"):
        build_bundle(
            source,
            tmp_path / "bundle",
            selective_rebuild=ROOT / "cases/n31/evaluations/selective_rebuild_v1.json",
        )
