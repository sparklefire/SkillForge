"""Private local completion and feedback records for mobile checklists."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from .contracts import validate_document


SESSION_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
FEEDBACK_CATEGORIES = {
    "CONTENT_ERROR",
    "EVIDENCE_ISSUE",
    "STEP_BLOCKED",
    "OTHER",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChecklistSessionStore:
    """Persist checklist interactions under an ignored, mode-600 directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self._lock = Lock()

    def _path(self, session_id: str) -> Path:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise FileNotFoundError(session_id)
        return self.root / f"{session_id}.json"

    def _write(self, document: dict[str, Any]) -> None:
        validate_document(document, "checklist_session.schema.json")
        path = self._path(document["session_id"])
        temporary = self.root / f".{document['session_id']}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)

    def create(self, checklist: dict[str, Any]) -> dict[str, Any]:
        validate_document(checklist, "mobile_checklist.schema.json")
        now = _timestamp()
        document = {
            "artifact_type": "CHECKLIST_SESSION",
            "version": 1,
            "session_id": uuid.uuid4().hex,
            "case_id": checklist["case_id"],
            "sop_version": checklist["sop_version"],
            "created_at": now,
            "updated_at": now,
            "status": "NOT_STARTED",
            "progress": {
                "total_items": len(checklist["items"]),
                "completed_items": 0,
            },
            "items": [
                {
                    "item_id": item["item_id"],
                    "step_id": item["step_id"],
                    "title": item["title"],
                    "completed": False,
                }
                for item in checklist["items"]
            ],
            "completion_log": [],
            "feedback_log": [],
        }
        with self._lock:
            self._write(document)
        return document

    def get(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(session_id)
        document = json.loads(path.read_text(encoding="utf-8"))
        return validate_document(document, "checklist_session.schema.json")

    def update_item(
        self,
        session_id: str,
        step_id: str,
        *,
        completed: bool | None = None,
        feedback_category: str | None = None,
        feedback_comment: str | None = None,
    ) -> dict[str, Any]:
        if completed is None and feedback_category is None and feedback_comment is None:
            raise ValueError("至少提供完成状态或问题反馈")
        if feedback_category is None and feedback_comment is not None:
            raise ValueError("问题反馈必须选择分类")
        if feedback_category is not None:
            if not isinstance(feedback_category, str) or (
                feedback_comment is not None
                and not isinstance(feedback_comment, str)
            ):
                raise ValueError("问题反馈分类和内容必须为文本")
            if feedback_category not in FEEDBACK_CATEGORIES:
                raise ValueError("不支持的问题反馈分类")
            comment = (feedback_comment or "").strip()
            if not comment:
                raise ValueError("问题反馈内容不能为空")
            if len(comment) > 500:
                raise ValueError("问题反馈不能超过500字")
        else:
            comment = ""

        with self._lock:
            document = self.get(session_id)
            item = next(
                (candidate for candidate in document["items"] if candidate["step_id"] == step_id),
                None,
            )
            if item is None:
                raise ValueError("检查清单中不存在该步骤")
            now = _timestamp()
            if completed is not None and item["completed"] != completed:
                item["completed"] = completed
                document["completion_log"].append(
                    {
                        "event_id": uuid.uuid4().hex,
                        "step_id": step_id,
                        "action": "COMPLETED" if completed else "REOPENED",
                        "recorded_at": now,
                    }
                )
            if feedback_category is not None:
                document["feedback_log"].append(
                    {
                        "feedback_id": uuid.uuid4().hex,
                        "step_id": step_id,
                        "category": feedback_category,
                        "comment": comment,
                        "recorded_at": now,
                    }
                )
            completed_count = sum(item["completed"] for item in document["items"])
            document["progress"]["completed_items"] = completed_count
            if completed_count == len(document["items"]):
                document["status"] = "COMPLETED"
            elif document["completion_log"] or document["feedback_log"]:
                document["status"] = "IN_PROGRESS"
            else:
                document["status"] = "NOT_STARTED"
            document["updated_at"] = now
            self._write(document)
        return document
