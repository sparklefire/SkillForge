"""Guide local media reviews and a timed rehearsal without auto-approving gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .contracts import ContractValidationError, validate_document
from .final_recording_review import (
    DEFAULT_BUILD_REPORT,
    DEFAULT_INPUT as DEFAULT_FINAL_RECORDING_INPUT,
    DEFAULT_MACHINE_QA,
    DEFAULT_POLICY as DEFAULT_FINAL_RECORDING_POLICY,
    DEFAULT_RECORDING,
    DEFAULT_REPORT as DEFAULT_FINAL_RECORDING_REPORT,
    DEFAULT_STORYBOARD,
    FinalRecordingReviewError,
    _load_basis as _load_final_recording_basis,
    _write_private_json as _write_final_recording_json,
    initialize_final_recording_review,
    verify_final_recording_review,
    verify_final_recording_review_document,
)
from .final_rehearsal import (
    DEFAULT_INPUT as DEFAULT_REHEARSAL_INPUT,
    DEFAULT_POLICY as DEFAULT_REHEARSAL_POLICY,
    DEFAULT_REPORT as DEFAULT_REHEARSAL_REPORT,
    DEFAULT_RUNBOOK,
    FinalRehearsalError,
    _write_private_json as _write_rehearsal_json,
    initialize_final_rehearsal,
    load_policy,
    load_runbook,
    verify_final_rehearsal,
    verify_final_rehearsal_document,
)
from .training_video_review import (
    DEFAULT_INPUT as DEFAULT_TRAINING_INPUT,
    DEFAULT_MANIFEST,
    DEFAULT_REPORT as DEFAULT_TRAINING_REPORT,
    DEFAULT_VIDEO,
    MAXIMUM_WATCH_ELAPSED_MS,
    TrainingVideoReviewError,
    _load_basis as _load_training_basis,
    _write_private_json as _write_training_json,
    initialize_training_video_review,
    migrate_pending_training_video_review,
    verify_training_video_review,
    verify_training_video_review_document,
)


TRAINING_CHECK_PROMPTS = {
    "full_playback_completed": "确认从头到尾完整播放，没有拖动跳过？",
    "narration_audible": "确认旁白全程清晰可听？",
    "narration_pacing_acceptable": "确认旁白节奏可接受？",
    "visuals_and_narration_in_sync": "确认画面与旁白同步？",
    "all_steps_understandable": "确认全部操作步骤可以理解？",
    "no_sensitive_content_observed": "确认未看到敏感内容或私人界面？",
    "no_playback_corruption": "确认没有黑屏、卡死、花屏或播放损坏？",
    "final_cut_accepted": "确认接受这版80秒最终剪辑？",
}

FINAL_RECORDING_CHECK_PROMPTS = {
    "full_playback_completed": "确认从头到尾完整播放，没有拖动跳过？",
    "subtitles_present_and_readable": "确认字幕完整且清晰可读？",
    "no_private_content_or_personal_ui": "确认未出现私人内容或个人界面？",
    "narration_audible": "确认旁白全程清晰可听？",
    "narration_pacing_acceptable": "确认旁白节奏可接受？",
    "narration_and_demo_in_sync": "确认旁白与演示画面同步？",
    "all_nine_scenes_understandable": "确认九个场景都能理解？",
    "claims_and_boundaries_accurate": "确认事实主张和能力边界准确？",
    "no_playback_corruption": "确认没有黑屏、卡死、花屏或播放损坏？",
    "final_cut_accepted": "确认接受这版最终录屏剪辑？",
    "official_video_requirements_not_assumed": (
        "确认本次只审核内容质量，没有假定官方视频格式规则已经确认？"
    ),
}

REHEARSAL_SEGMENT_KEYS = (
    "script_completed",
    "operator_action_completed",
    "proof_points_verified",
    "fallback_ready",
)
REHEARSAL_COMPLETION_PROMPTS = {
    "full_sequence_completed": "确认七段路演连续完成？",
    "no_unrecovered_failure": "确认没有未恢复的故障？",
    "no_sensitive_material_shown": "确认彩排中未展示敏感材料？",
}


class GuidedHumanReviewError(ValueError):
    """Raised when a guided review cannot safely create a QA report."""


class GuidedHumanReviewDeclined(GuidedHumanReviewError):
    """Raised when the participant does not affirm a required human check."""


@dataclass(frozen=True)
class PlaybackResult:
    started_at: datetime
    completed_at: datetime
    elapsed_ms: int


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GuidedHumanReviewError(f"{label}无法读取或不是合法JSON") from exc
    if not isinstance(value, dict):
        raise GuidedHumanReviewError(f"{label}必须是JSON对象")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _require_private_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.stat().st_size < 1:
        raise GuidedHumanReviewError(f"{label}不存在或为空")
    if (
        stat.S_IMODE(resolved.stat().st_mode) != 0o600
        or stat.S_IMODE(resolved.parent.stat().st_mode) != 0o700
    ):
        raise GuidedHumanReviewError(f"{label}权限必须为目录0700、文件0600")
    return resolved


def _private_destination(path: Path, private_root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    root = private_root.expanduser().resolve()
    if resolved == root or root not in resolved.parents:
        raise GuidedHumanReviewError(f"{label}必须位于私有审核目录内")
    return resolved


def _restore_private_bytes(
    content: bytes,
    destination: Path,
    *,
    private_root: Path,
) -> None:
    destination = _private_destination(destination, private_root, "审核草稿")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.rollback.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _persist_review_transaction(
    document: dict[str, Any],
    *,
    input_path: Path,
    report_path: Path,
    private_root: Path,
    writer: Callable[..., Path],
    verifier: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Write a review and its QA together, restoring the draft on any failure."""

    original = input_path.read_bytes()
    try:
        writer(document, input_path, private_root=private_root)
        report = verifier()
        writer(report, report_path, private_root=private_root)
    except Exception:
        try:
            report_path.unlink(missing_ok=True)
            _restore_private_bytes(
                original,
                input_path,
                private_root=private_root,
            )
        except OSError as rollback_error:
            raise GuidedHumanReviewError(
                "人工审核写入失败且无法恢复原草稿"
            ) from rollback_error
        raise
    return report


