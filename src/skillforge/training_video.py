"""Render the evidence-grounded N31 training video from privacy-safe footage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from .contracts import validate_document
from .media import MediaProcessingError, probe_media, resolve_ffmpeg
from .media_privacy import measure_loudness
from .observability import StructuredLogger, redact
from .step_plan import StepPlanError, load_dotenv


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STORYBOARD = ROOT / "cases" / "n31" / "training_video_storyboard.json"
DEFAULT_GOLD = ROOT / "cases" / "n31" / "gold" / "gold_sop.json"
DEFAULT_INGEST = ROOT / "cases" / "n31" / "ingest_manifest.json"
DEFAULT_OUTPUT = ROOT / "output" / "video" / "n31_training_video_v1.mp4"
DEFAULT_MANIFEST = ROOT / "output" / "video" / "n31_training_video_manifest_v1.json"
DEFAULT_EVIDENCE_PACK = (
    ROOT / "output" / "video" / "n31_training_video_evidence_pack_v1.json"
)
DEFAULT_WORK = ROOT / "outputs" / "n31_training_video"


class TrainingVideoError(RuntimeError):
    """Raised when a storyboard, TTS request or deterministic export fails."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _run(command: list[str], *, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error")[-2000:]
        raise MediaProcessingError(f"培训视频处理失败: {detail}")
    return completed


def build_tts_payload(
    text: str,
    *,
    model: str,
    voice: str,
    instruction: str,
    sample_rate: int,
) -> dict[str, Any]:
    if not text or len(text) > 1000:
        raise ValueError("TTS文本必须为1到1000个字符")
    if len(instruction) > 200:
        raise ValueError("TTS instruction不能超过200个字符")
    return {
        "model": model,
        "input": text,
        "voice": voice,
        "instruction": instruction,
        "sample_rate": sample_rate,
        "response_format": "mp3",
        "stream_format": "audio",
    }


class StepAudioTTSClient:
    """Minimal binary TTS client that never logs text, keys or headers."""

    def __init__(
        self,
        *,
        logger: StructuredLogger | None = None,
        timeout_seconds: int = 240,
        transport: Callable[[dict[str, Any]], bytes] | None = None,
    ) -> None:
        load_dotenv()
        self.api_key = os.getenv("STEP_API_KEY", "")
        self.url = os.getenv(
            "STEP_TTS_URL",
            "https://api.stepfun.com/step_plan/v1/audio/speech",
        )
        self.logger = logger or StructuredLogger()
        self.timeout_seconds = timeout_seconds
        self.transport = transport or self._curl_transport

    def _curl_transport(self, payload: dict[str, Any]) -> bytes:
        if not self.api_key:
            raise StepPlanError("STEP_API_KEY 未配置")
        header_path: str | None = None
        response_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False
            ) as header_file:
                header_path = header_file.name
                header_file.write("Content-Type: application/json\n")
                header_file.write(f"Authorization: Bearer {self.api_key}\n")
            os.chmod(header_path, 0o600)
            with tempfile.NamedTemporaryFile(delete=False) as response_file:
                response_path = response_file.name
            os.chmod(response_path, 0o600)
            completed = subprocess.run(
                [
                    "curl",
                    "--silent",
                    "--show-error",
                    "--fail-with-body",
                    "--connect-timeout",
                    "15",
                    "--max-time",
                    str(self.timeout_seconds),
                    "--header",
                    f"@{header_path}",
                    "--data-binary",
                    "@-",
                    "--output",
                    response_path,
                    self.url,
                ],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                check=False,
            )
            data = Path(response_path).read_bytes()
            if completed.returncode != 0:
                detail = data[:1200].decode("utf-8", errors="replace")
                raise StepPlanError(f"StepAudio TTS 请求失败: {redact(detail)}")
            if len(data) < 128:
                raise StepPlanError("StepAudio TTS 返回的音频为空或过短")
            return data
        except subprocess.TimeoutExpired as exc:
            raise StepPlanError("StepAudio TTS 请求超时") from exc
        finally:
            if header_path:
                Path(header_path).unlink(missing_ok=True)
            if response_path:
                Path(response_path).unlink(missing_ok=True)

    def synthesize(
        self,
        text: str,
        destination: Path,
        *,
        model: str,
        voice: str,
        instruction: str,
        sample_rate: int,
    ) -> Path:
        payload = build_tts_payload(
            text,
            model=model,
            voice=voice,
            instruction=instruction,
            sample_rate=sample_rate,
        )
        self.logger.emit(
            "step_audio.tts.request",
            model=model,
            voice=voice,
            text_length=len(text),
            sample_rate=sample_rate,
        )
        data = self.transport(payload)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".partial")
        temporary.write_bytes(data)
        temporary.chmod(0o600)
        temporary.replace(destination)
        destination.chmod(0o600)
        self.logger.emit(
            "step_audio.tts.success",
            model=model,
            voice=voice,
            bytes=len(data),
            audio_sha256=_sha256(destination),
        )
        return destination


