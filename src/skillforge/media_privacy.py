"""Reproducible local-only privacy processing for sensitive video sources."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .media import MediaProcessingError, probe_media, resolve_ffmpeg


@dataclass(frozen=True)
class Segment:
    start_seconds: float
    end_seconds: float


@dataclass(frozen=True)
class OpaqueMask:
    x: int
    y: int
    width: int
    height: int
    start_seconds: float | None = None
    end_seconds: float | None = None


@dataclass(frozen=True)
class PrivacyJob:
    job_id: str
    source: Path
    destination: Path
    segments: tuple[Segment, ...]
    masks: tuple[OpaqueMask, ...]
    normalize_audio: bool
    target_lufs: float
    max_true_peak_dbtp: float


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"媒体路径必须位于项目目录内: {path}") from exc
    return resolved


def _parse_segment(raw: dict[str, Any]) -> Segment:
    segment = Segment(
        start_seconds=float(raw["start_seconds"]),
        end_seconds=float(raw["end_seconds"]),
    )
    if segment.start_seconds < 0 or segment.end_seconds <= segment.start_seconds:
        raise ValueError(f"非法安全片段: {raw}")
    return segment


def _parse_mask(raw: dict[str, Any]) -> OpaqueMask:
    mask = OpaqueMask(
        x=int(raw["x"]),
        y=int(raw["y"]),
        width=int(raw["width"]),
        height=int(raw["height"]),
        start_seconds=(
            float(raw["start_seconds"])
            if raw.get("start_seconds") is not None
            else None
        ),
        end_seconds=(
            float(raw["end_seconds"]) if raw.get("end_seconds") is not None else None
        ),
    )
    if min(mask.x, mask.y) < 0 or min(mask.width, mask.height) <= 0:
        raise ValueError(f"非法遮挡区域: {raw}")
    if (mask.start_seconds is None) != (mask.end_seconds is None):
        raise ValueError("遮挡开始与结束时间必须同时提供或同时省略")
    if (
        mask.start_seconds is not None
        and mask.end_seconds is not None
        and (
            mask.start_seconds < 0
            or mask.end_seconds <= mask.start_seconds
        )
    ):
        raise ValueError(f"非法遮挡时间范围: {raw}")
    return mask


def load_jobs(config_path: Path, project_root: Path) -> tuple[PrivacyJob, ...]:
    project_root = project_root.expanduser().resolve()
    config_path = _inside(config_path, project_root)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if raw.get("version") != 1:
        raise ValueError("不支持的视频隐私处理配置版本")
    jobs: list[PrivacyJob] = []
    seen_ids: set[str] = set()
    for item in raw.get("jobs", []):
        job_id = str(item["job_id"])
        if not job_id or job_id in seen_ids:
            raise ValueError(f"重复或空的 job_id: {job_id!r}")
        seen_ids.add(job_id)
        source = _inside(project_root / item["source"], project_root)
        destination = _inside(project_root / item["destination"], project_root)
        if source == destination:
            raise ValueError(f"{job_id}: 不允许覆盖原始视频")
        if "_private_review" in destination.name:
            raise ValueError(f"{job_id}: 正式输出不能使用 private_review 文件名")
        segments = tuple(_parse_segment(value) for value in item.get("segments", []))
        previous_end = -1.0
        for segment in segments:
            if segment.start_seconds < previous_end:
                raise ValueError(f"{job_id}: 安全片段必须按时间排序且不能重叠")
            previous_end = segment.end_seconds
        jobs.append(
            PrivacyJob(
                job_id=job_id,
                source=source,
                destination=destination,
                segments=segments,
                masks=tuple(_parse_mask(value) for value in item.get("masks", [])),
                normalize_audio=bool(item.get("normalize_audio", True)),
                target_lufs=float(item.get("target_lufs", -16.0)),
                max_true_peak_dbtp=float(item.get("max_true_peak_dbtp", -1.5)),
            )
        )
    if not jobs:
        raise ValueError("视频隐私处理配置中没有任务")
    return tuple(jobs)


def _drawbox(mask: OpaqueMask) -> str:
    rule = (
        f"drawbox=x={mask.x}:y={mask.y}:w={mask.width}:h={mask.height}"
        ":color=black@1:t=fill"
    )
    if mask.start_seconds is not None and mask.end_seconds is not None:
        rule += (
            ":enable='between(t,"
            f"{mask.start_seconds:.3f},{mask.end_seconds:.3f})'"
        )
    return rule


def build_filter_graph(job: PrivacyJob, *, has_audio: bool) -> tuple[str, str, str | None]:
    video_filters = [_drawbox(mask) for mask in job.masks]
    video_filters.append("format=yuv420p")
    video_tail = ",".join(video_filters)
    audio_tail = (
        "loudnorm="
        f"I={job.target_lufs}:LRA=11:TP={job.max_true_peak_dbtp},"
        "alimiter=limit=0.75:attack=5:release=50:level=false,"
        "aresample=48000"
        if job.normalize_audio
        else "anull"
    )
    parts: list[str] = []
    if job.segments:
        count = len(job.segments)
        parts.append(
            f"[0:v:0]split={count}"
            + "".join(f"[vsrc{index}]" for index in range(count))
        )
        if has_audio:
            parts.append(
                f"[0:a:0]asplit={count}"
                + "".join(f"[asrc{index}]" for index in range(count))
            )
        for index, segment in enumerate(job.segments):
            parts.append(
                f"[vsrc{index}]trim=start={segment.start_seconds}:"
                f"end={segment.end_seconds},setpts=PTS-STARTPTS[v{index}]"
            )
            if has_audio:
                parts.append(
                    f"[asrc{index}]atrim=start={segment.start_seconds}:"
                    f"end={segment.end_seconds},asetpts=PTS-STARTPTS[a{index}]"
                )
        if has_audio:
            concat_inputs = "".join(
                f"[v{index}][a{index}]" for index in range(count)
            )
            parts.append(f"{concat_inputs}concat=n={count}:v=1:a=1[vcat][acat]")
            parts.append(f"[acat]{audio_tail}[aout]")
        else:
            concat_inputs = "".join(f"[v{index}]" for index in range(count))
            parts.append(f"{concat_inputs}concat=n={count}:v=1:a=0[vcat]")
        parts.append(f"[vcat]{video_tail}[vout]")
    else:
        parts.append(f"[0:v:0]{video_tail}[vout]")
        if has_audio:
            parts.append(f"[0:a:0]{audio_tail}[aout]")
    return ";".join(parts), "[vout]", "[aout]" if has_audio else None


def _run(command: list[str], *, timeout: int = 1200) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error")[-1600:]
        raise MediaProcessingError(f"视频隐私处理失败: {detail}")
    return completed


def _validate_job_against_media(job: PrivacyJob, probe: dict[str, Any]) -> None:
    duration_seconds = (probe.get("duration_ms") or 0) / 1000
    video_streams = probe.get("video_streams") or []
    if not video_streams:
        raise ValueError(f"{job.job_id}: 输入中没有视频流")
    width = int(video_streams[0]["width"])
    height = int(video_streams[0]["height"])
    for segment in job.segments:
        if segment.end_seconds > duration_seconds + 0.1:
            raise ValueError(f"{job.job_id}: 安全片段超出视频时长")
    for mask in job.masks:
        if mask.x + mask.width > width or mask.y + mask.height > height:
            raise ValueError(f"{job.job_id}: 遮挡区域超出画面尺寸")
        if mask.end_seconds is not None and mask.end_seconds > duration_seconds + 0.1:
            raise ValueError(f"{job.job_id}: 遮挡时间超出视频时长")


def measure_loudness(path: Path) -> dict[str, float] | None:
    if not probe_media(path).get("audio_streams"):
        return None
    completed = _run(
        [
            str(resolve_ffmpeg()),
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-16:LRA=11:TP=-1.5:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    matches = re.findall(r"\{\s*\"input_i\".*?\}", completed.stderr, re.DOTALL)
    if not matches:
        raise MediaProcessingError("无法解析响度检测结果")
    raw = json.loads(matches[-1])
    return {
        "integrated_lufs": float(raw["input_i"]),
        "loudness_range_lu": float(raw["input_lra"]),
        "true_peak_dbtp": float(raw["input_tp"]),
    }


def _mask_sample_time(mask: OpaqueMask, duration_seconds: float) -> float:
    if mask.start_seconds is None or mask.end_seconds is None:
        return max(0.0, min(duration_seconds / 2, duration_seconds - 0.1))
    return min(mask.end_seconds - 0.1, mask.start_seconds + 1.0)


def verify_opaque_masks(path: Path, masks: tuple[OpaqueMask, ...]) -> list[dict[str, Any]]:
    duration_seconds = (probe_media(path).get("duration_ms") or 0) / 1000
    checks: list[dict[str, Any]] = []
    for index, mask in enumerate(masks, start=1):
        inset = 4
        sample_time = _mask_sample_time(mask, duration_seconds)
        completed = _run(
            [
                str(resolve_ffmpeg()),
                "-hide_banner",
                "-nostats",
                "-ss",
                f"{sample_time:.3f}",
                "-i",
                str(path),
                "-vf",
                (
                    f"crop={max(1, mask.width - inset * 2)}:"
                    f"{max(1, mask.height - inset * 2)}:"
                    f"{mask.x + inset}:{mask.y + inset},"
                    "signalstats,metadata=print"
                ),
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ],
            timeout=120,
        )
        match = re.search(r"lavfi\.signalstats\.YAVG=([0-9.]+)", completed.stderr)
        if not match:
            raise MediaProcessingError("无法解析遮挡亮度检测结果")
        y_average = float(match.group(1))
        checks.append(
            {
                "mask_index": index,
                "sample_time_seconds": sample_time,
                "y_average": y_average,
                "passed": y_average <= 24.0,
            }
        )
    return checks


def process_job(job: PrivacyJob) -> dict[str, Any]:
    if not job.source.is_file():
        raise FileNotFoundError(job.source)
    source_probe = probe_media(job.source)
    _validate_job_against_media(job, source_probe)
    has_audio = bool(source_probe.get("audio_streams"))
    graph, video_map, audio_map = build_filter_graph(job, has_audio=has_audio)
    job.destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(resolve_ffmpeg()),
        "-hide_banner",
        "-y",
        "-i",
        str(job.source),
        "-filter_complex",
        graph,
        "-map",
        video_map,
    ]
    if audio_map:
        command.extend(["-map", audio_map])
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if audio_map:
        command.extend(["-c:a", "aac", "-b:a", "160k", "-ar", "48000"])
    command.extend(["-movflags", "+faststart", str(job.destination)])
    _run(command)
    output_probe = probe_media(job.destination)
    expected_duration_ms = (
        round(
            sum(
                segment.end_seconds - segment.start_seconds
                for segment in job.segments
            )
            * 1000
        )
        if job.segments
        else source_probe.get("duration_ms")
    )
    actual_duration_ms = output_probe.get("duration_ms")
    duration_delta_ms = (
        abs(actual_duration_ms - expected_duration_ms)
        if actual_duration_ms is not None and expected_duration_ms is not None
        else None
    )
    mask_checks = verify_opaque_masks(job.destination, job.masks)
    duration_passed = duration_delta_ms is None or duration_delta_ms <= 250
    return {
        "job_id": job.job_id,
        "source": job.source.name,
        "destination": job.destination.name,
        "source_sha256": _sha256(job.source),
        "destination_sha256": _sha256(job.destination),
        "source_probe": source_probe,
        "output_probe": output_probe,
        "safe_segments": [
            {
                "start_seconds": segment.start_seconds,
                "end_seconds": segment.end_seconds,
            }
            for segment in job.segments
        ],
        "mask_count": len(job.masks),
        "mask_checks": mask_checks,
        "loudness": measure_loudness(job.destination),
        "expected_duration_ms": expected_duration_ms,
        "duration_delta_ms": duration_delta_ms,
        "passed": duration_passed and all(item["passed"] for item in mask_checks),
    }


def process_config(
    config_path: Path,
    project_root: Path,
    *,
    job_ids: set[str] | None = None,
) -> dict[str, Any]:
    all_jobs = load_jobs(config_path, project_root)
    available_ids = {job.job_id for job in all_jobs}
    unknown_ids = (job_ids or set()) - available_ids
    if unknown_ids:
        raise ValueError(f"未知视频处理任务: {sorted(unknown_ids)}")
    jobs = tuple(
        job for job in all_jobs if job_ids is None or job.job_id in job_ids
    )
    results = [process_job(job) for job in jobs]
    for job, result in zip(jobs, results, strict=True):
        loudness = result["loudness"]
        loudness_passed = (
            loudness is None
            or loudness["true_peak_dbtp"] <= job.max_true_peak_dbtp + 0.1
        )
        result["loudness_passed"] = loudness_passed
        result["passed"] = result["passed"] and loudness_passed
    return {
        "status": "PASSED" if all(item["passed"] for item in results) else "FAILED",
        "local_only": True,
        "config": config_path.name,
        "jobs": results,
    }
