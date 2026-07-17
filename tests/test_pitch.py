import copy
import json
from pathlib import Path

from skillforge.contracts import validate_document
from skillforge.pitch import PHASE_ORDER, _check_timeline


ROOT = Path(__file__).resolve().parents[1]


def _runbook() -> dict:
    return json.loads(
        (ROOT / "cases/n31/pitch_runbook.json").read_text(encoding="utf-8")
    )


def test_pitch_runbook_is_valid_and_exactly_three_minutes() -> None:
    runbook = validate_document(_runbook(), "pitch_runbook.schema.json")
    assert [item["phase"] for item in runbook["segments"]] == PHASE_ORDER
    assert _check_timeline(runbook)["status"] == "PASSED"
    assert runbook["segments"][0]["start_ms"] == 0
    assert runbook["segments"][-1]["end_ms"] == 180_000


def test_pitch_timeline_rejects_a_gap() -> None:
    runbook = copy.deepcopy(_runbook())
    runbook["segments"][2]["start_ms"] += 1_000
    result = _check_timeline(runbook)
    assert result["status"] == "FAILED"
    assert any("不连续" in detail for detail in result["details"])


def test_pitch_declares_live_preprocessed_and_offline_fallbacks() -> None:
    runbook = _runbook()
    assert [item["mode"] for item in runbook["demo_modes"]] == [
        "LIVE",
        "PREPROCESSED",
        "OFFLINE",
    ]
    assert all(item["expected"]["after_errors"] == 0 for item in runbook["demo_modes"])
    assert all("Docker" not in item["command"] for item in runbook["demo_modes"])


def test_pitch_keeps_human_review_as_a_submission_gate() -> None:
    gates = {item["gate_id"]: item for item in _runbook()["human_gates"]}
    video_gate = gates["TRAINING_VIDEO_FULL_WATCH"]
    assert video_gate["status"] == "PENDING"
    assert video_gate["blocking_for_submission"] is True