def load_storyboard(path: Path = DEFAULT_STORYBOARD) -> dict[str, Any]:
    return validate_document(_read_json(path), "training_video_storyboard.schema.json")


def validate_storyboard_against_case(
    storyboard: dict[str, Any],
    gold: dict[str, Any],
    ingest: dict[str, Any],
    *,
    check_media_files: bool = True,
) -> dict[str, Any]:
    validate_document(storyboard, "training_video_storyboard.schema.json")
    if storyboard["case_id"] != gold["case_id"] or gold["case_id"] != ingest["case_id"]:
        raise TrainingVideoError("Storyboard、Gold与摄取清单的case_id不一致")

    source_map = {
        item["source_id"]: item
        for item in ingest["sources"]
        if item.get("privacy_status") == "LOCAL_QA_PASSED"
        and item.get("path", "").lower().endswith(".mp4")
    }
    step_map = {item["step_id"]: item for item in gold["steps"]}
    evidence_map = {item["evidence_id"]: item for item in gold["evidence_catalog"]}
    duration = sum(
        (scene["end_ms"] - scene["start_ms"]) / 1000
        for scene in storyboard["scenes"]
    )
    if abs(duration - storyboard["target_duration_seconds"]) > 0.001:
        raise TrainingVideoError("分镜时长之和与target_duration_seconds不一致")

    covered: list[str] = []
    used_sources: dict[str, Path] = {}
    evidence_boundary_passed = True
    for scene in storyboard["scenes"]:
        if scene["end_ms"] <= scene["start_ms"]:
            raise TrainingVideoError(f"{scene['scene_id']}: 结束时间必须晚于开始时间")
        source = source_map.get(scene["source_id"])
        if source is None:
            raise TrainingVideoError(
                f"{scene['scene_id']}: 来源不是LOCAL_QA_PASSED视频"
            )
        path = ROOT / source["path"]
        used_sources[scene["source_id"]] = path
        if check_media_files:
            if not path.is_file():
                raise FileNotFoundError(path)
            duration_ms = probe_media(path).get("duration_ms") or 0
            if scene["end_ms"] > duration_ms + 100:
                raise TrainingVideoError(f"{scene['scene_id']}: 片段超出源视频时长")
            if crop := scene.get("crop"):
                stream = (probe_media(path).get("video_streams") or [{}])[0]
                if (
                    crop["x"] + crop["width"] > int(stream.get("width") or 0)
                    or crop["y"] + crop["height"] > int(stream.get("height") or 0)
                ):
                    raise TrainingVideoError(f"{scene['scene_id']}: 裁切区域超出源画面")

        if scene["kind"] == "STEP":
            if not scene["step_ids"] or not scene["evidence_ids"]:
                raise TrainingVideoError(f"{scene['scene_id']}: STEP场景必须绑定步骤和证据")
            allowed_evidence: set[str] = set()
            for step_id in scene["step_ids"]:
                step = step_map.get(step_id)
                if step is None:
                    raise TrainingVideoError(f"未知Gold步骤: {step_id}")
                covered.append(step_id)
                allowed_evidence.update(step["evidence"])
                if scene["conditional"] == step["required"]:
                    raise TrainingVideoError(f"{scene['scene_id']}: 条件步骤标记与Gold不一致")
            unknown = set(scene["evidence_ids"]) - allowed_evidence
            if unknown:
                raise TrainingVideoError(
                    f"{scene['scene_id']}: 引用了不属于对应Gold步骤的证据 {sorted(unknown)}"
                )
            matching_video = False
            for evidence_id in scene["evidence_ids"]:
                evidence = evidence_map[evidence_id]
                locator = evidence.get("locator") or {}
                overlaps = (
                    locator.get("start_ms", -1) < scene["end_ms"]
                    and locator.get("end_ms", -1) > scene["start_ms"]
                )
                if (
                    evidence.get("source_type") == "video"
                    and evidence.get("source_ref") == scene["source_id"]
                    and overlaps
                ):
                    matching_video = True
                    break
            if not matching_video:
                evidence_boundary_passed = False
                raise TrainingVideoError(
                    f"{scene['scene_id']}: 没有与片段时间重叠的同源视频证据"
                )
        elif scene["step_ids"] or scene["evidence_ids"] or scene["conditional"]:
            raise TrainingVideoError(f"{scene['scene_id']}: 片头片尾不能伪装成Gold步骤")

    if len(covered) != len(set(covered)):
        raise TrainingVideoError("同一个Gold步骤被重复覆盖")
    if set(covered) != set(step_map):
        raise TrainingVideoError("分镜没有完整覆盖全部Gold步骤")
    narration = " ".join(scene["narration"] for scene in storyboard["scenes"])
    if len(narration) > 1000:
        raise TrainingVideoError("合并旁白超过StepAudio单次1000字符限制")
    return {
        "duration_seconds": duration,
        "narration": narration,
        "covered_step_ids": sorted(covered),
        "required_step_ids": sorted(
            step["step_id"] for step in gold["steps"] if step["required"]
        ),
        "evidence_reference_count": sum(
            len(scene["evidence_ids"]) for scene in storyboard["scenes"]
        ),
        "evidence_boundary_passed": evidence_boundary_passed,
        "source_paths": used_sources,
    }