def _iso(value: datetime, label: str) -> str:
    if value.tzinfo is None:
        raise GuidedHumanReviewError(f"{label}必须包含时区")
    return value.astimezone(timezone.utc).isoformat()


def _validate_playback(
    playback: PlaybackResult,
    *,
    minimum_ms: int,
) -> None:
    if playback.started_at.tzinfo is None or playback.completed_at.tzinfo is None:
        raise GuidedHumanReviewError("播放器计时必须包含时区")
    wall_elapsed = round(
        (playback.completed_at - playback.started_at).total_seconds() * 1000
    )
    if playback.completed_at < playback.started_at:
        raise GuidedHumanReviewError("播放器完成时间早于开始时间")
    if abs(wall_elapsed - playback.elapsed_ms) > 2000:
        raise GuidedHumanReviewError("播放器单调计时与时间戳差异过大")
    if not minimum_ms <= playback.elapsed_ms <= MAXIMUM_WATCH_ELAPSED_MS:
        raise GuidedHumanReviewError("播放时长不足或异常；没有写入人工结论")


def _require_true_answers(
    answers: dict[str, bool],
    expected_keys: set[str],
    label: str,
) -> None:
    if set(answers) != expected_keys or not all(
        isinstance(value, bool) for value in answers.values()
    ):
        raise GuidedHumanReviewError(f"{label}回答集合不完整")
    rejected = [key for key, value in answers.items() if not value]
    if rejected:
        raise GuidedHumanReviewDeclined(
            f"{label}存在未确认项；草稿和QA均未修改"
        )


