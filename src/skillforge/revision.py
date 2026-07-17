"""Evidence-bound local revision with before/after audit records."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from .contracts import validate_document


def digest(document: dict[str, Any]) -> str:
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def revise_sop(
    sop: dict[str, Any],
    report: dict[str, Any],
    reference_sop: dict[str, Any],
    constraints: dict[str, Any],
    *,
    iteration: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if iteration > 3:
        raise ValueError("自动修订最多三轮")
    started_at = datetime.now(UTC).isoformat()
    revised = copy.deepcopy(sop)
    before_digest = digest(revised)
    reference = {step["step_id"]: step for step in reference_sop["steps"]}
    changes: list[dict[str, Any]] = []

    def evidence_ids(conflict: dict[str, Any]) -> list[str]:
        return sorted({item["evidence_id"] for item in conflict["evidence"]})

    for conflict in report["conflicts"]:
        if not conflict["automatic"] or conflict["kind"] != "MISSING_STEP":
            continue
        step_id = conflict["details"]["step_id"]
        if any(step["step_id"] == step_id for step in revised["steps"]):
            continue
        inserted = copy.deepcopy(reference[step_id])
        inserted["status"] = "REVISED"
        revised["steps"].append(inserted)
        changes.append(
            {
                "conflict_id": conflict["conflict_id"],
                "action": "INSERT",
                "path": f"/steps/{step_id}",
                "before": None,
                "after": inserted,
                "reason": conflict["message"],
                "evidence_ids": evidence_ids(conflict),
            }
        )

    for conflict in report["conflicts"]:
        if not conflict["automatic"]:
            continue
        if conflict["kind"] == "UNSUPPORTED_TOOL":
            step_id = conflict["details"]["step_id"]
            tool = conflict["details"]["tool"]
            step = next(item for item in revised["steps"] if item["step_id"] == step_id)
            if tool in step["tools"]:
                step["tools"].remove(tool)
                step["status"] = "REVISED"
                changes.append(
                    {
                        "conflict_id": conflict["conflict_id"],
                        "action": "REMOVE",
                        "path": f"/steps/{step_id}/tools",
                        "before": tool,
                        "after": None,
                        "reason": conflict["message"],
                        "evidence_ids": evidence_ids(conflict),
                    }
                )
        elif conflict["kind"] == "UNSUPPORTED_PARAMETER":
            step_id = conflict["details"]["step_id"]
            name = conflict["details"]["parameter_name"]
            step = next(item for item in revised["steps"] if item["step_id"] == step_id)
            invalid_parameter = conflict["details"].get("parameter")
            parameter_index = conflict["details"].get("parameter_index")
            reference_step = reference.get(step_id)
            reference_parameter = copy.deepcopy(
                conflict["details"].get("reference_parameter")
            )
            if reference_parameter is None and reference_step is not None:
                reference_parameter = next(
                    (
                        copy.deepcopy(item)
                        for item in reference_step["parameters"]
                        if item["name"] == name
                    ),
                    None,
                )
            actual_index = None
            if (
                isinstance(parameter_index, int)
                and 0 <= parameter_index < len(step["parameters"])
                and step["parameters"][parameter_index] == invalid_parameter
            ):
                actual_index = parameter_index
            else:
                actual_index = next(
                    (
                        index
                        for index, item in enumerate(step["parameters"])
                        if item == invalid_parameter
                    ),
                    None,
                )
            if actual_index is not None:
                before = copy.deepcopy(step["parameters"][actual_index])
                if reference_parameter is None:
                    step["parameters"].pop(actual_index)
                    action = "REMOVE"
                    after = None
                else:
                    step["parameters"][actual_index] = reference_parameter
                    action = "REPLACE"
                    after = reference_parameter
                step["status"] = "REVISED"
                changes.append(
                    {
                        "conflict_id": conflict["conflict_id"],
                        "action": action,
                        "path": f"/steps/{step_id}/parameters/{actual_index}",
                        "before": before,
                        "after": after,
                        "reason": conflict["message"],
                        "evidence_ids": (
                            list(reference_parameter["evidence_ids"])
                            if reference_parameter
                            else []
                        ),
                    }
                )
        elif conflict["kind"] == "UNSUPPORTED_SAFETY_CLAIM":
            step_id = conflict["details"]["step_id"]
            field = conflict["details"]["field"]
            step = next(item for item in revised["steps"] if item["step_id"] == step_id)
            if field == "warnings":
                claim = conflict["details"]["claim"]
                if claim not in step["warnings"]:
                    continue
                step["warnings"].remove(claim)
                before = claim
                after = None
                action = "REMOVE"
                path = f"/steps/{step_id}/warnings"
                source_ids: list[str] = evidence_ids(conflict)
            else:
                if step_id not in reference:
                    continue
                before = step[field]
                after = reference[step_id][field]
                if before == after:
                    continue
                step[field] = after
                action = "REPLACE"
                path = f"/steps/{step_id}/{field}"
                source_ids = evidence_ids(conflict)
            step["status"] = "REVISED"
            changes.append(
                {
                    "conflict_id": conflict["conflict_id"],
                    "action": action,
                    "path": path,
                    "before": before,
                    "after": after,
                    "reason": conflict["message"],
                    "evidence_ids": source_ids,
                }
            )

    if any(item["kind"] == "ORDER_ERROR" and item["automatic"] for item in report["conflicts"]):
        order = {step_id: index for index, step_id in enumerate(constraints["expected_order"])}
        before_order = [step["step_id"] for step in revised["steps"]]
        revised["steps"].sort(key=lambda step: order.get(step["step_id"], len(order)))
        after_order = [step["step_id"] for step in revised["steps"]]
        if before_order != after_order:
            conflict = next(item for item in report["conflicts"] if item["kind"] == "ORDER_ERROR")
            changes.append(
                {
                    "conflict_id": conflict["conflict_id"],
                    "action": "REORDER",
                    "path": "/steps",
                    "before": before_order,
                    "after": after_order,
                    "reason": conflict["message"],
                    "evidence_ids": evidence_ids(conflict),
                }
            )

    revised["version"] = sop["version"] + 1
    validate_document(revised, "sop.schema.json")
    audit = {
        "case_id": revised["case_id"],
        "iteration": iteration,
        "started_at": started_at,
        "completed_at": datetime.now(UTC).isoformat(),
        "before_digest": before_digest,
        "after_digest": digest(revised),
        "changes": changes,
    }
    validate_document(audit, "revision_audit.schema.json")
    return revised, audit
