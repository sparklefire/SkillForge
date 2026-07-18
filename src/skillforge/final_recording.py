"""Run private, local-only machine QA for the final three-minute demo recording."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import ContractValidationError, validate_document
from .demo import ROOT
from .media import MediaProcessingError, probe_media, resolve_ffmpeg
from .media_privacy import measure_loudness


DEFAULT_POLICY = ROOT / "config/final_recording_policy.json"
DEFAULT_PRIVATE_ROOT = ROOT / "outputs/submission"
DEFAULT_RECORDING = DEFAULT_PRIVATE_ROOT / "skillforge_final_recording.mp4"
DEFAULT_REPORT = DEFAULT_PRIVATE_ROOT / "final_recording_qa.json"


class FinalRecordingError(ValueError):
    """Raised when final-recording QA cannot be executed or trusted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalRecordingError("录屏QA配置或报告无法读取") from exc
    if not isinstance(value, dict):
        raise FinalRecordingError("录屏QA配置或报告必须是JSON对象")
    return value


def load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    try:
        return validate_document(
            _read_json(path.expanduser().resolve()),
            "final_recording_policy.schema.json",
        )
    except (ContractValidationError, FinalRecordingError) as exc:
        raise FinalRecordingError("最终录屏内部QA策略无效") from exc


def _inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FinalRecordingError("最终录屏必须保存在项目私有提交目录") from exc
    return resolved


def _mode(path: Path) -> str:
    return f"{stat.S_IMODE(path.stat().st_mode):04o}"


def _duration_measurements(log: str, label: str, duration_ms: int) -> tuple[int, float, int]:
    values = [
        max(0, round(float(value) * 1000))
        for value in re.findall(
            rf"{re.escape(label)}:\s*([0-9]+(?:\.[0-9]+)?)", log
        )
    ]
    total = min(duration_ms, sum(values))
    ratio = round(total / duration_ms, 6) if duration_ms > 0 else 1.0
    return total, ratio, max(values, default=0)