def complete_training_video_review(
    playback: PlaybackResult,
    answers: dict[str, bool],
    *,
    input_path: Path = DEFAULT_TRAINING_INPUT,
    report_path: Path = DEFAULT_TRAINING_REPORT,
    manifest_path: Path = DEFAULT_MANIFEST,
    video_path: Path = DEFAULT_VIDEO,
    notes: str = "",
) -> dict[str, Any]:
    private_root = input_path.expanduser().resolve().parent
    report_path = _private_destination(
        report_path,
        private_root,
        "培训视频观看QA",
    )
    if report_path.exists():
        raise GuidedHumanReviewError("培训视频观看QA已存在，拒绝覆盖")
    migrate_pending_training_video_review(input_path, private_root=private_root)
    input_path = _require_private_file(input_path, "培训视频观看草稿")
    document = validate_document(
        _read_json(input_path, "培训视频观看草稿"),
        "training_video_review.schema.json",
    )
    if document["status"] != "PENDING_INPUT":
        raise GuidedHumanReviewError("培训视频观看记录不是待填写状态")
    _require_true_answers(
        answers,
        set(TRAINING_CHECK_PROMPTS),
        "培训视频观看检查",
    )
    basis = _load_training_basis(manifest_path, video_path)
    _validate_playback(
        playback,
        minimum_ms=basis["video"]["duration_ms"] - 2000,
    )
    completed = _iso(playback.completed_at, "观看完成时间")
    document.update(
        {
            "updated_at": completed,
            "status": "READY_FOR_CHECK",
            "watch_started_at": _iso(playback.started_at, "观看开始时间"),
            "watch_completed_at": completed,
            "watched_at": completed,
            "playback_method": "LOCAL_PLAYER",
            "checks": dict(answers),
            "notes": notes,
        }
    )
    verify_training_video_review_document(
        document,
        review_sha256="0" * 64,
        review_bytes=1,
        basis=basis,
    )
    return _persist_review_transaction(
        document,
        input_path=input_path,
        report_path=report_path,
        private_root=private_root,
        writer=_write_training_json,
        verifier=lambda: verify_training_video_review(
            input_path,
            manifest_path=manifest_path,
            video_path=video_path,
            private_root=private_root,
        ),
    )


def complete_final_recording_review(
    playback: PlaybackResult,
    answers: dict[str, bool],
    *,
    input_path: Path = DEFAULT_FINAL_RECORDING_INPUT,
    report_path: Path = DEFAULT_FINAL_RECORDING_REPORT,
    recording_path: Path = DEFAULT_RECORDING,
    machine_qa_path: Path = DEFAULT_MACHINE_QA,
    build_report_path: Path = DEFAULT_BUILD_REPORT,
    storyboard_path: Path = DEFAULT_STORYBOARD,
    policy_path: Path = DEFAULT_FINAL_RECORDING_POLICY,
    notes: str = "",
) -> dict[str, Any]:
    private_root = input_path.expanduser().resolve().parent
    report_path = _private_destination(
        report_path,
        private_root,
        "最终录屏观看QA",
    )
    if report_path.exists():
        raise GuidedHumanReviewError("最终录屏观看QA已存在，拒绝覆盖")
    input_path = _require_private_file(input_path, "最终录屏观看草稿")
    document = validate_document(
        _read_json(input_path, "最终录屏观看草稿"),
        "final_recording_review.schema.json",
    )
    if document["status"] != "PENDING_INPUT":
        raise GuidedHumanReviewError("最终录屏观看记录不是待填写状态")
    _require_true_answers(
        answers,
        set(FINAL_RECORDING_CHECK_PROMPTS),
        "最终录屏观看检查",
    )
    basis = _load_final_recording_basis(
        recording_path=recording_path,
        machine_qa_path=machine_qa_path,
        build_report_path=build_report_path,
        storyboard_path=storyboard_path,
        policy_path=policy_path,
        private_root=private_root,
    )
    _validate_playback(
        playback,
        minimum_ms=basis["recording"]["duration_ms"] - 2000,
    )
    completed = _iso(playback.completed_at, "观看完成时间")
    document.update(
        {
            "updated_at": completed,
            "status": "READY_FOR_CHECK",
            "watch_started_at": _iso(playback.started_at, "观看开始时间"),
            "watch_completed_at": completed,
            "playback_method": "LOCAL_PLAYER",
            "checks": dict(answers),
            "notes": notes,
        }
    )
    verify_final_recording_review_document(
        document,
        review_sha256="0" * 64,
        review_bytes=1,
        basis=basis,
    )
    return _persist_review_transaction(
        document,
        input_path=input_path,
        report_path=report_path,
        private_root=private_root,
        writer=_write_final_recording_json,
        verifier=lambda: verify_final_recording_review(
            input_path,
            recording_path=recording_path,
            machine_qa_path=machine_qa_path,
            build_report_path=build_report_path,
            storyboard_path=storyboard_path,
            policy_path=policy_path,
            private_root=private_root,
        ),
    )


