"""Controlled fault injection for the clearly labelled synthetic demo case."""

from __future__ import annotations

import copy
from typing import Any


def inject_faults(
    reference_sop: dict[str, Any], fault_spec: dict[str, Any]
) -> dict[str, Any]:
    draft = copy.deepcopy(reference_sop)
    draft["version"] = 1
    draft["title"] = f"{reference_sop['title']}（含受控错误的首轮草稿）"
    for step in draft["steps"]:
        step["status"] = "DRAFT"

    remove_step_id = fault_spec["remove_step_id"]
    draft["steps"] = [
        step for step in draft["steps"] if step["step_id"] != remove_step_id
    ]

    first_id, second_id = fault_spec["swap_step_ids"]
    first_index = next(
        index for index, step in enumerate(draft["steps"]) if step["step_id"] == first_id
    )
    second_index = next(
        index for index, step in enumerate(draft["steps"]) if step["step_id"] == second_id
    )
    draft["steps"][first_index], draft["steps"][second_index] = (
        draft["steps"][second_index],
        draft["steps"][first_index],
    )

    tool_fault = fault_spec["unsupported_tool"]
    tool_step = next(
        step for step in draft["steps"] if step["step_id"] == tool_fault["step_id"]
    )
    tool_step["tools"].append(tool_fault["tool"])

    parameter_fault = fault_spec["unsupported_parameter"]
    parameter_step = next(
        step
        for step in draft["steps"]
        if step["step_id"] == parameter_fault["step_id"]
    )
    parameter_step["parameters"].append(parameter_fault["parameter"])
    return draft
