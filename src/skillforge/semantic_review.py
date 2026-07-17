"""High-reasoning semantic review over safe, structured Gold evidence."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .demo import ROOT, read_json, write_json
from .observability import StructuredLogger
from .step_plan import StepPlanClient, StepPlanError


DIMENSIONS = [
    "SOURCE_DISTORTION",
    "SOURCE_CONFLICT",
    "ORDERING_RISK",
    "EXCEPTION_OMISSION",
]
FORBIDDEN_PAYLOAD_MARKERS = (
    "/Users/",
    "/home/",
    "file://",
    "Authorization:",
    "Bearer ",
)


def build_safe_review_payload(
    gold: dict[str, Any], constraints: dict[str, Any]
) -> dict[str, Any]:
    """Project Gold into a text-only payload without files, pages, or transcripts."""

    evidence_map = {item["evidence_id"]: item for item in gold["evidence_catalog"]}
    referenced_ids = list(
        dict.fromkeys(
            evidence_id
            for step in gold["steps"]
            for evidence_id in step["evidence"]
        )
    )
    steps = [
        {
            key: step[key]
            for key in (
                "step_id",
                "title",
                "action",
                "object",
                "prerequisites",
                "tools",
                "parameters",
                "warnings",
                "success_check",
                "evidence",
                "required",
            )
        }
        for step in gold["steps"]
    ]
    evidence = []
    for evidence_id in referenced_ids:
        item = evidence_map[evidence_id]
        locator = item["locator"]
        safe_locator = (
            {"page": locator["page"]}
            if "page" in locator
            else {
                "start_ms": locator["start_ms"],
                "end_ms": locator["end_ms"],
            }
        )
        evidence.append(
            {
                "evidence_id": evidence_id,
                "source_type": item["source_type"],
                "source_ref": item["source_ref"],
                "claim": item["claim"],
                "locator": safe_locator,
                "classification": item["classification"],
                "review_status": item["review_status"],
            }
        )
    payload = {
        "case_id": gold["case_id"],
        "review_dimensions": DIMENSIONS,
        "expected_order": constraints["expected_order"],
        "order_rules": constraints["order_rules"],
        "steps": steps,
        "evidence": evidence,
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    forbidden = [marker for marker in FORBIDDEN_PAYLOAD_MARKERS if marker in encoded]
    if forbidden:
        raise ValueError(f"语义复核安全载荷包含禁止标记: {forbidden}")
    return payload


def _validate_response(
    response: dict[str, Any], gold: dict[str, Any]
) -> dict[str, Any]:
    validate_document(response, "semantic_review_response.schema.json")
    if response["case_id"] != gold["case_id"]:
        raise ValueError("语义复核返回了错误case_id")
    steps = {item["step_id"]: item for item in gold["steps"]}
    assessment_ids = [item["step_id"] for item in response["assessments"]]
    if len(assessment_ids) != len(set(assessment_ids)):
        raise ValueError("语义复核返回重复步骤")
    if set(assessment_ids) != set(steps):
        missing = sorted(set(steps) - set(assessment_ids))
        unknown = sorted(set(assessment_ids) - set(steps))
        raise ValueError(f"语义复核步骤覆盖不完整 missing={missing} unknown={unknown}")
    for assessment in response["assessments"]:
        allowed = set(steps[assessment["step_id"]]["evidence"])
        unknown = sorted(set(assessment["evidence_ids"]) - allowed)
        if unknown:
            raise ValueError(
                f"{assessment['step_id']} 评估引用了非本步骤Evidence: {unknown}"
            )

    finding_ids = [item["finding_id"] for item in response["findings"]]
    if len(finding_ids) != len(set(finding_ids)):
        raise ValueError("语义复核返回重复finding_id")
    evidence_catalog = {
        item["evidence_id"]: item for item in gold["evidence_catalog"]
    }
    for finding in response["findings"]:
        unknown_steps = sorted(set(finding["step_ids"]) - set(steps))
        if unknown_steps:
            raise ValueError(f"{finding['finding_id']} 引用了未知步骤: {unknown_steps}")
        allowed_evidence = {
            evidence_id
            for step_id in finding["step_ids"]
            for evidence_id in steps[step_id]["evidence"]
        }
        unknown_evidence = sorted(set(finding["evidence_ids"]) - allowed_evidence)
        if unknown_evidence:
            raise ValueError(
                f"{finding['finding_id']} 引用了受影响步骤外Evidence: {unknown_evidence}"
            )
        if finding["kind"] == "SOURCE_CONFLICT":
            sources = {
                evidence_catalog[item]["source_ref"] for item in finding["evidence_ids"]
            }
            if len(finding["evidence_ids"]) < 2 or len(sources) < 2:
                raise ValueError(
                    f"{finding['finding_id']} 来源冲突必须引用至少两个独立来源"
                )
        if finding["kind"] == "ORDERING_RISK" and len(finding["step_ids"]) < 2:
            raise ValueError(
                f"{finding['finding_id']} 顺序风险必须引用至少两个步骤"
            )
    return response


def _prompt(payload: dict[str, Any]) -> list[dict[str, Any]]:
    shape_example = {
        "case_id": payload["case_id"],
        "assessments": [
            {
                "step_id": "S01",
                "verdict": "SUPPORTED",
                "reviewed_dimensions": DIMENSIONS,
                "evidence_ids": ["E001"],
                "rationale": "步骤表述与引用Evidence一致。",
                "risk_notes": [],
                "confidence": 0.85,
            }
        ],
        "findings": [],
    }
    instructions = (
        "你是SkillForge语义质检Agent。只根据输入的结构化SOP、Evidence陈述和约束复核，"
        "不得引入常识、设备知识或输入之外的事实。必须逐一评估全部步骤，四个维度都要检查："
        "SOURCE_DISTORTION=步骤是否扩大、缩小或曲解Evidence；"
        "SOURCE_CONFLICT=同一步骤引用的来源是否实质冲突，不把NOT_VISIBLE当作冲突；"
        "ORDERING_RISK=前置关系或顺序是否与约束、动作语义矛盾；"
        "EXCEPTION_OMISSION=异常、停止条件或回退是否在步骤、风险或相邻条件步骤中遗漏。"
        "SUPPORTED表示四项均有充分依据；PARTIAL表示依据不足但未形成冲突；"
        "CONFLICT表示存在有证据的实质矛盾；NEEDS_REVIEW表示无法可靠裁决。"
        "每个assessment只能引用该步骤自身的Evidence ID。finding只用于真实问题，"
        "必须引用受影响步骤内的Evidence；来源冲突至少引用两个独立来源，顺序风险至少涉及两个步骤。"
        "所有finding的automatic必须为false，模型结论不得直接修改Gold。"
        "输出必须是严格JSON对象，仅包含case_id、assessments和findings；"
        "assessments必须覆盖输入全部步骤且不重复，reviewed_dimensions必须严格按给定四项顺序。"
        "risk_notes必须是字符串数组，没有风险时用空数组[]；confidence必须是0到1之间的数字；"
        "findings字段始终存在，没有问题时必须是空数组[]。"
        f"合法字段形状示例={json.dumps(shape_example, ensure_ascii=False, separators=(',', ':'))}。"
        "示例只说明字段和类型，正式输出仍必须覆盖输入全部步骤，并使用各步骤真实Evidence ID。"
        "不要输出Markdown、解释性前后缀或未知ID。"
    )
    return [
        {"role": "system", "content": instructions},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def run_semantic_review(
    gold_sop_path: Path,
    constraints_path: Path,
    output_path: Path,
    *,
    client: StepPlanClient | None = None,
    logger: StructuredLogger | None = None,
    prior_model_calls: int = 0,
) -> dict[str, Any]:
    if not 0 <= prior_model_calls <= 9:
        raise ValueError("prior_model_calls必须在0到9之间")
    gold = read_json(gold_sop_path)
    constraints = read_json(constraints_path)
    validate_document(gold, "sop.schema.json")
    payload = build_safe_review_payload(gold, constraints)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_logger = logger or StructuredLogger(
        ROOT / "cases/n31/output/semantic_review_v1/semantic_review.jsonl"
    )
    model_client = client or StepPlanClient(logger=run_logger, timeout_seconds=180)
    messages = _prompt(payload)
    before_calls = int(getattr(model_client, "call_count", 0))
    result: dict[str, Any] | None = None
    last_error: Exception | None = None
    review_rounds = 0
    for review_rounds in range(1, 3):
        candidate = model_client.chat_json(
            messages=messages,
            route="verifier",
            schema_name="semantic_review_response.schema.json",
            max_attempts=3,
            max_tokens=16_384,
        )
        try:
            result = _validate_response(candidate, gold)
            break
        except ValueError as exc:
            last_error = exc
            run_logger.emit(
                "semantic_review.invalid_reference",
                review_round=review_rounds,
                error=str(exc)[:500],
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "上一条虽然通过Schema，但未通过Evidence边界校验："
                        f"{str(exc)[:600]}。请返回完整修正JSON，只能使用输入中的步骤和Evidence。"
                    ),
                }
            )
    if result is None:
        raise StepPlanError(
            f"语义复核连续未通过Evidence边界校验: {type(last_error).__name__}"
        ) from last_error

    selected = model_client.router.reasoning("verifier")
    after_calls = int(getattr(model_client, "call_count", before_calls + review_rounds))
    model_calls = prior_model_calls + max(review_rounds, after_calls - before_calls)
    verdicts = Counter(item["verdict"] for item in result["assessments"])
    finding_kinds = Counter(item["kind"] for item in result["findings"])
    needs_review = bool(result["findings"]) or any(
        item["verdict"] != "SUPPORTED" for item in result["assessments"]
    )
    report = {
        "version": 1,
        "case_id": gold["case_id"],
        "report_id": "N31_SEMANTIC_REVIEW_V1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "NEEDS_REVIEW" if needs_review else "COMPLETED",
        "model": selected["model"],
        "reasoning_effort": selected["reasoning_effort"],
        "model_calls": model_calls,
        "external_processing_authorized": True,
        "review_scope": {
            "step_count": len(gold["steps"]),
            "evidence_count": len(payload["evidence"]),
            "dimensions": DIMENSIONS,
            "structured_sop_sent": True,
            "evidence_claims_sent": True,
            "raw_media_sent": False,
            "full_transcript_sent": False,
            "manual_pages_sent": False,
            "local_paths_sent": False,
            "credentials_sent": False,
        },
        "assessments": result["assessments"],
        "findings": result["findings"],
        "summary": {
            "step_count": len(result["assessments"]),
            "supported_count": verdicts["SUPPORTED"],
            "partial_count": verdicts["PARTIAL"],
            "conflict_count": verdicts["CONFLICT"],
            "needs_review_count": verdicts["NEEDS_REVIEW"],
            "finding_count": len(result["findings"]),
            "high_severity_count": sum(
                item["severity"] in {"HIGH", "CRITICAL"}
                for item in result["findings"]
            ),
            "finding_kind_counts": {
                kind: finding_kinds[kind] for kind in DIMENSIONS
            },
            "human_review_finding_ids": [
                item["finding_id"] for item in result["findings"]
            ],
            "automatic_gold_changes": 0,
        },
        "guardrails": {
            "model_output_classification": "MODEL_INFERENCE",
            "may_override_gold": False,
            "automatic_gold_changes": 0,
            "unknown_evidence_rejected": True,
            "complete_step_coverage_enforced": True,
        },
        "data_policy": {
            "contains_raw_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "safe_structured_derivative_only": True,
        },
    }
    validate_document(report, "semantic_review_report.schema.json")
    write_json(output_path, report)
    run_logger.emit(
        "semantic_review.completed",
        status=report["status"],
        model=report["model"],
        reasoning_effort=report["reasoning_effort"],
        model_calls=report["model_calls"],
        **report["summary"],
    )
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
        default=ROOT / "cases/n31/evaluations/semantic_review_v1.json",
    )
    parser.add_argument("--external-processing-authorized", action="store_true")
    parser.add_argument("--prior-model-calls", type=int, default=0)
    args = parser.parse_args()
    if not args.external_processing_authorized:
        raise ValueError("语义复核前必须明确确认外部处理授权")
    report = run_semantic_review(
        args.gold_sop,
        args.constraints,
        args.output,
        prior_model_calls=args.prior_model_calls,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "model": report["model"],
                "reasoning_effort": report["reasoning_effort"],
                "model_calls": report["model_calls"],
                "summary": report["summary"],
                "review_scope": report["review_scope"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
