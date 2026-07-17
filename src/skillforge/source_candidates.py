"""Extract source-separated step candidates and synthesize an evidence-bound order."""

from __future__ import annotations

import argparse
import copy
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .candidate_sop import resolve_selector
from .contracts import validate_document


PHASE_ORDER = {
    "PREPARATION": 0,
    "EXECUTION": 1,
    "VERIFICATION": 2,
    "RESET": 3,
}


class SourceCandidateError(ValueError):
    """Raised when source candidates cannot be grounded or safely synthesized."""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _evidence_catalog(document: dict[str, Any]) -> list[dict[str, Any]]:
    catalog = document.get("evidence")
    if catalog is None:
        catalog = document.get("evidence_catalog")
    if not isinstance(catalog, list):
        raise SourceCandidateError("证据文档缺少 evidence 或 evidence_catalog 数组")
    for item in catalog:
        validate_document(item, "evidence.schema.json")
    return catalog


def _normalize_action(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def _stable_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _resolve_selectors(
    evidence_catalog: list[dict[str, Any]],
    selectors: list[dict[str, Any]],
) -> list[str]:
    return _stable_unique(
        [resolve_selector(evidence_catalog, selector) for selector in selectors]
    )


def _canonical_steps(plan: dict[str, Any]) -> list[dict[str, Any]]:
    required_metadata = {
        "semantic_key",
        "phase",
        "irreversible",
        "recovery",
    }
    result: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_ids: set[str] = set()
    for raw in plan.get("steps", []):
        missing = sorted(required_metadata - set(raw))
        if missing:
            raise SourceCandidateError(
                f"{raw.get('step_id', '<unknown>')} 缺少合并元数据: {missing}"
            )
        semantic_key = str(raw["semantic_key"])
        step_id = str(raw["step_id"])
        if not re.fullmatch(r"[A-Z][A-Z0-9_]+", semantic_key):
            raise SourceCandidateError(f"非法 semantic_key: {semantic_key}")
        if semantic_key in seen_keys or step_id in seen_ids:
            raise SourceCandidateError("canonical step_id 或 semantic_key 重复")
        if raw["phase"] not in PHASE_ORDER:
            raise SourceCandidateError(f"未知阶段: {raw['phase']}")
        recovery = raw["recovery"]
        if set(recovery) != {
            "mode",
            "target_step_id",
            "action",
            "evidence_ids",
        }:
            raise SourceCandidateError(f"{step_id} recovery 字段不完整或含未知字段")
        result.append(copy.deepcopy(raw))
        seen_keys.add(semantic_key)
        seen_ids.add(step_id)
    if not 8 <= len(result) <= 15:
        raise SourceCandidateError("合并后的规范步骤必须为8至15步")
    return result


def _topological_order(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {step["step_id"]: step for step in steps}
    input_order = {step["step_id"]: index for index, step in enumerate(steps)}
    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree = {step_id: 0 for step_id in by_id}
    for step in steps:
        prerequisites = step.get("prerequisites", [])
        if len(prerequisites) != len(set(prerequisites)):
            raise SourceCandidateError(f"{step['step_id']} prerequisites 重复")
        for prerequisite in prerequisites:
            if prerequisite not in by_id:
                raise SourceCandidateError(
                    f"{step['step_id']} 引用了未知前置步骤 {prerequisite}"
                )
            if prerequisite == step["step_id"]:
                raise SourceCandidateError(f"{step['step_id']} 不能依赖自身")
            outgoing[prerequisite].append(step["step_id"])
            indegree[step["step_id"]] += 1

    available = [step_id for step_id, degree in indegree.items() if degree == 0]
    ordered: list[dict[str, Any]] = []
    while available:
        available.sort(
            key=lambda step_id: (
                PHASE_ORDER[by_id[step_id]["phase"]],
                input_order[step_id],
            )
        )
        step_id = available.pop(0)
        ordered.append(by_id[step_id])
        for successor in outgoing[step_id]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
                available.append(successor)
    if len(ordered) != len(steps):
        cyclic = sorted(step_id for step_id, degree in indegree.items() if degree)
        raise SourceCandidateError(f"步骤依赖图存在环: {cyclic}")

    positions = {step["step_id"]: index for index, step in enumerate(ordered)}
    for step in ordered:
        if any(positions[item] >= positions[step["step_id"]] for item in step["prerequisites"]):
            raise SourceCandidateError(f"{step['step_id']} 的前置步骤排序失败")
    phases = [PHASE_ORDER[step["phase"]] for step in ordered]
    if phases != sorted(phases):
        raise SourceCandidateError("阶段顺序必须为准备、执行、验证、复位")
    return ordered


def _validate_and_deduplicate_candidates(
    source_plan: dict[str, Any],
    evidence_by_id: dict[str, dict[str, Any]],
    canonical_by_key: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    validate_document(source_plan, "source_candidate_plan.schema.json")
    seen_ids: set[str] = set()
    seen_fingerprints: dict[tuple[Any, ...], str] = {}
    kept: list[dict[str, Any]] = []
    deduplicated: list[str] = []
    for raw in source_plan["candidates"]:
        candidate = copy.deepcopy(raw)
        candidate_id = candidate["candidate_id"]
        if candidate_id in seen_ids:
            raise SourceCandidateError(f"source candidate ID 重复: {candidate_id}")
        seen_ids.add(candidate_id)
        unknown_keys = sorted(set(candidate["semantic_keys"]) - set(canonical_by_key))
        if unknown_keys:
            raise SourceCandidateError(
                f"{candidate_id} 引用了未知 semantic_key: {unknown_keys}"
            )
        if candidate["granularity"] == "TOO_COARSE" and len(candidate["semantic_keys"]) < 2:
            raise SourceCandidateError(f"{candidate_id} 标为 TOO_COARSE 但未覆盖多个步骤")
        if candidate["granularity"] != "TOO_COARSE" and len(candidate["semantic_keys"]) != 1:
            raise SourceCandidateError(
                f"{candidate_id} 非粗粒度候选只能对应一个 semantic_key"
            )
        target_phases = {canonical_by_key[key]["phase"] for key in candidate["semantic_keys"]}
        if target_phases != {candidate["phase"]}:
            raise SourceCandidateError(
                f"{candidate_id} 阶段与目标步骤不一致: {sorted(target_phases)}"
            )
        for evidence_id in candidate["evidence_ids"]:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                raise SourceCandidateError(
                    f"{candidate_id} 引用了未知 Evidence ID: {evidence_id}"
                )
            if evidence["source_type"] != candidate["source_type"]:
                raise SourceCandidateError(
                    f"{candidate_id} 的 {evidence_id} 来源类型不匹配"
                )
            if evidence["source_ref"] != candidate["source_ref"]:
                raise SourceCandidateError(
                    f"{candidate_id} 的 {evidence_id} 来源引用不匹配"
                )
            if evidence["review_status"] == "REJECTED":
                raise SourceCandidateError(
                    f"{candidate_id} 不得引用已拒绝证据 {evidence_id}"
                )
        fingerprint = (
            candidate["source_type"],
            candidate["source_ref"],
            _normalize_action(candidate["proposed_action"]),
            tuple(candidate["semantic_keys"]),
            tuple(sorted(candidate["evidence_ids"])),
        )
        if fingerprint in seen_fingerprints:
            deduplicated.append(candidate_id)
            continue
        seen_fingerprints[fingerprint] = candidate_id
        kept.append(candidate)
    source_types = {candidate["source_type"] for candidate in kept}
    if source_types != {"video", "pdf", "audio"}:
        raise SourceCandidateError(
            f"必须分别提供 video、pdf、audio 候选，实际 {sorted(source_types)}"
        )
    return kept, deduplicated


def _group_confidence(candidates: list[dict[str, Any]]) -> float:
    best_by_source: dict[str, float] = {}
    for candidate in candidates:
        value = float(candidate["confidence"])
        if candidate["support_status"] == "PARTIAL":
            value *= 0.85
        best_by_source[candidate["source_type"]] = max(
            value,
            best_by_source.get(candidate["source_type"], 0.0),
        )
    base = sum(best_by_source.values()) / len(best_by_source)
    corroboration = 0.04 * (len(best_by_source) - 1)
    return round(min(0.99, base + corroboration), 3)


def synthesize_source_candidates(
    source_plan: dict[str, Any],
    candidate_plan: dict[str, Any],
    catalog_document: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate, split, merge and order source-specific step candidates."""

    case_ids = {
        str(source_plan.get("case_id", "")),
        str(candidate_plan.get("case_id", "")),
        str(catalog_document.get("case_id", "")),
    }
    if len(case_ids) != 1 or "" in case_ids:
        raise SourceCandidateError(f"输入 case_id 不一致: {sorted(case_ids)}")
    evidence_catalog = _evidence_catalog(catalog_document)
    evidence_by_id = {item["evidence_id"]: item for item in evidence_catalog}
    if len(evidence_by_id) != len(evidence_catalog):
        raise SourceCandidateError("Evidence Catalog 含重复 evidence_id")

    canonical = _canonical_steps(candidate_plan)
    canonical_by_key = {step["semantic_key"]: step for step in canonical}
    ordered_canonical = _topological_order(canonical)
    candidates, deduplicated = _validate_and_deduplicate_candidates(
        source_plan,
        evidence_by_id,
        canonical_by_key,
    )

    parts_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        for index, semantic_key in enumerate(candidate["semantic_keys"], start=1):
            parts_by_key[semantic_key].append(
                {
                    "part_id": f"{candidate['candidate_id']}-P{index:02d}",
                    "candidate": candidate,
                }
            )
    uncovered = [
        step["semantic_key"]
        for step in ordered_canonical
        if not parts_by_key[step["semantic_key"]]
    ]
    if uncovered:
        raise SourceCandidateError(f"规范步骤没有任何来源候选: {uncovered}")

    merge_groups: list[dict[str, Any]] = []
    ordered_steps: list[dict[str, Any]] = []
    ordered_positions = {
        step["step_id"]: index for index, step in enumerate(ordered_canonical)
    }
    for group_index, canonical_step in enumerate(ordered_canonical, start=1):
        semantic_key = canonical_step["semantic_key"]
        parts = parts_by_key[semantic_key]
        members = [part["candidate"] for part in parts]
        candidate_ids = _stable_unique([item["candidate_id"] for item in members])
        source_types = sorted({item["source_type"] for item in members})
        evidence_ids = _stable_unique(
            [evidence_id for item in members for evidence_id in item["evidence_ids"]]
        )
        granularities = {item["granularity"] for item in members}
        normalized_actions = {
            _normalize_action(item["proposed_action"]) for item in members
        }
        operations = []
        rationale_parts = []
        if "TOO_COARSE" in granularities:
            operations.append("COARSE_SPLIT")
            rationale_parts.append("将跨多个完成目标的粗粒度候选拆分")
        if "TOO_FINE" in granularities:
            operations.append("FINE_GRAIN_MERGE")
            rationale_parts.append("把同一完成目标下的过细动作合并")
        if len(members) > 1 and len(normalized_actions) > 1:
            operations.append("SYNONYM_MERGE")
            rationale_parts.append("归一不同来源的同义表述并合并证据")
        if not operations:
            operations = ["KEEP"]
            rationale_parts = ["保留单一原子候选"]
        rationale = canonical_step.get("merge_rationale") or (
            "；".join(rationale_parts) + "。"
        )
        merge_groups.append(
            {
                "group_id": f"MG{group_index:02d}",
                "semantic_key": semantic_key,
                "step_id": canonical_step["step_id"],
                "phase": canonical_step["phase"],
                "operations": operations,
                "candidate_ids": candidate_ids,
                "candidate_part_ids": [part["part_id"] for part in parts],
                "source_types": source_types,
                "evidence_ids": evidence_ids,
                "rationale": rationale,
            }
        )

        parameters = []
        for parameter in canonical_step.get("parameters", []):
            parameter_ids = _resolve_selectors(
                evidence_catalog,
                parameter.get("evidence_selectors", []),
            )
            parameter_ids = _stable_unique(
                parameter_ids + list(parameter.get("synthesis_evidence_ids", []))
            )
            unknown_parameter_ids = sorted(set(parameter_ids) - set(evidence_by_id))
            if unknown_parameter_ids:
                raise SourceCandidateError(
                    f"{canonical_step['step_id']} 参数引用未知证据: {unknown_parameter_ids}"
                )
            parameters.append(
                {
                    "name": parameter["name"],
                    "value": parameter["value"],
                    "unit": parameter.get("unit", ""),
                    "evidence_ids": parameter_ids,
                }
            )
        recovery = copy.deepcopy(canonical_step["recovery"])
        unknown_recovery = sorted(set(recovery["evidence_ids"]) - set(evidence_by_id))
        if unknown_recovery:
            raise SourceCandidateError(
                f"{canonical_step['step_id']} recovery 引用未知证据: {unknown_recovery}"
            )
        target = recovery["target_step_id"]
        if target is not None and target not in {step["step_id"] for step in canonical}:
            raise SourceCandidateError(
                f"{canonical_step['step_id']} recovery 目标不存在: {target}"
            )
        mode = recovery["mode"]
        if mode == "RETURN_TO_STEP":
            if target is None:
                raise SourceCandidateError(
                    f"{canonical_step['step_id']} RETURN_TO_STEP 缺少目标"
                )
            if ordered_positions[target] >= ordered_positions[canonical_step["step_id"]]:
                raise SourceCandidateError(
                    f"{canonical_step['step_id']} recovery 目标必须早于当前步骤"
                )
        elif target is not None:
            raise SourceCandidateError(
                f"{canonical_step['step_id']} 仅 RETURN_TO_STEP 可指定恢复目标"
            )
        confidence = _group_confidence(members)
        ordered_steps.append(
            {
                "step_id": canonical_step["step_id"],
                "semantic_key": semantic_key,
                "title": canonical_step["title"],
                "action": canonical_step["action"],
                "object": canonical_step["object"],
                "phase": canonical_step["phase"],
                "prerequisites": list(canonical_step.get("prerequisites", [])),
                "tools": list(canonical_step.get("tools", [])),
                "parameters": parameters,
                "warnings": list(canonical_step.get("warnings", [])),
                "success_check": canonical_step["success_check"],
                "required": bool(canonical_step["required"]),
                "irreversible": bool(canonical_step["irreversible"]),
                "recovery": recovery,
                "candidate_ids": candidate_ids,
                "source_types": source_types,
                "evidence_ids": evidence_ids,
                "confidence": confidence,
                "status": "NEEDS_REVIEW",
            }
        )

    phase_counts = Counter(step["phase"] for step in ordered_steps)
    phase_applicability = source_plan["phase_applicability"]
    for phase in PHASE_ORDER:
        expected = "APPLICABLE" if phase_counts[phase] else "NOT_APPLICABLE"
        if phase_applicability[phase]["status"] != expected:
            raise SourceCandidateError(
                f"{phase} 阶段适用性应为 {expected}"
            )
    source_counts = Counter(item["source_type"] for item in candidates)
    summary = {
        "source_candidate_count": len(candidates),
        "source_candidate_counts": {
            source_type: source_counts[source_type]
            for source_type in ("video", "pdf", "audio")
        },
        "deduplicated_candidate_ids": deduplicated,
        "ordered_step_count": len(ordered_steps),
        "phase_counts": {
            phase: phase_counts[phase]
            for phase in ("PREPARATION", "EXECUTION", "VERIFICATION", "RESET")
        },
        "coarse_candidate_count": sum(
            item["granularity"] == "TOO_COARSE" for item in candidates
        ),
        "coarse_fragment_count": sum(
            len(item["semantic_keys"])
            for item in candidates
            if item["granularity"] == "TOO_COARSE"
        ),
        "fine_candidate_count": sum(
            item["granularity"] == "TOO_FINE" for item in candidates
        ),
        "coarse_split_group_count": sum(
            "COARSE_SPLIT" in group["operations"] for group in merge_groups
        ),
        "fine_merge_group_count": sum(
            "FINE_GRAIN_MERGE" in group["operations"] for group in merge_groups
        ),
        "synonym_merge_group_count": sum(
            "SYNONYM_MERGE" in group["operations"] for group in merge_groups
        ),
        "multi_source_step_count": sum(
            len(step["source_types"]) >= 2 for step in ordered_steps
        ),
        "three_source_step_count": sum(
            len(step["source_types"]) == 3 for step in ordered_steps
        ),
        "irreversible_step_ids": [
            step["step_id"] for step in ordered_steps if step["irreversible"]
        ],
        "recovery_step_count": sum(
            step["recovery"]["mode"] != "NOT_REQUIRED" for step in ordered_steps
        ),
        "low_confidence_step_ids": [
            step["step_id"] for step in ordered_steps if step["confidence"] < 0.75
        ],
        "all_steps_evidence_grounded": all(
            step["evidence_ids"] for step in ordered_steps
        ),
        "graph_acyclic": True,
    }
    report = {
        "version": 1,
        "case_id": candidate_plan["case_id"],
        "report_id": "SOURCE_CANDIDATE_SYNTHESIS_V1",
        "status": "NEEDS_REVIEW",
        "extraction_mode": source_plan["extraction_mode"],
        "uses_gold_step_text": source_plan["uses_gold_step_text"],
        "source_candidates": candidates,
        "merge_groups": merge_groups,
        "ordered_steps": ordered_steps,
        "phase_applicability": phase_applicability,
        "summary": summary,
        "data_policy": {
            "contains_credentials": False,
            "contains_raw_media": False,
            "contains_absolute_paths": False,
            "external_model_calls": 0,
        },
    }
    validate_document(report, "source_candidate_synthesis.schema.json")

    sop = {
        "case_id": candidate_plan["case_id"],
        "title": candidate_plan["title"],
        "version": int(candidate_plan.get("output_version", 1)),
        "evidence_catalog": evidence_catalog,
        "steps": [
            {
                "step_id": step["step_id"],
                "title": step["title"],
                "action": step["action"],
                "object": step["object"],
                "prerequisites": step["prerequisites"],
                "tools": step["tools"],
                "parameters": step["parameters"],
                "warnings": step["warnings"],
                "success_check": step["success_check"],
                "evidence": step["evidence_ids"],
                "confidence": step["confidence"],
                "required": step["required"],
                "status": "NEEDS_REVIEW",
            }
            for step in ordered_steps
        ],
    }
    validate_document(sop, "sop.schema.json")
    return report, sop


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-plan", type=Path, required=True)
    parser.add_argument("--candidate-plan", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-report", type=Path)
    args = parser.parse_args()
    report, sop = synthesize_source_candidates(
        _read_json(args.source_plan),
        _read_json(args.candidate_plan),
        _read_json(args.catalog),
    )
    args.output.mkdir(parents=True, exist_ok=True)
    _write_json(args.output / "source_candidate_synthesis.json", report)
    _write_json(args.output / "merged_candidate_sop.json", sop)
    if args.public_report is not None:
        _write_json(args.public_report, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "report_id": report["report_id"],
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
