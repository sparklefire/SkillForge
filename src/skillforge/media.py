"""Native FFmpeg media probing, normalization and evidence frame extraction."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


class MediaProcessingError(RuntimeError):
    """Raised when native FFmpeg processing fails."""


def _resolve_binary(env_name: str, command: str, user_fallback: Path | None = None) -> Path:
    configured = os.getenv(env_name)
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
        raise MediaProcessingError(f"{env_name} 指向不可执行文件: {path}")
    discovered = shutil.which(command)
    if discovered:
        return Path(discovered)
    if user_fallback and user_fallback.is_file() and os.access(user_fallback, os.X_OK):
        return user_fallback
    raise MediaProcessingError(f"找不到 {command}，请配置 {env_name}")


def resolve_ffmpeg() -> Path:
    return _resolve_binary(
        "SKILLFORGE_FFMPEG_BIN",
        "ffmpeg",
        Path.home() / "skillforge" / "bin" / "ffmpeg",
    )


def resolve_ffprobe(required: bool = False) -> Path | None:
    try:
        return _resolve_binary(
            "SKILLFORGE_FFPROBE_BIN",
            "ffprobe",
            Path.home() / "skillforge" / "bin" / "ffprobe",
        )
    except MediaProcessingError:
        if required:
            raise
        return None


def _run(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error")[-1600:]
        raise MediaProcessingError(f"FFmpeg 处理失败: {detail}")
    return completed


def _ratio(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator) if float(denominator) else None
    return float(value)


def _probe_with_ffprobe(path: Path, ffprobe: Path) -> dict[str, Any]:
    completed = _run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        timeout=60,
    )
    raw = json.loads(completed.stdout)
    streams = raw.get("streams") or []
    duration = raw.get("format", {}).get("duration")
    result: dict[str, Any] = {
        "probe_backend": "ffprobe",
        "duration_ms": round(float(duration) * 1000) if duration else None,
        "video_streams": [],
        "audio_streams": [],
    }
    for stream in streams:
        if stream.get("codec_type") == "video":
            result["video_streams"].append(
                {
                    "codec": stream.get("codec_name"),
                    "width": stream.get("width"),
                    "height": stream.get("height"),
                    "fps": _ratio(stream.get("avg_frame_rate")),
                }
            )
        elif stream.get("codec_type") == "audio":
            result["audio_streams"].append(
                {
                    "codec": stream.get("codec_name"),
                    "sample_rate": int(stream["sample_rate"])
                    if stream.get("sample_rate")
                    else None,
                    "channels": stream.get("channels"),
                }
            )
    return result


def _probe_with_ffmpeg(path: Path, ffmpeg: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-i",
            str(path),
            "-t",
            "0.05",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    text = completed.stderr or completed.stdout
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration_ms = None
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration_ms = round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)
    video_streams = []
    audio_streams = []
    for line in text.splitlines():
        if " Video: " in line:
            codec = re.search(r"Video:\s*([^,\s]+)", line)
            size = re.search(r"(?:^|,\s)(\d{2,5})x(\d{2,5})(?:[\s,])", line)
            fps = re.search(r"(\d+(?:\.\d+)?)\s+fps", line)
            video_streams.append(
                {
                    "codec": codec.group(1) if codec else None,
                    "width": int(size.group(1)) if size else None,
                    "height": int(size.group(2)) if size else None,
                    "fps": float(fps.group(1)) if fps else None,
                }
            )
        elif " Audio: " in line:
            codec = re.search(r"Audio:\s*([^,\s]+)", line)
            rate = re.search(r"(\d+)\s+Hz", line)
            channels = 1 if "mono" in line else 2 if "stereo" in line else None
            audio_streams.append(
                {
                    "codec": codec.group(1) if codec else None,
                    "sample_rate": int(rate.group(1)) if rate else None,
                    "channels": channels,
                }
            )
    if not duration_match and not video_streams and not audio_streams:
        raise MediaProcessingError(f"无法读取媒体信息: {text[-1200:]}")
    return {
        "probe_backend": "ffmpeg",
        "duration_ms": duration_ms,
        "video_streams": video_streams,
        "audio_streams": audio_streams,
    }


def probe_media(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    ffprobe = resolve_ffprobe()
    if ffprobe:
        return _probe_with_ffprobe(path, ffprobe)
    return _probe_with_ffmpeg(path, resolve_ffmpeg())


def normalize_video(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(resolve_ffmpeg()),
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    return destination


def normalize_audio(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(resolve_ffmpeg()),
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )
    return destination


def extract_keyframes(
    source: Path, destination_dir: Path, *, interval_seconds: float = 5.0
) -> list[dict[str, Any]]:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds 必须大于 0")
    destination_dir.mkdir(parents=True, exist_ok=True)
    for stale in destination_dir.glob("frame_*.jpg"):
        stale.unlink()
    _run(
        [
            str(resolve_ffmpeg()),
            "-y",
            "-i",
            str(source),
            "-vf",
            f"fps=1/{interval_seconds}",
            "-q:v",
            "2",
            str(destination_dir / "frame_%06d.jpg"),
        ]
    )
    return [
        {
            "path": path,
            "start_ms": round(index * interval_seconds * 1000),
            "end_ms": round((index + 1) * interval_seconds * 1000),
        }
        for index, path in enumerate(sorted(destination_dir.glob("frame_*.jpg")))
    ]
