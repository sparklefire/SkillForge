from __future__ import annotations

import json
from pathlib import Path

import fitz
import pytest
from PIL import Image

import skillforge.pdf_ingest as pdf_ingest
from skillforge.contracts import validate_document
from skillforge.pdf_ingest import (
    OCRRequiredError,
    build_pdf_search_index,
    extract_pdf,
    search_pdf_index,
)
from skillforge.pdf_structure_report import build_pdf_structure_report


def _structured_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Media Installation", fontsize=18)
    page.insert_text((72, 112), "WARNING: Disconnect power before service.", fontsize=12)
    page.insert_text((72, 148), "1. Adjust the paper guide.", fontsize=12)
    page.insert_text((72, 184), "Run gap label learning before the print test.", fontsize=12)
    page.insert_text((72, 220), "Figure 1. Paper path", fontsize=10)
    document.save(path)
    document.close()


def test_pdf_structure_preserves_pages_and_classifies_blocks(tmp_path: Path) -> None:
    source = tmp_path / "manual.pdf"
    _structured_pdf(source)
    result = extract_pdf(source, tmp_path / "pages")
    assert result["page_count"] == 1
    assert result["needs_ocr_page_count"] == 0
    assert result["block_count"] >= 5
    kinds = {item["kind"] for item in result["pages"][0]["blocks"]}
    assert {"HEADING", "WARNING", "LIST_ITEM", "CAPTION"}.issubset(kinds)
    assert result["pages"][0]["text_quality_score"] == 1.0
    assert (tmp_path / "pages/page_0001.png").is_file()


def test_pdf_search_index_returns_exact_page_and_never_changes_locator(
    tmp_path: Path,
) -> None:
    source = tmp_path / "manual.pdf"
    _structured_pdf(source)
    extracted = extract_pdf(source, tmp_path / "pages")
    index = build_pdf_search_index("MANUAL", extracted)
    results = search_pdf_index(index, "gap label learning")
    assert index["page_count"] == 1
    assert index["chunk_count"] == extracted["block_count"]
    assert results[0]["source_ref"] == "MANUAL"
    assert results[0]["page"] == 1
    assert results[0]["exact_match"] is True
    assert results[0]["block_id"].startswith("P0001-B")


def test_pdf_ocr_quality_gate_flags_image_only_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = tmp_path / "scan.png"
    Image.new("RGB", (200, 100), "white").save(image_path)
    source = tmp_path / "scan.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_image(fitz.Rect(72, 72, 272, 172), filename=str(image_path))
    document.save(source)
    document.close()

    disabled = extract_pdf(source, tmp_path / "disabled", ocr_mode="disabled")
    page_result = disabled["pages"][0]
    assert page_result["needs_ocr"] is True
    assert page_result["ocr_status"] == "DISABLED"
    assert "IMAGE_ONLY_PAGE" in page_result["needs_ocr_reasons"]

    def unavailable(*args, **kwargs):
        raise RuntimeError("OCR unavailable")

    monkeypatch.setattr(pdf_ingest, "_run_pymupdf_ocr", unavailable)
    with pytest.raises(OCRRequiredError, match="需要OCR"):
        extract_pdf(source, tmp_path / "required", ocr_mode="required")


def test_pdf_structure_report_is_text_free_and_schema_valid(tmp_path: Path) -> None:
    source = tmp_path / "manual.pdf"
    _structured_pdf(source)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "case_id": "pdf_case",
                "title": "PDF case",
                "external_processing_authorized": False,
                "sources": [
                    {
                        "source_id": "MANUAL",
                        "type": "pdf",
                        "path": "manual.pdf",
                        "approved_for_local_ingest": True,
                        "rights_status": "LOCAL_EVIDENCE_USE_ONLY",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    queries = tmp_path / "queries.json"
    queries.write_text(
        json.dumps(
            {
                "version": 1,
                "case_id": "pdf_case",
                "queries": [
                    {"query_id": "Q01", "query": "gap label learning"}
                ],
            }
        ),
        encoding="utf-8",
    )
    report = build_pdf_structure_report(
        manifest,
        queries,
        tmp_path / "private",
        project_root=tmp_path,
    )
    validate_document(report, "pdf_structure_report.schema.json")
    assert report["status"] == "COMPLETED"
    assert report["summary"]["passed_query_count"] == 1
    assert report["queries"][0]["top_hits"][0]["page"] == 1
    serialized = json.dumps(report, ensure_ascii=False)
    assert "Disconnect power" not in serialized
    assert str(tmp_path) not in serialized
    assert (tmp_path / "private/MANUAL/search_index.json").is_file()
