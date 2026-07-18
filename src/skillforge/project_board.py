"""Validate the P0 project board without treating human gates as a blocked goal."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from .contracts import ContractValidationError, validate_document
from .demo import ROOT


DEFAULT_BOARD = ROOT / "config/project_board.json"
DEFAULT_RUNBOOK = ROOT / "cases/n31/pitch_runbook.json"
EXPECTED_ROLES = {"TECHNICAL_OWNER", "EVIDENCE_OWNER", "CONTENT_OWNER", "DEMO_OPERATOR", "SUBMISSION_OWNER", "FINAL_REVIEWER"}
EXPECTED_TASK_IDS = {
    "TECHNICAL_PACKAGE_FREEZE", "TRAINING_VIDEO_FULL_WATCH", "TEAM_ROSTER_AND_ELIGIBILITY",
    "OFFICIAL_RULES_REVIEW", "FINAL_STAGE_REHEARSAL", "FINAL_RECORDING_REVIEW",
    "FINAL_CLEAN_PREFLIGHT", "SUBMISSION_UPLOAD", "PUBLIC_LINK_QA", "SUBMISSION_RECEIPT",
}
EXPECTED_RISK_IDS = {
    "OFFICIAL_RULES_UNAVAILABLE", "FINAL_RECORDING_NOT_READY", "TEAM_ELIGIBILITY_UNCONFIRMED",
    "PUBLIC_LINK_UNAVAILABLE", "SENSITIVE_DATA_LEAK", "DGX_CONNECTIVITY", "DOCKER_PERMISSION",
}
EXPECTED_DAILY_COMMANDS = {
    "bash scripts/check_project_board.sh", "bash scripts/check_submission.sh",
    "bash scripts/check_pitch.sh", "bash scripts/build_release_manifest.sh --verify-only",
    "bash scripts/manage_human_gates.sh status", "bash scripts/dgx_demo_tunnel.sh --smoke",
}


class ProjectBoardError(ValueError):
    """Raised when the task board is incomplete or misrepresents blockers."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectBoardError("任务看板无法读取") from exc
    if not isinstance(value, dict):
        raise ProjectBoardError("任务看板必须是JSON对象")
    return value


def build_project_board_status(
    *,
    board_path: Path = DEFAULT_BOARD,
    runbook_path: Path = DEFAULT_RUNBOOK,
    as_of: date | None = None,
) -> dict[str, Any]:
    try:
        board = validate_document(_read_json(board_path), "project_board.schema.json")
        runbook = validate_document(_read_json(runbook_path), "pitch_runbook.schema.json")
    except ContractValidationError as exc:
        raise ProjectBoardError("任务看板或路演运行单不符合严格Schema") from exc
    tasks = board["tasks"]
    task_ids = [item["task_id"] for item in tasks]
    risk_ids = [item["risk_id"] for item in board["risks"]]
    if len(task_ids) != len(set(task_ids)) or len(risk_ids) != len(set(risk_ids)):
        raise ProjectBoardError("任务或风险ID不能重复")
    if set(task_ids) != EXPECTED_TASK_IDS or set(risk_ids) != EXPECTED_RISK_IDS:
        raise ProjectBoardError("任务或风险集合与冻结P0范围不一致")
    if set(board["daily_commands"]) != EXPECTED_DAILY_COMMANDS:
        raise ProjectBoardError("每日检查命令集合不完整")
    serialized = json.dumps(board, ensure_ascii=False)
    if any(marker in serialized for marker in ("/Users/", "/home/", "file://")):
        raise ProjectBoardError("任务看板不能包含绝对路径")
    if {item["owner_role"] for item in [*tasks, *board["risks"]]} - EXPECTED_ROLES:
        raise ProjectBoardError("任务或风险引用了未知角色")
    gate_ids = {item["gate_id"] for item in runbook["human_gates"]}
    board_gate_ids = [item["human_gate_id"] for item in tasks if item["human_gate_id"]]
    if len(board_gate_ids) != len(set(board_gate_ids)) or set(board_gate_ids) != gate_ids:
        raise ProjectBoardError("五项人工门禁必须在看板中各出现一次")
    deadline = date.fromisoformat(board["deadline"])
    if any(date.fromisoformat(item["due_date"]) > deadline for item in tasks):
        raise ProjectBoardError("P0任务截止日不能晚于作品提交日")
    docker_risk = next(item for item in board["risks"] if item["risk_id"] == "DOCKER_PERMISSION")
    if docker_risk["status"] != "ACCEPTED" or docker_risk["blocking_scope"] != "NON_BLOCKING":
        raise ProjectBoardError("Docker权限必须保持非阻塞已接受风险")

    current = as_of or date.today()
    incomplete = [item for item in tasks if item["status"] != "COMPLETED"]
    overdue = sorted(
        item["task_id"]
        for item in incomplete
        if date.fromisoformat(item["due_date"]) < current
    )
    counts = {
        status: sum(item["status"] == status for item in tasks)
        for status in board["status_vocabulary"]
    }
    if current > deadline and incomplete:
        status = "DEADLINE_MISSED"
    elif overdue:
        status = "ATTENTION_REQUIRED"
    else:
        status = "ON_TRACK"
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "PROJECT_BOARD_STATUS",
        "as_of": current.isoformat(),
        "deadline": board["deadline"],
        "status": status,
        "implementation_goal_blocked": False,
        "task_count": len(tasks),
        "completed_count": counts["COMPLETED"],
        "ready_count": counts["READY"],
        "awaiting_human_count": counts["AWAITING_HUMAN"],
        "awaiting_external_count": counts["AWAITING_EXTERNAL"],
        "submission_blocking_task_count": sum(
            item["blocking_scope"] == "FORMAL_SUBMISSION" and item["status"] != "COMPLETED"
            for item in tasks
        ),
        "overdue_task_ids": overdue,
        "next_due_date": min((item["due_date"] for item in incomplete), default=None),
        "open_risk_count": sum(item["status"] == "OPEN" for item in board["risks"]),
        "accepted_non_blocking_risk_count": sum(
            item["status"] == "ACCEPTED" and item["blocking_scope"] == "NON_BLOCKING"
            for item in board["risks"]
        ),
        "data_policy": {"contains_credentials": False, "contains_personal_data": False, "contains_absolute_paths": False},
    }
    return validate_document(report, "project_board_status.schema.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=Path, default=DEFAULT_BOARD)
    parser.add_argument("--runbook", type=Path, default=DEFAULT_RUNBOOK)
    parser.add_argument("--as-of", type=date.fromisoformat)
    args = parser.parse_args()
    try:
        report = build_project_board_status(board_path=args.board, runbook_path=args.runbook, as_of=args.as_of)
    except (OSError, ProjectBoardError) as exc:
        print(json.dumps({"status": "ERROR", "message": "任务看板验证失败", "error_type": type(exc).__name__}, ensure_ascii=False))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ON_TRACK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
