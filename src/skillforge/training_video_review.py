"""Create and validate a private full-watch review for the current training video."""

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


DEFAULT_MANIFEST = ROOT / "output/video/n31_training_video_manifest_v1.json"
DEFAULT_VIDEO = ROOT / "output/video/n31_training_video_v1.mp4"
DEFAULT_PRIVATE_ROOT = ROOT / "outputs/submission"
DEFAULT_INPUT = DEFAULT_PRIVATE_ROOT / "training_video_review.json"
DEFAULT_REPORT = DEFAULT_PRIVATE_ROOT / "training_video_review_qa.json"
MAXIMUM_WATCH_ELAPSED_MS = 6 * 60 * 60 * 1000


class TrainingVideoReviewError(ValueError):
    """Raised when the private watch review or its public basis cannot be trusted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrainingVideoReviewError(f"{label}不是合法时间") from exc
    if parsed.tzinfo is None:
        raise TrainingVideoReviewError(f"{label}必须包含时区")
    return parsed


def _inside(path: Path, root: Path = DEFAULT_PRIVATE_ROOT) -> Path:
    resolved = path.expanduser().resolve()
    root = root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise TrainingVideoReviewError("观看审核记录和报告必须保存在私有提交目录") from exc
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrainingVideoReviewError(f"{label}无法读取或不是合法JSON") from exc
    if not isinstance(value, dict):
        raise TrainingVideoReviewError(f"{label}必须是JSON对象")
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
        raise TrainingVideoReviewError("观看审核私有目录权限必须为0700")
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


def _load_basis(manifest_path: Path, video_path: Path) -> dict[str, Any]:
    manifest_path = manifest_path.expanduser().resolve()
    video_path = video_path.expanduser().resolve()
    try:
        manifest = validate_document(
            _read_json(manifest_path, "培训视频生成清单"),
            "training_video_manifest.schema.json",
        )
    except (ContractValidationError, TrainingVideoReviewError) as exc:
        raise TrainingVideoReviewError("培训视频生成清单无效") from exc
    if not video_path.is_file() or video_path.stat().st_size < 1:
        raise TrainingVideoReviewError("当前培训视频不存在或为空")
    if video_path.name != manifest["output"]["filename"]:
        raise TrainingVideoReviewError("培训视频文件名与生成清单不一致")
    video_sha256 = _sha256(video_path)
    video_bytes = video_path.stat().st_size
    if (
        manifest["output"]["sha256"] != video_sha256
        or manifest["output"]["bytes"] != video_bytes
    ):
        raise TrainingVideoReviewError("培训视频内容与生成清单不一致")
    return {
        "manifest": manifest,
        "manifest_sha256": _sha256(manifest_path),
        "video": {
            "filename": manifest["output"]["filename"],
            "sha256": video_sha256,
            "bytes": video_bytes,
            "duration_ms": manifest["output"]["duration_ms"],
        },
    }


def initialize_training_video_review(
    destination: Path = DEFAULT_INPUT,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    video_path: Path = DEFAULT_VIDEO,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    destination = _inside(destination, private_root)
    if destination.exists():
        raise TrainingVideoReviewError("观看审核记录已存在；初始化不会覆盖已有内容")
    basis = _load_basis(manifest_path, video_path)
    document = {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": _now(),
        "status": "PENDING_INPUT",
        "watch_started_at": None,
        "watch_completed_at": None,
        "watched_at": None,
        "playback_method": None,
        "video": basis["video"],
        "manifest_sha256": basis["manifest_sha256"],
        "checks": {
            "full_playback_completed": False,
            "narration_audible": False,
            "narration_pacing_acceptable": False,
            "visuals_and_narration_in_sync": False,
            "all_steps_understandable": False,
            "no_sensitive_content_observed": False,
            "no_playback_corruption": False,
            "final_cut_accepted": False,
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
        validate_document(document, "training_video_review.schema.json"),
        destination,
        private_root=private_root,
    )


def migrate_pending_training_video_review(
    input_path: Path = DEFAULT_INPUT,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> bool:
    """Add watch timing fields only to a pristine legacy pending template."""

    input_path = _inside(input_path, private_root)
    if not input_path.is_file():
        raise TrainingVideoReviewError("观看审核记录不存在；请先使用--init")
    if (
        stat.S_IMODE(input_path.parent.stat().st_mode) != 0o700
        or stat.S_IMODE(input_path.stat().st_mode) != 0o600
    ):
        raise TrainingVideoReviewError("观看审核记录权限必须为目录0700、文件0600")
    document = _read_json(input_path, "观看审核记录")
    timing_keys = {"watch_started_at", "watch_completed_at"}
    present = timing_keys.intersection(document)
    if present == timing_keys:
        validate_document(document, "training_video_review.schema.json")
        return False
    if present:
        raise TrainingVideoReviewError("旧观看审核记录的计时字段不完整，拒绝自动迁移")
    if (
        document.get("status") != "PENDING_INPUT"
        or document.get("watched_at") is not None
        or document.get("playback_method") is not None
        or document.get("notes")
        or not isinstance(document.get("checks"), dict)
        or any(document["checks"].values())
    ):
        raise TrainingVideoReviewError("旧观看审核记录已含人工内容，拒绝自动迁移")
    document["watch_started_at"] = None
    document["watch_completed_at"] = None
    _write_private_json(
        validate_document(document, "training_video_review.schema.json"),
        input_path,
        private_root=private_root,
    )
    return True


def verify_training_video_review_document(
    document: dict[str, Any],
    *,
    review_sha256: str,
    review_bytes: int,
    basis: dict[str, Any],
) -> dict[str, Any]:
    try:
        validate_document(document, "training_video_review.schema.json")
    except ContractValidationError as exc:
        raise TrainingVideoReviewError("观看审核记录不符合严格Schema") from exc
    if document["status"] not in ("READY_FOR_CHECK", "FINAL_APPROVED"):
        raise TrainingVideoReviewError("观看审核记录尚未填写完成")

    started = _timestamp(document["watch_started_at"], "观看开始时间")
    completed = _timestamp(document["watch_completed_at"], "观看完成时间")
    watched = _timestamp(document["watched_at"], "观看确认时间")
    updated = _timestamp(document["updated_at"], "审核更新时间")
    elapsed_ms = round((completed - started).total_seconds() * 1000)
    minimum_elapsed_ms = basis["video"]["duration_ms"] - 2000
    if completed < started or updated < completed or watched != completed:
        raise TrainingVideoReviewError("培训视频观看时间顺序或确认时间无效")
    if not minimum_elapsed_ms <= elapsed_ms <= MAXIMUM_WATCH_ELAPSED_MS:
        raise TrainingVideoReviewError("培训视频观看时长不足或异常")

    manifest = basis["manifest"]
    review_checks = document["checks"]
    automated = manifest["automated_qa"]
    checks = {
        "manifest_schema_valid": True,
        "manifest_ready_for_human_review": (
            (manifest["status"] == "READY_FOR_HUMAN_REVIEW"
             and manifest["final_human_review_required"] is True)
            or (manifest["status"] == "FINAL_APPROVED"
                and manifest["final_human_review_required"] is False)
        ),
        "automated_qa_passed": all(
            value is True for key, value in automated.items() if key.endswith("_passed")
        ),
        "visual_review_passed": manifest["visual_review"]["status"] == "PASSED",
        "video_matches_manifest": basis["video"] == {
            key: manifest["output"][key]
            for key in ("filename", "sha256", "bytes", "duration_ms")
        },
        "review_matches_current_artifacts": (
            document["video"] == basis["video"]
            and document["manifest_sha256"] == basis["manifest_sha256"]
        ),
        "watch_duration_sufficient": elapsed_ms >= minimum_elapsed_ms,
        **review_checks,
    }
    if not all(checks.values()):
        failed = ",".join(key for key, value in checks.items() if not value)
        raise TrainingVideoReviewError(f"完整观看或当前成片绑定未通过：{failed}")

    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "TRAINING_VIDEO_FULL_WATCH_QA",
        "checked_at": _now(),
        "status": "READY_FOR_HUMAN_CONFIRMATION",
        "review_sha256": review_sha256,
        "review_bytes": review_bytes,
        "manifest_sha256": basis["manifest_sha256"],
        "video": basis["video"],
        "watch_elapsed_ms": elapsed_ms,
        "checks": checks,
        "human_gate_status": "PENDING",
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
    return validate_document(report, "training_video_review_qa.schema.json")


def verify_training_video_review(
    input_path: Path = DEFAULT_INPUT,
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    video_path: Path = DEFAULT_VIDEO,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> dict[str, Any]:
    input_path = _inside(input_path, private_root)
    if not input_path.is_file():
        raise TrainingVideoReviewError("观看审核记录不存在；请先使用--init")
    if (
        stat.S_IMODE(input_path.parent.stat().st_mode) != 0o700
        or stat.S_IMODE(input_path.stat().st_mode) != 0o600
    ):
        raise TrainingVideoReviewError("观看审核记录权限必须为目录0700、文件0600")
    return verify_training_video_review_document(
        _read_json(input_path, "观看审核记录"),
        review_sha256=_sha256(input_path),
        review_bytes=input_path.stat().st_size,
        basis=_load_basis(manifest_path, video_path),
    )


def training_video_review_qa_issue(
    report_path: Path,
    evidence: dict[str, Any],
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    video_path: Path = DEFAULT_VIDEO,
) -> str | None:
    if evidence.get("kind") != "LOCAL_FILE":
        return "TRAINING_VIDEO_REVIEW_REQUIRES_LOCAL_FILE"
    locator = evidence.get("locator")
    if not isinstance(locator, str) or not locator:
        return "TRAINING_VIDEO_REVIEW_LOCATION_INVALID"
    report_path = report_path.expanduser().resolve()
    review_path = Path(locator).expanduser().resolve()
    if (
        review_path.name != "training_video_review.json"
        or review_path.parent != report_path.parent
    ):
        return "TRAINING_VIDEO_REVIEW_LOCATION_INVALID"
    if (
        not review_path.is_file()
        or stat.S_IMODE(review_path.stat().st_mode) != 0o600
        or stat.S_IMODE(review_path.parent.stat().st_mode) != 0o700
    ):
        return "TRAINING_VIDEO_REVIEW_PERMISSIONS_UNSAFE"
    if not report_path.is_file():
        return "TRAINING_VIDEO_REVIEW_QA_MISSING"
    if (
        stat.S_IMODE(report_path.stat().st_mode) != 0o600
        or stat.S_IMODE(report_path.parent.stat().st_mode) != 0o700
    ):
        return "TRAINING_VIDEO_REVIEW_QA_PERMISSIONS_UNSAFE"
    try:
        report = validate_document(
            _read_json(report_path, "观看审核QA报告"),
            "training_video_review_qa.schema.json",
        )
        basis = _load_basis(manifest_path, video_path)
    except (ContractValidationError, TrainingVideoReviewError, OSError):
        return "TRAINING_VIDEO_REVIEW_QA_INVALID"
    if report["manifest_sha256"] != basis["manifest_sha256"]:
        return "TRAINING_VIDEO_REVIEW_MANIFEST_CHANGED"
    if report["video"] != basis["video"]:
        return "TRAINING_VIDEO_REVIEW_VIDEO_CHANGED"
    if (
        report["review_sha256"] != evidence.get("sha256")
        or report["review_bytes"] != evidence.get("size_bytes")
    ):
        return "TRAINING_VIDEO_REVIEW_RECORD_CHANGED"
    try:
        current = verify_training_video_review_document(
            _read_json(review_path, "观看审核记录"),
            review_sha256=_sha256(review_path),
            review_bytes=review_path.stat().st_size,
            basis=basis,
        )
    except (ContractValidationError, TrainingVideoReviewError, OSError):
        return "TRAINING_VIDEO_REVIEW_QA_INVALID"
    if any(
        report[key] != value
        for key, value in current.items()
        if key != "checked_at"
    ):
        return "TRAINING_VIDEO_REVIEW_STATE_CHANGED"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--video", type=Path, default=DEFAULT_VIDEO)
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    try:
        if args.init:
            initialize_training_video_review(
                args.input,
                manifest_path=args.manifest,
                video_path=args.video,
            )
            print(json.dumps({"status": "PENDING_INPUT"}, ensure_ascii=False))
            return 0
        report = verify_training_video_review(
            args.input,
            manifest_path=args.manifest,
            video_path=args.video,
        )
        _write_private_json(report, args.output)
    except (ContractValidationError, OSError, TrainingVideoReviewError) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": str(exc)
                    if isinstance(exc, TrainingVideoReviewError)
                    else "培训视频观看审核失败",
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
                "duration_ms": report["video"]["duration_ms"],
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
