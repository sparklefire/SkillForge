"""Auditable automatic and operator decisions for verifier conflicts."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from .contracts import validate_document
from .observability import redact
from .revision import digest


SESSION_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
HUMAN_REQUIRED_KINDS = (
    "UNSUPPORTED_SAFETY_CLAIM",
    "MISSING_EVIDENCE",
    "INVALID_EVIDENCE",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _event(
    event_type: str,
    *,
    conflict_id: str | None,
    actor: str,
    automatic: bool,
    detail: str,
) -> dict[str, Any]:
    return {
        "event_id": uuid.uuid4().hex,
        "event_type": event_type,
        "conflict_id": conflict_id,
        "actor": actor,
        "automatic": automatic,
        "detail": detail,
        "recorded_at": _now(),
    }


def source_bindings(
    initial_report: dict[str, Any],
    revision_audit: dict[str, Any],
    proposed_sop: dict[str, Any],
    final_report: dict[str, Any],
) -> dict[str, str]:
    return {
        "initial_conflicts_sha256": digest(initial_report),
        "revision_audit_sha256": digest(revision_audit),
        "proposed_sop_sha256": digest(proposed_sop),
        "final_conflicts_sha256": digest(final_report),
    }


def route_conflict(conflict: dict[str, Any]) -> tuple[str, str, bool]:
    """Return route, reason and safety override without trusting automatic=true."""

    if conflict["kind"] in HUMAN_REQUIRED_KINDS:
        return (
            "HUMAN",
            "安全声明或证据完整性冲突禁止自动采用，必须由操作者确认",
            True,
        )
    if conflict["proposed_action"] == "REVIEW":
        return "HUMAN", "Verifier明确要求人工复核", False
    if conflict["status"] == "NEEDS_REVIEW" or not conflict["automatic"]:
        return "HUMAN", "冲突未获准自动修订", False
    return "AUTO", "确定性规则、Evidence边界和局部修订均允许自动采用", False


def _recalculate(document: dict[str, Any]) -> None:
    decisions = document["decisions"]
    pending = sum(item["final_result"] == "PENDING" for item in decisions)
    rejected = sum(item["final_result"] == "KEPT_ORIGINAL" for item in decisions)
    adopted = len(decisions) - pending - rejected
    proposed_residual = document["finalization"]["proposed_residual_conflict_count"]
    unresolved = pending + rejected + proposed_residual
    publishable = unresolved == 0
    has_human = any(item["route"] == "HUMAN" for item in decisions)
    if pending:
        status = "AWAITING_HUMAN"
    elif rejected or proposed_residual:
        status = "NEEDS_REVIEW"
    else:
        status = "FINALIZED" if has_human else "AUTO_FINALIZED"
    document["status"] = status
    document["finalization"].update(
        {
            "publishable": publishable,
            "final_sop_sha256": (
                document["source_bindings"]["proposed_sop_sha256"]
                if publishable
                else None
            ),
            "adopted_unresolved_conflict_count": unresolved,
            "adopted_conflict_count": adopted,
            "rejected_conflict_count": rejected,
            "pending_conflict_count": pending,
        }
    )


def _validate_session(document: dict[str, Any]) -> dict[str, Any]:
    validate_document(document, "conflict_decision_session.schema.json")
    decisions = document["decisions"]
    ids = [item["conflict_id"] for item in decisions]
    if len(ids) != len(set(ids)):
        raise ValueError("冲突裁决会话包含重复冲突ID")
    for item in decisions:
        if item["kind"] in HUMAN_REQUIRED_KINDS and (
            item["route"] != "HUMAN" or not item["safety_override"]
        ):
            raise ValueError("安全或证据冲突必须进入人工确认")
        if item["route"] == "AUTO":
            if (
                item["automatic_decision"] != "ADOPT_PROPOSED"
                or item["human_decision"] != "NOT_REQUIRED"
                or item["confirmed_by"] is not None
                or item["confirmed_at"] is not None
                or item["final_result"] == "PENDING"
            ):
                raise ValueError("自动裁决字段组合无效")
        else:
            if item["automatic_decision"] != "DEFER_TO_HUMAN":
                raise ValueError("人工路由不能预先自动采用")
            pending = item["human_decision"] == "PENDING"
            if pending != (item["final_result"] == "PENDING"):
                raise ValueError("人工裁决待处理状态不一致")
            if pending and (item["confirmed_by"] is not None or item["confirmed_at"] is not None):
                raise ValueError("待处理裁决不能包含确认人或确认时间")
            if not pending and (
                item["confirmed_by"] != "OPERATOR" or item["confirmed_at"] is None
            ):
                raise ValueError("已处理人工裁决必须记录操作者和时间")
            if item["human_decision"] == "APPROVED" and item["final_result"] not in {
                "ADOPTED",
                "RESOLVED_BY_RELATED_CHANGE",
            }:
                raise ValueError("批准的人工裁决必须采用修订结果")
            if item["human_decision"] == "REJECTED" and item["final_result"] != "KEPT_ORIGINAL":
                raise ValueError("拒绝的人工裁决必须保留原内容")
    copy = json.loads(json.dumps(document))
    _recalculate(copy)
    if copy["status"] != document["status"] or copy["finalization"] != document["finalization"]:
        raise ValueError("裁决会话汇总与逐项决策不一致")
    event_ids = [item["event_id"] for item in document["events"]]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("冲突裁决事件ID重复")
    if any(item["actor"] == "OPERATOR" and item["automatic"] for item in document["events"]):
        raise ValueError("操作者事件不能标记为自动")
    return document


class ConflictDecisionStore:
    """Persist hash-bound conflict decisions in a private mode-600 store."""

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
        descriptor, temporary = tempfile.mkstemp(prefix=f".{document['session_id']}.", dir=self.root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _assert_sources(
        document: dict[str, Any],
        initial_report: dict[str, Any],
        revision_audit: dict[str, Any],
        proposed_sop: dict[str, Any],
        final_report: dict[str, Any],
    ) -> None:
        if document["source_bindings"] != source_bindings(
            initial_report,
            revision_audit,
            proposed_sop,
            final_report,
        ):
            raise ValueError("冲突裁决会话绑定的来源已经变化，请新建会话")

    def create(
        self,
        initial_report: dict[str, Any],
        revision_audit: dict[str, Any],
        proposed_sop: dict[str, Any],
        final_report: dict[str, Any],
    ) -> dict[str, Any]:
        validate_document(initial_report, "conflict.schema.json")
        validate_document(revision_audit, "revision_audit.schema.json")
        validate_document(proposed_sop, "sop.schema.json")
        validate_document(final_report, "conflict.schema.json")
        case_ids = {
            initial_report["case_id"],
            revision_audit["case_id"],
            proposed_sop["case_id"],
            final_report["case_id"],
        }
        if len(case_ids) != 1:
            raise ValueError("冲突、修订和SOP案例编号不一致")
        if revision_audit["after_digest"] != digest(proposed_sop):
            raise ValueError("Revision Audit未绑定当前建议修订SOP")
        conflicts = initial_report["conflicts"]
        conflict_ids = {item["conflict_id"] for item in conflicts}
        if any(item["conflict_id"] not in conflict_ids for item in revision_audit["changes"]):
            raise ValueError("Revision Audit引用了未知冲突")
        paths_by_conflict: dict[str, list[str]] = {item: [] for item in conflict_ids}
        for change in revision_audit["changes"]:
            paths_by_conflict[change["conflict_id"]].append(change["path"])

        now = _now()
        decisions = []
        events = [
            _event(
                "SESSION_CREATED",
                conflict_id=None,
                actor="SYSTEM",
                automatic=True,
                detail="绑定冲突报告、Revision Audit、建议SOP和复检报告",
            )
        ]
        for conflict in conflicts:
            route, reason, safety_override = route_conflict(conflict)
            change_paths = sorted(set(paths_by_conflict[conflict["conflict_id"]]))
            if route == "AUTO":
                final_result = "ADOPTED" if change_paths else "RESOLVED_BY_RELATED_CHANGE"
                events.append(
                    _event(
                        "AUTO_ADOPTED",
                        conflict_id=conflict["conflict_id"],
                        actor="SYSTEM",
                        automatic=True,
                        detail=reason,
                    )
                )
                human_decision = "NOT_REQUIRED"
                automatic_decision = "ADOPT_PROPOSED"
            else:
                final_result = "PENDING"
                human_decision = "PENDING"
                automatic_decision = "DEFER_TO_HUMAN"
            decisions.append(
                {
                    "conflict_id": conflict["conflict_id"],
                    "kind": conflict["kind"],
                    "severity": conflict["severity"],
                    "step_ids": list(conflict["step_ids"]),
                    "message": conflict["message"],
                    "evidence_ids": sorted(
                        {item["evidence_id"] for item in conflict["evidence"]}
                    ),
                    "proposed_action": conflict["proposed_action"],
                    "change_paths": change_paths,
                    "route": route,
                    "route_reason": reason,
                    "safety_override": safety_override,
                    "automatic_decision": automatic_decision,
                    "human_decision": human_decision,
                    "final_result": final_result,
                    "confirmed_by": None,
                    "confirmed_at": None,
                    "comment": None,
                }
            )

        document = {
            "artifact_type": "CONFLICT_DECISION_SESSION",
            "version": 1,
            "session_id": uuid.uuid4().hex,
            "case_id": proposed_sop["case_id"],
            "created_at": now,
            "updated_at": now,
            "status": "AWAITING_HUMAN",
            "routing_policy": {
                "safety_override_enabled": True,
                "human_required_kinds": list(HUMAN_REQUIRED_KINDS),
                "human_required_action": "REVIEW",
            },
            "source_bindings": source_bindings(
                initial_report,
                revision_audit,
                proposed_sop,
                final_report,
            ),
            "decisions": decisions,
            "finalization": {
                "publishable": False,
                "final_sop_sha256": None,
                "proposed_residual_conflict_count": len(final_report["conflicts"]),
                "adopted_unresolved_conflict_count": 0,
                "adopted_conflict_count": 0,
                "rejected_conflict_count": 0,
                "pending_conflict_count": 0,
            },
            "events": events,
            "data_policy": {
                "external_model_calls": 0,
                "contains_raw_media": False,
                "contains_credentials": False,
                "contains_absolute_paths": False,
            },
        }
        _recalculate(document)
        if document["finalization"]["publishable"]:
            document["events"].append(
                _event(
                    "FINALIZED",
                    conflict_id=None,
                    actor="SYSTEM",
                    automatic=True,
                    detail="全部冲突自动采用且复检无残留，可发布最终SOP",
                )
            )
        with self._lock:
            self._write(document)
        return document

    def get(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(session_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("冲突裁决会话必须是JSON对象")
        return _validate_session(payload)

    def get_bound(
        self,
        session_id: str,
        initial_report: dict[str, Any],
        revision_audit: dict[str, Any],
        proposed_sop: dict[str, Any],
        final_report: dict[str, Any],
    ) -> dict[str, Any]:
        document = self.get(session_id)
        self._assert_sources(
            document,
            initial_report,
            revision_audit,
            proposed_sop,
            final_report,
        )
        return document

    def decide(
        self,
        session_id: str,
        conflict_id: str,
        *,
        approved: bool,
        comment: str,
        initial_report: dict[str, Any],
        revision_audit: dict[str, Any],
        proposed_sop: dict[str, Any],
        final_report: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_comment = str(redact(comment.strip()))
        if not normalized_comment:
            raise ValueError("人工裁决必须填写说明")
        if len(normalized_comment) > 500:
            raise ValueError("人工裁决说明不能超过500字")
        with self._lock:
            document = self.get(session_id)
            self._assert_sources(
                document,
                initial_report,
                revision_audit,
                proposed_sop,
                final_report,
            )
            decision = next(
                (item for item in document["decisions"] if item["conflict_id"] == conflict_id),
                None,
            )
            if decision is None:
                raise ValueError("裁决会话中不存在该冲突")
            if decision["route"] != "HUMAN":
                raise ValueError("自动路由冲突不能伪装成人工裁决")
            if decision["human_decision"] != "PENDING":
                document["events"].append(
                    _event(
                        "REOPENED",
                        conflict_id=conflict_id,
                        actor="OPERATOR",
                        automatic=False,
                        detail="操作者重新裁决此前已处理的冲突",
                    )
                )
            timestamp = _now()
            decision["human_decision"] = "APPROVED" if approved else "REJECTED"
            decision["final_result"] = (
                "ADOPTED"
                if approved and decision["change_paths"]
                else "RESOLVED_BY_RELATED_CHANGE"
                if approved
                else "KEPT_ORIGINAL"
            )
            decision["confirmed_by"] = "OPERATOR"
            decision["confirmed_at"] = timestamp
            decision["comment"] = normalized_comment
            document["events"].append(
                _event(
                    "HUMAN_APPROVED" if approved else "HUMAN_REJECTED",
                    conflict_id=conflict_id,
                    actor="OPERATOR",
                    automatic=False,
                    detail=normalized_comment,
                )
            )
            previous_status = document["status"]
            document["updated_at"] = timestamp
            _recalculate(document)
            if document["finalization"]["publishable"] and previous_status != "FINALIZED":
                document["events"].append(
                    _event(
                        "FINALIZED",
                        conflict_id=None,
                        actor="SYSTEM",
                        automatic=True,
                        detail="人工门禁全部通过且建议结果复检无残留，可发布最终SOP",
                    )
                )
            self._write(document)
        return document
