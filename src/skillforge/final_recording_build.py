"""Build a private three-minute SkillForge demo candidate from safe visual assets."""

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
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .contracts import ContractValidationError, validate_document
from .demo import ROOT
from .final_recording import evaluate_final_recording, write_private_report
from .media import MediaProcessingError, probe_media, resolve_ffmpeg
from .training_video import StepAudioTTSClient


DEFAULT_STORYBOARD = ROOT / "config/final_recording_storyboard.json"
DEFAULT_PRIVATE_ROOT = ROOT / "outputs/submission"
DEFAULT_ASSETS = DEFAULT_PRIVATE_ROOT / "final_recording_assets"
DEFAULT_WORK = DEFAULT_PRIVATE_ROOT / "final_recording_work"
DEFAULT_OUTPUT = DEFAULT_PRIVATE_ROOT / "skillforge_final_recording.mp4"
DEFAULT_BUILD_REPORT = DEFAULT_PRIVATE_ROOT / "final_recording_build.json"
DEFAULT_QA_REPORT = DEFAULT_PRIVATE_ROOT / "final_recording_qa.json"
EXPECTED_SCREENSHOTS = {
    "01_overview.png",
    "02_agents.png",
    "03_dgx.png",
    "04_grounding.png",
    "05_revision.png",
    "06_outputs.png",
    "07_evidence.png",
    "08_live_run.png",
}
FORBIDDEN_PUBLIC_TEXT = (
    "/Users/",
    "/home/Developer/",
    "file://",
    "Authorization",
    "Bearer ",
    "outputs/submission",
    "全部模型均在DGX本地运行",
    "原始多模态处理仅需44.8毫秒",
    "无需人工审核",
)
FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)


