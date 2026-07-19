"""Evaluate evidence coverage/order for a review-only step discovery report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .demo import ROOT, read_json, write_json


def _sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evaluate_step_discovery(
    discovery: dict[str, Any], spec: dict[str, Any]
) -> dict[str, Any]:
    validate_document(discovery, "step_discovery_report.schema.json")
    validate_document(spec, "step_discovery_evaluation_spec.schema.json")
    if discovery["case_id"] != spec["case_id"]:
        raise ValueError("候选发现报告与评测规范case_id不一致")

    first_positions: dict[str, int] = {}
    for index, candidate in enumerate(discovery["ordered_candidates"]):
        for evidence_id in candidate["evidence_ids"]:
            first_positions.setdefault(evidence_id, index)

    expected = spec["ordered_evidence_ids"]
    observed = [item for item in expected if item in first_positions]
    violations = [
        f"{before}>{after}"
        for before, after in zip(expected, expected[1:])
        if before in first_positions
        and after in first_positions
        and first_positions[before] > first_positions[after]
    ]
    required_sources = set(spec["required_source_types"])
    observed_sources = {
        item["source_type"] for item in discovery["evidence_catalog"]
    } & required_sources
    candidates = discovery["ordered_candidates"]
    review_count = sum(
        item["review_status"] == "HUMAN_REVIEW_REQUIRED" for item in candidates
    )

    failures: list[str] = []
    missing = sorted(set(expected) - set(observed))
    if missing:
        failures.append(f"缺少期望Evidence: {missing}")
    if violations:
        failures.append(f"Evidence相邻顺序冲突: {violations}")
    missing_sources = sorted(required_sources - observed_sources)
    if missing_sources:
        failures.append(f"缺少来源类型: {missing_sources}")
    if review_count != len(candidates):
        failures.append("存在未进入人工复核态的候选步骤")
    if discovery["guardrails"]["automatic_gold_changes"] != 0:
        failures.append("候选发现不应自动修改Gold")

    report = {
        "version": 1,
        "case_id": discovery["case_id"],
        "report_id": "STEP_DISCOVERY_EVALUATION_V1",
        "status": "PASSED" if not failures else "FAILED",
        "discovery_report_sha256": _sha256(discovery),
        "uses_gold_as_model_input": False,
        "evaluation_reference_not_sent_to_model": True,
        "semantic_text_quality_evaluated": False,
        "metrics": {
            "expected_evidence_count": len(expected),
            "observed_evidence_count": len(observed),
            "evidence_recall": round(len(observed) / len(expected), 6),
            "ordering_violation_count": len(violations),
            "required_source_type_count": len(required_sources),
            "observed_source_type_count": len(observed_sources),
            "source_type_coverage": round(
                len(observed_sources) / len(required_sources), 6
            ),
            "candidate_count": len(candidates),
            "review_containment_rate": round(review_count / len(candidates), 6),
            "automatic_gold_changes": 0,
        },
        "failures": failures,
        "limitations": [
            "本评测只核验证据覆盖、相邻顺序、来源覆盖和人工复核边界。",
            "标题、动作措辞、步骤拆分粒度和领域正确性仍必须由人工审核。",
            "评测规范只在模型输出完成后使用，未作为候选发现模型输入。",
        ],
        "data_policy": {
            "contains_raw_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "external_model_calls": 0,
        },
    }
    validate_document(report, "step_discovery_evaluation.schema.json")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--discovery",
        type=Path,
        default=ROOT / "outputs" / "step_discovery" / "report.json",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=(
            ROOT
            / "cases"
            / "demo_case"
            / "synthetic"
            / "discovery_evaluation_spec.json"
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "outputs" / "step_discovery" / "evaluation.json",
    )
    args = parser.parse_args()
    report = evaluate_step_discovery(read_json(args.discovery), read_json(args.spec))
    write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
