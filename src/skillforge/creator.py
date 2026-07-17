"""Deterministic SOP views, checklist, and evidence-backed quiz generation."""

from __future__ import annotations

from typing import Any

from .contracts import validate_document


def _catalog(sop: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["evidence_id"]: item for item in sop["evidence_catalog"]}


def _source_summaries(
    evidence_ids: list[str], catalog: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    sources = []
    for evidence_id in evidence_ids:
        evidence = catalog[evidence_id]
        key = (evidence["source_type"], evidence["source_ref"])
        if key in seen:
            continue
        seen.add(key)
        sources.append({"source_type": key[0], "source_ref": key[1]})
    return sources


def _evidence_details(
    evidence_ids: list[str], catalog: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {
            "evidence_id": evidence_id,
            "source_type": catalog[evidence_id]["source_type"],
            "source_ref": catalog[evidence_id]["source_ref"],
            "claim": catalog[evidence_id]["claim"],
            "locator": catalog[evidence_id]["locator"],
            "classification": catalog[evidence_id]["classification"],
            "review_status": catalog[evidence_id]["review_status"],
        }
        for evidence_id in evidence_ids
    ]


def _base_view_step(
    step: dict[str, Any], catalog: dict[str, dict[str, Any]], *, concise: bool
) -> dict[str, Any]:
    completion = step["success_check"]
    return {
        "step_id": step["step_id"],
        "title": step["title"],
        "action": step["action"],
        "reason": (
            f"用于确认：{completion}"
            if concise
            else f"执行本步是为了达到可验证的完成状态：{completion}"
        ),
        "completion_marker": completion,
        "risks": list(step["warnings"]),
        "sources": _source_summaries(step["evidence"], catalog),
    }


def create_sop_views(sop: dict[str, Any]) -> dict[str, Any]:
    """Render three traceable reading depths from one verified SOP."""

    catalog = _catalog(sop)
    concise_steps = [
        _base_view_step(step, catalog, concise=True) for step in sop["steps"]
    ]
    detailed_steps = []
    evidence_steps = []
    for step in sop["steps"]:
        base = _base_view_step(step, catalog, concise=False)
        detailed_steps.append(
            {
                **base,
                "prerequisites": list(step["prerequisites"]),
                "tools": list(step["tools"]),
                "parameters": list(step["parameters"]),
                "required": step["required"],
                "status": step["status"],
            }
        )
        evidence_steps.append(
            {
                **base,
                "evidence_details": _evidence_details(step["evidence"], catalog),
            }
        )
    document = {
        "artifact_type": "SOP_VIEWS",
        "version": 1,
        "case_id": sop["case_id"],
        "sop_version": sop["version"],
        "title": sop["title"],
        "views": {
            "concise": {
                "view_id": "CONCISE",
                "description": "现场快速执行；保留动作、原因、完成标志、风险和来源。",
                "steps": concise_steps,
            },
            "detailed": {
                "view_id": "DETAILED",
                "description": "培训与审核；补充前置步骤、工具、参数和状态。",
                "steps": detailed_steps,
            },
            "evidence": {
                "view_id": "EVIDENCE",
                "description": "逐步展开证据分类、审核状态和页码或时间点。",
                "steps": evidence_steps,
            },
        },
    }
    return validate_document(document, "sop_views.schema.json")


def _visual_index(visual_review: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if visual_review is None:
        return {}
    return {item["step_id"]: item for item in visual_review.get("assessments", [])}


def _keyframe(
    step: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
    assessment: dict[str, Any] | None,
    *,
    preview_path: str | None,
) -> dict[str, Any] | None:
    if assessment and assessment.get("frames"):
        frame = assessment["frames"][0]
        return {
            "evidence_id": frame["evidence_id"],
            "source_ref": frame["source_ref"],
            "start_ms": frame["start_ms"],
            "end_ms": frame["end_ms"],
            "keyframe": frame["keyframe"],
            "visual_status": assessment["model_result"]["verdict"],
            "preview_path": preview_path,
        }
    for evidence_id in step["evidence"]:
        evidence = catalog[evidence_id]
        locator = evidence["locator"]
        if evidence["source_type"] == "video" and locator.get("keyframe"):
            return {
                "evidence_id": evidence_id,
                "source_ref": evidence["source_ref"],
                "start_ms": locator["start_ms"],
                "end_ms": locator["end_ms"],
                "keyframe": locator["keyframe"],
                "visual_status": "UNREVIEWED",
                "preview_path": preview_path,
            }
    return None


def create_checklist(
    sop: dict[str, Any], visual_review: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a one-step-per-screen checklist template with evidence locators."""

    catalog = _catalog(sop)
    assessments = _visual_index(visual_review)
    document = {
        "artifact_type": "MOBILE_CHECKLIST",
        "version": 1,
        "case_id": sop["case_id"],
        "sop_version": sop["version"],
        "interaction_mode": "ONE_STEP_PER_SCREEN",
        "progress": {
            "total_items": len(sop["steps"]),
            "completed_items": 0,
            "status": "NOT_STARTED",
        },
        "items": [
            {
                "item_id": f"CL{index:02d}",
                "screen_index": index,
                "step_id": step["step_id"],
                "title": step["title"],
                "action": step["action"],
                "reason": f"完成本步后应确认：{step['success_check']}",
                "check": step["success_check"],
                "warnings": step["warnings"],
                "risk_level": "WARNING" if step["warnings"] else "NORMAL",
                "keyframe": _keyframe(
                    step,
                    catalog,
                    assessments.get(step["step_id"]),
                    preview_path=(
                        f"output/checklist_thumbnails/{step['step_id']}.jpg"
                        if sop["case_id"] == "n31_media_change"
                        else None
                    ),
                ),
                "evidence_ids": list(step["evidence"]),
                "evidence_details": _evidence_details(step["evidence"], catalog),
                "required": step["required"],
                "completed": False,
            }
            for index, step in enumerate(sop["steps"], start=1)
        ],
        "completion_log": [],
        "feedback_log": [],
    }
    return validate_document(document, "mobile_checklist.schema.json")


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _step_evidence(steps: list[dict[str, Any]]) -> list[str]:
    return _unique(
        [evidence_id for step in steps for evidence_id in step["evidence"]]
    )


def _quiz_options(
    values: list[tuple[str, list[str]]], *, ordering: list[int] | None = None
) -> list[dict[str, Any]]:
    indices = ordering or list(range(len(values)))
    return [
        {
            "option_id": f"O{position:02d}",
            "text": values[index][0],
            "value": values[index][0],
            "evidence_ids": list(values[index][1]),
        }
        for position, index in enumerate(indices, start=1)
    ]


def _ordered_steps(steps: list[dict[str, Any]], count: int = 4) -> list[dict[str, Any]]:
    by_id = {step["step_id"]: step for step in steps}
    children: dict[str, list[str]] = {step_id: [] for step_id in by_id}
    for step in steps:
        for prerequisite in step["prerequisites"]:
            if prerequisite in children:
                children[prerequisite].append(step["step_id"])
    order = {step["step_id"]: index for index, step in enumerate(steps)}
    for values in children.values():
        values.sort(key=order.__getitem__)

    def path_from(step_id: str, visiting: frozenset[str] = frozenset()) -> list[str]:
        if step_id in visiting:
            return [step_id]
        next_visiting = visiting | {step_id}
        best = [step_id]
        for child in children[step_id]:
            if child in next_visiting:
                continue
            candidate = [step_id, *path_from(child, next_visiting)]
            if len(candidate) > len(best):
                best = candidate
        return best

    longest = max((path_from(step["step_id"]) for step in steps), key=len)
    selected_ids = longest[:count] if len(longest) >= 3 else [
        step["step_id"] for step in steps[:count]
    ]
    return [by_id[step_id] for step_id in selected_ids]


def _question(
    *,
    question_id: str,
    category: str,
    question_type: str,
    prompt: str,
    steps: list[dict[str, Any]],
    options: list[dict[str, Any]],
    answer: bool | str | list[str],
    explanation: str,
    answer_evidence_ids: list[str],
    explanation_evidence_ids: list[str],
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    evidence_ids = _unique(
        [
            *_step_evidence(steps),
            *(item for option in options for item in option["evidence_ids"]),
        ]
    )
    return {
        "question_id": question_id,
        "category": category,
        "type": question_type,
        "prompt": prompt,
        "step_ids": [step["step_id"] for step in steps],
        "options": options,
        "answer": answer,
        "explanation": explanation,
        "evidence_ids": evidence_ids,
        "answer_evidence_ids": _unique(answer_evidence_ids),
        "explanation_evidence_ids": _unique(explanation_evidence_ids),
        "evidence_details": [catalog[item] for item in evidence_ids],
    }


def _validate_quiz_integrity(
    quiz: dict[str, Any], sop: dict[str, Any]
) -> dict[str, Any]:
    catalog_ids = {item["evidence_id"] for item in sop["evidence_catalog"]}
    step_by_id = {step["step_id"]: step for step in sop["steps"]}
    for question in quiz["questions"]:
        evidence_ids = set(question["evidence_ids"])
        if not evidence_ids <= catalog_ids:
            raise ValueError(f"{question['question_id']} 引用了未知Evidence")
        if not set(question["answer_evidence_ids"]) <= evidence_ids:
            raise ValueError(f"{question['question_id']} 答案来源越界")
        if not set(question["explanation_evidence_ids"]) <= evidence_ids:
            raise ValueError(f"{question['question_id']} 解析来源越界")
        detail_ids = {item["evidence_id"] for item in question["evidence_details"]}
        if detail_ids != evidence_ids:
            raise ValueError(f"{question['question_id']} Evidence详情不完整")
        option_values = [option["value"] for option in question["options"]]
        if len(option_values) != len(set(option_values)):
            raise ValueError(f"{question['question_id']} 选项重复")
        if any(
            not set(option["evidence_ids"]) <= evidence_ids
            for option in question["options"]
        ):
            raise ValueError(f"{question['question_id']} 选项来源越界")
        answers = question["answer"]
        if question["type"] != "TRUE_FALSE":
            answer_values = answers if isinstance(answers, list) else [answers]
            if not set(answer_values) <= set(option_values):
                raise ValueError(f"{question['question_id']} 答案不在选项中")
        target = step_by_id[question["step_ids"][0]]
        if question["category"] == "TOOL_SELECTION" and set(answers) != set(
            target["tools"]
        ):
            raise ValueError("工具题答案与SOP不一致")
        if question["category"] == "ORDERING":
            if set(answers) != set(question["step_ids"]):
                raise ValueError("排序题答案步骤不完整")
            positions = {step_id: index for index, step_id in enumerate(answers)}
            for step_id in answers:
                for prerequisite in step_by_id[step_id]["prerequisites"]:
                    if prerequisite in positions and positions[prerequisite] > positions[
                        step_id
                    ]:
                        raise ValueError("排序题答案违反SOP依赖")
        if question["category"] == "RISK_RESPONSE" and answers not in target[
            "warnings"
        ]:
            raise ValueError("风险题答案与SOP不一致")
        if question["category"] == "STATUS_RECOGNITION" and answers != target[
            "success_check"
        ]:
            raise ValueError("状态题答案与SOP不一致")
        if question["category"] == "ERROR_JUDGMENT" and answers is not False:
            raise ValueError("错误判断题必须明确拒绝风险操作")
    return validate_document(quiz, "training_quiz.schema.json")


def create_quiz(sop: dict[str, Any]) -> dict[str, Any]:
    """Build five different, fully evidence-backed training questions."""

    steps = sop["steps"]
    catalog = _catalog(sop)

    ordering_steps = _ordered_steps(steps)
    ordering_values = [
        (step["step_id"], list(step["evidence"])) for step in ordering_steps
    ]
    order_pattern = [2, 0, 3, 1] if len(ordering_steps) == 4 else [1, 2, 0]
    ordering_options = _quiz_options(ordering_values, ordering=order_pattern)

    tool_step = next((step for step in steps if step["tools"]), None)
    if tool_step is None:
        raise ValueError("无法生成工具题：SOP没有已绑定工具")
    tool_values: list[tuple[str, list[str]]] = [
        (tool, list(tool_step["evidence"])) for tool in tool_step["tools"]
    ]
    for step in steps:
        for tool in step["tools"]:
            if tool not in {item[0] for item in tool_values}:
                tool_values.append((tool, list(step["evidence"])))
            if len(tool_values) >= 4:
                break
        if len(tool_values) >= 4:
            break
    if len(tool_values) < 2:
        raise ValueError("无法生成工具题：缺少有来源的干扰项")
    tool_options = _quiz_options(tool_values)

    warning_steps = [step for step in steps if step["warnings"]]
    if len(warning_steps) < 3:
        raise ValueError("无法生成风险题：至少需要三个有风险提示的步骤")
    risk_step = max(warning_steps, key=lambda item: len(item["warnings"]))
    risk_warning = risk_step["warnings"][0]
    risk_values = [(risk_warning, list(risk_step["evidence"]))]
    for step in warning_steps:
        if step["step_id"] == risk_step["step_id"]:
            continue
        warning = step["warnings"][0]
        if warning not in {item[0] for item in risk_values}:
            risk_values.append((warning, list(step["evidence"])))
        if len(risk_values) == 3:
            break
    risk_options = _quiz_options(risk_values, ordering=[1, 0, 2])

    status_step = steps[-1]
    status_values = [(status_step["success_check"], list(status_step["evidence"]))]
    for step in steps:
        if step["step_id"] == status_step["step_id"]:
            continue
        value = step["success_check"]
        if value in {item[0] for item in status_values}:
            value = f"{step['step_id']}步骤完成标志：{value}"
        status_values.append((value, list(step["evidence"])))
        if len(status_values) == 3:
            break
    status_options = _quiz_options(status_values, ordering=[2, 0, 1])

    error_step = next(
        step for step in reversed(warning_steps) if step["step_id"] != risk_step["step_id"]
    )
    error_warning = error_step["warnings"][0]

    questions = [
        _question(
            question_id="Q01",
            category="ORDERING",
            question_type="ORDERING",
            prompt="请按SOP依赖关系排列以下步骤。",
            steps=ordering_steps,
            options=ordering_options,
            answer=[step["step_id"] for step in ordering_steps],
            explanation=(
                "正确顺序为"
                + " → ".join(step["step_id"] for step in ordering_steps)
                + "；每一步均在SOP中绑定前置关系和Evidence。"
            ),
            answer_evidence_ids=_step_evidence(ordering_steps),
            explanation_evidence_ids=_step_evidence(ordering_steps),
            catalog=catalog,
        ),
        _question(
            question_id="Q02",
            category="TOOL_SELECTION",
            question_type="MULTIPLE_SELECT",
            prompt=f"执行“{tool_step['title']}”需要选择哪些已确认工具或介质？",
            steps=[tool_step],
            options=tool_options,
            answer=list(tool_step["tools"]),
            explanation=(
                f"SOP {tool_step['step_id']} 仅列出："
                + "、".join(tool_step["tools"])
                + "；其他选项属于其他步骤，不能跨步骤借用。"
            ),
            answer_evidence_ids=list(tool_step["evidence"]),
            explanation_evidence_ids=list(tool_step["evidence"]),
            catalog=catalog,
        ),
        _question(
            question_id="Q03",
            category="RISK_RESPONSE",
            question_type="SINGLE_CHOICE",
            prompt=f"哪一项是“{risk_step['title']}”必须遵守的风险提示？",
            steps=[risk_step],
            options=risk_options,
            answer=risk_warning,
            explanation=(
                f"该提示直接来自SOP {risk_step['step_id']}；其余选项虽有来源，"
                "但属于其他步骤。"
            ),
            answer_evidence_ids=list(risk_step["evidence"]),
            explanation_evidence_ids=list(risk_step["evidence"]),
            catalog=catalog,
        ),
        _question(
            question_id="Q04",
            category="STATUS_RECOGNITION",
            question_type="SINGLE_CHOICE",
            prompt=f"完成“{status_step['title']}”后，哪种状态符合验收标准？",
            steps=[status_step],
            options=status_options,
            answer=status_step["success_check"],
            explanation=(
                f"正确状态是SOP {status_step['step_id']} 的完成标志，"
                "其他状态属于较早步骤。"
            ),
            answer_evidence_ids=list(status_step["evidence"]),
            explanation_evidence_ids=list(status_step["evidence"]),
            catalog=catalog,
        ),
        _question(
            question_id="Q05",
            category="ERROR_JUDGMENT",
            question_type="TRUE_FALSE",
            prompt=(
                f"判断：在“{error_step['title']}”中，即使出现以下风险提示，"
                f"仍可忽略并继续操作——{error_warning}"
            ),
            steps=[error_step],
            options=[],
            answer=False,
            explanation=(
                f"错误。SOP {error_step['step_id']} 明确记录该风险提示，"
                "不能将有来源的停止或防护要求改写为可忽略。"
            ),
            answer_evidence_ids=list(error_step["evidence"]),
            explanation_evidence_ids=list(error_step["evidence"]),
            catalog=catalog,
        ),
    ]
    quiz = {
        "artifact_type": "TRAINING_QUIZ",
        "version": 1,
        "case_id": sop["case_id"],
        "sop_version": sop["version"],
        "coverage": {
            "question_count": 5,
            "category_count": 5,
            "categories": [
                "ORDERING",
                "TOOL_SELECTION",
                "RISK_RESPONSE",
                "STATUS_RECOGNITION",
                "ERROR_JUDGMENT",
            ],
            "all_answers_grounded": True,
            "all_explanations_grounded": True,
        },
        "questions": questions,
    }
    return _validate_quiz_integrity(quiz, sop)
