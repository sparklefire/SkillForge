"""Create and validate a private full-watch review for the final demo recording."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import ContractValidationError, validate_document
from .demo import ROOT


DEFAULT_PRIVATE_ROOT = ROOT / "outputs/submission"
DEFAULT_RECORDING = DEFAULT_PRIVATE_ROOT / "skillforge_final_recording.mp4"
DEFAULT_MACHINE_QA = DEFAULT_PRIVATE_ROOT / "final_recording_qa.json"
DEFAULT_BUILD_REPORT = DEFAULT_PRIVATE_ROOT / "final_recording_build.json"
DEFAULT_STORYBOARD = ROOT / "config/final_recording_storyboard.json"
DEFAULT_POLICY = ROOT / "config/final_recording_policy.json"
DEFAULT_INPUT = DEFAULT_PRIVATE_ROOT / "final_recording_review.json"
DEFAULT_REPORT = DEFAULT_PRIVATE_ROOT / "final_recording_review_qa.json"
MAXIMUM_WATCH_ELAPSED_MS = 6 * 60 * 60 * 1000


class FinalRecordingReviewError(ValueError):
    """Raised when the private final-recording review cannot be trusted."""


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
        raise FinalRecordingReviewError("最终录屏审核记录必须保存在私有提交目录") from exc
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalRecordingReviewError(f"{label}无法读取或不是合法JSON") from exc
    if not isinstance(value, dict):
        raise FinalRecordingReviewError(f"{label}必须是JSON对象")
    return value


def _timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinalRecordingReviewError(f"{label}不是合法时间") from exc
    if parsed.tzinfo is None:
        raise FinalRecordingReviewError(f"{label}必须包含时区")
    return parsed


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
        raise FinalRecordingReviewError("最终录屏审核私有目录权限必须为0700")
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


def _private_artifact(path: Path, private_root: Path, label: str) -> Path:
    path = _inside(path, private_root)
    if not path.is_file() or path.stat().st_size < 1:
        raise FinalRecordingReviewError(f"{label}不存在或为空")
    if (
        stat.S_IMODE(path.stat().st_mode) != 0o600
        or stat.S_IMODE(path.parent.stat().st_mode) != 0o700
    ):
        raise FinalRecordingReviewError(f"{label}权限必须为目录0700、文件0600")
    return path


def _load_basis(
    *,
    recording_path: Path = DEFAULT_RECORDING,
    machine_qa_path: Path = DEFAULT_MACHINE_QA,
    build_report_path: Path = DEFAULT_BUILD_REPORT,
    storyboard_path: Path = DEFAULT_STORYBOARD,
    policy_path: Path = DEFAULT_POLICY,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> dict[str, Any]:
    recording_path = _private_artifact(recording_path, private_root, "最终录屏")
    machine_qa_path = _private_artifact(
        machine_qa_path, private_root, "最终录屏机器QA"
    )
    build_report_path = _private_artifact(
        build_report_path, private_root, "最终录屏构建报告"
    )
    storyboard_path = storyboard_path.expanduser().resolve()
    policy_path = policy_path.expanduser().resolve()
    try:
        machine_qa = validate_document(
            _read_json(machine_qa_path, "最终录屏机器QA"),
            "final_recording_qa.schema.json",
        )
        build_report = validate_document(
            _read_json(build_report_path, "最终录屏构建报告"),
            "final_recording_build.schema.json",
        )
        validate_document(
            _read_json(storyboard_path, "最终录屏故事板"),
            "final_recording_storyboard.schema.json",
        )
        validate_document(
            _read_json(policy_path, "最终录屏内部策略"),
            "final_recording_policy.schema.json",
        )
    except (ContractValidationError, FinalRecordingReviewError, OSError) as exc:
        raise FinalRecordingReviewError("最终录屏审核的当前事实基线无效") from exc

    recording = {
        "filename": recording_path.name,
        "sha256": _sha256(recording_path),
        "bytes": recording_path.stat().st_size,
        "duration_ms": int(machine_qa["media"]["duration_ms"] or 0),
    }
    qa_recording = {
        key: machine_qa["media"][key]
        for key in ("filename", "sha256", "bytes", "duration_ms")
    }
    build_recording = {
        key: build_report["media"][key]
        for key in ("filename", "sha256", "bytes", "duration_ms")
    }
    storyboard_sha256 = _sha256(storyboard_path)
    policy_sha256 = _sha256(policy_path)
    if machine_qa["status"] != "READY_FOR_HUMAN_REVIEW" or not all(
        machine_qa["checks"].values()
    ):
        raise FinalRecordingReviewError("最终录屏机器QA尚未通过")
    if (
        build_report["status"] != "READY_FOR_HUMAN_REVIEW"
        or not build_report["machine_qa"]["all_checks_passed"]
        or not build_report["machine_qa"]["scene_sequence_all_matched"]
        or not all(scene["sequence_match"] for scene in build_report["scenes"])
    ):
        raise FinalRecordingReviewError("最终录屏构建或九场景序列检查尚未通过")
    if recording != qa_recording or recording != build_recording:
        raise FinalRecordingReviewError("最终录屏与机器QA或构建报告不一致")
    if build_report["storyboard_sha256"] != storyboard_sha256:
        raise FinalRecordingReviewError("最终录屏故事板已变化")
    if machine_qa["policy_sha256"] != policy_sha256:
        raise FinalRecordingReviewError("最终录屏内部策略已变化")
    return {
        "recording": recording,
        "machine_qa": machine_qa,
        "machine_qa_sha256": _sha256(machine_qa_path),
        "build_report": build_report,
        "build_report_sha256": _sha256(build_report_path),
        "storyboard_sha256": storyboard_sha256,
        "policy_sha256": policy_sha256,
    }


def initialize_final_recording_review(
    destination: Path = DEFAULT_INPUT,
    *,
    recording_path: Path = DEFAULT_RECORDING,
    machine_qa_path: Path = DEFAULT_MACHINE_QA,
    build_report_path: Path = DEFAULT_BUILD_REPORT,
    storyboard_path: Path = DEFAULT_STORYBOARD,
    policy_path: Path = DEFAULT_POLICY,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    destination = _inside(destination, private_root)
    if destination.exists():
        raise FinalRecordingReviewError("最终录屏审核记录已存在；初始化不会覆盖已有内容")
    basis = _load_basis(
        recording_path=recording_path,
        machine_qa_path=machine_qa_path,
        build_report_path=build_report_path,
        storyboard_path=storyboard_path,
        policy_path=policy_path,
        private_root=private_root,
    )
    document = {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": _now(),
        "status": "PENDING_INPUT",
        "watch_started_at": None,
        "watch_completed_at": None,
        "playback_method": None,
        "recording": basis["recording"],
        "machine_qa_sha256": basis["machine_qa_sha256"],
        "build_report_sha256": basis["build_report_sha256"],
        "storyboard_sha256": basis["storyboard_sha256"],
        "policy_sha256": basis["policy_sha256"],
        "checks": {
            "full_playback_completed": False,
            "subtitles_present_and_readable": False,
            "no_private_content_or_personal_ui": False,
            "narration_audible": False,
            "narration_pacing_acceptable": False,
            "narration_and_demo_in_sync": False,
            "all_nine_scenes_understandable": False,
            "claims_and_boundaries_accurate": False,
            "no_playback_corruption": False,
            "final_cut_accepted": False,
            "official_video_requirements_not_assumed": False,
        },
        "notes": "",
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": False,
            "contains_credentials": False,
            "git_tracked": False,
            "automatic_human_approval": False,
        },
    }
    return _write_private_json(
        validate_document(document, "final_recording_review.schema.json"),
        destination,
        private_root=private_root,
    )


def verify_final_recording_review_document(
    document: dict[str, Any],
    *,
    review_sha256: str,
    review_bytes: int,
    basis: dict[str, Any],
) -> dict[str, Any]:
    try:
        validate_document(document, "final_recording_review.schema.json")
    except ContractValidationError as exc:
        raise FinalRecordingReviewError("最终录屏审核记录不符合严格Schema") from exc
    if document["status"] != "READY_FOR_CHECK":
        raise FinalRecordingReviewError("最终录屏审核记录尚未填写完成")
    started = _timestamp(document["watch_started_at"], "观看开始时间")
    completed = _timestamp(document["watch_completed_at"], "观看完成时间")
    updated = _timestamp(document["updated_at"], "审核更新时间")
    elapsed_ms = round((completed - started).total_seconds() * 1000)
    minimum_elapsed_ms = basis["recording"]["duration_ms"] - 2000
    if completed < started or updated < completed:
        raise FinalRecordingReviewError("最终录屏观看时间顺序无效")
    if not minimum_elapsed_ms <= elapsed_ms <= MAXIMUM_WATCH_ELAPSED_MS:
        raise FinalRecordingReviewError("最终录屏观看时长不足或异常")
    current_binding = {
        "recording": basis["recording"],
        "machine_qa_sha256": basis["machine_qa_sha256"],
        "build_report_sha256": basis["build_report_sha256"],
        "storyboard_sha256": basis["storyboard_sha256"],
        "policy_sha256": basis["policy_sha256"],
    }
    review_binding = {key: document[key] for key in current_binding}
    machine_qa = basis["machine_qa"]
    build_report = basis["build_report"]
    checks = {
        "machine_qa_ready": machine_qa["status"] == "READY_FOR_HUMAN_REVIEW",
        "machine_checks_passed": all(machine_qa["checks"].values()),
        "build_report_ready": build_report["status"] == "READY_FOR_HUMAN_REVIEW",
        "build_checks_passed": build_report["machine_qa"]["all_checks_passed"],
        "scene_sequence_matched": build_report["machine_qa"][
            "scene_sequence_all_matched"
        ] and all(scene["sequence_match"] for scene in build_report["scenes"]),
        "recording_matches_machine_qa": basis["recording"]["sha256"]
        == machine_qa["media"]["sha256"],
        "recording_matches_build": basis["recording"]["sha256"]
        == build_report["media"]["sha256"],
        "review_matches_current_artifacts": review_binding == current_binding,
        "storyboard_matches_build": basis["storyboard_sha256"]
        == build_report["storyboard_sha256"],
        "policy_matches_machine_qa": basis["policy_sha256"]
        == machine_qa["policy_sha256"],
        "watch_duration_sufficient": elapsed_ms >= minimum_elapsed_ms,
        **document["checks"],
    }
    if not all(checks.values()):
        failed = ",".join(key for key, value in checks.items() if not value)
        raise FinalRecordingReviewError(f"最终录屏完整观看或当前产物绑定未通过：{failed}")
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "FINAL_RECORDING_FULL_WATCH_QA",
        "checked_at": _now(),
        "status": "READY_FOR_HUMAN_CONFIRMATION",
        "review_sha256": review_sha256,
        "review_bytes": review_bytes,
        "recording": basis["recording"],
        "machine_qa_sha256": basis["machine_qa_sha256"],
        "build_report_sha256": basis["build_report_sha256"],
        "storyboard_sha256": basis["storyboard_sha256"],
        "policy_sha256": basis["policy_sha256"],
        "watch_elapsed_ms": elapsed_ms,
        "checks": checks,
        "human_gate_status": "PENDING",
        "official_rules_boundary": {
            "official_video_requirements_verified": False,
            "separate_official_rules_gate_required": True,
        },
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": False,
            "contains_notes": False,
            "contains_watch_timestamps": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "contains_media": False,
            "human_confirmation_generated": False,
        },
    }
    return validate_document(report, "final_recording_review_qa.schema.json")


def verify_final_recording_review(
    input_path: Path = DEFAULT_INPUT,
    *,
    recording_path: Path = DEFAULT_RECORDING,
    machine_qa_path: Path = DEFAULT_MACHINE_QA,
    build_report_path: Path = DEFAULT_BUILD_REPORT,
    storyboard_path: Path = DEFAULT_STORYBOARD,
    policy_path: Path = DEFAULT_POLICY,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> dict[str, Any]:
    input_path = _private_artifact(input_path, private_root, "最终录屏审核记录")
    return verify_final_recording_review_document(
        _read_json(input_path, "最终录屏审核记录"),
        review_sha256=_sha256(input_path),
        review_bytes=input_path.stat().st_size,
        basis=_load_basis(
            recording_path=recording_path,
            machine_qa_path=machine_qa_path,
            build_report_path=build_report_path,
            storyboard_path=storyboard_path,
            policy_path=policy_path,
            private_root=private_root,
        ),
    )


def final_recording_review_qa_issue(
    report_path: Path,
    evidence: dict[str, Any],
    *,
    review_path: Path | None = None,
    recording_path: Path | None = None,
    machine_qa_path: Path | None = None,
    build_report_path: Path | None = None,
    storyboard_path: Path = DEFAULT_STORYBOARD,
    policy_path: Path = DEFAULT_POLICY,
) -> str | None:
    if evidence.get("kind") != "LOCAL_FILE":
        return "FINAL_RECORDING_REVIEW_REQUIRES_LOCAL_FILE"
    report_path = report_path.expanduser().resolve()
    private_root = report_path.parent
    review_path = (review_path or private_root / DEFAULT_INPUT.name).expanduser().resolve()
    recording_path = (
        recording_path or private_root / DEFAULT_RECORDING.name
    ).expanduser().resolve()
    machine_qa_path = (
        machine_qa_path or private_root / DEFAULT_MACHINE_QA.name
    ).expanduser().resolve()
    build_report_path = (
        build_report_path or private_root / DEFAULT_BUILD_REPORT.name
    ).expanduser().resolve()
    locator = evidence.get("locator")
    if not isinstance(locator, str) or Path(locator).expanduser().resolve() != recording_path:
        return "FINAL_RECORDING_REVIEW_RECORDING_LOCATION_INVALID"
    if not report_path.is_file():
        return "FINAL_RECORDING_REVIEW_QA_MISSING"
    if not review_path.is_file():
        return "FINAL_RECORDING_REVIEW_RECORD_MISSING"
    if (
        stat.S_IMODE(report_path.stat().st_mode) != 0o600
        or stat.S_IMODE(review_path.stat().st_mode) != 0o600
        or stat.S_IMODE(private_root.stat().st_mode) != 0o700
    ):
        return "FINAL_RECORDING_REVIEW_PERMISSIONS_UNSAFE"
    try:
        report = validate_document(
            _read_json(report_path, "最终录屏观看QA"),
            "final_recording_review_qa.schema.json",
        )
        basis = _load_basis(
            recording_path=recording_path,
            machine_qa_path=machine_qa_path,
            build_report_path=build_report_path,
            storyboard_path=storyboard_path,
            policy_path=policy_path,
            private_root=private_root,
        )
        verified_report = verify_final_recording_review_document(
            _read_json(review_path, "最终录屏审核记录"),
            review_sha256=_sha256(review_path),
            review_bytes=review_path.stat().st_size,
            basis=basis,
        )
    except (ContractValidationError, FinalRecordingReviewError, OSError):
        return "FINAL_RECORDING_REVIEW_QA_INVALID"
    if report["review_sha256"] != _sha256(review_path) or report[
        "review_bytes"
    ] != review_path.stat().st_size:
        return "FINAL_RECORDING_REVIEW_RECORD_CHANGED"
    bindings = {
        "recording": basis["recording"],
        "machine_qa_sha256": basis["machine_qa_sha256"],
        "build_report_sha256": basis["build_report_sha256"],
        "storyboard_sha256": basis["storyboard_sha256"],
        "policy_sha256": basis["policy_sha256"],
    }
    if any(report[key] != value for key, value in bindings.items()):
        return "FINAL_RECORDING_REVIEW_ARTIFACTS_CHANGED"
    if any(
        report[key] != value
        for key, value in verified_report.items()
        if key != "checked_at"
    ):
        return "FINAL_RECORDING_REVIEW_QA_INVALID"
    if (
        report["recording"]["sha256"] != evidence.get("sha256")
        or report["recording"]["bytes"] != evidence.get("size_bytes")
    ):
        return "FINAL_RECORDING_REVIEW_RECORDING_CHANGED"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--recording", type=Path, default=DEFAULT_RECORDING)
    parser.add_argument("--machine-qa", type=Path, default=DEFAULT_MACHINE_QA)
    parser.add_argument("--build-report", type=Path, default=DEFAULT_BUILD_REPORT)
    parser.add_argument("--storyboard", type=Path, default=DEFAULT_STORYBOARD)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    try:
        if args.init:
            initialize_final_recording_review(
                args.input,
                recording_path=args.recording,
                machine_qa_path=args.machine_qa,
                build_report_path=args.build_report,
                storyboard_path=args.storyboard,
                policy_path=args.policy,
            )
            print(json.dumps({"status": "PENDING_INPUT"}, ensure_ascii=False))
            return 0
        report = verify_final_recording_review(
            args.input,
            recording_path=args.recording,
            machine_qa_path=args.machine_qa,
            build_report_path=args.build_report,
            storyboard_path=args.storyboard,
            policy_path=args.policy,
        )
        _write_private_json(report, args.output)
    except (ContractValidationError, FinalRecordingReviewError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": str(exc)
                    if isinstance(exc, FinalRecordingReviewError)
                    else "最终录屏完整观看审核失败",
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
                "duration_ms": report["recording"]["duration_ms"],
                "watch_elapsed_ms": report["watch_elapsed_ms"],
                "human_gate_status": report["human_gate_status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
