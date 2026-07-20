"""Verify that live, preprocessed and offline demos preserve one P0 result."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .contracts import ContractValidationError, validate_document
from .demo import ROOT
from .gold_rehearsal import run_gold_rehearsal


DEFAULT_PRIVATE_ROOT = ROOT / "outputs/demo_mode_parity"
DEFAULT_OUTPUT = DEFAULT_PRIVATE_ROOT / "demo_mode_parity.json"
DEFAULT_PREPROCESSED_DIR = ROOT / "cases/n31/output/gold_rehearsal_v1"
DEFAULT_OFFLINE_DIR = ROOT / "cases/n31/demo_bundle"
MODE_ORDER = ("live", "preprocessed", "offline")
REQUIRED_FILES = (
    "summary.json",
    "after_sop.json",
    "initial_conflicts.json",
    "final_conflicts.json",
    "revision_audit.json",
    "workflow.json",
)
EXPECTED_CONFLICT_KINDS = (
    "MISSING_STEP",
    "MISSING_PREREQUISITE",
    "ORDER_ERROR",
    "UNSUPPORTED_PARAMETER",
    "UNSUPPORTED_TOOL",
)
SEMANTIC_FINGERPRINT_KEYS = (
    "summary_projection_sha256",
    "final_step_projection_sha256",
    "initial_conflict_projection_sha256",
    "revision_projection_sha256",
)
RefreshRunner = Callable[[Path], None]


class DemoModeParityError(ValueError):
    """Raised when one demo fallback is unavailable, stale or inconsistent."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemoModeParityError(f"{label}无法读取") from exc
    if not isinstance(value, dict):
        raise DemoModeParityError(f"{label}必须是JSON对象")
    return value


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _summary_projection(summary: dict[str, Any]) -> dict[str, Any]:
    try:
        projection = {
            "case_id": summary["case_id"],
            "synthetic": summary["synthetic"],
            "evaluation_basis": summary["evaluation_basis"],
            "gold_status": summary["gold_status"],
            "metrics_status": summary["metrics_status"],
            "external_model_calls": summary["external_model_calls"],
            "workflow_state": summary["workflow_state"],
            "before": summary["before"],
            "after": summary["after"],
            "revision_count": summary["revision_count"],
            "conflict_kinds_before": summary["conflict_kinds_before"],
            "human_review_required": summary["human_review_required"],
        }
    except KeyError as exc:
        raise DemoModeParityError("演示摘要缺少闭环字段") from exc
    expected = {
        "case_id": "n31_media_change",
        "synthetic": False,
        "evaluation_basis": "OPERATOR_REVIEWED_GOLD",
        "gold_status": "GOLD",
        "metrics_status": "FINAL",
        "external_model_calls": 0,
        "workflow_state": "COMPLETED",
        "before": {
            "conflict_count": 5,
            "evidence_supported_required_steps": 0.9,
            "required_step_coverage": 0.9,
            "severe_error_count": 5,
        },
        "after": {
            "conflict_count": 0,
            "evidence_supported_required_steps": 1.0,
            "required_step_coverage": 1.0,
            "severe_error_count": 0,
        },
        "revision_count": 4,
        "conflict_kinds_before": list(EXPECTED_CONFLICT_KINDS),
        "human_review_required": False,
    }
    if projection != expected:
        raise DemoModeParityError("演示摘要不再满足冻结P0闭环")
    return projection


def _step_projection(after_sop: dict[str, Any]) -> list[dict[str, Any]]:
    steps = after_sop.get("steps")
    if not isinstance(steps, list) or len(steps) != 13:
        raise DemoModeParityError("演示最终SOP必须包含13步")
    if [item.get("step_id") for item in steps] != [f"S{index:02d}" for index in range(1, 14)]:
        raise DemoModeParityError("演示最终SOP步骤顺序已漂移")
    return steps


