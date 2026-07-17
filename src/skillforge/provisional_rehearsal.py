"""Run a real-source P0 rehearsal against a non-Gold candidate SOP."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .creator import create_checklist, create_quiz, create_sop_views
from .demo import read_json, write_json
from .observability import StructuredLogger
from .revision import revise_sop
from .synthetic_case import inject_faults
from .verifier import metrics, verify_sop
from .workflow import WorkflowState, WorkflowStateMachine


def run_provisional_rehearsal(
    candidate_sop_path: Path,
    constraints_path: Path,
    fault_spec_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = StructuredLogger(output_dir / "run.jsonl")
    workflow = WorkflowStateMachine(logger)

    workflow.transition(WorkflowState.INGESTING, "载入真实素材生成的候选SOP")
    reference = read_json(candidate_sop_path)
    constraints = read_json(constraints_path)
    fault_spec = read_json(fault_spec_path)
    if constraints.get("evaluation_basis") != "CANDIDATE_NOT_GOLD":
        raise ValueError("真实案例彩排必须明确标记 CANDIDATE_NOT_GOLD")
    if fault_spec.get("controlled_rehearsal") is not True:
        raise ValueError("错误注入必须明确标记 controlled_rehearsal=true")
    validate_document(reference, "sop.schema.json")

    workflow.transition(WorkflowState.EXTRACTING, "复用本地Evidence Catalog")
    workflow.transition(WorkflowState.PLANNING, "载入13步非Gold候选SOP")
    draft = inject_faults(reference, fault_spec)
    validate_document(draft, "sop.schema.json")
    workflow.transition(WorkflowState.CREATING, "冻结受控错误草稿")
    write_json(output_dir / "before_sop.json", draft)

    workflow.transition(WorkflowState.VERIFYING, "执行真实证据引用的确定性质检")
    initial_report = verify_sop(draft, reference, constraints, iteration=1)
    before_metrics = metrics(draft, initial_report, constraints)
    write_json(output_dir / "initial_conflicts.json", initial_report)

    if initial_report["conflicts"]:
        workflow.transition(WorkflowState.REVISING, "按引用证据执行局部修订")
        revised, audit = revise_sop(
            draft,
            initial_report,
            reference,
            constraints,
            iteration=1,
        )
        write_json(output_dir / "revision_audit.json", audit)
        write_json(output_dir / "after_sop.json", revised)
        workflow.transition(WorkflowState.VERIFYING, "重新执行确定性质检")
    else:
        revised = draft
        audit = None

    final_report = verify_sop(revised, reference, constraints, iteration=2)
    after_metrics = metrics(revised, final_report, constraints)
    write_json(output_dir / "final_conflicts.json", final_report)
    if after_metrics["severe_error_count"]:
        workflow.transition(WorkflowState.NEEDS_REVIEW, "仍有确定性高严重度问题")
    else:
        workflow.transition(WorkflowState.RENDERING, "生成候选检查清单和测验")
        write_json(output_dir / "sop_views.json", create_sop_views(revised))
        write_json(output_dir / "checklist.json", create_checklist(revised))
        write_json(output_dir / "quiz.json", create_quiz(revised))
        workflow.transition(WorkflowState.COMPLETED, "非Gold真实案例闭环彩排完成")

    summary = {
        "case_id": revised["case_id"],
        "synthetic": False,
        "evaluation_basis": "CANDIDATE_NOT_GOLD",
        "gold_status": "NOT_GOLD",
        "metrics_status": "PROVISIONAL_ONLY",
        "external_model_calls": 0,
        "workflow_state": workflow.state.value,
        "before": before_metrics,
        "after": after_metrics,
        "revision_count": len(audit["changes"]) if audit else 0,
        "conflict_kinds_before": [
            item["kind"] for item in initial_report["conflicts"]
        ],
        "human_review_required": True,
    }
    write_json(output_dir / "workflow.json", workflow.snapshot())
    write_json(output_dir / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sop", type=Path, required=True)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--faults", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = run_provisional_rehearsal(
        args.candidate_sop,
        args.constraints,
        args.faults,
        args.output,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["workflow_state"] == WorkflowState.COMPLETED.value else 1


if __name__ == "__main__":
    raise SystemExit(main())