def resolve_cjk_font() -> Path:
    configured = os.getenv("SKILLFORGE_CJK_FONT")
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.home() / "Library" / "Fonts" / "NotoSansCJKsc-Regular.otf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf"),
        Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise TrainingVideoError("找不到可用于培训视频叠字的中文字体")


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
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
    return lines


def render_scene_overlay(
    scene: dict[str, Any],
    destination: Path,
    *,
    font_path: Path,
    width: int,
    height: int,
    scene_number: int,
    scene_count: int,
) -> Path:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(str(font_path), 54)
    body_font = ImageFont.truetype(str(font_path), 38)
    small_font = ImageFont.truetype(str(font_path), 25)
    badge_font = ImageFont.truetype(str(font_path), 26)
    green = (118, 185, 0, 255)
    white = (246, 249, 247, 255)
    muted = (195, 207, 199, 255)

    draw.rounded_rectangle((70, 46, 475, 104), radius=18, fill=(7, 18, 13, 220))
    draw.text((94, 59), "SkillForge · N31 培训", font=badge_font, fill=green)
    draw.rounded_rectangle(
        (70, 770, width - 70, height - 48),
        radius=30,
        fill=(5, 14, 10, 220),
        outline=(48, 78, 60, 255),
        width=2,
    )
    title = scene["title"]
    if scene["kind"] == "INTRO":
        title = "匠传 SkillForge｜" + title
    draw.text((110, 806), title, font=title_font, fill=white)
    lines = _wrap_text(draw, scene["narration"], body_font, width - 220)
    for index, line in enumerate(lines[:2]):
        draw.text((110, 880 + index * 48), line, font=body_font, fill=white)
    if scene["evidence_ids"]:
        evidence = "证据  " + " · ".join(scene["evidence_ids"])
    elif scene["kind"] == "INTRO":
        evidence = "真实设备实拍 · Gold v1 · 80秒培训版"
    else:
        evidence = "完成标准：无跳纸、偏斜、卡纸或异常报警"
    draw.text((110, 986), evidence, font=small_font, fill=muted)
    progress_width = round((width - 140) * scene_number / scene_count)
    draw.rounded_rectangle(
        (70, height - 22, width - 70, height - 12), 5, fill=(31, 51, 40, 255)
    )
    draw.rounded_rectangle(
        (70, height - 22, 70 + progress_width, height - 12), 5, fill=green
    )
    draw.text(
        (width - 240, 62),
        f"{scene_number:02d} / {scene_count:02d}",
        font=small_font,
        fill=muted,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    return destination


def _render_segment(
    scene: dict[str, Any],
    source: Path,
    overlay: Path,
    destination: Path,
    *,
    width: int,
    height: int,
    fps: int,
) -> Path:
    duration = (scene["end_ms"] - scene["start_ms"]) / 1000
    destination.parent.mkdir(parents=True, exist_ok=True)
    crop_filter = ""
    if crop := scene.get("crop"):
        crop_filter = (
            f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']},"
        )
    graph = (
        f"[0:v]{crop_filter}scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},setsar=1,setpts=PTS-STARTPTS[base];"
        "[1:v]format=rgba[card];"
        "[base][card]overlay=0:0:shortest=1,format=yuv420p[out]"
    )
    _run(
        [
            str(resolve_ffmpeg()),
            "-hide_banner",
            "-y",
            "-ss",
            f"{scene['start_ms'] / 1000:.3f}",
            "-i",
            str(source),
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(overlay),
            "-filter_complex",
            graph,
            "-map",
            "[out]",
            "-t",
            f"{duration:.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-g",
            str(fps * 2),
            str(destination),
        ]
    )
    return destination


def _concat_segments(segments: list[Path], destination: Path, work_dir: Path) -> Path:
    concat_file = work_dir / "segments.txt"
    concat_file.write_text(
        "".join(f"file '{path.resolve()}'\n" for path in segments),
        encoding="utf-8",
    )
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
            str(concat_file),
            "-c",
            "copy",
            str(destination),
        ]
    )
    return destination


