import copy
import json
import shutil
import stat
from pathlib import Path

import pytest

from skillforge.agent_trace import (
    AGENT_ORDER,
    ARTIFACT_FILES,
    build_agent_trace,
    validate_agent_trace,
    verify_agent_trace_artifacts,
)


ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "cases/n31/evaluations/agent_tool_trace_v1.json"


def _checked_in() -> dict:
    return json.loads(TRACE.read_text(encoding="utf-8"))


def test_checked_in_trace_binds_five_agents_tools_handoffs_and_artifacts() -> None:
    report = verify_agent_trace_artifacts(_checked_in(), project_root=ROOT)
    assert [item["agent_id"] for item in report["agents"]] == AGENT_ORDER
    assert report["summary"] == {
        "agent_count": 5,
        "tool_count": 13,
        "tool_call_count": 14,
        "artifact_count": 19,
        "handoff_count": 5,
        "evidence_bound_call_count": 14,
        "human_confirmation_call_count": 1,
        "trace_generation_external_model_calls": 0,
    }
    assert set(item["artifact_id"] for item in report["artifacts"]) == set(
        ARTIFACT_FILES
    )
    assert ("REVISION_AGENT", "VERIFIER_AGENT") in {
        (item["from_agent"], item["to_agent"]) for item in report["handoffs"]
    }
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(ROOT) not in serialized
    assert "Authorization" not in serialized


def test_trace_rejects_unauthorized_tool_and_artifact_hash_drift() -> None:
    report = _checked_in()
    unauthorized = copy.deepcopy(report)
    unauthorized["agents"][0]["calls"][0]["tool_id"] = "LOCAL_REVISION"
    with pytest.raises(ValueError, match="未授权工具"):
        validate_agent_trace(unauthorized)

    drifted = copy.deepcopy(report)
    drifted["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="大小或SHA-256"):
        verify_agent_trace_artifacts(drifted, project_root=ROOT)


def test_trace_regeneration_is_atomic_and_public(tmp_path) -> None:
    output_dir = ROOT / "outputs/tests/agent-trace" / tmp_path.name
    output = output_dir / "trace.json"
    try:
        report = build_agent_trace(project_root=ROOT, output_path=output)
        assert report["status"] == "COMPLETED"
        assert output.is_file()
        assert stat.S_IMODE(output.stat().st_mode) == 0o644
        assert not list(output.parent.glob(".trace.json.*"))
    finally:
        shutil.rmtree(output_dir, ignore_errors=True)