def analyze_interruptions(path: Path, policy: dict[str, Any]) -> dict[str, int | float]:
    audio = policy["audio"]
    screen = policy["screen"]
    command = [
        str(resolve_ffmpeg()),
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-vf",
        (
            "scale=320:-2,blackdetect="
            f"d={screen['black_minimum_ms'] / 1000:.3f}:"
            f"pix_th={screen['black_pixel_threshold']}:"
            f"pic_th={screen['black_picture_ratio']}"
        ),
        "-af",
        (
            "silencedetect="
            f"noise={audio['silence_threshold_db']}dB:"
            f"d={audio['silence_minimum_ms'] / 1000:.3f}"
        ),
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise FinalRecordingError("最终录屏静音与黑屏检测超时") from exc
    if completed.returncode != 0:
        raise FinalRecordingError("最终录屏静音与黑屏检测失败")
    duration_ms = int(probe_media(path).get("duration_ms") or 0)
    log = completed.stderr or completed.stdout
    silence_total, silence_ratio, max_silence = _duration_measurements(
        log, "silence_duration", duration_ms
    )
    black_total, black_ratio, max_black = _duration_measurements(
        log, "black_duration", duration_ms
    )
    return {
        "silence_total_ms": silence_total,
        "silence_ratio": silence_ratio,
        "maximum_contiguous_silence_ms": max_silence,
        "black_total_ms": black_total,
        "black_ratio": black_ratio,
        "maximum_contiguous_black_ms": max_black,
    }


def evaluate_final_recording(
    source: Path,
    *,
    policy_path: Path = DEFAULT_POLICY,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
    probe_fn: Callable[[Path], dict[str, Any]] = probe_media,
    loudness_fn: Callable[[Path], dict[str, float] | None] = measure_loudness,
    interruption_fn: Callable[[Path, dict[str, Any]], dict[str, int | float]] = (
        analyze_interruptions
    ),
) -> dict[str, Any]:
    policy_path = policy_path.expanduser().resolve()
    policy = load_policy(policy_path)
    source = _inside(source, private_root)
    if not source.is_file() or source.stat().st_size < 1:
        raise FinalRecordingError("最终录屏不存在或为空")

    probe = probe_fn(source)
    duration_ms = int(probe.get("duration_ms") or 0) or None
    video_streams = probe.get("video_streams") or []
    audio_streams = probe.get("audio_streams") or []
    video = video_streams[0] if video_streams else {}
    audio = audio_streams[0] if audio_streams else {}
    loudness = loudness_fn(source) if audio_streams else None
    interruptions = (
        interruption_fn(source, policy)
        if duration_ms and video_streams and audio_streams
        else {
            "silence_total_ms": None,
            "silence_ratio": None,
            "maximum_contiguous_silence_ms": None,
            "black_total_ms": None,
            "black_ratio": None,
            "maximum_contiguous_black_ms": None,
        }
    )

    width = int(video["width"]) if video.get("width") else None
    height = int(video["height"]) if video.get("height") else None
    fps = float(video["fps"]) if video.get("fps") else None
    if fps is not None and not math.isfinite(fps):
        fps = None
    video_codec = str(video["codec"]) if video.get("codec") else None
    audio_codec = str(audio["codec"]) if audio.get("codec") else None
    sample_rate = int(audio["sample_rate"]) if audio.get("sample_rate") else None
    channels = int(audio["channels"]) if audio.get("channels") else None
    integrated_lufs = (
        float(loudness["integrated_lufs"])
        if loudness and loudness.get("integrated_lufs") is not None
        else None
    )
    true_peak_dbtp = (
        float(loudness["true_peak_dbtp"])
        if loudness and loudness.get("true_peak_dbtp") is not None
        else None
    )
    if integrated_lufs is not None and not math.isfinite(integrated_lufs):
        integrated_lufs = None
    if true_peak_dbtp is not None and not math.isfinite(true_peak_dbtp):
        true_peak_dbtp = None
    file_mode = _mode(source)
    storage = policy["private_storage"]
    duration_policy = policy["duration"]
    video_policy = policy["video"]
    audio_policy = policy["audio"]
    screen_policy = policy["screen"]

    checks = {
        "private_storage_passed": (
            file_mode == storage["required_file_mode"]
            and _mode(source.parent) == storage["required_directory_mode"]
        ),
        "container_passed": (
            source.name == storage["required_filename"]
            and source.suffix.lower() in storage["allowed_extensions"]
        ),
        "duration_passed": bool(
            duration_ms
            and duration_policy["minimum_ms"]
            <= duration_ms
            <= duration_policy["maximum_ms"]
        ),
        "video_stream_passed": bool(video_streams),
        "dimensions_passed": (
            width == video_policy["width"] and height == video_policy["height"]
        ),
        "fps_passed": bool(
            fps
            and video_policy["minimum_fps"] <= fps <= video_policy["maximum_fps"]
        ),
        "video_codec_passed": video_codec in video_policy["allowed_codecs"],
        "audio_stream_passed": bool(audio_streams),
        "audio_codec_passed": audio_codec in audio_policy["allowed_codecs"],
        "audio_sample_rate_passed": bool(
            sample_rate and sample_rate >= audio_policy["minimum_sample_rate"]
        ),
        "loudness_passed": bool(
            integrated_lufs is not None
            and audio_policy["minimum_integrated_lufs"]
            <= integrated_lufs
            <= audio_policy["maximum_integrated_lufs"]
        ),
        "true_peak_passed": bool(
            true_peak_dbtp is not None
            and true_peak_dbtp <= audio_policy["maximum_true_peak_dbtp"]
        ),
        "silence_ratio_passed": bool(
            interruptions["silence_ratio"] is not None
            and interruptions["silence_ratio"]
            <= audio_policy["maximum_silence_ratio"]
        ),
        "maximum_silence_passed": bool(
            interruptions["maximum_contiguous_silence_ms"] is not None
            and interruptions["maximum_contiguous_silence_ms"]
            <= audio_policy["maximum_contiguous_silence_ms"]
        ),
        "black_ratio_passed": bool(
            interruptions["black_ratio"] is not None
            and interruptions["black_ratio"] <= screen_policy["maximum_black_ratio"]
        ),
        "maximum_black_passed": bool(
            interruptions["maximum_contiguous_black_ms"] is not None
            and interruptions["maximum_contiguous_black_ms"]
            <= screen_policy["maximum_contiguous_black_ms"]
        ),
    }
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "FINAL_RECORDING_QA",
        "generated_at": _now(),
        "status": (
            "READY_FOR_HUMAN_REVIEW"
            if all(checks.values())
            else "MACHINE_QA_FAILED"
        ),
        "policy_sha256": _sha256(policy_path),
        "media": {
            "filename": storage["required_filename"],
            "sha256": _sha256(source),
            "bytes": source.stat().st_size,
            "file_mode": file_mode,
            "duration_ms": duration_ms,
            "width": width,
            "height": height,
            "fps": fps,
            "video_codec": video_codec,
            "audio_codec": audio_codec,
            "audio_sample_rate": sample_rate,
            "audio_channels": channels,
        },
        "measurements": {
            "integrated_lufs": integrated_lufs,
            "true_peak_dbtp": true_peak_dbtp,
            **interruptions,
        },
        "checks": checks,
        "human_review": {
            "required": True,
            "status": "PENDING",
            "checks": policy["human_checks"],
        },
        "official_rules_boundary": {
            "policy_basis": policy["policy_basis"],
            "official_video_requirements_verified": policy[
                "official_video_requirements_verified"
            ],
        },
        "data_policy": {
            "private_local_state": True,
            "contains_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "external_model_calls": 0,
        },
    }
    return validate_document(report, "final_recording_qa.schema.json")