def _mux_narration(
    silent_video: Path,
    narration: Path,
    destination: Path,
    *,
    target_duration_seconds: float,
    narration_duration_ms: int,
) -> float:
    ratio = max(1.0, narration_duration_ms / (target_duration_seconds * 1000))
    if ratio > 1.35:
        raise TrainingVideoError(
            f"旁白需要加速{ratio:.3f}倍才能放入成片，超过1.35上限"
        )
    audio_filters = []
    if ratio > 1.001:
        audio_filters.append(f"atempo={ratio:.6f}")
    audio_filters.extend(
        [
            "loudnorm=I=-16:LRA=11:TP=-1.5",
            "aresample=48000",
            f"apad=whole_dur={target_duration_seconds:.3f}",
            "afade=t=in:st=0:d=0.25",
            f"afade=t=out:st={target_duration_seconds - 1.25:.3f}:d=1.25",
        ]
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(resolve_ffmpeg()),
            "-hide_banner",
            "-y",
            "-i",
            str(silent_video),
            "-i",
            str(narration),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            "48000",
            "-af",
            ",".join(audio_filters),
            "-t",
            f"{target_duration_seconds:.3f}",
            "-movflags",
            "+faststart",
            str(destination),
        ]
    )
    return ratio


def generate_contact_sheet(video: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(resolve_ffmpeg()),
            "-hide_banner",
            "-y",
            "-i",
            str(video),
            "-vf",
            "fps=1/5,scale=384:216,tile=4x4:padding=4:margin=4",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(destination),
        ]
    )
    return destination


def build_training_video_evidence_pack(
    storyboard: dict[str, Any],
    gold: dict[str, Any],
    *,
    video_sha256: str,
) -> dict[str, Any]:
    """Build a portable locator-only audit pack without embedding raw media."""

    evidence_by_id = {
        item["evidence_id"]: item for item in gold.get("evidence_catalog", [])
    }
    evidence_ids = sorted(
        {
            evidence_id
            for scene in storyboard["scenes"]
            for evidence_id in scene["evidence_ids"]
        }
    )
    missing = sorted(set(evidence_ids) - set(evidence_by_id))
    if missing:
        raise TrainingVideoError(f"培训视频证据包引用未知Evidence: {missing}")
    bundle = {
        "version": 1,
        "case_id": storyboard["case_id"],
        "artifact_type": "TRAINING_VIDEO_EVIDENCE_PACK",
        "generated_at": _utc_now(),
        "training_video_sha256": video_sha256,
        "contains_raw_media": False,
        "contains_credentials": False,
        "scenes": [
            {
                "scene_id": scene["scene_id"],
                "kind": scene["kind"],
                "step_ids": scene["step_ids"],
                "source_id": scene["source_id"],
                "start_ms": scene["start_ms"],
                "end_ms": scene["end_ms"],
                "conditional": scene["conditional"],
                "evidence_ids": scene["evidence_ids"],
            }
            for scene in storyboard["scenes"]
        ],
        "evidence": [evidence_by_id[evidence_id] for evidence_id in evidence_ids],
    }
    return validate_document(bundle, "training_video_evidence_pack.schema.json")


