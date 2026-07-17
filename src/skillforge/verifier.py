"""Deterministic P0 checks for completeness, order and evidence grounding."""

from __future__ import annotations

from typing import Any

from .contracts import ContractValidationError, validate_document


UNSUPPORTED_SAFETY_PROMISE_PATTERNS = (
    "100%安全",
    "绝对安全",
    "保证安全",
    "完全无风险",
    "无任何风险",
    "零风险",
    "绝不会发生",
    "100% safe",
    "guaranteed safe",
    "zero risk",
)


def _evidence_ids(step: dict[str, Any]) -> set[str]:
    return set(step.get("evidence", []))


def verify_sop(
    sop: dict[str, Any],
    reference_sop: dict[str, Any],
    constraints: dict[str, Any],
    *,
    iteration: int = 1,
) -> dict[str, Any]:
    """Return a schema-valid conflict report without invoking a model."""

    current_steps = sop["steps"]
    current = {step["step_id"]: step for step in current_steps}
    reference = {step["step_id"]: step for step in reference_sop["steps"]}
    reference_evidence = {
        item["evidence_id"]: item for item in reference_sop["evidence_catalog"]
    }
    current_evidence = {item["evidence_id"]: item for item in sop["evidence_catalog"]}
    positions = {step["step_id"]: index for index, step in enumerate(current_steps)}
    conflicts: list[dict[str, Any]] = []

    def resolve(step: dict[str, Any]) -> list[dict[str, Any]]:
        return [reference_evidence[item] for item in step["evidence"] if item in reference_evidence]

    def add(
        kind: str,
        severity: str,
        step_ids: list[str],
        message: str,
        evidence: list[dict[str, Any]],
        details: dict[str, Any],
        action: str,
        automatic: bool,
    ) -> None:
        conflicts.append(
            {
                "conflict_id": f"C{len(conflicts) + 1:03d}",
                "kind": kind,
                "severity": severity,
                "step_ids": step_ids,
                "message": message,
                "evidence": evidence,
                "details": details,
                "proposed_action": action,
                "automatic": automatic,
                "status": "OPEN",
            }
        )

    for step_id in constraints["required_step_ids"]:
        if step_id not in current:
            source_step = reference[step_id]
            add(
                "MISSING_STEP",
                "CRITICAL",
                [step_id],
                f"缺少必要步骤 {step_id}: {source_step['title']}",
                resolve(source_step),
                {"step_id": step_id},
                "INSERT",
                True,
            )

    for step in current_steps:
        for prerequisite in step["prerequisites"]:
            if prerequisite not in current:
                evidence = resolve(reference[prerequisite]) if prerequisite in reference else []
                add(
                    "MISSING_PREREQUISITE",
                    "HIGH",
                    [prerequisite, step["step_id"]],
                    f"{step['step_id']} 依赖不存在的步骤 {prerequisite}",
                    evidence,
                    {"missing": prerequisite, "dependent": step["step_id"]},
                    "INSERT",
                    True,
                )

    for rule in constraints["order_rules"]:
        before, after = rule["before"], rule["after"]
        if before in positions and after in positions and positions[before] > positions[after]:
            evidence = resolve(reference[before]) + resolve(reference[after])
            add(
                "ORDER_ERROR",
                "HIGH",
                [before, after],
                f"步骤 {before} 必须先于 {after}",
                evidence,
                {"before": before, "after": after},
                "REORDER",
                True,
            )

    allowed_tools = set(constraints["allowed_tools"])
    allowed_parameters = set(constraints["allowed_parameters"])
    for step in current_steps:
        reference_step = reference.get(step["step_id"])
        reference_tools = set(reference_step["tools"]) if reference_step else set()
        for tool in step["tools"]:
            if tool not in allowed_tools or tool not in reference_tools:
                add(
                    "UNSUPPORTED_TOOL",
                    "HIGH",
                    [step["step_id"]],
                    f"工具“{tool}”没有来源依据",
                    resolve(reference_step) if reference_step else [],
                    {
                        "step_id": step["step_id"],
                        "tool": tool,
                        "globally_allowed": tool in allowed_tools,
                        "supported_in_step": tool in reference_tools,
                        "reference_tools": sorted(reference_tools),
                    },
                    "REMOVE",
                    True,
                )
        available_evidence = _evidence_ids(step)
        reference_parameters = reference_step["parameters"] if reference_step else []
        for parameter_index, parameter in enumerate(step["parameters"]):
            evidence_ids = set(parameter["evidence_ids"])
            reference_name_match = next(
                (
                    item
                    for item in reference_parameters
                    if item["name"] == parameter["name"]
                ),
                None,
            )
            reference_parameter = next(
                (
                    item
                    for item in reference_parameters
                    if item["name"] == parameter["name"]
                    and item["value"] == parameter["value"]
                    and item["unit"] == parameter["unit"]
                    and set(item["evidence_ids"]) == evidence_ids
                ),
                None,
            )
            if (
                parameter["name"] not in allowed_parameters
                or not evidence_ids
                or not evidence_ids.issubset(available_evidence)
                or reference_parameter is None
            ):
                add(
                    "UNSUPPORTED_PARAMETER",
                    "HIGH",
                    [step["step_id"]],
                    f"参数“{parameter['name']}”没有有效来源依据",
                    (
                        resolve(reference_step)
                        if reference_step and reference_name_match is not None
                        else []
                    ),
                    {
                        "step_id": step["step_id"],
                        "parameter_index": parameter_index,
                        "parameter_name": parameter["name"],
                        "parameter": parameter,
                        "globally_allowed": parameter["name"] in allowed_parameters,
                        "matches_reference": reference_parameter is not None,
                        "reference_parameter": reference_name_match,
                    },
                    "REPLACE" if reference_name_match is not None else "REMOVE",
                    True,
                )

        reference_warnings = set(reference_step["warnings"]) if reference_step else set()
        for warning_index, warning in enumerate(step["warnings"]):
            if warning not in reference_warnings:
                add(
                    "UNSUPPORTED_SAFETY_CLAIM",
                    "HIGH",
                    [step["step_id"]],
                    f"风险或安全提示“{warning}”没有当前步骤来源依据",
                    resolve(reference_step) if reference_step else [],
                    {
                        "step_id": step["step_id"],
                        "field": "warnings",
                        "warning_index": warning_index,
                        "claim": warning,
                        "reference_warnings": sorted(reference_warnings),
                    },
                    "REMOVE",
                    True,
                )

        for field in ("title", "action", "success_check"):
            value = step[field]
            reference_value = reference_step[field] if reference_step else ""
            matched_patterns = [
                pattern
                for pattern in UNSUPPORTED_SAFETY_PROMISE_PATTERNS
                if pattern.lower() in value.lower()
                and pattern.lower() not in reference_value.lower()
            ]
            if matched_patterns:
                can_restore = reference_step is not None
                add(
                    "UNSUPPORTED_SAFETY_CLAIM",
                    "HIGH",
                    [step["step_id"]],
                    f"{field} 含无来源绝对安全承诺",
                    resolve(reference_step) if reference_step else [],
                    {
                        "step_id": step["step_id"],
                        "field": field,
                        "claim": value,
                        "matched_patterns": matched_patterns,
                        "reference_value": reference_value,
                    },
                    "REPLACE" if can_restore else "REVIEW",
                    can_restore,
                )

        if step["required"] and not step["evidence"]:
            add(
                "MISSING_EVIDENCE",
                "HIGH",
                [step["step_id"]],
                f"必要步骤 {step['step_id']} 没有证据",
                resolve(reference[step["step_id"]]) if step["step_id"] in reference else [],
                {"step_id": step["step_id"]},
                "REVIEW",
                False,
            )
        for evidence_id in step["evidence"]:
            try:
                evidence = current_evidence[evidence_id]
                validate_document(evidence, "evidence.schema.json")
            except (KeyError, ContractValidationError) as exc:
                add(
                    "INVALID_EVIDENCE",
                    "HIGH",
                    [step["step_id"]],
                    f"步骤 {step['step_id']} 的证据定位无效",
                    [],
                    {
                        "step_id": step["step_id"],
                        "evidence_id": evidence_id,
                        "error": str(exc)[:300],
                    },
                    "REVIEW",
                    False,
                )

    report = {
        "case_id": sop["case_id"],
        "iteration": iteration,
        "conflicts": conflicts,
    }
    return validate_document(report, "conflict.schema.json")


def metrics(
    sop: dict[str, Any],
    report: dict[str, Any],
    constraints: dict[str, Any],
) -> dict[str, Any]:
    present = {step["step_id"]: step for step in sop["steps"]}
    required = constraints["required_step_ids"]
    covered = sum(step_id in present for step_id in required)
    supported = sum(
        step_id in present and bool(present[step_id]["evidence"]) for step_id in required
    )
    severe = sum(
        item["severity"] in {"HIGH", "CRITICAL"} for item in report["conflicts"]
    )
    return {
        "required_step_coverage": covered / len(required),
        "evidence_supported_required_steps": supported / len(required),
        "severe_error_count": severe,
        "conflict_count": len(report["conflicts"]),
    }
