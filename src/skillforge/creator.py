"""Deterministic checklist and evidence-backed quiz generation."""

from __future__ import annotations

from typing import Any


def create_checklist(sop: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": sop["case_id"],
        "sop_version": sop["version"],
        "items": [
            {
                "step_id": step["step_id"],
                "title": step["title"],
                "check": step["success_check"],
                "warnings": step["warnings"],
                "evidence_ids": list(step["evidence"]),
                "completed": False,
            }
            for step in sop["steps"]
        ],
    }


def create_quiz(sop: dict[str, Any], limit: int = 5) -> dict[str, Any]:
    questions = []
    for index, step in enumerate(sop["steps"][:limit], start=1):
        success_check = step["success_check"].rstrip("。.!！?？")
        questions.append(
            {
                "question_id": f"Q{index:02d}",
                "type": "TRUE_FALSE",
                "prompt": f"完成“{step['title']}”后，应确认：{success_check}。",
                "answer": True,
                "explanation": f"该完成判据来自 SOP {step['step_id']} 的已绑定证据。",
                "evidence_ids": list(step["evidence"]),
            }
        )
    return {
        "case_id": sop["case_id"],
        "sop_version": sop["version"],
        "questions": questions,
    }
