"""Evidence-only SOP planning with cross-reference grounding checks."""

from __future__ import annotations

import json
from typing import Any

from .contracts import validate_document
from .step_plan import StepPlanClient


class PlannerGroundingError(ValueError):
    """Raised when a planned step cites evidence outside the supplied catalog."""


class SOPAgent:
    def __init__(self, client: StepPlanClient | None = None) -> None:
        self.client = client or StepPlanClient()

    def plan(
        self,
        evidence_catalog: list[dict[str, Any]],
        *,
        case_id: str,
        title: str,
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        for evidence in evidence_catalog:
            validate_document(evidence, "evidence.schema.json")
        available = {item["evidence_id"] for item in evidence_catalog}
        prompt = (
            "你是 SkillForge SOP Agent。根据给定 Evidence Catalog 生成8到15步结构化SOP。"
            "任何动作、工具、参数、警告和完成标准都只能来自证据；不能凭常识补充。"
            "每个必要步骤必须引用至少一个 evidence_id。步骤编号使用 S01、S02 顺序编号。"
            "prerequisites 只能引用较早步骤。参数没有明确证据时 parameters 必须为空。"
            "status 使用 DRAFT，version=1。每个 step 必须且只能包含以下字段："
            "step_id,title,action,object,prerequisites,tools,parameters,warnings,success_check,"
            "evidence,confidence,required,status。prerequisites、tools、parameters、warnings、evidence 都必须是数组，"
            "即使为空也要输出 []。parameter 若存在，必须包含 name,value,unit,evidence_ids。"
            "顶层必须且只能包含 case_id,title,version,steps。只返回 JSON，不要使用 Markdown。\n"
            "单步形状示例："
            '{"step_id":"S01","title":"准备","action":"执行有证据的动作",'
            '"object":"对象","prerequisites":[],"tools":[],"parameters":[],'
            '"warnings":[],"success_check":"有证据的完成标准","evidence":["E001"],'
            '"confidence":0.8,"required":true,"status":"DRAFT"}\n'
            f"case_id={case_id}\ntitle={title}\nEvidence Catalog:\n"
            + json.dumps(evidence_catalog, ensure_ascii=False, separators=(",", ":"))
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        last_error: PlannerGroundingError | None = None
        for _ in range(max_attempts):
            draft = self.client.chat_json(
                messages=messages,
                route="planner",
                schema_name="sop_draft.schema.json",
                max_attempts=2,
                max_tokens=8192,
            )
            unknown = sorted(
                {
                    evidence_id
                    for step in draft["steps"]
                    for evidence_id in step["evidence"]
                    if evidence_id not in available
                }
                | {
                    evidence_id
                    for step in draft["steps"]
                    for parameter in step["parameters"]
                    for evidence_id in parameter["evidence_ids"]
                    if evidence_id not in available
                }
            )
            missing = [
                step["step_id"]
                for step in draft["steps"]
                if step["required"] and not step["evidence"]
            ]
            if not unknown and not missing:
                document = {
                    "case_id": case_id,
                    "title": title,
                    "version": 1,
                    "evidence_catalog": evidence_catalog,
                    "steps": draft["steps"],
                }
                return validate_document(document, "sop.schema.json")
            last_error = PlannerGroundingError(
                f"未知证据={unknown or '无'}；无证据必要步骤={missing or '无'}"
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"上一次规划未通过证据约束：{last_error}。"
                        "请只使用给定 Evidence Catalog 中的 evidence_id 并重新返回完整 JSON。"
                    ),
                }
            )
        assert last_error is not None
        raise last_error
