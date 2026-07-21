"""Create and validate the private, timed final-stage rehearsal record."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cli_hints import print_error_hints
from .contracts import ContractValidationError, validate_document
from .demo import ROOT


DEFAULT_POLICY = ROOT / "config/final_rehearsal_policy.json"
DEFAULT_RUNBOOK = ROOT / "cases/n31/pitch_runbook.json"
DEFAULT_PRIVATE_ROOT = ROOT / "outputs/submission"
DEFAULT_INPUT = DEFAULT_PRIVATE_ROOT / "final_stage_rehearsal.json"
DEFAULT_REPORT = DEFAULT_PRIVATE_ROOT / "final_stage_rehearsal_qa.json"


class FinalRehearsalError(ValueError):
    """Raised when a final-stage rehearsal record is incomplete or unsafe."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path = DEFAULT_PRIVATE_ROOT) -> Path:
    resolved = path.expanduser().resolve()
    root = root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FinalRehearsalError("彩排记录和报告必须保存在私有提交目录") from exc
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalRehearsalError(f"{label}无法读取或不是合法JSON") from exc
    if not isinstance(value, dict):
        raise FinalRehearsalError(f"{label}必须是JSON对象")
    return value


def _write_private_json(
    document: dict[str, Any],
    destination: Path,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    destination = _inside(destination, private_root)
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(destination.parent, 0o700)
    elif stat.S_IMODE(destination.parent.stat().st_mode) != 0o700:
        raise FinalRehearsalError("彩排私有目录权限必须为0700")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    try:
        return validate_document(
            _read_json(path.expanduser().resolve(), "彩排策略"),
            "final_rehearsal_policy.schema.json",
        )
    except (ContractValidationError, FinalRehearsalError) as exc:
        raise FinalRehearsalError("最终彩排内部策略无效") from exc


def load_runbook(path: Path = DEFAULT_RUNBOOK) -> dict[str, Any]:
    try:
        return validate_document(
            _read_json(path.expanduser().resolve(), "路演运行单"),
            "pitch_runbook.schema.json",
        )
    except (ContractValidationError, FinalRehearsalError) as exc:
        raise FinalRehearsalError("路演运行单无效") from exc


def initialize_final_rehearsal(
    destination: Path = DEFAULT_INPUT,
    *,
    runbook_path: Path = DEFAULT_RUNBOOK,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    destination = _inside(destination, private_root)
    if destination.exists():
        raise FinalRehearsalError("彩排记录已存在；初始化不会覆盖已有内容")
    runbook = load_runbook(runbook_path)
    document = {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": _now(),
        "status": "PENDING_INPUT",
        "performed_at": None,
        "run_number": None,
        "timer_source": None,
        "total_duration_ms": None,
        "segments": [
            {
                "phase": item["phase"],
                "planned_start_ms": item["start_ms"],
                "planned_end_ms": item["end_ms"],
                "actual_start_ms": None,
                "actual_end_ms": None,
                "script_completed": False,
                "operator_action_completed": False,
                "proof_points_verified": False,
                "fallback_ready": False,
            }
            for item in runbook["segments"]
        ],
        "completion": {
            "full_sequence_completed": False,
            "no_unrecovered_failure": False,
            "no_sensitive_material_shown": False,
        },
        "notes": "",
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": False,
            "contains_credentials": False,
            "git_tracked": False,
        },
    }
    return _write_private_json(
        validate_document(document, "final_rehearsal_record.schema.json"),
        destination,
        private_root=private_root,
    )


def verify_final_rehearsal_document(
    document: dict[str, Any],
    *,
    record_sha256: str,
    record_bytes: int,
    runbook: dict[str, Any],
    runbook_sha256: str,
    policy: dict[str, Any],
    policy_sha256: str,
) -> dict[str, Any]:
    try:
        validate_document(document, "final_rehearsal_record.schema.json")
    except ContractValidationError as exc:
        raise FinalRehearsalError("彩排记录不符合严格Schema") from exc
    if document["status"] != "READY_FOR_CHECK":
        raise FinalRehearsalError("彩排记录尚未填写完成")

    segments = document["segments"]
    expected = runbook["segments"]
    actual_pairs = [
        (item["actual_start_ms"], item["actual_end_ms"]) for item in segments
    ]
    actual_contiguous = bool(actual_pairs) and actual_pairs[0][0] == 0
    for index, (start, end) in enumerate(actual_pairs):
        actual_contiguous = actual_contiguous and start is not None and end is not None
        actual_contiguous = actual_contiguous and end > start
        if index:
            actual_contiguous = actual_contiguous and start == actual_pairs[index - 1][1]

    total = document["total_duration_ms"]
    duration = policy["duration"]
    checks = {
        "phase_order_matches_runbook": [item["phase"] for item in segments]
        == [item["phase"] for item in expected],
        "planned_timeline_matches_runbook": [
            (item["planned_start_ms"], item["planned_end_ms"]) for item in segments
        ]
        == [(item["start_ms"], item["end_ms"]) for item in expected],
        "actual_timeline_contiguous": actual_contiguous,
        "actual_total_matches": bool(
            actual_contiguous and total == actual_pairs[-1][1]
        ),
        "duration_within_internal_target": (
            duration["minimum_ms"] <= total <= duration["maximum_ms"]
        ),
        "all_scripts_completed": all(item["script_completed"] for item in segments),
        "all_operator_actions_completed": all(
            item["operator_action_completed"] for item in segments
        ),
        "all_proof_points_verified": all(
            item["proof_points_verified"] for item in segments
        ),
        "all_fallbacks_ready": all(item["fallback_ready"] for item in segments),
        "full_sequence_completed": document["completion"]["full_sequence_completed"],
        "no_unrecovered_failure": document["completion"]["no_unrecovered_failure"],
        "no_sensitive_material_shown": document["completion"][
            "no_sensitive_material_shown"
        ],
    }
    if not all(checks.values()):
        failed = ",".join(key for key, value in checks.items() if not value)
        raise FinalRehearsalError(f"彩排计时或完整性检查未通过：{failed}")

    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "FINAL_STAGE_REHEARSAL_QA",
        "checked_at": _now(),
        "status": "READY_FOR_HUMAN_CONFIRMATION",
        "record_sha256": record_sha256,
        "record_bytes": record_bytes,
        "runbook_sha256": runbook_sha256,
        "policy_sha256": policy_sha256,
        "duration": {
            "actual_ms": total,
            "minimum_ms": duration["minimum_ms"],
            "maximum_ms": duration["maximum_ms"],
            "headroom_ms": duration["maximum_ms"] - total,
        },
        "phase_count": len(segments),
        "checks": checks,
        "human_gate_status": "PENDING",
        "official_rules_boundary": {
            "policy_basis": policy["policy_basis"],
            "official_video_requirements_verified": policy[
                "official_video_requirements_verified"
            ],
        },
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": False,
            "contains_notes": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "human_confirmation_generated": False,
        },
    }
    return validate_document(report, "final_rehearsal_qa.schema.json")


