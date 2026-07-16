import json

import pytest

from skillforge.candidate_sop import (
    build_candidate_sop,
    render_review_sheet,
    resolve_selector,
)
from skillforge.contracts import validate_document
from skillforge.creator import create_quiz


def _evidence(evidence_id: str, source_ref: str, page: int) -> dict:
    return {
        "evidence_id": evidence_id,
        "source_type": "pdf",
        "source_ref": source_ref,
        "claim": f"Page {page} source fact.",
        "locator": {"page": page, "paragraph": "page"},
        "classification": "SOURCE_FACT",
        "relevance": 0.8,
        "confidence": 0.9,
        "review_status": "UNREVIEWED",
    }


def test_resolve_selector_requires_unique_match() -> None:
    catalog = [_evidence("E001", "MANUAL", 1), _evidence("E002", "MANUAL", 2)]
    assert resolve_selector(catalog, {"source_ref": "MANUAL", "page": 2}) == "E002"
    with pytest.raises(ValueError, match="实际 0 条"):
        resolve_selector(catalog, {"source_ref": "MANUAL", "page": 3})


def test_build_candidate_sop_and_review_queue() -> None:
    evidence = [_evidence(f"E{index:03d}", "MANUAL", index) for index in range(1, 9)]
    steps = []
    for index in range(1, 9):
        steps.append(
            {
                "step_id": f"S{index:02d}",
                "title": f"Step {index}",
                "action": "Perform an action.",
                "object": "Object",
                "prerequisites": [f"S{index - 1:02d}"] if index > 1 else [],
                "tools": [],
                "parameters": (
                    [
                        {
                            "name": "Unverified setting",
                            "value": 1,
                            "unit": "mm",
                            "evidence_selectors": [],
                        }
                    ]
                    if index == 1
                    else []
                ),
                "warnings": [],
                "success_check": "Action is complete.",
                "evidence_selectors": [
                    {"source_ref": "MANUAL", "page": index}
                ],
                "confidence": 0.7,
                "required": True,
                "review_reasons": ["Human confirmation required."],
            }
        )
    sop, review = build_candidate_sop(
        {
            "case_id": "real_case",
            "title": "Candidate",
            "output_version": 1,
            "steps": steps,
        },
        {
            "case_id": "real_case",
            "synthetic": False,
            "evidence": evidence,
        },
    )
    validate_document(sop, "sop.schema.json")
    assert len(sop["steps"]) == 8
    assert all(step["status"] == "NEEDS_REVIEW" for step in sop["steps"])
    assert review["gold_status"] == "NOT_GOLD"
    assert review["external_model_calls"] == 0
    assert review["items"][0]["parameter_evidence_gaps"] == ["Unverified setting"]
    rendered = render_review_sheet(sop, review)
    assert "候选、非 Gold" in rendered
    assert "E001 MANUAL PDF第1页" in rendered
    assert "参数缺证据：Unverified setting" in rendered
    sop["steps"][0]["success_check"] = "Action is complete。"
    assert "。。" not in create_quiz(sop)["questions"][0]["prompt"]


def test_candidate_sop_rejects_synthetic_catalog() -> None:
    with pytest.raises(ValueError, match="不能使用模拟"):
        build_candidate_sop(
            {"case_id": "case", "title": "Candidate", "steps": []},
            {"synthetic": True, "evidence": []},
        )