def complete_final_rehearsal(
    boundaries_ms: list[int],
    segment_checks: list[dict[str, bool]],
    completion_checks: dict[str, bool],
    *,
    started_at: datetime,
    input_path: Path = DEFAULT_REHEARSAL_INPUT,
    report_path: Path = DEFAULT_REHEARSAL_REPORT,
    runbook_path: Path = DEFAULT_RUNBOOK,
    policy_path: Path = DEFAULT_REHEARSAL_POLICY,
    notes: str = "",
) -> dict[str, Any]:
    private_root = input_path.expanduser().resolve().parent
    report_path = _private_destination(
        report_path,
        private_root,
        "最终彩排QA",
    )
    if report_path.exists():
        raise GuidedHumanReviewError("最终彩排QA已存在，拒绝覆盖")
    input_path = _require_private_file(input_path, "最终彩排草稿")
    document = validate_document(
        _read_json(input_path, "最终彩排草稿"),
        "final_rehearsal_record.schema.json",
    )
    if document["status"] != "PENDING_INPUT":
        raise GuidedHumanReviewError("最终彩排记录不是待填写状态")
    runbook = load_runbook(runbook_path)
    policy = load_policy(policy_path)
    if (
        len(boundaries_ms) != len(runbook["segments"]) + 1
        or boundaries_ms[0] != 0
        or any(
            end <= start for start, end in zip(boundaries_ms, boundaries_ms[1:])
        )
    ):
        raise GuidedHumanReviewError("彩排时间边界数量或顺序无效")
    total_ms = boundaries_ms[-1]
    duration = policy["duration"]
    if not duration["minimum_ms"] <= total_ms <= duration["maximum_ms"]:
        raise GuidedHumanReviewError("彩排总时长不在内部175至180秒目标内；草稿未修改")
    if len(segment_checks) != len(runbook["segments"]):
        raise GuidedHumanReviewError("彩排逐段检查数量不完整")
    for checks in segment_checks:
        _require_true_answers(checks, set(REHEARSAL_SEGMENT_KEYS), "彩排逐段检查")
    _require_true_answers(
        completion_checks,
        set(REHEARSAL_COMPLETION_PROMPTS),
        "彩排整体检查",
    )
    completed_at = started_at + timedelta(milliseconds=total_ms)
    document.update(
        {
            "updated_at": _iso(completed_at, "彩排完成时间"),
            "status": "READY_FOR_CHECK",
            "performed_at": _iso(started_at, "彩排开始时间"),
            "run_number": document.get("run_number") or 1,
            "timer_source": "OTHER_MONOTONIC_TIMER",
            "total_duration_ms": total_ms,
            "completion": dict(completion_checks),
            "notes": notes,
        }
    )
    for index, segment in enumerate(document["segments"]):
        segment.update(
            {
                "actual_start_ms": boundaries_ms[index],
                "actual_end_ms": boundaries_ms[index + 1],
                **segment_checks[index],
            }
        )
    verify_final_rehearsal_document(
        document,
        record_sha256="0" * 64,
        record_bytes=1,
        runbook=runbook,
        runbook_sha256=_sha256(runbook_path.expanduser().resolve()),
        policy=policy,
        policy_sha256=_sha256(policy_path.expanduser().resolve()),
    )
    return _persist_review_transaction(
        document,
        input_path=input_path,
        report_path=report_path,
        private_root=private_root,
        writer=_write_rehearsal_json,
        verifier=lambda: verify_final_rehearsal(
            input_path,
            runbook_path=runbook_path,
            policy_path=policy_path,
            private_root=private_root,
        ),
    )


