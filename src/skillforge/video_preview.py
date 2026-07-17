"""Generate private low-bitrate previews with source-to-preview timeline bindings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .media import MediaProcessingError, probe_media, resolve_ffmpeg
from .revision import digest


REQUIRED_OUTPUT_FORMATS = {
    "STRUCTURED_SOP": "JSON",
    "MOBILE_CHECKLIST": "JSON",
    "TRAINING_QUIZ": "JSON",
    "A4_POSTER": "PDF",
    "TRAINING_VIDEO": "MP4",
    "REVISION_AUDIT": "JSON",
}


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON文件必须是对象: {path.name}")
    return payload


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _inside(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve()
    root = root.expanduser().resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"{label}必须位于项目目录内")
    return resolved


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_output_profile(profile: dict[str, Any]) -> dict[str, Any]:
    validate_document(profile, "case_output_profile.schema.json")
    duration = profile["duration"]
    if duration["source_video_min_seconds"] >= duration["source_video_max_seconds"]:
        raise ValueError("主案例源视频最短时长必须小于最长时长")
    if not (
        duration["training_video_min_seconds"]
        <= duration["training_video_target_seconds"]
        <= duration["training_video_max_seconds"]
    ):
        raise ValueError("培训视频目标时长必须位于最短和最长时长之间")
    outputs = profile["output_types"]
    by_type = {item["output_type"]: item for item in outputs}
    if len(by_type) != len(outputs):
        raise ValueError("输出类型配置包含重复类型")
    if set(by_type) != set(REQUIRED_OUTPUT_FORMATS):
        raise ValueError("P0输出类型必须完整包含SOP、清单、测验、海报、视频和修订审计")
    for output_type, expected_format in REQUIRED_OUTPUT_FORMATS.items():
        item = by_type[output_type]
        if item["format"] != expected_format or item["required"] is not True:
            raise ValueError(f"{output_type} 的格式或必需状态无效")
    preview = profile["preview_profile"]
    if preview["target_video_kbps"] + preview["audio_kbps"] >= preview["max_total_kbps"]:
        raise ValueError("预览总码率上限必须高于视频和音频目标码率之和")
    return profile


def _run(command: list[str], *, timeout: int = 1800) -> None:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error")[-1600:]
        raise MediaProcessingError(f"低码率预览生成失败: {detail}")


def _faststart(path: Path) -> bool:
    with path.open("rb") as handle:
        header = handle.read(min(path.stat().st_size, 4 * 1024 * 1024))
    moov = header.find(b"moov")
    mdat = header.find(b"mdat")
    return moov >= 0 and mdat >= 0 and moov < mdat


def _source_path(
    source: dict[str, Any],
    *,
    project_root: Path,
    source_dir: Path | None,
) -> Path:
    configured = Path(source["path"])
    if source_dir is None:
        return _inside(project_root / configured, project_root, label="视频来源")
    root = source_dir.expanduser().resolve()
    candidate = (root / configured.name).resolve()
    if candidate.parent != root:
        raise ValueError("DGX安全视频来源必须是指定目录的直接子文件")
    return candidate


def _transcode(
    source: Path,
    destination: Path,
    profile: dict[str, Any],
    *,
    has_audio: bool,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        f"scale={profile['max_width']}:{profile['max_height']}:"
        "force_original_aspect_ratio=decrease,"
        "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
        f"fps={profile['fps']},format=yuv420p"
    )
    target = profile["target_video_kbps"]
    command = [
        str(resolve_ffmpeg()),
        "-hide_banner",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
    ]
    if has_audio:
        command.extend(["-map", "0:a:0"])
    command.extend(
        [
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-profile:v",
            "main",
            "-preset",
            "veryfast",
            "-b:v",
            f"{target}k",
            "-maxrate",
            f"{round(target * 1.2)}k",
            "-bufsize",
            f"{target * 2}k",
            "-g",
            str(profile["fps"] * 2),
            "-keyint_min",
            str(profile["fps"] * 2),
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if has_audio:
        command.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                f"{profile['audio_kbps']}k",
                "-ac",
                "1",
            ]
        )
    else:
        command.append("-an")
    command.extend(["-movflags", "+faststart", str(destination)])
    _run(command)


def _build_item(
    source_config: dict[str, Any],
    source: Path,
    preview_path: Path,
    profile: dict[str, Any],
) -> dict[str, Any]:
    source_probe = probe_media(source)
    output_probe = probe_media(preview_path)
    if not source_probe["video_streams"] or not output_probe["video_streams"]:
        raise ValueError(f"{source_config['source_id']} 缺少视频流")
    source_duration = source_probe.get("duration_ms")
    preview_duration = output_probe.get("duration_ms")
    if not source_duration or not preview_duration:
        raise ValueError(f"{source_config['source_id']} 无法读取时长")
    duration_delta = abs(int(source_duration) - int(preview_duration))
    video = output_probe["video_streams"][0]
    audio = output_probe["audio_streams"][0] if output_probe["audio_streams"] else None
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    fps = float(video.get("fps") or 0)
    video_codec = str(video.get("codec") or "")
    audio_codec = str(audio.get("codec") or "") if audio else None
    preview_bytes = preview_path.stat().st_size
    average_bitrate = round(preview_bytes * 8 / int(preview_duration), 3)
    checks = {
        "duration_preserved": duration_delta <= 250,
        "dimensions_within_profile": (
            0 < width <= profile["max_width"]
            and 0 < height <= profile["max_height"]
            and width % 2 == 0
            and height % 2 == 0
        ),
        "fps_within_profile": 0 < fps <= profile["fps"] + 0.1,
        "codec_matches_profile": video_codec == profile["video_codec"]
        and (audio_codec in {None, profile["audio_codec"]}),
        "bitrate_within_profile": average_bitrate <= profile["max_total_kbps"],
        "faststart": _faststart(preview_path),
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise ValueError(
            f"{source_config['source_id']} 低码率预览检查失败: {', '.join(failed)}"
        )
    return {
        "source_id": source_config["source_id"],
        "source_role": source_config["role"],
        "source_privacy_status": source_config["privacy_status"],
        "source_sha256": _sha256(source),
        "source_duration_ms": int(source_duration),
        "preview_file": f"{source_config['source_id']}.mp4",
        "preview_sha256": _sha256(preview_path),
        "preview_bytes": preview_bytes,
        "preview_duration_ms": int(preview_duration),
        "duration_delta_ms": duration_delta,
        "width": width,
        "height": height,
        "fps": fps,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "average_total_bitrate_kbps": average_bitrate,
        "timeline_mapping": [
            {
                "source_start_ms": 0,
                "source_end_ms": int(source_duration),
                "preview_start_ms": 0,
                "preview_end_ms": int(preview_duration),
            }
        ],
        "checks": checks,
    }


def generate_previews(
    *,
    project_root: Path,
    ingest_manifest_path: Path,
    output_profile_path: Path,
    output_dir: Path,
    report_path: Path,
    source_dir: Path | None = None,
) -> dict[str, Any]:
    project_root = project_root.expanduser().resolve()
    ingest_manifest_path = _inside(
        ingest_manifest_path if ingest_manifest_path.is_absolute() else project_root / ingest_manifest_path,
        project_root,
        label="摄取清单",
    )
    output_profile_path = _inside(
        output_profile_path if output_profile_path.is_absolute() else project_root / output_profile_path,
        project_root,
        label="输出配置",
    )
    output_dir = _inside(
        output_dir if output_dir.is_absolute() else project_root / output_dir,
        project_root,
        label="预览输出目录",
    )
    report_path = _inside(
        report_path if report_path.is_absolute() else project_root / report_path,
        project_root,
        label="预览报告",
    )
    ingest = _read_json(ingest_manifest_path)
    profile_document = validate_output_profile(_read_json(output_profile_path))
    if ingest.get("case_id") != profile_document["case_id"]:
        raise ValueError("摄取清单与输出配置案例编号不一致")
    preview_profile = {
        key: value
        for key, value in profile_document["preview_profile"].items()
        if key != "accepted_privacy_status"
    }
    accepted = profile_document["preview_profile"]["accepted_privacy_status"]
    sources = sorted(
        (
            item
            for item in ingest.get("sources", [])
            if item.get("type") == "video" and item.get("approved_for_local_ingest") is True
        ),
        key=lambda item: item["source_id"],
    )
    if not sources:
        raise ValueError("摄取清单没有获准本地处理的视频")
    if any(item.get("privacy_status") != accepted for item in sources):
        raise ValueError("低码率预览只能处理已通过本地隐私QA的视频")

    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    staging = output_dir / f".run-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    items: list[dict[str, Any]] = []
    try:
        for source_config in sources:
            source = _source_path(
                source_config,
                project_root=project_root,
                source_dir=source_dir,
            )
            if not source.is_file():
                raise FileNotFoundError(source)
            source_probe = probe_media(source)
            preview_path = staging / f"{source_config['source_id']}.mp4"
            _transcode(
                source,
                preview_path,
                preview_profile,
                has_audio=bool(source_probe.get("audio_streams")),
            )
            os.chmod(preview_path, 0o600)
            items.append(
                _build_item(
                    source_config,
                    source,
                    preview_path,
                    preview_profile,
                )
            )
        manifest = {
            "artifact_type": "VIDEO_PREVIEW_MANIFEST",
            "version": 1,
            "case_id": profile_document["case_id"],
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "COMPLETED",
            "output_profile_sha256": digest(profile_document),
            "profile": preview_profile,
            "summary": {
                "source_count": len(items),
                "source_duration_ms": sum(item["source_duration_ms"] for item in items),
                "preview_duration_ms": sum(item["preview_duration_ms"] for item in items),
                "preview_bytes": sum(item["preview_bytes"] for item in items),
                "max_average_total_bitrate_kbps": max(
                    item["average_total_bitrate_kbps"] for item in items
                ),
                "all_checks_passed": True,
            },
            "items": items,
            "data_policy": {
                "storage_scope": "LOCAL_PRIVATE_DERIVATIVE",
                "external_model_calls": 0,
                "contains_raw_media": False,
                "contains_preview_media": False,
                "contains_credentials": False,
                "contains_absolute_paths": False,
            },
        }
        validate_document(manifest, "video_preview_manifest.schema.json")
        expected_files = {item["preview_file"] for item in items}
        for item in items:
            source = staging / item["preview_file"]
            destination = output_dir / item["preview_file"]
            os.replace(source, destination)
            os.chmod(destination, 0o600)
        for stale in output_dir.glob("*.mp4"):
            if stale.name not in expected_files:
                stale.unlink()
        _write_json_atomic(report_path, manifest)
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def verified_preview_path(
    manifest: dict[str, Any],
    *,
    source_id: str,
    output_dir: Path,
    output_profile: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    validate_document(manifest, "video_preview_manifest.schema.json")
    validate_output_profile(output_profile)
    if manifest["output_profile_sha256"] != digest(output_profile):
        raise ValueError("低码率预览未绑定当前输出配置")
    item = next(
        (candidate for candidate in manifest["items"] if candidate["source_id"] == source_id),
        None,
    )
    if item is None:
        raise FileNotFoundError(source_id)
    root = output_dir.expanduser().resolve()
    path = (root / item["preview_file"]).resolve()
    if path.parent != root or not path.is_file():
        raise FileNotFoundError(source_id)
    if path.stat().st_size != item["preview_bytes"] or _sha256(path) != item["preview_sha256"]:
        raise ValueError("低码率预览大小或SHA-256与报告不一致")
    return path, item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--ingest-manifest",
        type=Path,
        default=Path("cases/n31/ingest_manifest.json"),
    )
    parser.add_argument(
        "--output-profile",
        type=Path,
        default=Path("cases/n31/output_profile.json"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("cases/n31/output/video_previews_v1"),
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("cases/n31/evaluations/video_preview_manifest_v1.json"),
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="可选：从指定安全目录按文件名读取视频，用于DGX隔离数据目录",
    )
    args = parser.parse_args()
    manifest = generate_previews(
        project_root=args.project_root,
        ingest_manifest_path=args.ingest_manifest,
        output_profile_path=args.output_profile,
        output_dir=args.output_dir,
        report_path=args.report,
        source_dir=args.source_dir,
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "source_count": manifest["summary"]["source_count"],
                "preview_bytes": manifest["summary"]["preview_bytes"],
                "max_average_total_bitrate_kbps": manifest["summary"][
                    "max_average_total_bitrate_kbps"
                ],
                "external_model_calls": manifest["data_policy"]["external_model_calls"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
