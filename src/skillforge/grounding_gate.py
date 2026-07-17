"""Exercise deterministic rejection and revision of ungrounded SOP content."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .demo import ROOT, read_json, write_json
from .revision import revise_sop
from .verifier import verify_sop


REPORT_ID = "DETERMINISTIC_GROUNDING_GATE_V1"


def _step(sop: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(item for item in sop["steps"] if item["step_id"] == step_id)


def _mutate(
    reference: dict[str, Any], scenario_id: str
) -> tuple[dict[str, Any], dict[str, str]]:
    candidate = copy.deepcopy(reference)
    target = _step(candidate, "S01")
    if scenario_id == "CROSS_STEP_ALLOWED_TOOL":
        target["tools"].append("本批标签纸")
        return candidate, {
            "field": "tools",
            "expected_kind": "UNSUPPORTED_TOOL",
            "summary": "向S01加入全局允许、但仅在其他步骤有依据的工具“本批标签纸”",
        }
    if scenario_id == "ALLOWED_PARAMETER_WRONG_VALUE":
        target["parameters"][0]["value"] = 999
        return candidate, {
            "field": "parameters",
            "expected_kind": "UNSUPPORTED_PARAMETER",
            "summary": "保留参数名和Evidence，但把标签宽度从72毫米篡改为999毫米",
        }
    if scenario_id == "UNGROUNDED_WARNING":
        target["warnings"].append("操作时佩戴护目镜可避免所有风险。")
        return candidate, {
            "field": "warnings",
            "expected_kind": "UNSUPPORTED_SAFETY_CLAIM",
            "summary": "加入当前步骤来源中不存在的安全提示",
        }
    if scenario_id == "ABSOLUTE_SAFETY_PROMISE":
        target["success_check"] += " 完成以上检查即可保证100%安全。"
        return candidate, {
            "field": "success_check",
            "expected_kind": "UNSUPPORTED_SAFETY_CLAIM",
            "summary": "在完成标志中加入无来源的100%安全承诺",
        }
    raise ValueError(f"未知场景: {scenario_id}")


def build_grounding_gate(
    gold_sop_path: Path,
    constraints_path: Path,
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Run four isolated tamper cases and prove that each is locally restored."""

    reference = read_json(gold_sop_path)
    constraints = read_json(constraints_path)
    validate_document(reference, "sop.schema.json")
    baseline = verify_sop(reference, reference, constraints, iteration=1)
    if baseline["conflicts"]:
        raise ValueError("Gold SOP必须先通过确定性质检，才能建立无来源内容门禁")

    scenario_ids = (
        "CROSS_STEP_ALLOWED_TOOL",
        "ALLOWED_PARAMETER_WRONG_VALUE",
        "UNGROUNDED_WARNING",
        "ABSOLUTE_SAFETY_PROMISE",
    )
    scenarios: list[dict[str, Any]] = []
    for scenario_id in scenario_ids:
        candidate, spec = _mutate(reference, scenario_id)
        validate_document(candidate, "sop.schema.json")
        target_before = copy.deepcopy(_step(candidate, "S01")[spec["field"]])
        initial = verify_sop(candidate, reference, constraints, iteration=1)
        revised, audit = revise_sop(
            candidate,
            initial,
            reference,
            constraints,
            iteration=1,
        )
        final = verify_sop(revised, reference, constraints, iteration=2)
        target_after = copy.deepcopy(_step(revised, "S01")[spec["field"]])
        reference_value = copy.deepcopy(_step(reference, "S01")[spec["field"]])
        expected_conflicts = [
            item
            for item in initial["conflicts"]
            if item["kind"] == spec["expected_kind"]
        ]
        evidence_ids = sorted(
            {
                evidence["evidence_id"]
                for conflict in expected_conflicts
                for evidence in conflict["evidence"]
            }
        )
        restored = target_after == reference_value
        passed = (
            len(initial["conflicts"]) == 1
            and len(expected_conflicts) == 1
            and len(audit["changes"]) == 1
            and not final["conflicts"]
            and restored
        )
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "status": "PASSED" if passed else "FAILED",
                "target_step_id": "S01",
                "mutation_field": spec["field"],
                "mutation_summary": spec["summary"],
                "expected_conflict_kind": spec["expected_kind"],
                "detected_conflict_ids": [
                    item["conflict_id"] for item in expected_conflicts
                ],
                "detected_conflict_kinds": [
                    item["kind"] for item in initial["conflicts"]
                ],
                "rejection_reason": (
                    expected_conflicts[0]["message"] if expected_conflicts else "未检出"
                ),
                "proposed_actions": [
                    item["proposed_action"] for item in expected_conflicts
                ],
                "revision_actions": [item["action"] for item in audit["changes"]],
                "reference_evidence_ids": evidence_ids,
                "before_value": target_before,
                "after_value": target_after,
                "reference_value": reference_value,
                "residual_conflict_count": len(final["conflicts"]),
                "restored": restored,
            }
        )

    passed_count = sum(item["status"] == "PASSED" for item in scenarios)
    report = {
        "version": 1,
        "case_id": reference["case_id"],
        "report_id": REPORT_ID,
        "status": "PASSED" if passed_count == len(scenarios) else "FAILED",
        "model_calls": 0,
        "scenarios": scenarios,
        "summary": {
            "scenario_count": len(scenarios),
            "passed_count": passed_count,
            "detected_count": sum(bool(item["detected_conflict_ids"]) for item in scenarios),
            "revised_count": sum(item["restored"] for item in scenarios),
            "residual_conflict_count": sum(
                item["residual_conflict_count"] for item in scenarios
            ),
        },
        "data_policy": {
            "external_model_calls": 0,
            "contains_raw_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
        },
    }
    validate_document(report, "grounding_gate_report.schema.json")
    if output_path is not None:
        write_json(output_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gold-sop",
        type=Path,
        default=ROOT / "cases/n31/gold/gold_sop.json",
    )
    parser.add_argument(
        "--constraints",
        type=Path,
        default=ROOT / "cases/n31/gold/constraints.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "cases/n31/evaluations/deterministic_grounding_gate_v1.json",
    )
    args = parser.parse_args()
    report = build_grounding_gate(
        args.gold_sop,
        args.constraints,
        output_path=args.output,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