def write_private_report(report: dict[str, Any], destination: Path) -> Path:
    validate_document(report, "final_recording_qa.schema.json")
    destination = destination.expanduser().resolve()
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(destination.parent, 0o700)
    elif stat.S_IMODE(destination.parent.stat().st_mode) != 0o700:
        raise FinalRecordingError("录屏QA报告目录权限必须为0700")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def final_recording_qa_issue(report_path: Path, evidence: dict[str, Any]) -> str | None:
    if evidence.get("kind") != "LOCAL_FILE":
        return "FINAL_RECORDING_REQUIRES_LOCAL_FILE"
    if not report_path.is_file():
        return "FINAL_RECORDING_QA_MISSING"
    if (
        stat.S_IMODE(report_path.stat().st_mode) != 0o600
        or stat.S_IMODE(report_path.parent.stat().st_mode) != 0o700
    ):
        return "FINAL_RECORDING_QA_PERMISSIONS_UNSAFE"
    try:
        report = validate_document(
            _read_json(report_path),
            "final_recording_qa.schema.json",
        )
    except (ContractValidationError, FinalRecordingError):
        return "FINAL_RECORDING_QA_INVALID"
    if report["status"] != "READY_FOR_HUMAN_REVIEW":
        return "FINAL_RECORDING_MACHINE_QA_FAILED"
    try:
        current_policy_sha256 = _sha256(DEFAULT_POLICY)
    except OSError:
        return "FINAL_RECORDING_QA_POLICY_MISSING"
    if report["policy_sha256"] != current_policy_sha256:
        return "FINAL_RECORDING_QA_POLICY_CHANGED"
    if (
        report["media"]["sha256"] != evidence.get("sha256")
        or report["media"]["bytes"] != evidence.get("size_bytes")
    ):
        return "FINAL_RECORDING_QA_MEDIA_CHANGED"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_RECORDING)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    try:
        output = _inside(args.output, DEFAULT_PRIVATE_ROOT)
        report = evaluate_final_recording(args.input, policy_path=args.policy)
        write_private_report(report, output)
    except FinalRecordingError as exc:
        print(
            json.dumps(
                {"status": "ERROR", "message": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    except (ContractValidationError, MediaProcessingError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": "最终录屏机器QA执行失败",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "READY_FOR_HUMAN_REVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
