"""Run the safe offline P0 evidence-verification-revision demonstration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .creator import create_checklist, create_quiz, create_sop_views
from .observability import StructuredLogger
from .revision import revise_sop
from .synthetic_case import inject_faults
from .verifier import metrics, verify_sop
from .workflow import WorkflowState, WorkflowStateMachine


ROOT = Path(__file__).resolve().parents[2]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_demo(case_dir: Path, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    logger = StructuredLogger(output_dir / "run.jsonl")
    workflow = WorkflowStateMachine(logger)

    workflow.transition(WorkflowState.INGESTING, "载入明确标记的模拟案例")
    reference = read_json(case_dir / "reference_sop.json")
    constraints = read_json(case_dir / "constraints.json")
    fault_spec = read_json(case_dir / "fault_injection.json")
    draft = inject_faults(reference, fault_spec)
    validate_document(draft, "sop.schema.json")
    validate_document(reference, "sop.schema.json")

    workflow.transition(WorkflowState.EXTRACTING, "复用模拟案例内的证据定位")
    workflow.transition(WorkflowState.PLANNING, "载入八到十五步 SOP 草稿")
    workflow.transition(WorkflowState.CREATING, "冻结首轮草稿用于前后对比")
    write_json(output_dir / "before_sop.json", draft)

    workflow.transition(WorkflowState.VERIFYING, "执行确定性规则质检")
    initial_report = verify_sop(draft, reference, constraints, iteration=1)
    before_metrics = metrics(draft, initial_report, constraints)
    write_json(output_dir / "initial_conflicts.json", initial_report)

    if initial_report["conflicts"]:
        workflow.transition(WorkflowState.REVISING, "按证据执行首轮局部修订")
        revised, audit = revise_sop(
            draft,
            initial_report,
            reference,
            constraints,
            iteration=1,
        )
        write_json(output_dir / "revision_audit.json", audit)
        write_json(output_dir / "after_sop.json", revised)
        workflow.transition(WorkflowState.VERIFYING, "重新执行全量规则质检")
    else:
        revised = draft
        audit = None

    final_report = verify_sop(revised, reference, constraints, iteration=2)
    after_metrics = metrics(revised, final_report, constraints)
    write_json(output_dir / "final_conflicts.json", final_report)

    if after_metrics["severe_error_count"]:
        workflow.transition(WorkflowState.NEEDS_REVIEW, "仍存在高严重度问题")
    else:
        workflow.transition(WorkflowState.RENDERING, "生成检查清单和培训测验")
        write_json(output_dir / "sop_views.json", create_sop_views(revised))
        write_json(output_dir / "checklist.json", create_checklist(revised))
        write_json(output_dir / "quiz.json", create_quiz(revised))
        workflow.transition(WorkflowState.COMPLETED, "P0 模拟闭环完成")

    summary = {
        "case_id": revised["case_id"],
        "synthetic": True,
        "workflow_state": workflow.state.value,
        "before": before_metrics,
        "after": after_metrics,
        "revision_count": len(audit["changes"]) if audit else 0,
    }
    workflow.write_checkpoint(output_dir / "workflow.json")
    write_json(output_dir / "summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        type=Path,
        default=ROOT / "cases" / "demo_case" / "synthetic",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "outputs" / "demo_run",
    )
    args = parser.parse_args()
    summary = run_demo(args.case, args.output)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["workflow_state"] == WorkflowState.COMPLETED.value else 1


if __name__ == "__main__":
    raise SystemExit(main())
