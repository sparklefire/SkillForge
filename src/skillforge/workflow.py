"""Explicit, auditable SkillForge workflow state machine."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .observability import StructuredLogger


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


@dataclass(frozen=True)
class Transition:
    from_state: str
    to_state: str
    timestamp: str
    reason: str


class WorkflowStateMachine:
    def __init__(self, logger: StructuredLogger | None = None) -> None:
        self.state = WorkflowState.UPLOADED
        self.history: list[Transition] = []
        self.logger = logger or StructuredLogger()

    def transition(self, target: WorkflowState, reason: str = "") -> None:
        if target not in TRANSITIONS[self.state]:
            raise ValueError(f"非法状态迁移: {self.state} -> {target}")
        previous = self.state
        self.state = target
        item = Transition(
            from_state=previous.value,
            to_state=target.value,
            timestamp=datetime.now(UTC).isoformat(),
            reason=reason,
        )
        self.history.append(item)
        self.logger.emit(
            "workflow.transition",
            from_state=previous.value,
            to_state=target.value,
            reason=reason,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "history": [asdict(item) for item in self.history],
        }
