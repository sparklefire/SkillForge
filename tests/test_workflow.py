import pytest

from skillforge.workflow import WorkflowState, WorkflowStateMachine


def test_happy_path_and_illegal_transition() -> None:
    workflow = WorkflowStateMachine()
    workflow.transition(WorkflowState.INGESTING)
    workflow.transition(WorkflowState.EXTRACTING)
    assert workflow.state == WorkflowState.EXTRACTING
    with pytest.raises(ValueError, match="非法状态迁移"):
        workflow.transition(WorkflowState.COMPLETED)
