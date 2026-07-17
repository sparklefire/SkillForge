"""Explicit, auditable SkillForge workflow state machine."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .observability import StructuredLogger, redact


class WorkflowState(StrEnum):
    UPLOADED = "UPLOADED"
    INGESTING = "INGESTING"
    EXTRACTING = "EXTRACTING"
    PLANNING = "PLANNING"
    CREATING = "CREATING"
    VERIFYING = "VERIFYING"
    REVISING = "REVISING"
    RENDERING = "RENDERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.UPLOADED: {WorkflowState.INGESTING, WorkflowState.FAILED},
    WorkflowState.INGESTING: {WorkflowState.EXTRACTING, WorkflowState.FAILED},
    WorkflowState.EXTRACTING: {WorkflowState.PLANNING, WorkflowState.FAILED},
    WorkflowState.PLANNING: {WorkflowState.CREATING, WorkflowState.FAILED},
    WorkflowState.CREATING: {WorkflowState.VERIFYING, WorkflowState.FAILED},
    WorkflowState.VERIFYING: {
        WorkflowState.REVISING,
        WorkflowState.RENDERING,
        WorkflowState.NEEDS_REVIEW,
        WorkflowState.FAILED,
    },
    WorkflowState.REVISING: {
        WorkflowState.VERIFYING,
        WorkflowState.NEEDS_REVIEW,
        WorkflowState.FAILED,
    },
    WorkflowState.RENDERING: {WorkflowState.COMPLETED, WorkflowState.FAILED},
    WorkflowState.NEEDS_REVIEW: {WorkflowState.REVISING, WorkflowState.FAILED},
    WorkflowState.COMPLETED: set(),
    WorkflowState.FAILED: set(),
}

RERUNNABLE_STATES = (
    WorkflowState.INGESTING,
    WorkflowState.EXTRACTING,
    WorkflowState.PLANNING,
    WorkflowState.CREATING,
    WorkflowState.VERIFYING,
    WorkflowState.REVISING,
    WorkflowState.RENDERING,
)

TERMINAL_OR_REVIEW_STATES = {
    WorkflowState.COMPLETED,
    WorkflowState.FAILED,
    WorkflowState.NEEDS_REVIEW,
}


@dataclass(frozen=True)
class Transition:
    from_state: str
    to_state: str
    timestamp: str
    reason: str
    event_type: str = "TRANSITION"
    attempt: int = 1
    invalidated_states: list[str] = field(default_factory=list)


class WorkflowStateMachine:
    def __init__(self, logger: StructuredLogger | None = None) -> None:
        self.state = WorkflowState.UPLOADED
        self.history: list[Transition] = []
        self.stage_attempts = {state.value: 0 for state in WorkflowState}
        self.stage_attempts[WorkflowState.UPLOADED.value] = 1
        self.last_failure: dict[str, Any] | None = None
        self.logger = logger or StructuredLogger()

    def transition(self, target: WorkflowState, reason: str = "") -> None:
        if target == WorkflowState.FAILED:
            self.fail(RuntimeError(reason or "工作流失败"), retryable=False)
            return
        if target not in TRANSITIONS[self.state]:
            raise ValueError(f"非法状态迁移: {self.state} -> {target}")
        previous = self.state
        self.state = target
        self.stage_attempts[target.value] += 1
        item = Transition(
            from_state=previous.value,
            to_state=target.value,
            timestamp=datetime.now(UTC).isoformat(),
            reason=reason,
            attempt=self.stage_attempts[target.value],
        )
        self.history.append(item)
        self.logger.emit(
            "workflow.transition",
            from_state=previous.value,
            to_state=target.value,
            reason=reason,
        )

    def fail(self, error: Exception, *, retryable: bool, reason: str = "") -> None:
        if self.state in TERMINAL_OR_REVIEW_STATES:
            raise ValueError(f"当前状态不能记录失败: {self.state}")
        previous = self.state
        timestamp = datetime.now(UTC).isoformat()
        message = str(redact(str(error)))[:500] or type(error).__name__
        self.state = WorkflowState.FAILED
        self.stage_attempts[WorkflowState.FAILED.value] += 1
        self.last_failure = {
            "from_state": previous.value,
            "error_type": type(error).__name__,
            "message": message,
            "retryable": retryable,
            "timestamp": timestamp,
        }
        self.history.append(
            Transition(
                from_state=previous.value,
                to_state=WorkflowState.FAILED.value,
                timestamp=timestamp,
                reason=reason,
                event_type="FAILURE",
                attempt=self.stage_attempts[previous.value],
            )
        )
        self.logger.emit(
            "workflow.failure",
            from_state=previous.value,
            error_type=type(error).__name__,
            message=message,
            retryable=retryable,
            reason=reason,
        )

    def rerun_stage(self, target: WorkflowState, reason: str = "") -> None:
        if target not in RERUNNABLE_STATES:
            raise ValueError(f"状态不支持阶段重跑: {target}")
        if self.state not in TERMINAL_OR_REVIEW_STATES:
            raise ValueError("只有完成、失败或待人工复核的运行可以启动阶段重跑")
        if self.stage_attempts[target.value] < 1:
            raise ValueError(f"阶段尚未执行，不能标记为重跑: {target.value}")
        previous = self.state
        target_index = RERUNNABLE_STATES.index(target)
        invalidated = [
            state.value
            for state in RERUNNABLE_STATES[target_index + 1 :]
            if self.stage_attempts[state.value] > 0
        ]
        self.state = target
        self.stage_attempts[target.value] += 1
        self.last_failure = None
        item = Transition(
            from_state=previous.value,
            to_state=target.value,
            timestamp=datetime.now(UTC).isoformat(),
            reason=reason,
            event_type="RERUN",
            attempt=self.stage_attempts[target.value],
            invalidated_states=invalidated,
        )
        self.history.append(item)
        self.logger.emit(
            "workflow.rerun",
            from_state=previous.value,
            to_state=target.value,
            attempt=item.attempt,
            invalidated_states=invalidated,
            reason=reason,
        )

    def recover(self, reason: str = "") -> None:
        if self.state != WorkflowState.FAILED or self.last_failure is None:
            raise ValueError("只有失败运行可以恢复")
        if not self.last_failure["retryable"]:
            raise ValueError("该失败不可自动恢复，需要操作者处理后显式重跑阶段")
        target = WorkflowState(self.last_failure["from_state"])
        if target == WorkflowState.UPLOADED:
            previous = self.state
            invalidated = [
                state.value
                for state in RERUNNABLE_STATES
                if self.stage_attempts[state.value] > 0
            ]
            self.state = WorkflowState.UPLOADED
            self.stage_attempts[WorkflowState.UPLOADED.value] += 1
            self.last_failure = None
            item = Transition(
                from_state=previous.value,
                to_state=WorkflowState.UPLOADED.value,
                timestamp=datetime.now(UTC).isoformat(),
                reason=reason or "从上传阶段可重试失败恢复",
                event_type="RERUN",
                attempt=self.stage_attempts[WorkflowState.UPLOADED.value],
                invalidated_states=invalidated,
            )
            self.history.append(item)
            self.logger.emit(
                "workflow.rerun",
                from_state=previous.value,
                to_state=WorkflowState.UPLOADED.value,
                attempt=item.attempt,
                invalidated_states=invalidated,
                reason=item.reason,
            )
            return
        self.rerun_stage(target, reason or "从可重试失败恢复")

    def snapshot(self) -> dict[str, Any]:
        document = {
            "version": 1,
            "state": self.state.value,
            "history": [asdict(item) for item in self.history],
            "stage_attempts": dict(self.stage_attempts),
            "last_failure": self.last_failure,
            "data_policy": {"contains_credentials": False},
        }
        return validate_document(document, "workflow_run.schema.json")

    def write_checkpoint(self, path: Path) -> dict[str, Any]:
        document = self.snapshot()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".workflow-", dir=path.parent)
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
        return document

    @classmethod
    def from_snapshot(
        cls,
        document: dict[str, Any],
        logger: StructuredLogger | None = None,
    ) -> "WorkflowStateMachine":
        validate_document(document, "workflow_run.schema.json")
        instance = cls(logger)
        instance.state = WorkflowState(document["state"])
        instance.history = [
            Transition(
                from_state=item["from_state"],
                to_state=item["to_state"],
                timestamp=item["timestamp"],
                reason=item["reason"],
                event_type=item["event_type"],
                attempt=item["attempt"],
                invalidated_states=list(item["invalidated_states"]),
            )
            for item in document["history"]
        ]
        instance.stage_attempts = dict(document["stage_attempts"])
        instance.last_failure = document["last_failure"]
        expected_attempts = {state.value: 0 for state in WorkflowState}
        expected_attempts[WorkflowState.UPLOADED.value] = 1
        previous_state = WorkflowState.UPLOADED.value
        for event in instance.history:
            if event.from_state != previous_state:
                raise ValueError("工作流检查点事件链不连续")
            expected_attempts[event.to_state] += 1
            expected_attempt = (
                expected_attempts[event.from_state]
                if event.event_type == "FAILURE"
                else expected_attempts[event.to_state]
            )
            if event.attempt != expected_attempt:
                raise ValueError("工作流检查点事件尝试次数不一致")
            previous_state = event.to_state
        if instance.stage_attempts != expected_attempts:
            raise ValueError("工作流检查点阶段尝试次数与事件链不一致")
        if instance.history and instance.history[-1].to_state != instance.state.value:
            raise ValueError("工作流检查点最终事件与当前状态不一致")
        if instance.state == WorkflowState.FAILED and instance.last_failure is None:
            raise ValueError("失败工作流检查点缺少失败详情")
        if instance.state != WorkflowState.FAILED and instance.last_failure is not None:
            raise ValueError("非失败工作流检查点不能保留失败详情")
        if instance.state == WorkflowState.FAILED:
            last_event = instance.history[-1] if instance.history else None
            if (
                last_event is None
                or last_event.event_type != "FAILURE"
                or last_event.from_state != instance.last_failure["from_state"]
                or last_event.timestamp != instance.last_failure["timestamp"]
            ):
                raise ValueError("失败详情与最终失败事件不一致")
        return instance

    @classmethod
    def load_checkpoint(
        cls,
        path: Path,
        logger: StructuredLogger | None = None,
    ) -> "WorkflowStateMachine":
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("工作流检查点必须是JSON对象")
        return cls.from_snapshot(document, logger)