class FinalRecordingBuildError(ValueError):
    """Raised when the private candidate cannot be built safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FinalRecordingBuildError("最终录屏故事板无法读取") from exc
    if not isinstance(value, dict):
        raise FinalRecordingBuildError("最终录屏故事板必须是JSON对象")
    return value


def load_storyboard(path: Path = DEFAULT_STORYBOARD) -> dict[str, Any]:
    try:
        return validate_document(
            _read_json(path.expanduser().resolve()),
            "final_recording_storyboard.schema.json",
        )
    except ContractValidationError as exc:
        raise FinalRecordingBuildError("最终录屏故事板不符合严格Schema") from exc


def _relative_inside(root: Path, relative: str, label: str) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise FinalRecordingBuildError(f"{label}必须使用项目内相对路径")
    resolved = (root / relative_path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise FinalRecordingBuildError(f"{label}越出项目目录") from exc
    return resolved


def _private_inside(path: Path, private_root: Path) -> Path:
    resolved = path.expanduser().resolve()
    private_root = private_root.expanduser().resolve()
    try:
        resolved.relative_to(private_root)
    except ValueError as exc:
        raise FinalRecordingBuildError("最终录屏候选必须保存在私有提交目录") from exc
    return resolved


def validate_storyboard(
    storyboard: dict[str, Any],
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    try:
        validate_document(storyboard, "final_recording_storyboard.schema.json")
    except ContractValidationError as exc:
        raise FinalRecordingBuildError("最终录屏故事板不符合严格Schema") from exc
    scenes = storyboard["scenes"]
    if [scene["order"] for scene in scenes] != list(range(1, 10)):
        raise FinalRecordingBuildError("最终录屏场景顺序必须连续为1到9")
    if [scene["scene_id"] for scene in scenes] != [f"R{i:02d}" for i in range(1, 10)]:
        raise FinalRecordingBuildError("最终录屏场景编号必须连续为R01到R09")
    if sum(scene["duration_ms"] for scene in scenes) != storyboard["target_duration_ms"]:
        raise FinalRecordingBuildError("最终录屏场景时长之和必须为178000毫秒")
    screenshot_sources: set[str] = set()
    for scene in scenes:
        public_text = f"{scene['title']}\n{scene['narration']}"
        if any(marker in public_text for marker in FORBIDDEN_PUBLIC_TEXT):
            raise FinalRecordingBuildError(
                f"最终录屏场景包含私有定位、凭证或夸大表述: {scene['scene_id']}"
            )
        visual = scene["visual"]
        if visual["kind"] == "SCREENSHOT":
            source_path = Path(visual["source"])
            if source_path.name != visual["source"] or source_path.suffix.lower() != ".png":
                raise FinalRecordingBuildError("截图来源只能使用私有资产目录中的PNG文件名")
            screenshot_sources.add(visual["source"])
        elif visual["source"] != "output/video/n31_training_video_v1.mp4":
            raise FinalRecordingBuildError("视频场景只能使用已发布的80秒培训视频")
        for relative in scene["evidence_sources"]:
            evidence = _relative_inside(root, relative, "录屏事实来源")
            if not evidence.is_file() or evidence.stat().st_size < 1:
                raise FinalRecordingBuildError(
                    f"最终录屏事实来源缺失: {scene['scene_id']}"
                )
    if screenshot_sources != EXPECTED_SCREENSHOTS:
        raise FinalRecordingBuildError("最终录屏截图集合与冻结采集规范不一致")
    return storyboard


def _ensure_private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise FinalRecordingBuildError("最终录屏私有目录权限必须为0700")
    return path


def _font_path() -> Path:
    configured = os.getenv("SKILLFORGE_CJK_FONT")
    candidates = ([configured] if configured else []) + list(FONT_CANDIDATES)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise FinalRecordingBuildError("缺少可渲染中文字幕的CJK字体")


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_font_path()), size=size, index=0)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    *,
    max_lines: int = 4,
) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        width = draw.textbbox((0, 0), candidate, font=font)[2]
        if current and width > max_width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        raise FinalRecordingBuildError("最终录屏旁白字幕超过四行安全区")
    return lines


def _draw_overlay(
    image: Image.Image,
    scene: dict[str, Any],
    *,
    transparent: bool,
) -> Image.Image:
    canvas = image.convert("RGBA")
    draw = ImageDraw.Draw(canvas, "RGBA")
    title_font = _font(54)
    kicker_font = _font(22)
    subtitle_font = _font(34)
    draw.rectangle((0, 0, 1920, 158), fill=(5, 18, 13, 232))
    draw.rectangle((0, 790, 1920, 1080), fill=(5, 14, 11, 225))
    draw.rectangle((76, 36, 84, 132), fill=(130, 235, 164, 255))
    draw.text(
        (108, 32),
        f"SKILLFORGE 最终录屏候选 · {scene['order']}/9",
        font=kicker_font,
        fill=(139, 225, 171, 255),
    )
    draw.text((108, 68), scene["title"], font=title_font, fill=(247, 249, 248, 255))
    lines = _wrap_text(draw, scene["narration"], subtitle_font, 1710)
    y = 830
    for line in lines:
        draw.text((105, y), line, font=subtitle_font, fill=(245, 247, 246, 255))
        y += 52
    draw.text(
        (105, 1038),
        "候选成片 · 机器QA不替代完整人工观看",
        font=kicker_font,
        fill=(211, 172, 88, 255),
    )
    return canvas if transparent else canvas.convert("RGB")


def render_screenshot_frame(
    scene: dict[str, Any],
    source: Path,
    destination: Path,
) -> Path:
    try:
        screenshot = Image.open(source).convert("RGB")
    except (OSError, ValueError) as exc:
        raise FinalRecordingBuildError("最终录屏截图无法解码") from exc
    crop = scene["visual"].get("crop")
    if crop:
        x, y, width, height = crop
        if width < 320 or height < 180 or x + width > screenshot.width or y + height > screenshot.height:
            raise FinalRecordingBuildError("最终录屏截图裁剪区域无效")
        screenshot = screenshot.crop((x, y, x + width, y + height))
        background = Image.new("RGB", (1920, 1080), (10, 25, 19))
        fitted = ImageOps.contain(screenshot, (1720, 700), Image.Resampling.LANCZOS)
        background.paste(
            fitted,
            ((1920 - fitted.width) // 2, 175 + (590 - fitted.height) // 2),
        )
    else:
        background = ImageOps.fit(
            screenshot,
            (1920, 1080),
            method=Image.Resampling.LANCZOS,
        )
    frame = _draw_overlay(background, scene, transparent=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.save(destination, format="PNG", optimize=True)
    os.chmod(destination, 0o600)
    return destination


def render_video_overlay(scene: dict[str, Any], destination: Path) -> Path:
    overlay = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    overlay = _draw_overlay(overlay, scene, transparent=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    overlay.save(destination, format="PNG", optimize=True)
    os.chmod(destination, 0o600)
    return destination


def _run(command: list[str], label: str, *, timeout: int = 1800) -> None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise FinalRecordingBuildError(f"{label}超时") from exc
    if completed.returncode != 0:
        raise FinalRecordingBuildError(f"{label}失败，退出码={completed.returncode}")


def _atempo_chain(ratio: float) -> list[str]:
    if not 0.2 <= ratio <= 4.0:
        raise FinalRecordingBuildError("旁白时长与场景时长差异过大")
    filters: list[str] = []
    remaining = ratio
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 0.001:
        filters.append(f"atempo={remaining:.6f}")
    return filters


def _audio_filter(audio_duration_ms: int, target_ms: int) -> str:
    ratio = audio_duration_ms / target_ms
    filters = _atempo_chain(ratio)
    seconds = target_ms / 1000
    filters.extend(
        [
            "loudnorm=I=-18:LRA=7:TP=-1.5",
            "aresample=48000",
            f"apad=whole_dur={seconds:.3f}",
            f"atrim=duration={seconds:.3f}",
            "afade=t=in:st=0:d=0.15",
            f"afade=t=out:st={max(0.0, seconds - 0.65):.3f}:d=0.65",
        ]
    )
    return ",".join(filters)


def render_scene_video(
    scene: dict[str, Any],
    visual: Path,
    narration_audio: Path,
    destination: Path,
    *,
    work_dir: Path,
) -> Path:
    duration_seconds = scene["duration_ms"] / 1000
    probe = probe_media(narration_audio)
    audio_duration_ms = int(probe.get("duration_ms") or 0)
    if not audio_duration_ms or not probe.get("audio_streams"):
        raise FinalRecordingBuildError("TTS旁白缺少有效音频流")
    audio_filter = _audio_filter(audio_duration_ms, scene["duration_ms"])
    ffmpeg = str(resolve_ffmpeg())
    if scene["visual"]["kind"] == "SCREENSHOT":
        frame = render_screenshot_frame(
            scene,
            visual,
            work_dir / f"{scene['scene_id']}_frame.png",
        )
        command = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-loop",
            "1",
            "-framerate",
            "30",
            "-i",
            str(frame),
            "-i",
            str(narration_audio),
            "-filter_complex",
            f"[0:v]fps=30,format=yuv420p[v];[1:a]{audio_filter}[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
        ]
    else:
        overlay = render_video_overlay(
            scene,
            work_dir / f"{scene['scene_id']}_overlay.png",
        )
        command = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(visual),
            "-loop",
            "1",
            "-i",
            str(overlay),
            "-i",
            str(narration_audio),
            "-filter_complex",
            (
                "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,"
                "crop=1920:1080,fps=30[base];"
                f"[base][1:v]overlay=0:0,format=yuv420p[v];[2:a]{audio_filter}[a]"
            ),
            "-map",
            "[v]",
            "-map",
            "[a]",
        ]
    command.extend(
        [
            "-t",
            f"{duration_seconds:.3f}",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-g",
            "60",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    _run(command, f"场景{scene['scene_id']}渲染")
    os.chmod(destination, 0o600)
    return destination


def _write_private_json(document: dict[str, Any], destination: Path, schema: str) -> Path:
    validate_document(document, schema)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(destination.parent.stat().st_mode) != 0o700:
        raise FinalRecordingBuildError("最终录屏报告目录权限必须为0700")
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


def _concat_scenes(
    scene_paths: list[Path],
    destination: Path,
    *,
    work_dir: Path,
    target_duration_ms: int,
) -> Path:
    concat_path = work_dir / "concat.txt"
    concat_path.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in scene_paths),
        encoding="utf-8",
    )
    os.chmod(concat_path, 0o600)
    temporary = work_dir / "final_recording_candidate.mp4"
    _run(
        [
            str(resolve_ffmpeg()),
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-c",
            "copy",
            "-t",
            f"{target_duration_ms / 1000:.3f}",
            "-movflags",
            "+faststart",
            str(temporary),
        ],
        "最终录屏场景拼接",
    )
    os.chmod(temporary, 0o600)
    destination.unlink(missing_ok=True)
    os.replace(temporary, destination)
    os.chmod(destination, 0o600)
    return destination


def _extract_sequence_probe(media: Path, position_ms: int, destination: Path) -> Path:
    """Decode a small frame used only to verify the final scene ordering."""

    _run(
        [
            str(resolve_ffmpeg()),
            "-hide_banner",
            "-y",
            "-ss",
            f"{position_ms / 1000:.3f}",
            "-i",
            str(media),
            "-frames:v",
            "1",
            "-vf",
            "scale=320:180:flags=lanczos",
            str(destination),
        ],
        "最终录屏场景序列抽帧",
    )
    os.chmod(destination, 0o600)
    return destination


def _difference_hash(path: Path) -> int:
    try:
        image = Image.open(path).convert("L").resize((33, 18), Image.Resampling.LANCZOS)
    except (OSError, ValueError) as exc:
        raise FinalRecordingBuildError("最终录屏场景序列抽帧无法解码") from exc
    pixels = list(image.tobytes())
    value = 0
    for row in range(18):
        offset = row * 33
        for column in range(32):
            value = (value << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return value


def _sequence_distance(first: Path, second: Path) -> int:
    return (_difference_hash(first) ^ _difference_hash(second)).bit_count()


def validate_scene_sequence(
    scene_paths: list[Path],
    output_path: Path,
    scenes: list[dict[str, Any]],
    *,
    work_dir: Path,
    maximum_distance: int = 24,
) -> list[dict[str, Any]]:
    """Compare one midpoint frame per rendered scene with the concatenated output."""

    if len(scene_paths) != len(scenes):
        raise FinalRecordingBuildError("最终录屏场景序列数量不一致")
    probe_dir = _ensure_private_directory(work_dir / "sequence_probes")
    elapsed_ms = 0
    checks: list[dict[str, Any]] = []
    for scene_path, scene in zip(scene_paths, scenes, strict=True):
        midpoint_ms = scene["duration_ms"] // 2
        output_position_ms = elapsed_ms + midpoint_ms
        scene_probe = _extract_sequence_probe(
            scene_path,
            midpoint_ms,
            probe_dir / f"{scene['scene_id']}_scene.png",
        )
        output_probe = _extract_sequence_probe(
            output_path,
            output_position_ms,
            probe_dir / f"{scene['scene_id']}_output.png",
        )
        distance = _sequence_distance(scene_probe, output_probe)
        checks.append(
            {
                "scene_id": scene["scene_id"],
                "output_probe_ms": output_position_ms,
                "difference_hash_distance": distance,
                "sequence_match": distance <= maximum_distance,
            }
        )
        elapsed_ms += scene["duration_ms"]
    return checks


def build_final_recording_candidate(
    *,
    root: Path = ROOT,
    storyboard_path: Path = DEFAULT_STORYBOARD,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
    assets_dir: Path = DEFAULT_ASSETS,
    work_dir: Path = DEFAULT_WORK,
    output_path: Path = DEFAULT_OUTPUT,
    build_report_path: Path = DEFAULT_BUILD_REPORT,
    qa_report_path: Path = DEFAULT_QA_REPORT,
    force_tts: bool = False,
    tts_client: StepAudioTTSClient | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    storyboard_path = storyboard_path.expanduser().resolve()
    private_root = private_root.expanduser().resolve()
    assets_dir = _private_inside(assets_dir, private_root)
    work_dir = _private_inside(work_dir, private_root)
    output_path = _private_inside(output_path, private_root)
    build_report_path = _private_inside(build_report_path, private_root)
    qa_report_path = _private_inside(qa_report_path, private_root)
    _ensure_private_directory(private_root)
    _ensure_private_directory(assets_dir)
    _ensure_private_directory(work_dir)
    storyboard = validate_storyboard(load_storyboard(storyboard_path), root=root)
    client = tts_client or StepAudioTTSClient()
    tts_dir = _ensure_private_directory(work_dir / "tts")
    rendered_dir = _ensure_private_directory(work_dir / "scenes")
    tts_generated = 0
    scene_reports: list[dict[str, Any]] = []
    rendered_paths: list[Path] = []
    for scene in storyboard["scenes"]:
        visual_spec = scene["visual"]
        if visual_spec["kind"] == "SCREENSHOT":
            visual = _private_inside(assets_dir / visual_spec["source"], private_root)
            if not visual.is_file() or visual.stat().st_size < 1:
                raise FinalRecordingBuildError(
                    f"缺少浏览器安全截图: {visual_spec['source']}"
                )
            os.chmod(visual, 0o600)
        else:
            visual = _relative_inside(root, visual_spec["source"], "培训视频")
            if not visual.is_file() or visual.stat().st_size < 1:
                raise FinalRecordingBuildError("已发布的80秒培训视频缺失")
        tts = storyboard["tts"]
        narration_key = _text_sha256(
            json.dumps(
                {
                    "text": scene["narration"],
                    "model": tts["model"],
                    "voice": tts["voice"],
                    "instruction": tts["instruction"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        narration_audio = tts_dir / f"{scene['scene_id']}_{narration_key[:16]}.mp3"
        if force_tts or not narration_audio.is_file():
            client.synthesize(
                scene["narration"],
                narration_audio,
                model=tts["model"],
                voice=tts["voice"],
                instruction=tts["instruction"],
                sample_rate=tts["sample_rate"],
            )
            tts_generated += 1
        os.chmod(narration_audio, 0o600)
        rendered = rendered_dir / f"{scene['scene_id']}.mp4"
        render_scene_video(
            scene,
            visual,
            narration_audio,
            rendered,
            work_dir=work_dir,
        )
        rendered_paths.append(rendered)
        scene_reports.append(
            {
                "scene_id": scene["scene_id"],
                "order": scene["order"],
                "duration_ms": scene["duration_ms"],
                "visual_kind": visual_spec["kind"],
                "visual_source_sha256": _sha256(visual),
                "narration_sha256": _text_sha256(scene["narration"]),
                "tts_audio_sha256": _sha256(narration_audio),
                "rendered_sha256": _sha256(rendered),
            }
        )
    _concat_scenes(
        rendered_paths,
        output_path,
        work_dir=work_dir,
        target_duration_ms=storyboard["target_duration_ms"],
    )
    sequence_checks = validate_scene_sequence(
        rendered_paths,
        output_path,
        storyboard["scenes"],
        work_dir=work_dir,
    )
    for scene_report, sequence_check in zip(
        scene_reports, sequence_checks, strict=True
    ):
        scene_report.update(
            {
                "output_probe_ms": sequence_check["output_probe_ms"],
                "difference_hash_distance": sequence_check[
                    "difference_hash_distance"
                ],
                "sequence_match": sequence_check["sequence_match"],
            }
        )
    qa = evaluate_final_recording(output_path)
    write_private_report(qa, qa_report_path)
    scene_sequence_all_matched = all(
        item["sequence_match"] for item in sequence_checks
    )
    all_machine_checks_passed = (
        all(qa["checks"].values()) and scene_sequence_all_matched
    )
    build_status = (
        "READY_FOR_HUMAN_REVIEW"
        if all_machine_checks_passed
        else "MACHINE_QA_FAILED"
    )
    output_probe = probe_media(output_path)
    video = (output_probe.get("video_streams") or [{}])[0]
    audio = (output_probe.get("audio_streams") or [{}])[0]
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "FINAL_RECORDING_BUILD",
        "generated_at": _now(),
        "status": build_status,
        "storyboard_sha256": _sha256(storyboard_path),
        "scene_count": len(scene_reports),
        "target_duration_ms": storyboard["target_duration_ms"],
        "media": {
            "filename": output_path.name,
            "sha256": _sha256(output_path),
            "bytes": output_path.stat().st_size,
            "duration_ms": int(output_probe.get("duration_ms") or 0),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "fps": float(video.get("fps") or 0),
            "video_codec": video.get("codec"),
            "audio_codec": audio.get("codec"),
        },
        "scenes": scene_reports,
        "tts": {
            "model": storyboard["tts"]["model"],
            "voice": storyboard["tts"]["voice"],
            "scene_count": len(scene_reports),
            "generated_count": tts_generated,
            "reused_count": len(scene_reports) - tts_generated,
            "external_model_calls": tts_generated,
            "text_only": True,
        },
        "machine_qa": {
            "status": build_status,
            "report_sha256": _sha256(qa_report_path),
            "all_checks_passed": all_machine_checks_passed,
            "scene_sequence_all_matched": scene_sequence_all_matched,
        },
        "human_review": {
            "required": True,
            "status": "PENDING",
            "automatic_approval": False,
        },
        "data_policy": {
            "private_local_state": True,
            "screenshot_assets_private": True,
            "raw_media_sent_to_tts": False,
            "tts_text_only": True,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "automatic_human_approval": False,
        },
    }
    _write_private_json(
        report,
        build_report_path,
        "final_recording_build.schema.json",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storyboard", type=Path, default=DEFAULT_STORYBOARD)
    parser.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--force-tts", action="store_true")
    args = parser.parse_args()
    try:
        report = build_final_recording_candidate(
            storyboard_path=args.storyboard,
            assets_dir=args.assets,
            output_path=args.output,
            force_tts=args.force_tts,
        )
    except (
        FinalRecordingBuildError,
        ContractValidationError,
        MediaProcessingError,
        OSError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": "最终录屏候选生成失败",
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
                "status": report["status"],
                "duration_ms": report["media"]["duration_ms"],
                "scene_count": report["scene_count"],
                "tts_generated_count": report["tts"]["generated_count"],
                "tts_reused_count": report["tts"]["reused_count"],
                "machine_checks_passed": report["machine_qa"]["all_checks_passed"],
                "human_review": report["human_review"]["status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "READY_FOR_HUMAN_REVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