def verify_final_rehearsal(
    input_path: Path = DEFAULT_INPUT,
    *,
    runbook_path: Path = DEFAULT_RUNBOOK,
    policy_path: Path = DEFAULT_POLICY,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> dict[str, Any]:
    input_path = _inside(input_path, private_root)
    if not input_path.is_file():
        raise FinalRehearsalError("彩排记录不存在；请先使用--init")
    if (
        stat.S_IMODE(input_path.parent.stat().st_mode) != 0o700
        or stat.S_IMODE(input_path.stat().st_mode) != 0o600
    ):
        raise FinalRehearsalError("彩排记录权限必须为目录0700、文件0600")
    runbook_path = runbook_path.expanduser().resolve()
    policy_path = policy_path.expanduser().resolve()
    return verify_final_rehearsal_document(
        _read_json(input_path, "彩排记录"),
        record_sha256=_sha256(input_path),
        record_bytes=input_path.stat().st_size,
        runbook=load_runbook(runbook_path),
        runbook_sha256=_sha256(runbook_path),
        policy=load_policy(policy_path),
        policy_sha256=_sha256(policy_path),
    )


def final_rehearsal_qa_issue(
    report_path: Path,
    evidence: dict[str, Any],
    *,
    runbook_path: Path = DEFAULT_RUNBOOK,
    policy_path: Path = DEFAULT_POLICY,
) -> str | None:
    if evidence.get("kind") != "LOCAL_FILE":
        return "FINAL_REHEARSAL_REQUIRES_LOCAL_FILE"
    locator = evidence.get("locator")
    if not isinstance(locator, str) or not locator:
        return "FINAL_REHEARSAL_RECORD_LOCATION_INVALID"
    record_path = Path(locator).expanduser().resolve()
    if (
        record_path.name != "final_stage_rehearsal.json"
        or record_path.parent != report_path.expanduser().resolve().parent
    ):
        return "FINAL_REHEARSAL_RECORD_LOCATION_INVALID"
    if (
        not record_path.is_file()
        or stat.S_IMODE(record_path.stat().st_mode) != 0o600
        or stat.S_IMODE(record_path.parent.stat().st_mode) != 0o700
    ):
        return "FINAL_REHEARSAL_RECORD_PERMISSIONS_UNSAFE"
    if not report_path.is_file():
        return "FINAL_REHEARSAL_QA_MISSING"
    if (
        stat.S_IMODE(report_path.stat().st_mode) != 0o600
        or stat.S_IMODE(report_path.parent.stat().st_mode) != 0o700
    ):
        return "FINAL_REHEARSAL_QA_PERMISSIONS_UNSAFE"
    try:
        report = validate_document(
            _read_json(report_path, "彩排QA报告"),
            "final_rehearsal_qa.schema.json",
        )
        current_runbook_sha256 = _sha256(runbook_path.expanduser().resolve())
        current_policy_sha256 = _sha256(policy_path.expanduser().resolve())
    except OSError:
        return "FINAL_REHEARSAL_QA_BASIS_MISSING"
    except (ContractValidationError, FinalRehearsalError):
        return "FINAL_REHEARSAL_QA_INVALID"
    if report["runbook_sha256"] != current_runbook_sha256:
        return "FINAL_REHEARSAL_QA_RUNBOOK_CHANGED"
    if report["policy_sha256"] != current_policy_sha256:
        return "FINAL_REHEARSAL_QA_POLICY_CHANGED"
    if (
        report["record_sha256"] != evidence.get("sha256")
        or report["record_bytes"] != evidence.get("size_bytes")
    ):
        return "FINAL_REHEARSAL_QA_RECORD_CHANGED"
    try:
        current = verify_final_rehearsal(
            record_path,
            runbook_path=runbook_path,
            policy_path=policy_path,
            private_root=record_path.parent,
        )
    except (ContractValidationError, FinalRehearsalError, OSError):
        return "FINAL_REHEARSAL_QA_INVALID"
    if any(
        report[key] != value
        for key, value in current.items()
        if key != "checked_at"
    ):
        return "FINAL_REHEARSAL_QA_STATE_CHANGED"
    return None


_FINAL_REHEARSAL_ERROR_HINTS = {
    "彩排记录不存在；请先使用--init": [
        "── 最终彩排待办（私有） ──",
        "  1. 初始化空白彩排记录：bash scripts/check_final_rehearsal.sh --init",
        "  2. 建议改用引导式流程：bash scripts/run_guided_human_review.sh final-rehearsal",
        "  3. 按运行单完成连续真人计时彩排并填写记录，把 status 改为 READY_FOR_CHECK",
    ],
    "彩排记录尚未填写完成": [
        "  提示：按运行单补全彩排各阶段计时与结论，并把 status 改为 READY_FOR_CHECK，"
        "再重新运行本脚本；或改用 bash scripts/run_guided_human_review.sh final-rehearsal。",
    ],
    "彩排记录不符合严格Schema": [
        "  提示：对照私有彩排记录模板的字段名与取值检查并修正，再重新运行本脚本。",
    ],
    "彩排记录权限必须为目录0700、文件0600": [
        "  提示：私有目录应为 0700、彩排记录文件应为 0600；修正后重新运行本脚本。",
    ],
    "彩排私有目录权限必须为0700": [
        "  提示：私有提交目录权限应为 0700；修正后重新运行本脚本。",
    ],
}

_FINAL_REHEARSAL_PREFIX_HINTS = {
    "彩排计时或完整性检查未通过": [
        "  提示：按运行单重做上述未通过项，补全计时与结论，"
        "并把 status 改为 READY_FOR_CHECK，再重新运行本脚本。",
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--runbook", type=Path, default=DEFAULT_RUNBOOK)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    try:
        if args.init:
            initialize_final_rehearsal(args.input, runbook_path=args.runbook)
            print(json.dumps({"status": "PENDING_INPUT"}, ensure_ascii=False))
            return 0
        report = verify_final_rehearsal(
            args.input,
            runbook_path=args.runbook,
            policy_path=args.policy,
        )
        _write_private_json(report, args.output)
    except (ContractValidationError, FinalRehearsalError, OSError) as exc:
        if isinstance(exc, FinalRehearsalError):
            print_error_hints(
                str(exc),
                exact_hints=_FINAL_REHEARSAL_ERROR_HINTS,
                prefix_hints=_FINAL_REHEARSAL_PREFIX_HINTS,
            )
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": str(exc)
                    if isinstance(exc, FinalRehearsalError)
                    else "最终彩排验证失败",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "duration_ms": report["duration"]["actual_ms"],
                "headroom_ms": report["duration"]["headroom_ms"],
                "phase_count": report["phase_count"],
                "human_gate_status": report["human_gate_status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