def prepare_pending_reviews() -> dict[str, str]:
    actions: dict[str, str] = {}
    if DEFAULT_TRAINING_INPUT.exists():
        migrated = migrate_pending_training_video_review()
        actions["training_video"] = "MIGRATED" if migrated else "PRESENT"
    else:
        initialize_training_video_review()
        actions["training_video"] = "INITIALIZED"
    if DEFAULT_REHEARSAL_INPUT.exists():
        actions["final_rehearsal"] = "PRESENT"
    else:
        initialize_final_rehearsal()
        actions["final_rehearsal"] = "INITIALIZED"
    if DEFAULT_FINAL_RECORDING_INPUT.exists():
        actions["final_recording"] = "PRESENT"
    else:
        initialize_final_recording_review()
        actions["final_recording"] = "INITIALIZED"
    return actions


def review_status() -> dict[str, Any]:
    items = {
        "training_video": (
            DEFAULT_TRAINING_INPUT,
            DEFAULT_TRAINING_REPORT,
            DEFAULT_VIDEO,
        ),
        "final_rehearsal": (
            DEFAULT_REHEARSAL_INPUT,
            DEFAULT_REHEARSAL_REPORT,
            DEFAULT_RUNBOOK,
        ),
        "final_recording": (
            DEFAULT_FINAL_RECORDING_INPUT,
            DEFAULT_FINAL_RECORDING_REPORT,
            DEFAULT_RECORDING,
        ),
    }
    result: dict[str, Any] = {}
    for name, (record, report, basis) in items.items():
        status = "ABSENT"
        if record.is_file():
            try:
                status = _read_json(record, "私有草稿").get("status", "INVALID")
            except GuidedHumanReviewError:
                status = "INVALID"
        result[name] = {
            "record_status": status,
            "qa_present": report.is_file(),
            "basis_present": basis.is_file(),
        }
    return {
        "status": "HUMAN_ACTION_REQUIRED",
        "automatic_human_confirmations": 0,
        "items": result,
    }


_STATUS_LABELS = {
    "training_video": ("80秒培训视频观看确认", "training-video"),
    "final_recording": ("最终录屏观看确认", "final-recording"),
    "final_rehearsal": ("最终舞台计时彩排", "final-rehearsal"),
}
_STATUS_DONE = {"FINAL_APPROVED", "READY_FOR_CHECK"}


def _print_status_guidance(result: dict[str, Any]) -> None:
    """Print a human-readable Chinese summary to stderr; stdout JSON stays unchanged."""
    items = result.get("items", {})
    lines = ["── 人工审核状态（仅供本人阅读，不会自动通过任何门禁）──"]
    pending: list[str] = []
    for key, (label, action) in _STATUS_LABELS.items():
        entry = items.get(key, {})
        status = entry.get("record_status", "ABSENT")
        if status in _STATUS_DONE and entry.get("qa_present"):
            lines.append(f"✅ {label}：已完成")
        else:
            lines.append(f"⏳ {label}：待完成（当前状态 {status}）")
            pending.append(f"bash scripts/run_guided_human_review.sh {action}")
    if pending:
        lines.append("下一步（在本机交互式终端依次运行）：")
        lines.extend(f"  {command}" for command in pending)
    else:
        lines.append("三项人工审核均已完成。")
    print("\n".join(lines), file=sys.stderr)


