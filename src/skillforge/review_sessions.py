"""Private operator review sessions with bounded SOP operations."""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from .contracts import validate_document
from .creator import create_checklist, create_quiz, create_sop_views
from .revision import digest


SESSION_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(
    action: str,
    step_id: str,
    *,
    detail: str,
    before_position: int | None = None,
    after_position: int | None = None,
) -> dict[str, Any]:
    return {
        "event_id": uuid.uuid4().hex,
        "action": action,
        "step_id": step_id,
        "before_position": before_position,
        "after_position": after_position,
        "actor": "OPERATOR",
        "automatic": False,
        "detail": detail,
        "recorded_at": _timestamp(),
    }


def _validate_order(steps: list[dict[str, Any]]) -> None:
    positions = {item["step_id"]: index for index, item in enumerate(steps, 1)}
    if len(positions) != len(steps):
        raise ValueError("审核步骤ID重复")
    if sorted(item["position"] for item in steps) != list(range(1, len(steps) + 1)):
        raise ValueError("审核步骤位置必须连续且唯一")
    if any(item["position"] != index for index, item in enumerate(steps, 1)):
        raise ValueError("审核步骤数组顺序与位置字段不一致")
    for item in steps:
        for prerequisite in item["prerequisites"]:
            if prerequisite not in positions:
                raise ValueError(f"{item['step_id']} 引用未知前置步骤 {prerequisite}")
            if positions[prerequisite] >= positions[item["step_id"]]:
                raise ValueError(
                    f"重排违反前置依赖：{prerequisite} 必须先于 {item['step_id']}"
                )


def _validate_session(document: dict[str, Any]) -> dict[str, Any]:
    validate_document(document, "sop_review_session.schema.json")
    _validate_order(document["steps"])
    if any(item["confirmed"] and not item["locked"] for item in document["steps"]):
        raise ValueError("已确认步骤必须保持锁定")
    expected = "COMPLETED" if all(item["confirmed"] for item in document["steps"]) else "IN_REVIEW"
    if document["status"] != expected:
        raise ValueError("审核会话状态与步骤确认状态不一致")
    return document


