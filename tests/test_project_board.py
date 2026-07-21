from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path

import pytest

import skillforge.project_board as project_board_module
from skillforge.contracts import ContractValidationError, validate_document
from skillforge.project_board import ProjectBoardError, build_project_board_status


ROOT = Path(__file__).resolve().parents[1]
BOARD = ROOT / "config/project_board.json"
RUNBOOK = ROOT / "cases/n31/pitch_runbook.json"


def _write_board(tmp_path: Path, board: dict) -> Path:
    path = tmp_path / "project_board.json"
    path.write_text(json.dumps(board, ensure_ascii=False), encoding="utf-8")
    return path


def test_tracked_board_is_on_track_and_does_not_mark_goal_blocked() -> None:
    report = build_project_board_status(as_of=date(2026, 7, 18))
    validate_document(report, "project_board_status.schema.json")
    assert report["status"] == "ON_TRACK"
    assert report["implementation_goal_blocked"] is False
    assert report["task_count"] == 11
    assert report["completed_count"] == 3
    assert report["ready_count"] == 1
    assert report["awaiting_human_count"] == 6
    assert report["awaiting_external_count"] == 1
    assert report["accepted_non_blocking_risk_count"] == 2


def test_default_date_uses_contest_timezone_not_machine_timezone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDatetime:
        @classmethod
        def now(cls, timezone):
            assert getattr(timezone, "key", None) == "Asia/Shanghai"
            return datetime(2026, 7, 19, 3, 30, tzinfo=timezone)

    monkeypatch.setattr(project_board_module, "datetime", FrozenDatetime)
    report = build_project_board_status()
    assert report["as_of"] == "2026-07-19"


def test_overdue_and_deadline_states_are_explicit_not_blocked() -> None:
    attention = build_project_board_status(as_of=date(2026, 7, 22))
    missed = build_project_board_status(as_of=date(2026, 7, 23))
    assert attention["status"] == "ATTENTION_REQUIRED"
    assert attention["overdue_task_ids"]
    assert attention["implementation_goal_blocked"] is False
    assert missed["status"] == "DEADLINE_MISSED"
    assert missed["implementation_goal_blocked"] is False


def test_board_covers_each_human_gate_exactly_once() -> None:
    board = validate_document(json.loads(BOARD.read_text(encoding="utf-8")), "project_board.schema.json")
    runbook = validate_document(json.loads(RUNBOOK.read_text(encoding="utf-8")), "pitch_runbook.schema.json")
    assert {item["human_gate_id"] for item in board["tasks"] if item["human_gate_id"]} == {item["gate_id"] for item in runbook["human_gates"]}
    assert len(board["daily_commands"]) == 8
    assert "bash scripts/check_submission_closeout.sh" in board["daily_commands"]
    assert "bash scripts/check_demo_mode_parity.sh" in board["daily_commands"]


def test_duplicate_task_or_missing_gate_is_rejected(tmp_path: Path) -> None:
    board = json.loads(BOARD.read_text(encoding="utf-8"))
    duplicate = deepcopy(board)
    duplicate["tasks"][-1]["task_id"] = duplicate["tasks"][0]["task_id"]
    with pytest.raises(ProjectBoardError, match="不能重复"):
        build_project_board_status(board_path=_write_board(tmp_path, duplicate), as_of=date(2026, 7, 18))

    missing = deepcopy(board)
    missing["tasks"][1]["human_gate_id"] = None
    with pytest.raises(ProjectBoardError, match="各出现一次"):
        build_project_board_status(board_path=_write_board(tmp_path, missing), as_of=date(2026, 7, 18))


def test_docker_cannot_be_promoted_to_blocking_risk(tmp_path: Path) -> None:
    board = json.loads(BOARD.read_text(encoding="utf-8"))
    risk = next(item for item in board["risks"] if item["risk_id"] == "DOCKER_PERMISSION")
    risk["blocking_scope"] = "FORMAL_SUBMISSION"
    with pytest.raises(ProjectBoardError, match="Docker"):
        build_project_board_status(board_path=_write_board(tmp_path, board), as_of=date(2026, 7, 18))


def test_equal_count_substitution_and_absolute_path_are_rejected(tmp_path: Path) -> None:
    board = json.loads(BOARD.read_text(encoding="utf-8"))
    substituted = deepcopy(board)
    substituted["risks"][0]["risk_id"] = "UNRELATED_RISK"
    with pytest.raises(ProjectBoardError, match="冻结P0范围"):
        build_project_board_status(board_path=_write_board(tmp_path, substituted), as_of=date(2026, 7, 18))

    unsafe = deepcopy(board)
    unsafe["tasks"][0]["evidence"] = "/Users/example/private.txt"
    with pytest.raises(ProjectBoardError, match="绝对路径"):
        build_project_board_status(board_path=_write_board(tmp_path, unsafe), as_of=date(2026, 7, 18))


def test_schema_has_no_blocked_status() -> None:
    board = json.loads(BOARD.read_text(encoding="utf-8"))
    board["tasks"][0]["status"] = "BLOCKED"
    with pytest.raises(ContractValidationError):
        validate_document(board, "project_board.schema.json")


def test_project_board_script_is_executable() -> None:
    script = ROOT / "scripts/check_project_board.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