def _conflict_projection(report: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        validated = validate_document(report, "conflict.schema.json")
    except ContractValidationError as exc:
        raise DemoModeParityError("演示冲突报告不符合严格Schema") from exc
    return [
        {
            "conflict_id": item["conflict_id"],
            "kind": item["kind"],
            "severity": item["severity"],
            "status": item["status"],
            "automatic": item["automatic"],
            "proposed_action": item["proposed_action"],
            "step_ids": item["step_ids"],
        }
        for item in validated["conflicts"]
    ]


def _revision_projection(report: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        validated = validate_document(report, "revision_audit.schema.json")
    except ContractValidationError as exc:
        raise DemoModeParityError("演示修订审计不符合严格Schema") from exc
    return [
        {
            "conflict_id": item["conflict_id"],
            "action": item["action"],
            "path": item["path"],
        }
        for item in validated["changes"]
    ]


def _analyze_mode(
    mode: str,
    priority: int,
    source_state: str,
    directory: Path,
) -> dict[str, Any]:
    if any(not (directory / name).is_file() for name in REQUIRED_FILES):
        raise DemoModeParityError("演示模式缺少必要结构化产物")
    summary = _read_json(directory / "summary.json", f"{mode}摘要")
    after_sop = _read_json(directory / "after_sop.json", f"{mode}最终SOP")
    initial = _read_json(directory / "initial_conflicts.json", f"{mode}初检")
    final = _read_json(directory / "final_conflicts.json", f"{mode}复检")
    audit = _read_json(directory / "revision_audit.json", f"{mode}修订审计")
    workflow = _read_json(directory / "workflow.json", f"{mode}工作流")
    try:
        workflow = validate_document(workflow, "workflow_run.schema.json")
    except ContractValidationError as exc:
        raise DemoModeParityError("演示工作流不符合严格Schema") from exc

    summary_projection = _summary_projection(summary)
    steps = _step_projection(after_sop)
    initial_projection = _conflict_projection(initial)
    final_projection = _conflict_projection(final)
    revision_projection = _revision_projection(audit)
    if (
        len(initial_projection) != 5
        or tuple(item["kind"] for item in initial_projection)
        != EXPECTED_CONFLICT_KINDS
        or final_projection
        or len(revision_projection) != 4
        or workflow["state"] != "COMPLETED"
        or workflow.get("last_failure") is not None
    ):
        raise DemoModeParityError("演示冲突、修订或工作流状态不满足冻结P0")
    summary_sha256 = _canonical_sha256(summary_projection)
    steps_sha256 = _canonical_sha256(steps)
    initial_sha256 = _canonical_sha256(initial_projection)
    final_sha256 = _canonical_sha256(final_projection)
    revision_sha256 = _canonical_sha256(revision_projection)
    workflow_projection = {
        "state": workflow["state"],
        "stage_attempts": workflow["stage_attempts"],
        "last_failure": workflow["last_failure"],
        "data_policy": workflow["data_policy"],
    }
    source_snapshot_sha256 = _canonical_sha256(
        {
            "required_files": list(REQUIRED_FILES),
            "summary": summary_sha256,
            "steps": steps_sha256,
            "initial_conflicts": initial_sha256,
            "final_conflicts": final_sha256,
            "revision": revision_sha256,
            "workflow": workflow_projection,
        }
    )
    return {
        "mode": mode,
        "priority": priority,
        "source_state": source_state,
        "required_file_count": len(REQUIRED_FILES),
        "source_snapshot_sha256": source_snapshot_sha256,
        "summary_projection_sha256": summary_sha256,
        "final_step_projection_sha256": steps_sha256,
        "initial_conflict_projection_sha256": initial_sha256,
        "revision_projection_sha256": revision_sha256,
        "before_severe_errors": summary["before"]["severe_error_count"],
        "after_severe_errors": summary["after"]["severe_error_count"],
        "revision_count": summary["revision_count"],
        "step_count": len(steps),
        "initial_conflict_count": len(initial_projection),
        "final_conflict_count": len(final_projection),
        "gold_status": summary["gold_status"],
        "metrics_status": summary["metrics_status"],
        "workflow_state": workflow["state"],
        "external_model_calls": summary["external_model_calls"],
    }


def semantic_fingerprint_from_directory(directory: Path) -> str:
    """Return the stable P0 semantics fingerprint for one completed run."""

    analysis = _analyze_mode(
        "live",
        1,
        "RECOMPUTED_FROM_GOLD",
        directory.expanduser().resolve(),
    )
    return _canonical_sha256(
        {key: analysis[key] for key in SEMANTIC_FINGERPRINT_KEYS}
    )


def _refresh_preprocessed(root: Path) -> None:
    environment = os.environ.copy()
    environment["SKILLFORGE_OFFLINE_OCR"] = "1"
    result = subprocess.run(
        ["bash", "scripts/run_n31_local.sh"],
        cwd=root,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise DemoModeParityError("预处理演示重建失败")


def build_demo_mode_parity(
    *,
    root: Path = ROOT,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
    preprocessed_dir: Path = DEFAULT_PREPROCESSED_DIR,
    offline_dir: Path = DEFAULT_OFFLINE_DIR,
    refresh_preprocessed: bool = False,
    refresh_runner: RefreshRunner | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    private_root = private_root.expanduser().resolve()
    preprocessed_dir = preprocessed_dir.expanduser().resolve()
    offline_dir = offline_dir.expanduser().resolve()
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(private_root, 0o700)
    if refresh_preprocessed:
        (refresh_runner or _refresh_preprocessed)(root)

    with tempfile.TemporaryDirectory(prefix="live-", dir=private_root) as temporary:
        live_dir = Path(temporary)
        run_gold_rehearsal(
            root / "cases/n31/gold/gold_sop.json",
            root / "cases/n31/gold/constraints.json",
            root / "cases/n31/gold/fault_injection.json",
            live_dir,
        )
        modes = [
            _analyze_mode("live", 1, "RECOMPUTED_FROM_GOLD", live_dir),
            _analyze_mode(
                "preprocessed",
                2,
                (
                    "REFRESHED_FROM_LOCAL_INPUTS"
                    if refresh_preprocessed
                    else "PREPARED_OUTPUT_VERIFIED"
                ),
                preprocessed_dir,
            ),
            _analyze_mode(
                "offline", 3, "TRACKED_OFFLINE_BUNDLE", offline_dir
            ),
        ]

    for key in SEMANTIC_FINGERPRINT_KEYS:
        if len({item[key] for item in modes}) != 1:
            raise DemoModeParityError("三种演示模式的核心语义结果不一致")
    document = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "DEMO_MODE_PARITY_REPORT",
        "generated_at": generated_at or _now(),
        "status": "PASSED",
        "mode_count": len(modes),
        "preprocessed_refresh_performed": refresh_preprocessed,
        "modes": modes,
        "parity": {
            "mode_order_exact": tuple(item["mode"] for item in modes) == MODE_ORDER,
            "all_required_files_current": True,
            "summary_semantics_equal": True,
            "final_step_projection_equal": True,
            "initial_conflict_projection_equal": True,
            "revision_projection_equal": True,
            "expected_closed_loop_metrics": True,
            "external_model_calls": 0,
        },
        "data_policy": {
            "private_local_state": True,
            "contains_credentials": False,
            "contains_personal_data": False,
            "contains_absolute_paths": False,
            "contains_raw_media": False,
            "contains_source_claims": False,
            "external_model_calls": 0,
            "network_requests": 0,
            "automatic_human_confirmations": 0,
        },
    }
    return validate_document(document, "demo_mode_parity.schema.json")


def write_demo_mode_parity(
    document: dict[str, Any],
    output_path: Path = DEFAULT_OUTPUT,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    validate_document(document, "demo_mode_parity.schema.json")
    private_root = private_root.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path.parent != private_root:
        raise DemoModeParityError("三模式报告必须直接保存到私有报告目录")
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(private_root, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", dir=private_root
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, output_path)
        os.chmod(output_path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return output_path


def verify_saved_demo_mode_parity(
    output_path: Path = DEFAULT_OUTPUT,
    *,
    root: Path = ROOT,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
    preprocessed_dir: Path = DEFAULT_PREPROCESSED_DIR,
    offline_dir: Path = DEFAULT_OFFLINE_DIR,
) -> dict[str, Any]:
    output_path = output_path.expanduser().resolve()
    private_root = private_root.expanduser().resolve()
    if (
        not output_path.is_file()
        or output_path.parent != private_root
        or stat.S_IMODE(private_root.stat().st_mode) != 0o700
        or stat.S_IMODE(output_path.stat().st_mode) != 0o600
    ):
        raise DemoModeParityError("三模式报告缺失或权限无效")
    saved = validate_document(
        _read_json(output_path, "三模式报告"), "demo_mode_parity.schema.json"
    )
    current = build_demo_mode_parity(
        root=root,
        private_root=private_root,
        preprocessed_dir=preprocessed_dir,
        offline_dir=offline_dir,
        refresh_preprocessed=saved["preprocessed_refresh_performed"],
        refresh_runner=(lambda _root: None),
        generated_at=saved["generated_at"],
    )
    if current != saved:
        raise DemoModeParityError("三模式报告与当前演示产物不一致")
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-root", type=Path, default=DEFAULT_PRIVATE_ROOT)
    parser.add_argument("--preprocessed-dir", type=Path, default=DEFAULT_PREPROCESSED_DIR)
    parser.add_argument("--offline-dir", type=Path, default=DEFAULT_OFFLINE_DIR)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--refresh-preprocessed", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    output_path = args.output or args.private_root / DEFAULT_OUTPUT.name
    try:
        if args.verify_only:
            document = verify_saved_demo_mode_parity(
                output_path,
                private_root=args.private_root,
                preprocessed_dir=args.preprocessed_dir,
                offline_dir=args.offline_dir,
            )
        else:
            document = build_demo_mode_parity(
                private_root=args.private_root,
                preprocessed_dir=args.preprocessed_dir,
                offline_dir=args.offline_dir,
                refresh_preprocessed=args.refresh_preprocessed,
            )
            write_demo_mode_parity(
                document, output_path, private_root=args.private_root
            )
            verify_saved_demo_mode_parity(
                output_path,
                private_root=args.private_root,
                preprocessed_dir=args.preprocessed_dir,
                offline_dir=args.offline_dir,
            )
    except (DemoModeParityError, ContractValidationError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": "三种演示模式一致性检查失败",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": document["status"],
                "mode_count": document["mode_count"],
                "preprocessed_refresh_performed": document[
                    "preprocessed_refresh_performed"
                ],
                "before_severe_errors": document["modes"][0][
                    "before_severe_errors"
                ],
                "after_severe_errors": document["modes"][0][
                    "after_severe_errors"
                ],
                "revision_count": document["modes"][0]["revision_count"],
                "external_model_calls": 0,
                "network_requests": 0,
                "automatic_human_confirmations": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
