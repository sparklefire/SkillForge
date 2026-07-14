"""Step 3.7 frame perception with canonical, non-model-controlled locators."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .step_plan import StepPlanClient


def image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


class PerceptionAgent:
    def __init__(self, client: StepPlanClient | None = None) -> None:
        self.client = client or StepPlanClient()

    def analyze_frame(
        self,
        frame_path: Path,
        *,
        evidence_id: str,
        source_ref: str,
        keyframe_ref: str,
        start_ms: int,
        end_ms: int,
    ) -> dict[str, Any]:
        prompt = (
            "你是 SkillForge Perception Agent。只描述画面中直接可见的工具、部件、动作和设备状态。"
            "不得根据常识补充未出现的参数、操作或安全承诺。返回一个符合 Evidence Schema 的 JSON 对象。"
            f"evidence_id 必须为 {evidence_id}；source_type=video；source_ref={source_ref}；"
            f"locator.start_ms={start_ms}；locator.end_ms={end_ms}；"
            f"locator.keyframe={keyframe_ref}；classification=MODEL_INFERENCE；"
            "review_status=UNREVIEWED。claim 用一句中文描述可见事实。"
            "必须包含0到1之间的 relevance 和 confidence。只返回 JSON，不要使用 Markdown。"
        )
        result = self.client.chat_json(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_data_url(frame_path),
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
            route="planner",
            schema_name="evidence.schema.json",
            max_attempts=2,
            max_tokens=2048,
        )
        # The model may describe the frame, but it may never choose or alter provenance.
        result.update(
            {
                "evidence_id": evidence_id,
                "source_type": "video",
                "source_ref": source_ref,
                "locator": {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "keyframe": keyframe_ref,
                },
                "classification": "MODEL_INFERENCE",
                "review_status": "UNREVIEWED",
            }
        )
        return validate_document(result, "evidence.schema.json")
