"""Native Python + FFmpeg ingestion for video, PDF and expert audio."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .asr import StepAudioASRClient
from .contracts import validate_document
from .media import (
    extract_keyframes,
    normalize_audio,
    normalize_video,
    probe_media,
)
from .observability import StructuredLogger
from .pdf_ingest import extract_pdf
from .perception import PerceptionAgent
from .planner import SOPAgent
from .step_plan import StepPlanClient


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _asset(path: Path) -> dict[str, Any]:
    return {
        "name": path.name,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class IngestionPipeline:
    def __init__(
        self,
        output_dir: Path,
        *,
        frame_interval_seconds: float = 5.0,
        logger: StructuredLogger | None = None,
    ) -> None:
        self.output_dir = output_dir.resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.frame_interval_seconds = frame_interval_seconds
        self.logger = logger or StructuredLogger(self.output_dir / "ingest.jsonl")
        self.evidence: list[dict[str, Any]] = []

    def _add_evidence(self, **fields: Any) -> dict[str, Any]:
        evidence = {"evidence_id": f"E{len(self.evidence) + 1:03d}", **fields}
        validate_document(evidence, "evidence.schema.json")
        self.evidence.append(evidence)
        return evidence

    def run(
        self,
        *,
        video: Path | None = None,
        pdf: Path | None = None,
        audio: Path | None = None,
        transcribe: bool = False,
        analyze_visuals: bool = False,
        plan_sop: bool = False,
        external_processing_authorized: bool = False,
        synthetic: bool = False,
        case_id: str = "UPLOADED-CASE",
        title: str = "上传素材生成的 SOP 草稿",
    ) -> dict[str, Any]:
        if not any((video, pdf, audio)):
            raise ValueError("至少需要 video、pdf 或 audio 中的一项")
        if (transcribe or analyze_visuals or plan_sop) and not external_processing_authorized:
            raise ValueError("调用外部模型前必须明确确认外部处理授权")
        manifest: dict[str, Any] = {
            "status": "INGESTED",
            "synthetic": synthetic,
            "assets": {},
            "derived": {},
        }
        audio_for_asr: Path | None = None

        if video:
            video = video.expanduser().resolve()
            self.logger.emit("ingest.video.started", asset=_asset(video))
            original_probe = probe_media(video)
            normalized = normalize_video(
                video, self.output_dir / "derived" / "video" / "normalized.mp4"
            )
            normalized_probe = probe_media(normalized)
            frames = extract_keyframes(
                normalized,
                self.output_dir / "derived" / "video" / "frames",
                interval_seconds=self.frame_interval_seconds,
            )
            if normalized_probe["audio_streams"]:
                audio_for_asr = normalize_audio(
                    normalized,
                    self.output_dir / "derived" / "audio" / "video_audio.wav",
                )
            manifest["assets"]["video"] = _asset(video)
            manifest["derived"]["video"] = {
                "original_probe": original_probe,
                "normalized_probe": normalized_probe,
                "normalized": _relative(normalized, self.output_dir),
                "frames": [
                    {**item, "path": _relative(item["path"], self.output_dir)}
                    for item in frames
                ],
            }
            perception = (
                PerceptionAgent(StepPlanClient(logger=self.logger))
                if analyze_visuals
                else None
            )
            for item in frames:
                keyframe_ref = _relative(item["path"], self.output_dir)
                if perception:
                    evidence = perception.analyze_frame(
                        item["path"],
                        evidence_id=f"E{len(self.evidence) + 1:03d}",
                        source_ref=video.name,
                        keyframe_ref=keyframe_ref,
                        start_ms=item["start_ms"],
                        end_ms=item["end_ms"],
                    )
                    self.evidence.append(evidence)
                else:
                    self._add_evidence(
                        source_type="video",
                        source_ref=video.name,
                        claim="视频关键帧候选，等待视觉模型确认动作、工具和设备状态。",
                        locator={
                            "start_ms": item["start_ms"],
                            "end_ms": item["end_ms"],
                            "keyframe": keyframe_ref,
                        },
                        classification="MODEL_INFERENCE",
                        relevance=0.5,
                        confidence=0.5,
                        review_status="UNREVIEWED",
                    )
            self.logger.emit(
                "ingest.video.completed",
                frame_count=len(frames),
                duration_ms=normalized_probe["duration_ms"],
            )

        if pdf:
            pdf = pdf.expanduser().resolve()
            self.logger.emit("ingest.pdf.started", asset=_asset(pdf))
            extracted = extract_pdf(pdf, self.output_dir / "derived" / "pdf" / "pages")
            manifest["assets"]["pdf"] = _asset(pdf)
            manifest["derived"]["pdf"] = {
                "page_count": extracted["page_count"],
                "pages": [
                    {**page, "preview": _relative(page["preview"], self.output_dir)}
                    for page in extracted["pages"]
                ],
            }
            for page in extracted["pages"]:
                claim = " ".join(page["text"].split())[:500]
                self._add_evidence(
                    source_type="pdf",
                    source_ref=pdf.name,
                    claim=claim or f"第 {page['page']} 页未提取到可验证文本，需要 OCR。",
                    locator={"page": page["page"], "paragraph": "页面全文候选"},
                    classification="SOURCE_FACT" if claim else "MODEL_INFERENCE",
                    relevance=0.7 if claim else 0.2,
                    confidence=0.9 if claim else 0.2,
                    review_status="UNREVIEWED",
                )
            self.logger.emit(
                "ingest.pdf.completed",
                page_count=extracted["page_count"],
                ocr_candidate_count=sum(page["needs_ocr"] for page in extracted["pages"]),
            )

        if audio:
            audio = audio.expanduser().resolve()
            self.logger.emit("ingest.audio.started", asset=_asset(audio))
            audio_for_asr = normalize_audio(
                audio, self.output_dir / "derived" / "audio" / "expert_audio.wav"
            )
            manifest["assets"]["audio"] = _asset(audio)
            manifest["derived"]["audio"] = {
                "normalized": _relative(audio_for_asr, self.output_dir),
                "probe": probe_media(audio_for_asr),
            }

        if audio_for_asr:
            manifest["derived"].setdefault("audio", {})["asr_source"] = _relative(
                audio_for_asr, self.output_dir
            )
            if transcribe:
                transcription = StepAudioASRClient(logger=self.logger).transcribe_wav(
                    audio_for_asr
                )
                manifest["transcription"] = transcription
                duration_ms = probe_media(audio_for_asr).get("duration_ms") or 1
                segments = [item for item in transcription["segments"] if item["text"]]
                if not segments and transcription["text"]:
                    segments = [
                        {
                            "text": transcription["text"],
                            "start_ms": 0,
                            "end_ms": duration_ms,
                        }
                    ]
                for segment in segments[:200]:
                    start_ms = segment.get("start_ms")
                    end_ms = segment.get("end_ms")
                    self._add_evidence(
                        source_type="audio",
                        source_ref=audio_for_asr.name,
                        claim=segment["text"],
                        locator={
                            "start_ms": int(start_ms) if start_ms is not None else 0,
                            "end_ms": int(end_ms) if end_ms is not None else duration_ms,
                        },
                        classification="SOURCE_FACT",
                        relevance=0.75,
                        confidence=0.75,
                        review_status="UNREVIEWED",
                    )
            else:
                manifest["transcription"] = {"status": "SKIPPED"}

        _write_json(self.output_dir / "evidence_candidates.json", self.evidence)
        manifest["evidence_candidate_count"] = len(self.evidence)
        if plan_sop:
            sop = SOPAgent(StepPlanClient(logger=self.logger)).plan(
                self.evidence,
                case_id=case_id,
                title=title,
            )
            planned_path = self.output_dir / "planned_sop.json"
            _write_json(planned_path, sop)
            manifest["planned_sop"] = _relative(planned_path, self.output_dir)
        _write_json(self.output_dir / "manifest.json", manifest)
        self.logger.emit(
            "ingest.completed",
            evidence_candidate_count=len(self.evidence),
            synthetic=synthetic,
        )
        return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frame-interval", type=float, default=5.0)
    parser.add_argument("--asr", action="store_true")
    parser.add_argument("--vision", action="store_true")
    parser.add_argument("--plan-sop", action="store_true")
    parser.add_argument("--external-processing-authorized", action="store_true")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--case-id", default="UPLOADED-CASE")
    parser.add_argument("--title", default="上传素材生成的 SOP 草稿")
    args = parser.parse_args()
    manifest = IngestionPipeline(
        args.output, frame_interval_seconds=args.frame_interval
    ).run(
        video=args.video,
        pdf=args.pdf,
        audio=args.audio,
        transcribe=args.asr,
        analyze_visuals=args.vision,
        plan_sop=args.plan_sop,
        external_processing_authorized=args.external_processing_authorized,
        synthetic=args.synthetic,
        case_id=args.case_id,
        title=args.title,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
