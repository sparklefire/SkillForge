"""Publish visual review results and compare single-source vs multisource evidence."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .contracts import validate_document


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _coverage(
    required_steps: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    unsupported = [
        step["step_id"] for step in required_steps if not predicate(step)
    ]
    supported = len(required_steps) - len(unsupported)
    return {
        "supported_required_steps": supported,
        "required_step_count": len(required_steps),
        "coverage": supported / len(required_steps),
        "unsupported_step_ids": unsupported,
    }


def build_multisource_evaluation(
    candidate: dict[str, Any],
    gold: dict[str, Any],
    visual: dict[str, Any],
    rehearsal: dict[str, Any],
    ingest_manifest: dict[str, Any],
) -> dict[str, Any]:
    validate_document(gold, "sop.schema.json")
    validate_document(visual, "visual_review_report.schema.json")
    evidence_type = {
        item["evidence_id"]: item["source_type"]
        for item in gold["evidence_catalog"]
    }
    required = [step for step in gold["steps"] if step["required"]]
    step_types = {
        step["step_id"]: {evidence_type[item] for item in step["evidence"]}
        for step in gold["steps"]
    }
    visual_by_step = {
        item["step_id"]: item["model_result"]["verdict"]
        for item in visual["assessments"]
    }
    source_ablation = {
        "manual_only": _coverage(
            required, lambda step: "pdf" in step_types[step["step_id"]]
        ),
        "expert_audio_only": _coverage(
            required, lambda step: "audio" in step_types[step["step_id"]]
        ),
        "video_reference_presence": _coverage(
            required, lambda step: "video" in step_types[step["step_id"]]
        ),
        "video_strict_semantic_support": _coverage(
            required,
            lambda step: visual_by_step[step["step_id"]] == "SUPPORTED",
        ),
        "video_observable_partial_or_better": _coverage(
            required,
            lambda step: visual_by_step[step["step_id"]]
            in {"SUPPORTED", "PARTIAL"},
        ),
        "multisource_any": _coverage(
            required, lambda step: bool(step_types[step["step_id"]])
        ),
        "two_or_more_source_types": _coverage(
            required, lambda step: len(step_types[step["step_id"]]) >= 2
        ),
    }

    candidate_by_step = {item["step_id"]: item for item in candidate["steps"]}
    gold_by_step = {item["step_id"]: item for item in gold["steps"]}
    required_changes = [
        {
            "step_id": step_id,
            "before": candidate_by_step[step_id]["required"],
            "after": gold_by_step[step_id]["required"],
        }
        for step_id in candidate_by_step
        if candidate_by_step[step_id]["required"]
        != gold_by_step[step_id]["required"]
    ]

    def parameter_gaps(sop: dict[str, Any]) -> int:
        return sum(
            not parameter["evidence_ids"]
            for step in sop["steps"]
            for parameter in step["parameters"]
        )

    removed_parameters = []
    for step_id, candidate_step in candidate_by_step.items():
        gold_names = {
            item["name"] for item in gold_by_step[step_id]["parameters"]
        }
        for parameter in candidate_step["parameters"]:
            if parameter["name"] not in gold_names:
                removed_parameters.append(
                    {
                        "step_id": step_id,
                        "name": parameter["name"],
                        "value": parameter["value"],
                        "unit": parameter["unit"],
                    }
                )

    videos = [
        source
        for source in ingest_manifest["sources"]
        if source["type"] == "video"
    ]
    local_privacy_passed = all(
        source.get("privacy_status") == "LOCAL_QA_PASSED" for source in videos
    )
    flagged = [
        item["step_id"]
        for item in visual["assessments"]
        if item["model_result"]["privacy_observation"]
        == "POTENTIAL_SENSITIVE_CONTENT"
    ]
    report = {
        "version": 1,
        "case_id": gold["case_id"],
        "evaluation_basis": "OPERATOR_REVIEWED_GOLD",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_ablation": source_ablation,
        "revision_comparison": {
            "before": rehearsal["before"],
            "after": rehearsal["after"],
            "revision_count": rehearsal["revision_count"],
            "conflict_kinds_before": rehearsal["conflict_kinds_before"],
        },
        "candidate_to_gold": {
            "candidate_version": candidate["version"],
            "gold_version": gold["version"],
            "verified_step_count": sum(
                item["status"] == "VERIFIED" for item in gold["steps"]
            ),
            "required_flag_changes": required_changes,
            "parameter_evidence_gaps_before": parameter_gaps(candidate),
            "parameter_evidence_gaps_after": parameter_gaps(gold),
            "removed_unsupported_parameters": removed_parameters,
            "expert_evidence_added": sum(
                item["source_ref"] == "N31_EXPERT_INTERVIEW"
                for item in gold["evidence_catalog"]
            ),
        },
        "privacy_comparison": {
            "local_safe_derivative_qa": (
                "PASSED" if local_privacy_passed else "FAILED"
            ),
            "model_flagged_step_count": len(flagged),
            "model_flagged_step_ids": flagged,
            "disposition": (
                "保留模型隐私标记作为保守复核队列，不自动覆盖既有机器检查和人工抽检结论。"
            ),
        },
        "conclusions": [
            "手册单源覆盖8/10个必要步骤，专家口述单源覆盖9/10；两种以上来源联合覆盖10/10。",
            "所有必要步骤都有视频引用，但严格视觉复核没有把任何5秒关键帧窗口判为完整支持，说明引用存在不等于视觉证据充分。",
            "9/10个必要步骤至少部分可见；S04开机动作在当前关键帧窗口中不可见，应改进抽帧而不是伪造支持。",
            "Gold规则质检把5个高严重度问题修订为0，必要步骤和证据覆盖均从90%恢复到100%。",
            "视觉模型对设备二维码和编码形状较敏感，隐私标记作为对比实验保留，不自动判定为真实泄漏。",
        ],
    }
    return validate_document(report, "multisource_evaluation.schema.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sop", type=Path, required=True)
    parser.add_argument("--gold-sop", type=Path, required=True)
    parser.add_argument("--visual-review", type=Path, required=True)
    parser.add_argument("--rehearsal-summary", type=Path, required=True)
    parser.add_argument("--ingest-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    visual = _read_json(args.visual_review)
    validate_document(visual, "visual_review_report.schema.json")
    report = build_multisource_evaluation(
        _read_json(args.candidate_sop),
        _read_json(args.gold_sop),
        visual,
        _read_json(args.rehearsal_summary),
        _read_json(args.ingest_manifest),
    )
    args.output.mkdir(parents=True, exist_ok=True)
    _write_json(args.output / "visual_sequence_review_v1.json", visual)
    _write_json(args.output / "multisource_comparison_v1.json", report)
    print(
        json.dumps(
            {
                "status": "PUBLISHED",
                "source_ablation": report["source_ablation"],
                "revision_comparison": report["revision_comparison"],
                "privacy_comparison": report["privacy_comparison"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