def render_training_video(
    *,
    storyboard_path: Path = DEFAULT_STORYBOARD,
    gold_path: Path = DEFAULT_GOLD,
    ingest_path: Path = DEFAULT_INGEST,
    output_path: Path = DEFAULT_OUTPUT,
    manifest_path: Path = DEFAULT_MANIFEST,
    evidence_pack_path: Path = DEFAULT_EVIDENCE_PACK,
    work_dir: Path = DEFAULT_WORK,
    force_tts: bool = False,
    tts_client: StepAudioTTSClient | None = None,
) -> dict[str, Any]:
    storyboard = load_storyboard(storyboard_path)
    gold = _read_json(gold_path)
    ingest = _read_json(ingest_path)
    validation = validate_storyboard_against_case(storyboard, gold, ingest)
    work_dir.mkdir(parents=True, exist_ok=True)
    font = resolve_cjk_font()
    tts = storyboard["tts"]
    text_hash = _text_sha256(validation["narration"])
    narration_path = work_dir / f"narration_{text_hash[:16]}.mp3"
    if force_tts or not narration_path.is_file():
        (tts_client or StepAudioTTSClient()).synthesize(
            validation["narration"],
            narration_path,
            model=tts["model"],
            voice=tts["voice"],
            instruction=tts["instruction"],
            sample_rate=tts["sample_rate"],
        )
    narration_probe = probe_media(narration_path)
    narration_duration_ms = narration_probe.get("duration_ms") or 0
    if not narration_duration_ms or not narration_probe.get("audio_streams"):
        raise TrainingVideoError("TTS旁白不是可读取的音频")

    overlays = work_dir / "overlays"
    segment_dir = work_dir / "segments"
    segments = []
    for index, scene in enumerate(storyboard["scenes"], start=1):
        overlay = render_scene_overlay(
            scene,
            overlays / f"{scene['scene_id']}.png",
            font_path=font,
            width=storyboard["width"],
            height=storyboard["height"],
            scene_number=index,
            scene_count=len(storyboard["scenes"]),
        )
        segments.append(
            _render_segment(
                scene,
                validation["source_paths"][scene["source_id"]],
                overlay,
                segment_dir / f"{scene['scene_id']}.mp4",
                width=storyboard["width"],
                height=storyboard["height"],
                fps=storyboard["fps"],
            )
        )
    silent_video = _concat_segments(segments, work_dir / "silent_video.mp4", work_dir)
    ratio = _mux_narration(
        silent_video,
        narration_path,
        output_path,
        target_duration_seconds=storyboard["target_duration_seconds"],
        narration_duration_ms=narration_duration_ms,
    )
    contact_sheet = generate_contact_sheet(
        output_path,
        work_dir / "n31_training_video_contact_sheet.jpg",
    )

    output_probe = probe_media(output_path)
    video = (output_probe.get("video_streams") or [{}])[0]
    audio = (output_probe.get("audio_streams") or [{}])[0]
    loudness = measure_loudness(output_path) or {}
    duration_ms = int(output_probe.get("duration_ms") or 0)
    target_ms = round(storyboard["target_duration_seconds"] * 1000)
    required = validation["required_step_ids"]
    sources = [
        {
            "source_id": source_id,
            "sha256": _sha256(path),
            "privacy_status": "LOCAL_QA_PASSED",
        }
        for source_id, path in sorted(validation["source_paths"].items())
    ]
    output_sha256 = _sha256(output_path)
    evidence_pack = build_training_video_evidence_pack(
        storyboard,
        gold,
        video_sha256=output_sha256,
    )
    _write_json(evidence_pack_path, evidence_pack)
    manifest = {
        "version": 1,
        "case_id": storyboard["case_id"],
        "generated_at": _utc_now(),
        "status": "READY_FOR_HUMAN_REVIEW",
        "storyboard_sha256": _sha256(storyboard_path),
        "output": {
            "filename": output_path.name,
            "sha256": output_sha256,
            "bytes": output_path.stat().st_size,
            "duration_ms": duration_ms,
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "fps": float(video.get("fps") or 0),
            "video_codec": video.get("codec"),
            "audio_codec": audio.get("codec"),
            "audio_sample_rate": int(audio.get("sample_rate") or 0),
        },
        "narration": {
            "model": tts["model"],
            "voice": tts["voice"],
            "text_sha256": text_hash,
            "source_audio_sha256": _sha256(narration_path),
            "source_duration_ms": int(narration_duration_ms),
            "tempo_ratio": round(ratio, 6),
            "text_only_external_processing": True,
        },
        "evidence_pack": {
            "filename": evidence_pack_path.name,
            "sha256": _sha256(evidence_pack_path),
            "evidence_count": len(evidence_pack["evidence"]),
            "scene_count": len(evidence_pack["scenes"]),
            "contains_raw_media": False,
        },
        "source_policy": {
            "accepted_privacy_status": "LOCAL_QA_PASSED",
            "processed_source_count": len(sources),
            "third_party_reference_processed": False,
            "raw_video_sent_external": False,
            "manual_sent_external": False,
            "private_label_sent_external": False,
        },
        "coverage": {
            "scene_count": len(storyboard["scenes"]),
            "gold_step_count": len(gold["steps"]),
            "covered_gold_step_count": len(validation["covered_step_ids"]),
            "required_step_count": len(required),
            "covered_required_step_count": len(
                set(required) & set(validation["covered_step_ids"])
            ),
            "evidence_reference_count": validation["evidence_reference_count"],
        },
        "sources": sources,
        "automated_qa": {
            "duration_passed": abs(duration_ms - target_ms) <= 250,
            "dimensions_passed": (
                video.get("width") == storyboard["width"]
                and video.get("height") == storyboard["height"]
            ),
            "fps_passed": abs(float(video.get("fps") or 0) - storyboard["fps"]) <= 0.1,
            "audio_passed": (
                audio.get("codec") == "aac" and audio.get("sample_rate") == 48000
            ),
            "step_coverage_passed": len(validation["covered_step_ids"]) == len(gold["steps"]),
            "evidence_boundary_passed": validation["evidence_boundary_passed"],
            "source_privacy_gate_passed": True,
            "integrated_lufs": float(loudness.get("integrated_lufs", -99)),
            "true_peak_dbtp": float(loudness.get("true_peak_dbtp", 99)),
        },
        "visual_review": {
            "status": "PENDING",
            "reviewer_type": "PENDING",
            "reviewed_at": None,
            "contact_sheet_sha256": _sha256(contact_sheet),
            "notes": "等待联系表视觉检查和最终参赛者观看确认。",
        },
        "final_human_review_required": True,
    }
    manifest = validate_document(manifest, "training_video_manifest.schema.json")
    _write_json(manifest_path, manifest)
    return manifest


