import copy
import json
from pathlib import Path

import pytest

from skillforge.contracts import validate_document
from skillforge.pitch import (
    PHASE_ORDER,
    _check_demo_modes,
    _check_metrics,
    _check_runtime_benchmark,
    _check_timeline,
    build_readiness,
)


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = ROOT / "cases/n31/pitch_runbook.json"


def _runbook() -> dict:
    return json.loads(
        (ROOT / "cases/n31/pitch_runbook.json").read_text(encoding="utf-8")
    )


def test_pitch_runbook_is_valid_and_exactly_three_minutes() -> None:
    runbook = validate_document(_runbook(), "pitch_runbook.schema.json")
    assert [item["phase"] for item in runbook["segments"]] == PHASE_ORDER
    assert _check_timeline(runbook)["status"] == "PASSED"
    assert runbook["segments"][0]["start_ms"] == 0
    assert runbook["segments"][-1]["end_ms"] == 180_000


def test_pitch_timeline_rejects_a_gap() -> None:
    runbook = copy.deepcopy(_runbook())
    runbook["segments"][2]["start_ms"] += 1_000
    result = _check_timeline(runbook)
    assert result["status"] == "FAILED"
    assert any("不连续" in detail for detail in result["details"])


def test_pitch_declares_live_preprocessed_and_offline_fallbacks() -> None:
    runbook = _runbook()
    assert [item["mode"] for item in runbook["demo_modes"]] == [
        "LIVE",
        "PREPROCESSED",
        "OFFLINE",
    ]
    assert all(item["expected"]["after_errors"] == 0 for item in runbook["demo_modes"])
    assert all("Docker" not in item["command"] for item in runbook["demo_modes"])


def test_pitch_keeps_human_review_as_a_submission_gate() -> None:
    gates = {item["gate_id"]: item for item in _runbook()["human_gates"]}
    video_gate = gates["TRAINING_VIDEO_FULL_WATCH"]
    assert video_gate["status"] == "PENDING"
    assert video_gate["blocking_for_submission"] is True


def test_pitch_requires_dgx_runtime_benchmark() -> None:
    artifact_ids = {item["artifact_id"] for item in _runbook()["required_artifacts"]}
    assert "RUNTIME_BENCHMARK" in artifact_ids
    result = _check_runtime_benchmark(ROOT)
    assert result["status"] == "PASSED"
    assert result["assertions"]["twenty_measured_runs"] is True
    assert result["metrics"]["gold_workflow_median_ms"] > 0
    assert result["metrics"]["web_live_rerun_median_ms"] > 0


def test_pitch_requires_bounded_temporal_windows() -> None:
    artifact_ids = {item["artifact_id"] for item in _runbook()["required_artifacts"]}
    assert "TEMPORAL_WINDOWS" in artifact_ids
    result = _check_metrics(ROOT)
    assert result["status"] == "PASSED"
    assert result["assertions"]["temporal_windows_bounded"] is True


def test_pitch_requires_grounded_pdf_structure_report() -> None:
    artifact_ids = {item["artifact_id"] for item in _runbook()["required_artifacts"]}
    assert "PDF_STRUCTURE" in artifact_ids
    result = _check_metrics(ROOT)
    assert result["status"] == "PASSED"
    assert result["assertions"]["pdf_structure_grounded"] is True


def test_pitch_requires_grounded_source_candidate_synthesis() -> None:
    artifact_ids = {item["artifact_id"] for item in _runbook()["required_artifacts"]}
    assert "SOURCE_CANDIDATES" in artifact_ids
    result = _check_metrics(ROOT)
    assert result["status"] == "PASSED"
    assert result["assertions"]["source_candidates_grounded"] is True


def test_pitch_requires_traceable_training_package() -> None:
    artifact_ids = {item["artifact_id"] for item in _runbook()["required_artifacts"]}
    assert "SOP_VIEWS" in artifact_ids
    result = _check_metrics(ROOT)
    assert result["assertions"]["training_package_traceable"] is True
    assert result["assertions"]["checklist_previews_public"] is True


def test_pitch_requires_output_profile_and_low_bitrate_preview_mapping() -> None:
    result = _check_metrics(ROOT)
    assert result["status"] == "PASSED"
    assert result["assertions"]["output_profile_complete"] is True
    assert result["assertions"]["low_bitrate_previews_mapped"] is True


def test_pitch_requires_auditable_five_agent_tool_trace() -> None:
    artifact_ids = {item["artifact_id"] for item in _runbook()["required_artifacts"]}
    assert "AGENT_TOOL_TRACE" in artifact_ids
    result = _check_metrics(ROOT)
    assert result["status"] == "PASSED"
    assert result["assertions"]["five_agent_tool_trace"] is True


def test_pitch_requires_five_grounded_quiz_categories() -> None:
    result = _check_metrics(ROOT)
    assert result["assertions"]["training_quiz_grounded"] is True


def test_pitch_requires_closed_deterministic_grounding_gate() -> None:
    artifact_ids = {item["artifact_id"] for item in _runbook()["required_artifacts"]}
    assert "GROUNDING_GATE" in artifact_ids
    result = _check_metrics(ROOT)
    assert result["status"] == "PASSED"
    assert result["assertions"]["grounding_gate_closed"] is True


def test_pitch_requires_safe_high_reasoning_semantic_review() -> None:
    artifact_ids = {item["artifact_id"] for item in _runbook()["required_artifacts"]}
    assert "SEMANTIC_REVIEW" in artifact_ids
    result = _check_metrics(ROOT)
    assert result["status"] == "PASSED"
    assert result["assertions"]["semantic_review_grounded"] is True


def test_pitch_requires_bounded_selective_rebuild() -> None:
    artifact_ids = {item["artifact_id"] for item in _runbook()["required_artifacts"]}
    assert "SELECTIVE_REBUILD" in artifact_ids
    result = _check_metrics(ROOT)
    assert result["status"] == "PASSED"
    assert result["assertions"]["selective_rebuild_bounded"] is True


def test_pitch_requires_safe_evidence_navigation_and_operator_review() -> None:
    result = _check_demo_modes(_runbook(), ROOT)
    assert result["status"] == "PASSED"
    assert result["assertions"]["evidence_locator_safe"] is True
    assert result["assertions"]["operator_review_controls"] is True
    assert result["assertions"]["workflow_checkpoint_safe"] is True
    assert result["assertions"]["artifact_stage_rerun"] is True
    assert result["assertions"]["stage_resource_metrics_recorded"] is True
    assert result["assertions"]["conflict_decision_auditable"] is True
    assert result["assertions"]["safety_conflict_human_gate"] is True
    assert result["assertions"]["asr_correction_auditable"] is True
    assert result["assertions"]["video_preview_configured"] is True
    assert result["assertions"]["agent_tool_interface_auditable"] is True


def test_pitch_accepts_only_known_explicit_human_gate_confirmations() -> None:
    confirmed = {
        "TRAINING_VIDEO_FULL_WATCH",
        "FINAL_STAGE_REHEARSAL",
        "FINAL_RECORDING_REVIEW",
        "TEAM_ELIGIBILITY_CONFIRMED",
        "OFFICIAL_RULES_VERIFIED",
    }
    result = build_readiness(RUNBOOK, confirmed_gate_ids=confirmed)
    assert result["status"] == "READY_FOR_SUBMISSION"
    assert result["pending_human_gates"] == []
    with pytest.raises(ValueError, match="未知人工门禁确认"):
        build_readiness(RUNBOOK, confirmed_gate_ids={"NOT_A_REAL_GATE"})
