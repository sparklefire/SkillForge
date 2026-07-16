import json
import subprocess
from pathlib import Path

import fitz
import pytest

from skillforge.case_ingest import CaseIngestionPipeline
from skillforge.contracts import validate_document
from skillforge.media import resolve_ffmpeg


def _make_video(path: Path, color: str) -> None:
    subprocess.run(
        [
            str(resolve_ffmpeg()),
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color={color}:size=320x180:rate=10",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_pdf(path: Path, text: str) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def test_case_ingest_multiple_videos_and_pdfs(tmp_path) -> None:
    _make_video(tmp_path / "main.mp4", "white")
    _make_video(tmp_path / "detail.mp4", "blue")
    _make_pdf(tmp_path / "manual.pdf", "Adjust the paper guides before feeding media.")
    _make_pdf(tmp_path / "guide.pdf", "Run gap label learning before the print test.")
    manifest_path = tmp_path / "case.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "case_id": "test_case",
                "title": "Test case",
                "external_processing_authorized": False,
                "frame_interval_seconds": 1,
                "sources": [
                    {
                        "source_id": "VIDEO_MAIN",
                        "type": "video",
                        "role": "MAIN",
                        "path": "main.mp4",
                        "approved_for_local_ingest": True,
                    },
                    {
                        "source_id": "VIDEO_DETAIL",
                        "type": "video",
                        "role": "DETAIL",
                        "path": "detail.mp4",
                        "approved_for_local_ingest": True,
                    },
                    {
                        "source_id": "PDF_MANUAL",
                        "type": "pdf",
                        "role": "MANUAL",
                        "path": "manual.pdf",
                        "approved_for_local_ingest": True,
                    },
                    {
                        "source_id": "PDF_GUIDE",
                        "type": "pdf",
                        "role": "GUIDE",
                        "path": "guide.pdf",
                        "approved_for_local_ingest": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "output"
    result = CaseIngestionPipeline(tmp_path, output).run(manifest_path)
    assert result["status"] == "INGESTED_LOCAL_ONLY"
    assert result["source_count"] == 4
    assert result["model_calls"] == 0
    assert result["counts_by_type"] == {"pdf": 2, "video": 4}
    catalog = json.loads(
        (output / "evidence_catalog.json").read_text(encoding="utf-8")
    )
    assert catalog["evidence_count"] == 6
    assert len({item["evidence_id"] for item in catalog["evidence"]}) == 6
    for item in catalog["evidence"]:
        validate_document(item, "evidence.schema.json")


def test_case_ingest_rejects_private_review_source(tmp_path) -> None:
    manifest_path = tmp_path / "case.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "case_id": "test_case",
                "title": "Test case",
                "external_processing_authorized": False,
                "sources": [
                    {
                        "source_id": "PRIVATE",
                        "type": "video",
                        "path": "source_private_review.mp4",
                        "approved_for_local_ingest": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="禁止摄取 private_review"):
        CaseIngestionPipeline(tmp_path, tmp_path / "output").run(manifest_path)


def test_case_ingest_requires_explicit_local_only_flag(tmp_path) -> None:
    manifest_path = tmp_path / "case.json"
    manifest_path.write_text(
        json.dumps(
            {
                "version": 1,
                "case_id": "test_case",
                "title": "Test case",
                "external_processing_authorized": True,
                "sources": [
                    {
                        "source_id": "PDF",
                        "type": "pdf",
                        "path": "manual.pdf",
                        "approved_for_local_ingest": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="external_processing_authorized=false"):
        CaseIngestionPipeline(tmp_path, tmp_path / "output").run(manifest_path)
