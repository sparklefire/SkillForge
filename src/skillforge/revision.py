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
                        "evidence_ids": [],
                    }
                )
        elif conflict["kind"] == "UNSUPPORTED_PARAMETER":
            step_id = conflict["details"]["step_id"]
            name = conflict["details"]["parameter_name"]
            step = next(item for item in revised["steps"] if item["step_id"] == step_id)
            removed = [item for item in step["parameters"] if item["name"] == name]
            if removed:
                step["parameters"] = [
                    item for item in step["parameters"] if item["name"] != name
                ]
                step["status"] = "REVISED"
                changes.append(
                    {
                        "conflict_id": conflict["conflict_id"],
                        "action": "REMOVE",
                        "path": f"/steps/{step_id}/parameters",
                        "before": removed,
                        "after": None,
                        "reason": conflict["message"],
                        "evidence_ids": [],
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
