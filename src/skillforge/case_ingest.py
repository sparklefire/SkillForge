"""Manifest-driven, multi-source, local-only case ingestion."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .media import extract_keyframes, probe_media
from .observability import StructuredLogger
from .pdf_ingest import extract_pdf


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
        raise ValueError(f"案例素材必须位于项目目录内: {path}") from exc
    return resolved


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class CaseIngestionPipeline:
    """Ingest multiple approved local sources without making model calls."""

    def __init__(
        self,
        project_root: Path,
        output_dir: Path,
        *,
        logger: StructuredLogger | None = None,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.output_dir = _inside(output_dir, self.project_root)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or StructuredLogger(self.output_dir / "ingest.jsonl")
        self.evidence: list[dict[str, Any]] = []

    def _add_evidence(self, **fields: Any) -> dict[str, Any]:
        next_number = len(self.evidence) + 1
        if next_number > 999:
            raise ValueError("Evidence 数量超过当前 E001-E999 编号容量")
        evidence = {"evidence_id": f"E{next_number:03d}", **fields}
        validate_document(evidence, "evidence.schema.json")
        self.evidence.append(evidence)
        return evidence

    def _load_manifest(self, manifest_path: Path) -> dict[str, Any]:
        if not manifest_path.is_absolute():
            manifest_path = self.project_root / manifest_path
        manifest_path = _inside(manifest_path, self.project_root)
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if raw.get("version") != 1:
            raise ValueError("不支持的案例摄取清单版本")
        if raw.get("external_processing_authorized") is not False:
            raise ValueError("本地摄取清单必须显式设置 external_processing_authorized=false")
        sources = raw.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError("案例摄取清单至少需要一个来源")
        seen_ids: set[str] = set()
        for source in sources:
            source_id = str(source.get("source_id", ""))
            if not source_id or source_id in seen_ids:
                raise ValueError(f"重复或空的 source_id: {source_id!r}")
            seen_ids.add(source_id)
            if source.get("type") not in {"video", "pdf"}:
                raise ValueError(f"{source_id}: 本地摄取暂只支持 video 和 pdf")
            source_path = str(source.get("path", ""))
            if "_private_review" in Path(source_path).name:
                raise ValueError(f"{source_id}: 禁止摄取 private_review 源文件")
            if source.get("approved_for_local_ingest") is not True:
                raise ValueError(f"{source_id}: 未明确批准本地摄取")
        return raw

    def _ingest_video(
        self,
        source: dict[str, Any],
        path: Path,
        frame_interval_seconds: float,
    ) -> dict[str, Any]:
        source_id = source["source_id"]
        probe = probe_media(path)
        video_streams = probe.get("video_streams") or []
        if not video_streams:
            raise ValueError(f"{source_id}: 输入中没有视频流")
        frames = extract_keyframes(
            path,
            self.output_dir / "derived" / "video" / source_id / "frames",
            interval_seconds=frame_interval_seconds,
        )
        for item in frames:
            self._add_evidence(
                source_type="video",
                source_ref=source_id,
                claim=(
                    f"{source.get('role', 'VIDEO')} 关键帧候选；"
                    "等待授权后的视觉理解或人工确认动作、部件和设备状态。"
                ),
                locator={
                    "start_ms": item["start_ms"],
                    "end_ms": min(
                        item["end_ms"],
                        int(probe.get("duration_ms") or item["end_ms"]),
                    ),
                    "keyframe": _relative(item["path"], self.output_dir),
                },
                classification="MODEL_INFERENCE",
                relevance=float(source.get("default_relevance", 0.5)),
                confidence=0.5,
                review_status="UNREVIEWED",
            )
        return {
            "source_id": source_id,
            "type": "video",
            "role": source.get("role"),
            "path": _relative(path, self.project_root),
            "sha256": _sha256(path),
            "probe": probe,
            "frame_interval_seconds": frame_interval_seconds,
            "frame_count": len(frames),
            "privacy_status": source.get("privacy_status"),
            "display_authorization": source.get("display_authorization"),
            "external_processing_authorization": False,
        }

    def _ingest_pdf(self, source: dict[str, Any], path: Path) -> dict[str, Any]:
        source_id = source["source_id"]
        extracted = extract_pdf(
            path,
            self.output_dir / "derived" / "pdf" / source_id / "pages",
        )
        character_count = 0
        ocr_candidate_count = 0
        for page in extracted["pages"]:
            character_count += page["character_count"]
            ocr_candidate_count += int(page["needs_ocr"])
            claim = " ".join(page["text"].split())[:500]
            self._add_evidence(
                source_type="pdf",
                source_ref=source_id,
                claim=claim or f"第 {page['page']} 页未提取到足够文本，需要 OCR。",
                locator={
                    "page": page["page"],
                    "paragraph": "页面全文候选",
                },
                classification="SOURCE_FACT" if claim else "MODEL_INFERENCE",
                relevance=float(source.get("default_relevance", 0.7 if claim else 0.2)),
                confidence=0.9 if claim else 0.2,
                review_status="UNREVIEWED",
            )
        return {
            "source_id": source_id,
            "type": "pdf",
            "role": source.get("role"),
            "path": _relative(path, self.project_root),
            "sha256": _sha256(path),
            "page_count": extracted["page_count"],
            "character_count": character_count,
            "ocr_candidate_count": ocr_candidate_count,
            "rights_status": source.get("rights_status"),
            "external_processing_authorization": False,
        }

    def run(self, manifest_path: Path) -> dict[str, Any]:
        config = self._load_manifest(manifest_path)
        frame_interval_seconds = float(config.get("frame_interval_seconds", 5.0))
        if frame_interval_seconds <= 0:
            raise ValueError("frame_interval_seconds 必须大于 0")
        self.evidence.clear()
        assets: list[dict[str, Any]] = []
        self.logger.emit(
            "case_ingest.started",
            case_id=config["case_id"],
            source_count=len(config["sources"]),
            external_processing_authorized=False,
        )
        for source in config["sources"]:
            path = _inside(self.project_root / source["path"], self.project_root)
            if not path.is_file():
                raise FileNotFoundError(path)
            self.logger.emit(
                "case_ingest.source.started",
                source_id=source["source_id"],
                source_type=source["type"],
                path=_relative(path, self.project_root),
            )
            if source["type"] == "video":
                asset = self._ingest_video(source, path, frame_interval_seconds)
            else:
                asset = self._ingest_pdf(source, path)
            assets.append(asset)
            self.logger.emit(
                "case_ingest.source.completed",
                source_id=source["source_id"],
                evidence_count=sum(
                    item["source_ref"] == source["source_id"] for item in self.evidence
                ),
            )
        counts_by_type = Counter(item["source_type"] for item in self.evidence)
        counts_by_source = Counter(item["source_ref"] for item in self.evidence)
        catalog = {
            "case_id": config["case_id"],
            "title": config["title"],
            "synthetic": False,
            "external_processing_authorized": False,
            "model_calls": 0,
            "evidence_count": len(self.evidence),
            "counts_by_type": dict(sorted(counts_by_type.items())),
            "counts_by_source": dict(sorted(counts_by_source.items())),
            "evidence": self.evidence,
        }
        result = {
            "status": "INGESTED_LOCAL_ONLY",
            "case_id": config["case_id"],
            "title": config["title"],
            "synthetic": False,
            "external_processing_authorized": False,
            "model_calls": 0,
            "source_count": len(assets),
            "assets": assets,
            "evidence_catalog": "evidence_catalog.json",
            "evidence_count": len(self.evidence),
            "counts_by_type": dict(sorted(counts_by_type.items())),
            "counts_by_source": dict(sorted(counts_by_source.items())),
        }
        _write_json(self.output_dir / "evidence_catalog.json", catalog)
        _write_json(self.output_dir / "manifest.json", result)
        self.logger.emit(
            "case_ingest.completed",
            case_id=config["case_id"],
            source_count=len(assets),
            evidence_count=len(self.evidence),
            model_calls=0,
        )
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    project_root = args.project_root.expanduser().resolve()
    output = args.output if args.output.is_absolute() else project_root / args.output
    result = CaseIngestionPipeline(project_root, output).run(args.manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
