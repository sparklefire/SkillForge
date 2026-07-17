import json
import stat

import pytest

from skillforge.workflow import WorkflowState, WorkflowStateMachine


def test_happy_path_and_illegal_transition() -> None:
    workflow = WorkflowStateMachine()
    workflow.transition(WorkflowState.INGESTING)
    workflow.transition(WorkflowState.EXTRACTING)
    assert workflow.state == WorkflowState.EXTRACTING
    with pytest.raises(ValueError, match="非法状态迁移"):
        workflow.transition(WorkflowState.COMPLETED)


def _complete_workflow() -> WorkflowStateMachine:
    workflow = WorkflowStateMachine()
    for state in (
        WorkflowState.INGESTING,
        WorkflowState.EXTRACTING,
        WorkflowState.PLANNING,
        WorkflowState.CREATING,
        WorkflowState.VERIFYING,
        WorkflowState.REVISING,
        WorkflowState.VERIFYING,
        WorkflowState.RENDERING,
        WorkflowState.COMPLETED,
    ):
        workflow.transition(state)
    return workflow


def test_completed_workflow_can_rerun_an_executed_stage_with_invalidation() -> None:
    workflow = _complete_workflow()
    assert workflow.stage_attempts["VERIFYING"] == 2
    workflow.rerun_stage(WorkflowState.EXTRACTING, "重新解析更新后的安全素材")
    assert workflow.state == WorkflowState.EXTRACTING
    assert workflow.stage_attempts["EXTRACTING"] == 2
    event = workflow.snapshot()["history"][-1]
    assert event["event_type"] == "RERUN"
    assert event["attempt"] == 2
    assert event["invalidated_states"] == [
        "PLANNING",
        "CREATING",
        "VERIFYING",
        "REVISING",
        "RENDERING",
    ]
    with pytest.raises(ValueError, match="只有完成、失败或待人工复核"):
        workflow.rerun_stage(WorkflowState.PLANNING)


def test_stage_that_never_ran_cannot_be_claimed_as_rerun() -> None:
    workflow = WorkflowStateMachine()
    workflow.transition(WorkflowState.INGESTING)
    workflow.fail(RuntimeError("停止"), retryable=False)
    with pytest.raises(ValueError, match="阶段尚未执行"):
        workflow.rerun_stage(WorkflowState.RENDERING)


def test_retryable_failure_checkpoint_can_be_loaded_and_recovered(tmp_path) -> None:
    workflow = WorkflowStateMachine()
    workflow.transition(WorkflowState.INGESTING)
    workflow.transition(WorkflowState.EXTRACTING)
    workflow.fail(
        TimeoutError("Authorization: Bearer private-token-value"),
        retryable=True,
        reason="PDF OCR暂时超时",
    )
    checkpoint = tmp_path / "private-run" / "workflow.json"
    document = workflow.write_checkpoint(checkpoint)
    assert document["state"] == "FAILED"
    assert document["last_failure"]["from_state"] == "EXTRACTING"
    assert "private-token-value" not in checkpoint.read_text()
    assert stat.S_IMODE(checkpoint.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600

    restored = WorkflowStateMachine.load_checkpoint(checkpoint)
    restored.recover("依赖恢复后重试")
    assert restored.state == WorkflowState.EXTRACTING
    assert restored.last_failure is None
    assert restored.stage_attempts["EXTRACTING"] == 2
    assert restored.snapshot()["history"][-1]["event_type"] == "RERUN"


def test_non_retryable_failure_requires_explicit_operator_rerun(tmp_path) -> None:
    workflow = WorkflowStateMachine()
    workflow.transition(WorkflowState.INGESTING)
    workflow.fail(ValueError("输入契约不合法"), retryable=False)
    with pytest.raises(ValueError, match="不可自动恢复"):
        workflow.recover()
    workflow.rerun_stage(WorkflowState.INGESTING, "操作者修复输入后重跑")
    assert workflow.state == WorkflowState.INGESTING
    assert workflow.snapshot()["history"][-1]["event_type"] == "RERUN"

    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(workflow.snapshot()), encoding="utf-8")
    broken = json.loads(path.read_text())
    broken["state"] = "COMPLETED"
    with pytest.raises(ValueError, match="最终事件与当前状态不一致"):
        WorkflowStateMachine.from_snapshot(broken)

    broken = workflow.snapshot()
    broken["stage_attempts"]["INGESTING"] += 1
    with pytest.raises(ValueError, match="阶段尝试次数与事件链不一致"):
        WorkflowStateMachine.from_snapshot(broken)
