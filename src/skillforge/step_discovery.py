"""Discover review-only candidate steps from a structured Evidence Catalog.

This path deliberately does not accept a Gold SOP or a semantic-key plan.  The
model may propose text and Evidence IDs, while local code owns provenance,
ordering invariants, privacy checks, and the final review-only status.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .demo import ROOT, read_json, write_json
from .observability import StructuredLogger, redact
from .step_plan import StepPlanClient


PHASE_ORDER = {
    "PREPARATION": 0,
    "EXECUTION": 1,
    "VERIFICATION": 2,
    "RESET": 3,
}
SOURCE_ORDER = {"video": 0, "pdf": 1, "audio": 2}
FORBIDDEN_PAYLOAD_MARKERS = (
    "/Users/",
    "/home/",
    "/var/",
    "/tmp/",
    "file://",
    "Authorization:",
    "Bearer ",
)
WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[\s\"'])[a-z]:[\\/]")


class StepDiscoveryError(ValueError):
    """Raised when evidence or a model proposal violates discovery guardrails."""


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _reject_unsafe_text(value: Any, *, label: str) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    forbidden = [marker for marker in FORBIDDEN_PAYLOAD_MARKERS if marker in encoded]
    if WINDOWS_ABSOLUTE_PATH.search(encoded):
        forbidden.append("WINDOWS_ABSOLUTE_PATH")
    if redact(encoded) != encoded:
        forbidden.append("CREDENTIAL_LIKE_VALUE")
    if forbidden:
        raise StepDiscoveryError(f"{label}包含禁止标记: {sorted(set(forbidden))}")


def build_safe_discovery_payload(
    evidence_catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate and project Evidence into the only payload sent to the model."""

    if len(evidence_catalog) < 8:
        raise StepDiscoveryError("开放候选发现至少需要8条结构化Evidence")
    evidence_ids: list[str] = []
    projected: list[dict[str, Any]] = []
    for item in evidence_catalog:
        validate_document(item, "evidence.schema.json")
        evidence_id = item["evidence_id"]
        if evidence_id in evidence_ids:
            raise StepDiscoveryError(f"Evidence ID重复: {evidence_id}")
        evidence_ids.append(evidence_id)
        if item["review_status"] == "REJECTED":
            raise StepDiscoveryError(f"拒绝使用已驳回Evidence: {evidence_id}")
        locator = item["locator"]
        safe_locator = (
            {"page": locator["page"]}
            if "page" in locator
            else {
                "start_ms": locator["start_ms"],
                "end_ms": locator["end_ms"],
            }
        )
        projected.append(
            {
                "evidence_id": evidence_id,
                "source_type": item["source_type"],
                "claim": item["claim"],
                "locator": safe_locator,
                "classification": item["classification"],
                "review_status": item["review_status"],
                "confidence": item["confidence"],
            }
        )

    # The local report preserves the original relative source_ref, so reject
    # unsafe input even though source_ref and keyframe are omitted from the API.
    _reject_unsafe_text(evidence_catalog, label="Evidence Catalog")
    payload = {
        "task": "discover_ordered_training_step_candidates",
        "evidence": projected,
    }
    _reject_unsafe_text(payload, label="候选发现安全载荷")
    return payload


def _all_step_evidence(step: dict[str, Any]) -> set[str]:
    evidence_ids = set(step["evidence_ids"])
    for tool in step["tools"]:
        evidence_ids.update(tool["evidence_ids"])
    for parameter in step["parameters"]:
        evidence_ids.update(parameter["evidence_ids"])
    for warning in step["warnings"]:
        evidence_ids.update(warning["evidence_ids"])
    evidence_ids.update(step["success_check"]["evidence_ids"])
    return evidence_ids