def _safe_player_environment() -> dict[str, str]:
    allowed = ("HOME", "PATH", "TMPDIR", "DISPLAY", "LANG", "LC_ALL", "LC_CTYPE")
    return {key: os.environ[key] for key in allowed if key in os.environ}


def run_ffplay(
    media_path: Path,
    title: str,
    *,
    player: str | None = None,
) -> PlaybackResult:
    executable = (
        str(Path(player).expanduser().resolve())
        if player and "/" in player
        else shutil.which(player or "ffplay")
    )
    if not executable or not Path(executable).is_file():
        raise GuidedHumanReviewError("未找到ffplay播放器")
    if not media_path.is_file() or media_path.stat().st_size < 1:
        raise GuidedHumanReviewError("待审核视频不存在或为空")
    started_at = datetime.now(timezone.utc)
    started_mono = time.monotonic()
    completed = subprocess.run(
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-autoexit",
            "-fs",
            "-window_title",
            title,
            str(media_path.resolve()),
        ],
        check=False,
        env=_safe_player_environment(),
    )
    completed_at = datetime.now(timezone.utc)
    elapsed_ms = round((time.monotonic() - started_mono) * 1000)
    if completed.returncode != 0:
        raise GuidedHumanReviewError("播放器未正常完成；草稿和QA均未修改")
    return PlaybackResult(started_at, completed_at, elapsed_ms)


def _confirm(prompt: str) -> bool:
    while True:
        answer = input(f"{prompt} [y/n] ").strip().lower()
        if answer in {"y", "yes", "是"}:
            return True
        if answer in {"n", "no", "否"}:
            return False
        print("请输入 y 或 n。")


def _require_tty() -> None:
    if not sys.stdin.isatty():
        raise GuidedHumanReviewError("引导式人工审核必须在交互式终端运行")


def _interactive_media(kind: str, player: str | None) -> dict[str, Any]:
    _require_tty()
    if kind == "training-video":
        print("即将全屏播放80秒培训视频。不要拖动进度；播放结束后逐项回答。")
        playback = run_ffplay(DEFAULT_VIDEO, "SkillForge 80秒培训视频审核", player=player)
        answers = {key: _confirm(text) for key, text in TRAINING_CHECK_PROMPTS.items()}
        report = complete_training_video_review(playback, answers)
        return {
            "status": report["status"],
            "review_type": "TRAINING_VIDEO_FULL_WATCH",
            "watch_elapsed_ms": report["watch_elapsed_ms"],
            "human_gate_status": report["human_gate_status"],
        }
    print("即将全屏播放178秒最终录屏。不要拖动进度；播放结束后逐项回答。")
    playback = run_ffplay(
        DEFAULT_RECORDING,
        "SkillForge 最终录屏完整观看审核",
        player=player,
    )
    answers = {
        key: _confirm(text) for key, text in FINAL_RECORDING_CHECK_PROMPTS.items()
    }
    report = complete_final_recording_review(playback, answers)
    return {
        "status": report["status"],
        "review_type": "FINAL_RECORDING_REVIEW",
        "watch_elapsed_ms": report["watch_elapsed_ms"],
        "human_gate_status": report["human_gate_status"],
    }


def _rehearsal_ppt_hint() -> str:
    presentation_dir = DEFAULT_RUNBOOK.parents[2] / "output" / "presentation"
    candidates = sorted(
        path
        for path in presentation_dir.glob("*v2.pptx")
        if not path.name.startswith("~$")
    )
    if candidates:
        return f"output/presentation/{candidates[0].name}"
    return "output/presentation/ 目录下最新版路演PPT（v2）"