def record_visual_review(
    manifest_path: Path,
    contact_sheet: Path,
    *,
    passed: bool,
    notes: str,
) -> dict[str, Any]:
    manifest = validate_document(
        _read_json(manifest_path), "training_video_manifest.schema.json"
    )
    if not contact_sheet.is_file():
        raise FileNotFoundError(contact_sheet)
    manifest["visual_review"] = {
        "status": "PASSED" if passed else "FAILED",
        "reviewer_type": "AI_ASSISTED_CONTACT_SHEET",
        "reviewed_at": _utc_now(),
        "contact_sheet_sha256": _sha256(contact_sheet),
        "notes": notes,
    }
    manifest["status"] = "READY_FOR_HUMAN_REVIEW"
    manifest["final_human_review_required"] = True
    manifest = validate_document(manifest, "training_video_manifest.schema.json")
    _write_json(manifest_path, manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--storyboard", type=Path, default=DEFAULT_STORYBOARD)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--ingest", type=Path, default=DEFAULT_INGEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--evidence-pack", type=Path, default=DEFAULT_EVIDENCE_PACK)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--force-tts", action="store_true")
    parser.add_argument("--record-visual-review", choices=["passed", "failed"])
    parser.add_argument("--review-notes", default="")
    args = parser.parse_args()
    if args.record_visual_review:
        manifest = record_visual_review(
            args.manifest,
            args.work_dir / "n31_training_video_contact_sheet.jpg",
            passed=args.record_visual_review == "passed",
            notes=args.review_notes or "联系表视觉检查已记录。",
        )
    else:
        manifest = render_training_video(
            storyboard_path=args.storyboard,
            gold_path=args.gold,
            ingest_path=args.ingest,
            output_path=args.output,
            manifest_path=args.manifest,
            evidence_pack_path=args.evidence_pack,
            work_dir=args.work_dir,
            force_tts=args.force_tts,
        )
    print(
        json.dumps(
            {"status": manifest["status"], **manifest["output"]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