def validate_discovery_response(
    response: dict[str, Any],
    evidence_catalog: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply graph and grounding rules that JSON Schema alone cannot express."""

    validate_document(response, "step_discovery_response.schema.json")
    available = {item["evidence_id"] for item in evidence_catalog}
    expected_ids = [f"P{index:02d}" for index in range(1, len(response["steps"]) + 1)]
    actual_ids = [step["provisional_id"] for step in response["steps"]]
    if actual_ids != expected_ids:
        raise StepDiscoveryError(
            f"候选步骤必须按P01连续编号: expected={expected_ids} actual={actual_ids}"
        )

    used: set[str] = set()
    previous_phase = -1
    seen_steps: set[str] = set()
    for step in response["steps"]:
        step_id = step["provisional_id"]
        unknown_prerequisites = sorted(set(step["prerequisites"]) - seen_steps)
        if unknown_prerequisites:
            raise StepDiscoveryError(
                f"{step_id}前置步骤必须引用较早候选: {unknown_prerequisites}"
            )
        phase = PHASE_ORDER[step["phase"]]
        if phase < previous_phase:
            raise StepDiscoveryError(f"{step_id}阶段顺序发生回退")
        previous_phase = phase
        seen_steps.add(step_id)

        step_evidence = set(step["evidence_ids"])
        nested_evidence = _all_step_evidence(step)
        unknown = sorted(nested_evidence - available)
        if unknown:
            raise StepDiscoveryError(f"{step_id}引用未知Evidence: {unknown}")
        outside_step = sorted(nested_evidence - step_evidence)
        if outside_step:
            raise StepDiscoveryError(
                f"{step_id}工具/参数/警告/完成标准引用未列入步骤的Evidence: {outside_step}"
            )
        used.update(step_evidence)

    exclusions = response["excluded_evidence"]
    excluded_ids = [item["evidence_id"] for item in exclusions]
    if len(excluded_ids) != len(set(excluded_ids)):
        raise StepDiscoveryError("excluded_evidence包含重复Evidence ID")
    unknown_excluded = sorted(set(excluded_ids) - available)
    if unknown_excluded:
        raise StepDiscoveryError(f"排除项引用未知Evidence: {unknown_excluded}")
    overlap = sorted(used & set(excluded_ids))
    if overlap:
        raise StepDiscoveryError(f"Evidence不能同时被使用和排除: {overlap}")
    unaccounted = sorted(available - used - set(excluded_ids))
    if unaccounted:
        raise StepDiscoveryError(f"Evidence未被使用或说明排除原因: {unaccounted}")
    return response


def _messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    shape_example = {
        "version": 1,
        "steps": [
            {
                "provisional_id": "P01",
                "title": "有证据的标题",
                "action": "有证据的动作",
                "object": "动作对象（必须是字符串）",
                "phase": "PREPARATION",
                "prerequisites": [],
                "tools": [
                    {"name": "有证据的工具", "evidence_ids": ["E001"]}
                ],
                "parameters": [],
                "warnings": [
                    {"text": "有证据的警告", "evidence_ids": ["E001"]}
                ],
                "success_check": {
                    "text": "有证据的完成标准",
                    "evidence_ids": ["E001"],
                },
                "required": True,
                "evidence_ids": ["E001"],
                "confidence": 0.9,
            }
        ],
        "excluded_evidence": [],
    }
    instructions = (
        "你是SkillForge候选步骤发现Agent。输入只有结构化Evidence，没有Gold SOP、标准步骤文本或semantic key。"
        "请把Evidence合并为8到15个有顺序的候选培训步骤。不得引入输入之外的动作、工具、参数、"
        "警告或完成标准；每项工具、参数、警告和完成标准必须单独引用Evidence ID，且这些ID也必须"
        "列入该步骤evidence_ids。步骤按P01连续编号，prerequisites只能引用较早步骤；phase只能按"
        "PREPARATION、EXECUTION、VERIFICATION、RESET单向推进。每条输入Evidence必须被至少一个"
        "步骤使用，或在excluded_evidence中给出原因，不能同时使用和排除。即使数组为空也必须输出。"
        "不要输出case_id、来源路径、source_types、审核状态、Gold结论或semantic key。"
        "object必须是一个字符串，不是数组；tools中的每一项必须是{name,evidence_ids}对象；"
        "warnings中的每一项必须是{text,evidence_ids}对象；success_check必须是{text,evidence_ids}对象。"
        "只返回符合step_discovery_response.schema.json的JSON，不要Markdown。完整字段形状示例："
        + json.dumps(shape_example, ensure_ascii=False, separators=(",", ":"))
    )
    return [
        {"role": "system", "content": instructions},
        {
            "role": "user",
            "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _review_reasons(
    step: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    reasons = ["无Gold或语义规范输入，候选动作和顺序必须由人工确认"]
    source_types = {evidence_by_id[item]["source_type"] for item in step["evidence_ids"]}
    if len(source_types) == 1:
        reasons.append("当前候选仅由单一来源类型支持")
    if any(
        evidence_by_id[item]["review_status"] == "UNREVIEWED"
        for item in step["evidence_ids"]
    ):
        reasons.append("当前候选包含尚未人工核验的Evidence")
    if step["confidence"] < 0.8:
        reasons.append("模型候选置信度低于0.80")
    return reasons


class StepDiscoveryAgent:
    def __init__(
        self,
        client: StepPlanClient | None = None,
        *,
        logger: StructuredLogger | None = None,
    ) -> None:
        self.client = client or StepPlanClient()
        self.logger = logger or StructuredLogger()

    def discover(
        self,
        evidence_catalog: list[dict[str, Any]],
        *,
        case_id: str,
        title: str,
        planning_attempts: int = 2,
        external_processing: bool = True,
    ) -> dict[str, Any]:
        if not 1 <= planning_attempts <= 2:
            raise ValueError("planning_attempts必须为1或2")
        if not case_id.strip() or not title.strip():
            raise StepDiscoveryError("case_id和title不能为空")
        _reject_unsafe_text({"case_id": case_id, "title": title}, label="案例元数据")
        payload = build_safe_discovery_payload(evidence_catalog)
        messages = _messages(payload)
        calls_before = self.client.call_count
        proposal: dict[str, Any] | None = None
        last_error: StepDiscoveryError | None = None
        for attempt in range(1, planning_attempts + 1):
            raw = self.client.chat_json(
                messages=messages,
                route="planner",
                schema_name="step_discovery_response.schema.json",
                max_attempts=3,
                max_tokens=8192,
            )
            try:
                proposal = validate_discovery_response(raw, evidence_catalog)
                break
            except StepDiscoveryError as exc:
                last_error = exc
                self.logger.emit(
                    "step_discovery.grounding_rejected",
                    attempt=attempt,
                    error=str(exc)[:500],
                )
                if attempt < planning_attempts:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "上一条JSON通过了形状校验，但未通过本地证据/顺序门禁："
                                f"{str(exc)[:600]}。请返回修正后的完整JSON。"
                            ),
                        }
                    )
        if proposal is None:
            assert last_error is not None
            raise StepDiscoveryError(
                f"候选发现连续{planning_attempts}次未通过本地门禁: {last_error}"
            ) from last_error

        evidence_by_id = {item["evidence_id"]: item for item in evidence_catalog}
        candidates: list[dict[str, Any]] = []
        for proposed in proposal["steps"]:
            candidate = copy.deepcopy(proposed)
            candidate["source_types"] = sorted(
                {
                    evidence_by_id[item]["source_type"]
                    for item in proposed["evidence_ids"]
                },
                key=SOURCE_ORDER.__getitem__,
            )
            candidate["review_reasons"] = _review_reasons(proposed, evidence_by_id)
            candidate["review_status"] = "HUMAN_REVIEW_REQUIRED"
            candidates.append(candidate)

        calls_used = self.client.call_count - calls_before
        model_calls = calls_used if external_processing else 0
        selected = self.client.router.reasoning("planner")
        source_counts = Counter(item["source_type"] for item in evidence_catalog)
        used = {
            evidence_id
            for candidate in candidates
            for evidence_id in candidate["evidence_ids"]
        }
        report = {
            "version": 1,
            "case_id": case_id,
            "title": title,
            "report_id": "GOLD_FREE_STEP_DISCOVERY_V1",
            "status": "NEEDS_REVIEW",
            "extraction_mode": "GOLD_FREE_EVIDENCE_DISCOVERY",
            "uses_gold_step_text": False,
            "semantic_spec_provided": False,
            "input_evidence_sha256": _canonical_sha256(evidence_catalog),
            "model": selected["model"] if external_processing else "offline-fixture",
            "reasoning_effort": selected["reasoning_effort"],
            "execution_mode": (
                "LIVE_STEP_PLAN" if external_processing else "OFFLINE_FIXTURE"
            ),
            "model_calls": model_calls,
            "evidence_catalog": copy.deepcopy(evidence_catalog),
            "ordered_candidates": candidates,
            "excluded_evidence": copy.deepcopy(proposal["excluded_evidence"]),
            "summary": {
                "input_evidence_count": len(evidence_catalog),
                "source_type_counts": {
                    source: source_counts[source]
                    for source in ("video", "pdf", "audio")
                },
                "step_count": len(candidates),
                "required_step_count": sum(item["required"] for item in candidates),
                "used_evidence_count": len(used),
                "excluded_evidence_count": len(proposal["excluded_evidence"]),
                "unused_evidence_count": 0,
                "multi_source_step_count": sum(
                    len(item["source_types"]) > 1 for item in candidates
                ),
                "human_review_required_count": len(candidates),
                "all_references_grounded": True,
                "all_evidence_accounted_for": True,
                "graph_acyclic": True,
                "phase_monotonic": True,
            },
            "guardrails": {
                "model_output_classification": "MODEL_INFERENCE",
                "model_controls_provenance": False,
                "unknown_evidence_rejected": True,
                "human_review_required": True,
                "may_override_gold": False,
                "may_publish": False,
                "automatic_gold_changes": 0,
            },
            "data_policy": {
                "contains_raw_media": False,
                "contains_credentials": False,
                "contains_absolute_paths": False,
                "safe_structured_derivative_only": True,
                "external_model_calls": model_calls,
            },
        }
        validate_document(report, "step_discovery_report.schema.json")
        self.logger.emit(
            "step_discovery.completed",
            case_id=case_id,
            execution_mode=report["execution_mode"],
            model=report["model"],
            model_calls=model_calls,
            **report["summary"],
        )
        return report


def _fixture_client(path: Path, logger: StructuredLogger) -> StepPlanClient:
    fixture = read_json(path)

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": "offline-fixture",
            "choices": [{"message": {"content": json.dumps(fixture, ensure_ascii=False)}}],
            "usage": {"total_tokens": 0},
        }

    return StepPlanClient(logger=logger, transport=transport, retry_sleep=lambda _: None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence",
        type=Path,
        default=ROOT / "cases" / "demo_case" / "synthetic" / "discovery_evidence.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "outputs" / "step_discovery" / "report.json",
    )
    parser.add_argument("--case-id", default="SYNTHETIC-DISCOVERY-001")
    parser.add_argument("--title", default="虚构过滤件更换候选步骤发现（仅工程测试）")
    parser.add_argument("--response-fixture", type=Path)
    parser.add_argument("--external-processing-authorized", action="store_true")
    args = parser.parse_args()

    if not args.response_fixture and not args.external_processing_authorized:
        raise ValueError("实时Step Plan发现前必须明确确认外部处理授权")
    evidence = read_json(args.evidence)
    if not isinstance(evidence, list):
        raise StepDiscoveryError("--evidence必须是Evidence对象数组")
    logger = StructuredLogger(args.output.with_suffix(".jsonl"))
    client = (
        _fixture_client(args.response_fixture, logger)
        if args.response_fixture
        else StepPlanClient(logger=logger)
    )
    report = StepDiscoveryAgent(client, logger=logger).discover(
        evidence,
        case_id=args.case_id,
        title=args.title,
        external_processing=args.response_fixture is None,
    )
    write_json(args.output, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "execution_mode": report["execution_mode"],
                "model": report["model"],
                "model_calls": report["model_calls"],
                "summary": report["summary"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
