import copy
import json
from pathlib import Path

import pytest

from skillforge.contracts import validate_document
from skillforge.source_candidates import (
    SourceCandidateError,
    synthesize_source_candidates,
)


ROOT = Path(__file__).resolve().parents[1]


def _fixture() -> tuple[dict, dict, dict]:
    source_plan = json.loads(
        (ROOT / "cases/n31/source_candidate_plan.json").read_text(encoding="utf-8")
    )
    candidate_plan = json.loads(
        (ROOT / "cases/n31/candidate_sop_plan.json").read_text(encoding="utf-8")
    )
    catalog = json.loads(
        (ROOT / "cases/n31/gold/gold_sop.json").read_text(encoding="utf-8")
    )
    return source_plan, candidate_plan, catalog


def test_n31_source_candidates_are_split_merged_and_ordered() -> None:
    source_plan, candidate_plan, catalog = _fixture()
    report, sop = synthesize_source_candidates(source_plan, candidate_plan, catalog)

    validate_document(report, "source_candidate_synthesis.schema.json")
    validate_document(sop, "sop.schema.json")
    assert report["status"] == "NEEDS_REVIEW"
    assert report["uses_gold_step_text"] is False
    assert report["data_policy"]["external_model_calls"] == 0
    assert report["summary"] == {
        "source_candidate_count": 33,
        "source_candidate_counts": {"video": 18, "pdf": 7, "audio": 8},
        "deduplicated_candidate_ids": [],
        "ordered_step_count": 13,
        "phase_counts": {
            "PREPARATION": 3,
            "EXECUTION": 6,
            "VERIFICATION": 4,
            "RESET": 0,
        },
        "coarse_candidate_count": 8,
        "coarse_fragment_count": 18,
        "fine_candidate_count": 8,
        "coarse_split_group_count": 10,
        "fine_merge_group_count": 4,
        "synonym_merge_group_count": 12,
        "multi_source_step_count": 12,
        "three_source_step_count": 10,
        "irreversible_step_ids": ["S11", "S12"],
        "recovery_step_count": 12,
        "confidence_band_counts": {"HIGH": 6, "MEDIUM": 6, "LOW": 1},
        "review_route_counts": {
            "AUTO_VERIFY": 6,
            "VERIFIER_QUEUE": 6,
            "HUMAN_REVIEW_REQUIRED": 1,
        },
        "conflicted_step_ids": ["S04"],
        "low_confidence_step_ids": ["S04"],
        "all_steps_evidence_grounded": True,
        "graph_acyclic": True,
    }
    assert [step["step_id"] for step in report["ordered_steps"]] == [
        f"S{index:02d}" for index in range(1, 14)
    ]
    s04 = next(step for step in report["ordered_steps"] if step["step_id"] == "S04")
    assert s04["source_types"] == ["pdf"]
    assert s04["candidate_ids"] == ["SC021"]
    assert "E014" not in s04["evidence_ids"]
    assert s04["confidence"] == 0.691
    assert s04["confidence_assessment"]["band"] == "LOW"
    assert s04["confidence_assessment"]["route"] == "HUMAN_REVIEW_REQUIRED"
    assert s04["confidence_assessment"]["observation_ids"] == ["NO001"]
    assert s04["confidence_assessment"]["observation_penalty"] == 0.108
    assert sop["steps"][0]["parameters"][0]["evidence_ids"] == ["E144"]
    assert sop["steps"][6]["prerequisites"] == ["S04"]


def test_exact_source_candidate_duplicate_is_removed_with_audit_id() -> None:
    source_plan, candidate_plan, catalog = _fixture()
    duplicate = copy.deepcopy(source_plan["candidates"][0])
    duplicate["candidate_id"] = "SC034"
    duplicate["raw_claim"] = "同一证据和动作的重复抽取结果。"
    source_plan["candidates"].append(duplicate)

    report, _ = synthesize_source_candidates(source_plan, candidate_plan, catalog)

    assert report["summary"]["source_candidate_count"] == 33
    assert report["summary"]["deduplicated_candidate_ids"] == ["SC034"]
    assert all(item["candidate_id"] != "SC034" for item in report["source_candidates"])


def test_source_candidate_rejects_cross_source_evidence() -> None:
    source_plan, candidate_plan, catalog = _fixture()
    source_plan["candidates"][0]["evidence_ids"] = ["E096"]

    with pytest.raises(SourceCandidateError, match="来源类型不匹配"):
        synthesize_source_candidates(source_plan, candidate_plan, catalog)

    source_plan, candidate_plan, catalog = _fixture()
    source_plan["negative_observations"][0]["evidence_ids"] = ["E142"]
    with pytest.raises(SourceCandidateError, match="来源类型不匹配"):
        synthesize_source_candidates(source_plan, candidate_plan, catalog)


def test_source_candidate_rejects_dependency_cycle() -> None:
    source_plan, candidate_plan, catalog = _fixture()
    candidate_plan["steps"][0]["prerequisites"] = ["S13"]

    with pytest.raises(SourceCandidateError, match="存在环"):
        synthesize_source_candidates(source_plan, candidate_plan, catalog)

    source_plan, candidate_plan, catalog = _fixture()
    candidate_plan["steps"][1]["recovery"]["target_step_id"] = "S13"
    with pytest.raises(SourceCandidateError, match="目标必须早于"):
        synthesize_source_candidates(source_plan, candidate_plan, catalog)