class SopReviewSessionStore:
    """Persist operator-only review state under an ignored mode-600 directory."""

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
        _validate_session(document)
        path = self._path(document["session_id"])
        temporary = self.root / f".{document['session_id']}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)

    @staticmethod
    def _assert_source(document: dict[str, Any], sop: dict[str, Any]) -> None:
        validate_document(sop, "sop.schema.json")
        if document["case_id"] != sop["case_id"] or document["sop_version"] != sop["version"]:
            raise ValueError("审核会话与当前SOP版本不一致")
        if document["source_sop_sha256"] != digest(sop):
            raise ValueError("审核会话绑定的SOP内容已经变化，请新建会话")

    def create(self, sop: dict[str, Any]) -> dict[str, Any]:
        validate_document(sop, "sop.schema.json")
        now = _timestamp()
        document = {
            "artifact_type": "SOP_REVIEW_SESSION",
            "version": 1,
            "session_id": uuid.uuid4().hex,
            "case_id": sop["case_id"],
            "sop_version": sop["version"],
            "source_sop_sha256": digest(sop),
            "created_at": now,
            "updated_at": now,
            "status": "IN_REVIEW",
            "steps": [
                {
                    "step_id": step["step_id"],
                    "title": step["title"],
                    "position": index,
                    "prerequisites": list(step["prerequisites"]),
                    "locked": False,
                    "confirmed": False,
                    "rebuild_count": 0,
                }
                for index, step in enumerate(sop["steps"], 1)
            ],
            "events": [],
            "data_policy": {
                "external_model_calls": 0,
                "contains_raw_media": False,
                "contains_credentials": False,
                "contains_absolute_paths": False,
            },
        }
        with self._lock:
            self._write(document)
        return document

    def get(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(session_id)
        return _validate_session(json.loads(path.read_text(encoding="utf-8")))

    def set_step_state(
        self,
        session_id: str,
        step_id: str,
        sop: dict[str, Any],
        *,
        locked: bool | None = None,
        confirmed: bool | None = None,
    ) -> dict[str, Any]:
        if locked is None and confirmed is None:
            raise ValueError("至少提供锁定或确认状态")
        with self._lock:
            document = self.get(session_id)
            self._assert_source(document, sop)
            item = next((value for value in document["steps"] if value["step_id"] == step_id), None)
            if item is None:
                raise ValueError("审核会话中不存在该步骤")
            if locked is False and item["confirmed"] and confirmed is not False:
                raise ValueError("已确认步骤必须先撤回确认才能解锁")
            if confirmed is True and not (item["locked"] or locked is True):
                raise ValueError("步骤必须先锁定才能确认")
            # Combined state changes are ordered like an operator would perform
            # them: reopen before unlocking, and lock before confirming.  This
            # keeps the audit trail valid even when both fields arrive in one
            # API request.
            if confirmed is False and item["confirmed"]:
                item["confirmed"] = False
                document["events"].append(
                    _event(
                        "REOPENED",
                        step_id,
                        detail="操作者撤回步骤确认",
                    )
                )
            if locked is not None and item["locked"] != locked:
                item["locked"] = locked
                document["events"].append(
                    _event(
                        "LOCKED" if locked else "UNLOCKED",
                        step_id,
                        detail="操作者锁定步骤内容与位置" if locked else "操作者解除步骤锁定",
                    )
                )
            if confirmed is True and not item["confirmed"]:
                item["confirmed"] = True
                document["events"].append(
                    _event(
                        "CONFIRMED",
                        step_id,
                        detail="操作者确认步骤",
                    )
                )
            document["status"] = (
                "COMPLETED" if all(value["confirmed"] for value in document["steps"]) else "IN_REVIEW"
            )
            document["updated_at"] = _timestamp()
            self._write(document)
        return document

    def reorder(
        self,
        session_id: str,
        step_id: str,
        target_position: int,
        sop: dict[str, Any],
    ) -> dict[str, Any]:
        with self._lock:
            document = self.get(session_id)
            self._assert_source(document, sop)
            if not 1 <= target_position <= len(document["steps"]):
                raise ValueError("目标位置超出步骤范围")
            current = next((value for value in document["steps"] if value["step_id"] == step_id), None)
            if current is None:
                raise ValueError("审核会话中不存在该步骤")
            if current["locked"] or current["confirmed"]:
                raise ValueError("锁定或已确认步骤不能重排")
            before_positions = {value["step_id"]: value["position"] for value in document["steps"]}
            reordered = [value for value in document["steps"] if value["step_id"] != step_id]
            reordered.insert(target_position - 1, current)
            for index, value in enumerate(reordered, 1):
                value["position"] = index
            for value in reordered:
                if value["locked"] and value["position"] != before_positions[value["step_id"]]:
                    raise ValueError(f"重排会移动已锁定步骤 {value['step_id']}")
            _validate_order(reordered)
            before = before_positions[step_id]
            document["steps"] = reordered
            if before != target_position:
                document["events"].append(
                    _event(
                        "REORDERED",
                        step_id,
                        before_position=before,
                        after_position=target_position,
                        detail=f"操作者将步骤从位置{before}移动到{target_position}",
                    )
                )
            document["updated_at"] = _timestamp()
            self._write(document)
        return document

    def record_rebuild(
        self, session_id: str, step_id: str, sop: dict[str, Any]
    ) -> dict[str, Any]:
        with self._lock:
            document = self.get(session_id)
            self._assert_source(document, sop)
            item = next((value for value in document["steps"] if value["step_id"] == step_id), None)
            if item is None:
                raise ValueError("审核会话中不存在该步骤")
            if item["locked"] or item["confirmed"]:
                raise ValueError("锁定或已确认步骤不能重建")
            item["rebuild_count"] += 1
            document["events"].append(
                _event(
                    "STEP_REBUILT",
                    step_id,
                    detail=f"确定性重建第{item['rebuild_count']}次；外部模型调用0",
                )
            )
            document["updated_at"] = _timestamp()
            self._write(document)
        return document


def _ordered_sop(sop: dict[str, Any], session: dict[str, Any]) -> dict[str, Any]:
    by_id = {step["step_id"]: step for step in sop["steps"]}
    ordered = copy.deepcopy(sop)
    ordered["steps"] = [
        copy.deepcopy(by_id[item["step_id"]])
        for item in sorted(session["steps"], key=lambda value: value["position"])
    ]
    validate_document(ordered, "sop.schema.json")
    return ordered


def rebuild_step_artifacts(
    store: SopReviewSessionStore,
    session_id: str,
    step_id: str,
    sop: dict[str, Any],
    *,
    visual_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session = store.record_rebuild(session_id, step_id, sop)
    ordered = _ordered_sop(sop, session)
    views = create_sop_views(ordered)
    checklist = create_checklist(ordered, visual_review=visual_review)
    quiz = create_quiz(ordered)
    selected_views = {
        name: next(item for item in view["steps"] if item["step_id"] == step_id)
        for name, view in views["views"].items()
    }
    checklist_item = next(item for item in checklist["items"] if item["step_id"] == step_id)
    quiz_questions = [
        item for item in quiz["questions"] if step_id in item["step_ids"]
    ]
    session_step = next(item for item in session["steps"] if item["step_id"] == step_id)
    source_step = next(item for item in ordered["steps"] if item["step_id"] == step_id)
    document = {
        "artifact_type": "SINGLE_STEP_REBUILD",
        "version": 1,
        "session_id": session_id,
        "case_id": ordered["case_id"],
        "sop_version": ordered["version"],
        "step_id": step_id,
        "rebuild_number": session_step["rebuild_count"],
        "generated_at": _timestamp(),
        "scope": {
            "sop_view_units": 3,
            "checklist_units": 1,
            "quiz_question_ids": [item["question_id"] for item in quiz_questions],
            "unchanged_step_count": len(ordered["steps"]) - 1,
            "external_model_calls": 0,
        },
        "sop_views": selected_views,
        "checklist_item": checklist_item,
        "quiz_questions": quiz_questions,
        "evidence_ids": list(source_step["evidence"]),
        "data_policy": {
            "external_model_calls": 0,
            "contains_raw_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
        },
    }
    return validate_document(document, "step_rebuild_response.schema.json")
