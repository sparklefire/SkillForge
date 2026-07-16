"""Build a deterministic, evidence-bound candidate SOP for human review."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .contracts import validate_document


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def resolve_selector(
    evidence_catalog: list[dict[str, Any]],
    selector: dict[str, Any],
) -> str:
    allowed = {"source_ref", "page", "start_ms"}
    unknown = set(selector) - allowed
    if unknown:
        raise ValueError(f"Evidence selector 含未知字段: {sorted(unknown)}")
    if "source_ref" not in selector:
        raise ValueError("Evidence selector 缺少 source_ref")
    locator_keys = {"page", "start_ms"} & set(selector)
    if len(locator_keys) != 1:
        raise ValueError("Evidence selector 必须且只能指定 page 或 start_ms")
    matches = []
    for item in evidence_catalog:
        if item["source_ref"] != selector["source_ref"]:
            continue
        key = next(iter(locator_keys))
        if item["locator"].get(key) == selector[key]:
            matches.append(item)
    if len(matches) != 1:
        raise ValueError(
            f"Evidence selector 应唯一命中，实际 {len(matches)} 条: {selector}"
        )
    return str(matches[0]["evidence_id"])


def _resolve_many(
    evidence_catalog: list[dict[str, Any]],
    selectors: list[dict[str, Any]],
) -> list[str]:
    result = [resolve_selector(evidence_catalog, selector) for selector in selectors]
    if len(set(result)) != len(result):
        raise ValueError("同一步骤的 Evidence selector 命中了重复证据")
    return result


def build_candidate_sop(
    plan: dict[str, Any],
    catalog: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if catalog.get("synthetic") is not False:
        raise ValueError("真实案例候选 SOP 不能使用模拟 Evidence Catalog")
    evidence_catalog = catalog["evidence"]
    evidence_by_id = {item["evidence_id"]: item for item in evidence_catalog}
    steps: list[dict[str, Any]] = []
    review_items: list[dict[str, Any]] = []
    for raw_step in plan["steps"]:
        evidence_ids = _resolve_many(
            evidence_catalog,
            raw_step.get("evidence_selectors", []),
        )
        parameters = []
        parameter_gaps = []
        for parameter in raw_step.get("parameters", []):
            parameter_ids = _resolve_many(
                evidence_catalog,
                parameter.get("evidence_selectors", []),
            )
            parameters.append(
                {
                    "name": parameter["name"],
                    "value": parameter["value"],
                    "unit": parameter.get("unit", ""),
                    "evidence_ids": parameter_ids,
                }
            )
            if not parameter_ids:
                parameter_gaps.append(parameter["name"])
        step = {
            "step_id": raw_step["step_id"],
            "title": raw_step["title"],
            "action": raw_step["action"],
            "object": raw_step["object"],
            "prerequisites": raw_step.get("prerequisites", []),
            "tools": raw_step.get("tools", []),
            "parameters": parameters,
            "warnings": raw_step.get("warnings", []),
            "success_check": raw_step["success_check"],
            "evidence": evidence_ids,
            "confidence": float(raw_step["confidence"]),
            "required": bool(raw_step["required"]),
            "status": "NEEDS_REVIEW",
        }
        steps.append(step)
        selected = [evidence_by_id[evidence_id] for evidence_id in evidence_ids]
        review_items.append(
            {
                "step_id": step["step_id"],
                "title": step["title"],
                "review_reasons": raw_step.get("review_reasons", []),
                "parameter_evidence_gaps": parameter_gaps,
                "evidence_ids": evidence_ids,
                "evidence_source_types": sorted(
                    {item["source_type"] for item in selected}
                ),
                "unreviewed_evidence_ids": [
                    item["evidence_id"]
                    for item in selected
                    if item["review_status"] == "UNREVIEWED"
                ],
                "model_inference_evidence_ids": [
                    item["evidence_id"]
                    for item in selected
                    if item["classification"] == "MODEL_INFERENCE"
                ],
                "human_decision": "PENDING",
            }
        )
    sop = {
        "case_id": plan["case_id"],
        "title": plan["title"],
        "version": int(plan.get("output_version", 1)),
        "evidence_catalog": evidence_catalog,
        "steps": steps,
    }
    validate_document(sop, "sop.schema.json")
    review_queue = {
        "case_id": plan["case_id"],
        "candidate_version": sop["version"],
        "status": "HUMAN_REVIEW_REQUIRED",
        "gold_status": "NOT_GOLD",
        "external_model_calls": 0,
        "step_count": len(steps),
        "pending_step_count": len(review_items),
        "items": review_items,
    }
    return sop, review_queue


def _format_time(milliseconds: int) -> str:
    total_seconds = milliseconds // 1000
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def render_review_sheet(
    sop: dict[str, Any],
    review_queue: dict[str, Any],
) -> str:
    evidence_by_id = {
        item["evidence_id"]: item for item in sop["evidence_catalog"]
    }
    review_by_step = {
        item["step_id"]: item for item in review_queue["items"]
    }
    lines = [
        f"# {sop['title']}人工审核表",
        "",
        "> 当前状态：候选、非 Gold。所有步骤均需实际操作者审核；未签字前不得作为正式操作规范。",
        "",
        "结论填写：`必需 / 可选 / 删除 / 改写`。如选择改写，请在备注中写明正确动作和适用条件。",
        "",
        "| ID | 候选动作 | 证据定位 | 当前缺口 | 结论 | 审核备注 |",
        "|---|---|---|---|---|---|",
    ]
    for step in sop["steps"]:
        review = review_by_step[step["step_id"]]
        locators = []
        for evidence_id in step["evidence"]:
            evidence = evidence_by_id[evidence_id]
            locator = evidence["locator"]
            if evidence["source_type"] == "pdf":
                where = f"{evidence['source_ref']} PDF第{locator['page']}页"
            else:
                where = (
                    f"{evidence['source_ref']} "
                    f"{_format_time(locator['start_ms'])}-"
                    f"{_format_time(locator['end_ms'])}"
                )
            locators.append(f"{evidence_id} {where}")
        gaps = list(review["review_reasons"])
        if review["parameter_evidence_gaps"]:
            gaps.append(
                "参数缺证据：" + "、".join(review["parameter_evidence_gaps"])
            )
        if review["model_inference_evidence_ids"]:
            gaps.append(
                "视频关键帧待确认："
                + "、".join(review["model_inference_evidence_ids"])
            )
        cells = [
            step["step_id"],
            step["action"].replace("|", "｜"),
            "<br>".join(locators).replace("|", "｜"),
            "<br>".join(gaps).replace("|", "｜"),
            "",
            "",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.extend(
        [
            "",
            "## 顺序、条件和成功标准",
            "",
            "- 不可交换的步骤：`________________________________`",
            "- 可选步骤及适用条件：`________________________________`",
            "- 缝标学习的触发条件：`________________________________`",
            "- 导纸夹合适松紧的判断：`________________________________`",
            "- 成功标准与允许偏差：`________________________________`",
            "- 常见异常及第一处理动作：`________________________________`",
            "",
            "## 审核签字",
            "",
            "- 第一审核人姓名/角色：`________________`",
            "- 审核日期：`____年__月__日`",
            "- 总体结论：`通过 / 修改后通过 / 不通过`",
            "- 签字：`________________`",
            "",
            "- 第二审核人姓名/角色：`________________ / 暂无`",
            "- 审核日期：`____年__月__日`",
            "- 总体结论：`通过 / 修改后通过 / 不通过`",
            "- 签字：`________________`",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    sop, review_queue = build_candidate_sop(plan, catalog)
    args.output.mkdir(parents=True, exist_ok=True)
    _write_json(args.output / "candidate_sop.json", sop)
    _write_json(args.output / "human_review_queue.json", review_queue)
    (args.output / "human_review_sheet.md").write_text(
        render_review_sheet(sop, review_queue),
        encoding="utf-8",
    )
    summary = {
        "status": review_queue["status"],
        "gold_status": review_queue["gold_status"],
        "step_count": review_queue["step_count"],
        "pending_step_count": review_queue["pending_step_count"],
        "evidence_count": len(sop["evidence_catalog"]),
        "external_model_calls": 0,
        "review_sheet": str(args.output / "human_review_sheet.md"),
        "output": str(args.output),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
