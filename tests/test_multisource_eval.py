import json
import copy
from pathlib import Path

from skillforge.multisource_eval import build_multisource_evaluation


ROOT = Path(__file__).resolve().parents[1]


def test_n31_multisource_ablation_uses_gold_and_visual_verdicts() -> None:
    gold = json.loads(
        (ROOT / "cases/n31/gold/gold_sop.json").read_text(encoding="utf-8")
    )
    candidate = copy.deepcopy(gold)
    candidate["version"] = 1
    for parameter in candidate["steps"][0]["parameters"]:
        parameter["evidence_ids"] = []
    visual = json.loads(
        (
            ROOT
            / "cases/n31/evaluations/visual_sequence_review_v1.json"
        ).read_text(encoding="utf-8")
    )
    rehearsal = {
        "before": {
            "conflict_count": 5,
            "evidence_supported_required_steps": 0.9,
            "required_step_coverage": 0.9,
            "severe_error_count": 5,
        },
        "after": {
            "conflict_count": 0,
            "evidence_supported_required_steps": 1.0,
            "required_step_coverage": 1.0,
            "severe_error_count": 0,
        },
        "revision_count": 4,
        "conflict_kinds_before": [
            "MISSING_STEP",
            "MISSING_PREREQUISITE",
            "ORDER_ERROR",
            "UNSUPPORTED_PARAMETER",
            "UNSUPPORTED_TOOL",
        ],
    }
    ingest = json.loads(
        (ROOT / "cases/n31/ingest_manifest.json").read_text(encoding="utf-8")
    )
    report = build_multisource_evaluation(
        candidate,
        gold,
        visual,
        rehearsal,
        ingest,
    )
    assert report["source_ablation"]["manual_only"]["coverage"] == 0.8
    assert report["source_ablation"]["expert_audio_only"]["coverage"] == 0.9
    assert report["source_ablation"]["two_or_more_source_types"]["coverage"] == 1.0
    assert (
        report["source_ablation"]["video_strict_semantic_support"]["coverage"]
        == 0.0
    )
    assert (
        report["source_ablation"]["video_observable_partial_or_better"]["coverage"]
        == 0.9
    )
    assert report["candidate_to_gold"]["parameter_evidence_gaps_before"] == 2
    assert report["candidate_to_gold"]["parameter_evidence_gaps_after"] == 0
    assert report["privacy_comparison"]["local_safe_derivative_qa"] == "PASSED"
