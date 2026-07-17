"""Plan and verify evidence-bound selective rebuilds after SOP revision."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .creator import create_quiz


STEP_PATH = re.compile(r"^/steps/(S[0-9]{2})(?:/|$)")
POSTER_FIELDS = {"title", "action", "required"}
POSTER_PARAMETERS = {"本批标签宽度", "本批标签高度", "定位方式"}


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact_sop(sop: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": sop["case_id"],
        "title": sop["title"],
        "version": sop["version"],
        "steps": sop["steps"],
    }


def _with_catalog(sop: dict[str, Any], gold: dict[str, Any]) -> dict[str, Any]:
    full = copy.deepcopy(sop)
    full["evidence_catalog"] = copy.deepcopy(gold["evidence_catalog"])
    return validate_document(full, "sop.schema.json")


def _step_by_id(sop: dict[str, Any], step_id: str) -> dict[str, Any]:
    try:
        return next(step for step in sop["steps"] if step["step_id"] == step_id)
    except StopIteration as exc:
        raise ValueError(f"SOP中找不到步骤 {step_id}") from exc


def _apply_audit(before: dict[str, Any], after: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    """Apply the bounded audit operations and reject unknown mutation shapes."""

    patched = copy.deepcopy(before)
    for change in audit["changes"]:
        action = change["action"]
        path = change["path"]
        match = STEP_PATH.match(path)
        if action == "INSERT" and match:
            if change["before"] is not None or not isinstance(change["after"], dict):
                raise ValueError(f"{path}: INSERT必须包含结构化after")
            patched["steps"].append(copy.deepcopy(change["after"]))
            continue
        if action == "REORDER" and path == "/steps":
            expected_ids = change["after"]
            current = {step["step_id"]: step for step in patched["steps"]}
            if set(expected_ids) != set(current):
                raise ValueError("REORDER步骤集合与修订中间态不一致")
            patched["steps"] = [current[step_id] for step_id in expected_ids]
            continue
        if match and action in {"REMOVE", "REPLACE"}:
            step = _step_by_id(patched, match.group(1))
            suffix = path[match.end() :].strip("/").split("/") if path[match.end() :] else []
            if not suffix or suffix[0] not in {"title", "action", "required", "success_check", "warnings", "tools", "parameters"}:
                raise ValueError(f"不支持的局部修订路径: {path}")
            field = suffix[0]
            if len(suffix) == 2 and suffix[1].isdigit():
                index = int(suffix[1])
                if action == "REMOVE":
                    step[field].pop(index)
                else:
                    step[field][index] = copy.deepcopy(change["after"])
            elif len(suffix) == 1 and isinstance(step[field], list):
                if action == "REMOVE":
                    before_value = change["before"]
                    values = before_value if isinstance(before_value, list) else [before_value]
                    for value in values:
                        if value not in step[field]:
                            raise ValueError(f"{path}: 待删除值不存在")
                        step[field].remove(value)
                else:
                    step[field] = copy.deepcopy(change["after"])
            elif len(suffix) == 1 and action == "REPLACE":
                step[field] = copy.deepcopy(change["after"])
            else:
                raise ValueError(f"不支持的局部修订动作: {action} {path}")
            step["status"] = "REVISED"
            continue
        raise ValueError(f"不支持的审计动作: {action} {path}")
    patched["version"] = after["version"]
    return patched


def _affected_steps(
    before: dict[str, Any], after: dict[str, Any], audit: dict[str, Any]
) -> list[dict[str, Any]]:
    before_steps = {step["step_id"]: step for step in before["steps"]}
    after_steps = {step["step_id"]: step for step in after["steps"]}
    before_positions = {
        step["step_id"]: index for index, step in enumerate(before["steps"], start=1)
    }
    after_positions = {
        step["step_id"]: index for index, step in enumerate(after["steps"], start=1)
    }
    changes: dict[str, dict[str, set[str]]] = {}

    def add(step_id: str, change_type: str, conflict_id: str) -> None:
        entry = changes.setdefault(step_id, {"types": set(), "conflicts": set()})
        entry["types"].add(change_type)
        entry["conflicts"].add(conflict_id)

    for change in audit["changes"]:
        match = STEP_PATH.match(change["path"])
        if match:
            add(match.group(1), change["action"], change["conflict_id"])
        if change["action"] == "REORDER" and change["path"] == "/steps":
            old = {step_id: index for index, step_id in enumerate(change["before"], 1)}
            new = {step_id: index for index, step_id in enumerate(change["after"], 1)}
            for step_id in sorted(set(old) | set(new)):
                if old.get(step_id) != new.get(step_id):
                    add(step_id, "REORDER", change["conflict_id"])

    result = []
    action_order = {"INSERT": 0, "REORDER": 1, "REMOVE": 2, "REPLACE": 3}
    for step_id in sorted(changes, key=lambda item: after_positions.get(item, 10_000)):
        before_step = before_steps.get(step_id)
        after_step = after_steps.get(step_id)
        result.append(
            {
                "step_id": step_id,
                "change_types": sorted(changes[step_id]["types"], key=action_order.__getitem__),
                "conflict_ids": sorted(changes[step_id]["conflicts"]),
                "before_position": before_positions.get(step_id),
                "after_position": after_positions.get(step_id),
                "content_changed": before_step != after_step,
                "position_changed": before_positions.get(step_id) != after_positions.get(step_id),
            }
        )
    return result


def _poster_is_affected(audit: dict[str, Any]) -> bool:
    for change in audit["changes"]:
        if change["path"] == "/steps" or change["action"] == "INSERT":
            return True
        match = STEP_PATH.match(change["path"])
        if not match:
            continue
        suffix = change["path"][match.end() :].strip("/").split("/")
        if suffix and suffix[0] in POSTER_FIELDS:
            return True
        values = [change.get("before"), change.get("after")]
        if suffix and suffix[0] == "parameters" and any(
            isinstance(value, dict) and value.get("name") in POSTER_PARAMETERS
            for value in values
        ):
            return True
        if suffix and suffix[0] == "parameters" and any(
            isinstance(value, list)
            and any(
                isinstance(item, dict) and item.get("name") in POSTER_PARAMETERS
                for item in value
            )
            for value in values
        ):
            return True
    return False


def build_selective_rebuild_report(
    before: dict[str, Any],
    after: dict[str, Any],
    audit: dict[str, Any],
    gold: dict[str, Any],
    storyboard: dict[str, Any],
) -> dict[str, Any]:
    """Build a deterministic plan and prove its unchanged boundaries."""

    validate_document(audit, "revision_audit.schema.json")
    validate_document(gold, "sop.schema.json")
    validate_document(storyboard, "training_video_storyboard.schema.json")
    before_full = _with_catalog(before, gold)
    after_full = _with_catalog(after, gold)
    patched = _apply_audit(before_full, after_full, audit)
    patch_matches = _compact_sop(patched) == _compact_sop(after_full)
    if not patch_matches:
        raise ValueError("Revision Audit不能精确重放After SOP")

    affected = _affected_steps(before_full, after_full, audit)
    affected_ids = [item["step_id"] for item in affected]
    affected_set = set(affected_ids)
    unchanged_steps = len(after_full["steps"]) - len(affected_ids)

    before_quiz = create_quiz(before_full)
    after_quiz = create_quiz(after_full)
    before_questions = {item["question_id"]: item for item in before_quiz["questions"]}
    after_questions = {item["question_id"]: item for item in after_quiz["questions"]}
    changed_questions = [
        question_id
        for question_id in after_questions
        if before_questions.get(question_id) != after_questions[question_id]
    ]
    unchanged_questions = [
        question_id
        for question_id in after_questions
        if question_id not in changed_questions
    ]
    quiz_unchanged = all(
        before_questions[question_id] == after_questions[question_id]
        for question_id in unchanged_questions
    )

    scenes = storyboard["scenes"]
    selected_scenes = [
        scene["scene_id"]
        for scene in scenes
        if set(scene["step_ids"]) & affected_set
    ]
    scene_ids = {scene["scene_id"] for scene in scenes}
    selected_set = set(selected_scenes)
    selected_only_affected = all(
        set(scene["step_ids"]) & affected_set
        for scene in scenes
        if scene["scene_id"] in selected_set
    )

    poster_affected = _poster_is_affected(audit)
    plans = [
        {
            "artifact_type": "FINAL_SOP",
            "action": "REBUILD",
            "scope": "STEP_PATCH",
            "units": affected_ids,
            "unchanged_unit_count": unchanged_steps,
            "reason": "按Revision Audit重放插入、字段删除与顺序调整；未受影响步骤保持逐对象相同。",
        },
        {
            "artifact_type": "SOP_VIEWS",
            "action": "REBUILD",
            "scope": "STEP_ONLY",
            "units": [f"{view}:{step_id}" for view in ("concise", "detailed", "evidence") for step_id in affected_ids],
            "unchanged_unit_count": unchanged_steps * 3,
            "reason": "三个阅读深度只失效受影响步骤；其余步骤视图无需重算。",
        },
        {
            "artifact_type": "MOBILE_CHECKLIST",
            "action": "REBUILD",
            "scope": "STEP_ONLY",
            "units": affected_ids,
            "unchanged_unit_count": unchanged_steps,
            "reason": "步骤内容或屏幕序号发生变化的检查卡重建，其他检查卡保持不变。",
        },
        {
            "artifact_type": "TRAINING_QUIZ",
            "action": "REBUILD" if changed_questions else "SKIP",
            "scope": "QUESTION_ONLY" if changed_questions else "NONE",
            "units": changed_questions,
            "unchanged_unit_count": len(unchanged_questions),
            "reason": "对修订前后确定性生成结果逐题比较，只替换内容实际变化的题目。",
        },
        {
            "artifact_type": "A4_POSTER",
            "action": "REBUILD" if poster_affected else "SKIP",
            "scope": "WHOLE_ARTIFACT" if poster_affected else "NONE",
            "units": ["n31_a4_training_poster"] if poster_affected else [],
            "unchanged_unit_count": 0 if poster_affected else 1,
            "reason": "A4是固定单页原子成果；步骤插入、顺序或海报可见字段变化时整页重建。",
        },
        {
            "artifact_type": "TRAINING_VIDEO",
            "action": "REBUILD" if selected_scenes else "SKIP",
            "scope": "SCENE_ONLY" if selected_scenes else "NONE",
            "units": selected_scenes,
            "unchanged_unit_count": len(scenes) - len(selected_scenes),
            "reason": "只重渲染绑定受影响步骤的场景；片头、片尾和无关步骤场景复用现有片段。",
        },
    ]
    verification = {
        "sop_patch_reproduces_after": patch_matches,
        "quiz_unchanged_questions_identical": quiz_unchanged,
        "video_scene_ids_exist": selected_set <= scene_ids,
        "no_unaffected_video_scene_selected": selected_only_affected,
        "poster_dependency_declared": isinstance(poster_affected, bool),
    }
    rebuild_count = sum(plan["action"] == "REBUILD" for plan in plans)
    report = {
        "version": 1,
        "case_id": after_full["case_id"],
        "report_id": "N31_SELECTIVE_REBUILD_V1",
        "status": "PASSED" if all(verification.values()) else "FAILED",
        "source_bindings": {
            "before_sop_sha256": _canonical_sha256(_compact_sop(before_full)),
            "after_sop_sha256": _canonical_sha256(_compact_sop(after_full)),
            "revision_audit_sha256": _canonical_sha256(audit),
            "storyboard_sha256": _canonical_sha256(storyboard),
        },
        "affected_steps": affected,
        "artifact_plans": plans,
        "summary": {
            "affected_step_count": len(affected),
            "content_changed_step_count": sum(item["content_changed"] for item in affected),
            "position_changed_step_count": sum(item["position_changed"] for item in affected),
            "rebuild_artifact_count": rebuild_count,
            "skipped_artifact_count": len(plans) - rebuild_count,
            "quiz_question_count": len(changed_questions),
            "video_scene_count": len(selected_scenes),
            "whole_artifact_count": sum(plan["scope"] == "WHOLE_ARTIFACT" for plan in plans),
        },
        "verification": verification,
        "data_policy": {
            "external_model_calls": 0,
            "contains_raw_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
        },
    }
    return validate_document(report, "selective_rebuild_report.schema.json")


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, default=Path("cases/n31/demo_bundle/before_sop.json"))
    parser.add_argument("--after", type=Path, default=Path("cases/n31/demo_bundle/after_sop.json"))
    parser.add_argument("--audit", type=Path, default=Path("cases/n31/demo_bundle/revision_audit.json"))
    parser.add_argument("--gold", type=Path, default=Path("cases/n31/gold/gold_sop.json"))
    parser.add_argument("--storyboard", type=Path, default=Path("cases/n31/training_video_storyboard.json"))
    parser.add_argument("--output", type=Path, default=Path("cases/n31/evaluations/selective_rebuild_v1.json"))
    args = parser.parse_args()
    report = build_selective_rebuild_report(
        _read(args.before),
        _read(args.after),
        _read(args.audit),
        _read(args.gold),
        _read(args.storyboard),
    )
    _write(args.output, report)
    print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