def _interactive_rehearsal() -> dict[str, Any]:
    _require_tty()
    runbook = load_runbook()
    total_seconds = int(runbook.get("total_duration_ms", 180_000)) // 1000
    segment_count = len(runbook["segments"])
    print(
        f"═══ 最终舞台计时彩排：共{segment_count}段，全程预算{total_seconds}秒 ═══\n"
        "开始前准备（约1分钟）：\n"
        f"  1. PPT：演示模式打开 {_rehearsal_ppt_hint()}，停在第1页\n"
        "  2. Web：浏览器打开 http://127.0.0.1:17860；若无法访问，另开终端运行 bash scripts/dgx_demo_tunnel.sh\n"
        "  3. 布局：PPT与浏览器各占半屏（或Alt+Tab切换）；界面说明：PPT=演示文稿窗口，WEB=浏览器页面\n"
        "  4. 不用录音、不用录屏——正式录屏已通过审核，本次只练节奏和切换\n"
        f"  5. 计时自动进行：每段讲完按一次回车即可，建议全程落在{total_seconds - 5}~{total_seconds}秒\n"
        "  6. 讲解词已写入PPT备注（演示者视图可见），下面再列一份供对照\n"
    )
    for index, segment in enumerate(runbook["segments"], start=1):
        planned = (segment["end_ms"] - segment["start_ms"]) // 1000
        print(
            f"{index}. {segment['label']} / {planned}秒 / {segment['surface']}\n"
            f"   讲解：{segment['speaker_script']}\n"
            f"   操作：{segment['operator_action']}\n"
            f"   证明：{'；'.join(segment['proof_points'])}\n"
            f"   兜底：{segment['fallback']}"
        )
    input("以上全部准备好后，按回车开始计时。")
    started_at = datetime.now(timezone.utc)
    started_mono = time.monotonic()
    boundaries = [0]
    for index, segment in enumerate(runbook["segments"], start=1):
        input(
            f"▶ 第{index}段「{segment['label']}」｜界面: {segment['surface']}｜操作: {segment['operator_action']}\n"
            "  讲完按回车继续。"
        )
        boundaries.append(round((time.monotonic() - started_mono) * 1000))
    print(f"计时结束：{boundaries[-1] / 1000:.3f}秒。现在核对人工事实。")
    segment_checks = []
    for index, segment in enumerate(runbook["segments"], start=1):
        accepted = _confirm(
            f"第{index}段「{segment['label']}」的讲解、操作、证明点和兜底准备是否全部完成？"
        )
        segment_checks.append({key: accepted for key in REHEARSAL_SEGMENT_KEYS})
    completion = {
        key: _confirm(text) for key, text in REHEARSAL_COMPLETION_PROMPTS.items()
    }
    report = complete_final_rehearsal(
        boundaries,
        segment_checks,
        completion,
        started_at=started_at,
    )
    return {
        "status": report["status"],
        "review_type": "FINAL_STAGE_REHEARSAL",
        "duration_ms": report["duration"]["actual_ms"],
        "headroom_ms": report["duration"]["headroom_ms"],
        "human_gate_status": report["human_gate_status"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "status",
            "prepare",
            "training-video",
            "final-recording",
            "final-rehearsal",
        ),
    )
    parser.add_argument("--player", help="ffplay可执行文件；默认从PATH查找")
    args = parser.parse_args()
    try:
        if args.action == "status":
            result = review_status()
        elif args.action == "prepare":
            result = {
                "status": "PENDING_INPUT",
                "automatic_human_confirmations": 0,
                "actions": prepare_pending_reviews(),
            }
        elif args.action in {"training-video", "final-recording"}:
            result = _interactive_media(args.action, args.player)
        else:
            result = _interactive_rehearsal()
    except (
        ContractValidationError,
        FinalRecordingReviewError,
        FinalRehearsalError,
        GuidedHumanReviewError,
        OSError,
        TrainingVideoReviewError,
    ) as exc:
        message = (
            str(exc)
            if isinstance(
                exc,
                (
                    FinalRecordingReviewError,
                    FinalRehearsalError,
                    GuidedHumanReviewError,
                    TrainingVideoReviewError,
                ),
            )
            else "引导式人工审核失败"
        )
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": message,
                    "automatic_human_confirmations": 0,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if args.action == "status":
        _print_status_guidance(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
