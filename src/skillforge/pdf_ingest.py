"""Page-preserving PDF extraction, structural chunking, OCR gates and search."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import fitz


OCRMode = Literal["disabled", "auto", "required"]

_WARNING_MARKERS = (
    "警告",
    "注意",
    "危险",
    "小心",
    "warning",
    "caution",
    "danger",
    "notice",
)
_CAPTION_PATTERN = re.compile(
    r"^(?:图|表|figure|fig\.?|table)\s*[0-9一二三四五六七八九十.-]+",
    re.IGNORECASE,
)
_LIST_PATTERN = re.compile(
    r"^(?:[-•●▪◦]|\(?[0-9一二三四五六七八九十]+[.)、）])\s*"
)
_LATIN_TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]+")


class OCRRequiredError(RuntimeError):
    """Raised when a page fails the text quality gate and OCR cannot repair it."""


def _clean_text(value: str) -> str:
    return "\n".join(
        line.strip() for line in value.replace("\x00", "").splitlines() if line.strip()
    )


def _bbox(value: Any) -> list[float]:
    return [round(float(item), 2) for item in value]


def _page_dictionary(page: fitz.Page, textpage: fitz.TextPage | None = None) -> dict:
    return page.get_text("dict", sort=True, textpage=textpage)


def _text_blocks(
    page: fitz.Page,
    *,
    textpage: fitz.TextPage | None = None,
) -> list[dict[str, Any]]:
    page_dict = _page_dictionary(page, textpage)
    candidates: list[dict[str, Any]] = []
    all_font_sizes: list[float] = []
    for raw_block in page_dict.get("blocks", []):
        if raw_block.get("type") != 0:
            continue
        lines: list[str] = []
        sizes: list[float] = []
        for line in raw_block.get("lines", []):
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            if text.strip():
                lines.append(text.strip())
            for span in line.get("spans", []):
                if span.get("text", "").strip():
                    size = float(span.get("size", 0.0))
                    sizes.append(size)
                    all_font_sizes.append(size)
        text = _clean_text("\n".join(lines))
        if text:
            candidates.append(
                {
                    "text": text,
                    "bbox": _bbox(raw_block.get("bbox", (0, 0, 0, 0))),
                    "max_font_size": round(max(sizes, default=0.0), 2),
                }
            )

    median_font_size = 0.0
    if all_font_sizes:
        ordered = sorted(all_font_sizes)
        median_font_size = ordered[len(ordered) // 2]

    for item in candidates:
        normalized = item["text"].strip()
        lower = normalized.casefold()
        line_count = normalized.count("\n") + 1
        if any(marker in lower for marker in _WARNING_MARKERS):
            kind = "WARNING"
        elif _CAPTION_PATTERN.match(normalized):
            kind = "CAPTION"
        elif _LIST_PATTERN.match(normalized):
            kind = "LIST_ITEM"
        elif (
            median_font_size > 0
            and item["max_font_size"] >= median_font_size * 1.2
            and line_count <= 3
            and len(normalized) <= 160
        ):
            kind = "HEADING"
        else:
            kind = "PARAGRAPH"
        item["kind"] = kind
        item["character_count"] = len(normalized)
    return candidates


def _table_blocks(page: fitz.Page) -> list[dict[str, Any]]:
    try:
        finder = page.find_tables()
    except (AttributeError, RuntimeError, ValueError):
        return []
    result: list[dict[str, Any]] = []
    for table in getattr(finder, "tables", []):
        rows = table.extract()
        rendered_rows = []
        for row in rows:
            rendered = " | ".join(_clean_text(str(cell or "")) for cell in row)
            if rendered.strip(" |"):
                rendered_rows.append(rendered)
        text = _clean_text("\n".join(rendered_rows))
        if not text:
            continue
        result.append(
            {
                "kind": "TABLE",
                "text": text,
                "bbox": _bbox(table.bbox),
                "max_font_size": 0.0,
                "character_count": len(text),
            }
        )
    return result


def _structure_page(
    page: fitz.Page,
    page_number: int,
    *,
    textpage: fitz.TextPage | None = None,
) -> list[dict[str, Any]]:
    blocks = _text_blocks(page, textpage=textpage) + _table_blocks(page)
    blocks.sort(key=lambda item: (item["bbox"][1], item["bbox"][0], item["kind"]))
    plain_text = page.get_text("text", sort=True, textpage=textpage)
    plain_compact = re.sub(r"\s+", "", plain_text)
    block_compact = re.sub(
        r"\s+", "", "".join(item["text"] for item in blocks if item["kind"] != "TABLE")
    )
    if plain_compact and len(block_compact) < len(plain_compact) * 0.75:
        existing = {re.sub(r"\s+", "", item["text"]) for item in blocks}
        for segment in re.split(r"\n\s*\n+", plain_text):
            text = _clean_text(segment)
            compact = re.sub(r"\s+", "", text)
            if not compact or compact in existing:
                continue
            lower = text.casefold()
            if any(marker in lower for marker in _WARNING_MARKERS):
                kind = "WARNING"
            elif _CAPTION_PATTERN.match(text):
                kind = "CAPTION"
            elif _LIST_PATTERN.match(text):
                kind = "LIST_ITEM"
            else:
                kind = "PARAGRAPH"
            blocks.append(
                {
                    "kind": kind,
                    "text": text,
                    "bbox": _bbox(page.rect),
                    "max_font_size": 0.0,
                    "character_count": len(text),
                }
            )
            existing.add(compact)
    structured: list[dict[str, Any]] = []
    for index, block in enumerate(blocks, start=1):
        structured.append(
            {
                "block_id": f"P{page_number:04d}-B{index:03d}",
                "page": page_number,
                **block,
            }
        )
    return structured


def _quality(
    text: str,
    image_count: int,
    image_coverage_ratio: float,
) -> dict[str, Any]:
    compact = "".join(text.split())
    replacement_count = text.count("�")
    replacement_ratio = replacement_count / max(1, len(compact))
    reasons: list[str] = []
    if compact and len(compact) < 20 and image_coverage_ratio >= 0.02:
        reasons.append("TOO_LITTLE_TEXT")
    if replacement_ratio > 0.02:
        reasons.append("HIGH_REPLACEMENT_RATIO")
    if not compact and image_count and image_coverage_ratio >= 0.02:
        reasons.append("TOO_LITTLE_TEXT")
        reasons.append("IMAGE_ONLY_PAGE")
    score = 1.0
    if "TOO_LITTLE_TEXT" in reasons:
        score -= 0.55
    if "HIGH_REPLACEMENT_RATIO" in reasons:
        score -= 0.35
    if "IMAGE_ONLY_PAGE" in reasons:
        score -= 0.1
    return {
        "text_quality_score": round(max(0.0, score), 3),
        "replacement_character_count": replacement_count,
        "replacement_character_ratio": round(replacement_ratio, 6),
        "image_count": image_count,
        "image_coverage_ratio": round(image_coverage_ratio, 6),
        "needs_ocr": bool(reasons),
        "needs_ocr_reasons": reasons,
    }


def _image_coverage_ratio(page: fitz.Page) -> float:
    page_area = max(1.0, float(page.rect.width * page.rect.height))
    area = 0.0
    for image in page.get_images(full=True):
        xref = int(image[0])
        try:
            rectangles = page.get_image_rects(xref)
        except (RuntimeError, ValueError):
            continue
        for rectangle in rectangles:
            clipped = rectangle & page.rect
            if not clipped.is_empty:
                area += float(clipped.width * clipped.height)
    return min(1.0, area / page_area)


def _run_pymupdf_ocr(
    page: fitz.Page,
    *,
    languages: str,
    dpi: int,
    tessdata: Path | None,
) -> fitz.TextPage:
    return page.get_textpage_ocr(
        language=languages,
        dpi=dpi,
        full=True,
        tessdata=str(tessdata) if tessdata else None,
    )


def extract_pdf(
    source: Path,
    destination_dir: Path,
    *,
    render_scale: float = 1.25,
    ocr_mode: OCRMode = "disabled",
    ocr_languages: str = "chi_sim+eng",
    ocr_dpi: int = 200,
    ocr_tessdata: Path | None = None,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if ocr_mode not in {"disabled", "auto", "required"}:
        raise ValueError("ocr_mode 必须是 disabled、auto 或 required")
    if not 72 <= ocr_dpi <= 600:
        raise ValueError("ocr_dpi 必须在72至600之间")
    if ocr_tessdata is not None:
        ocr_tessdata = ocr_tessdata.expanduser().resolve()
        if not ocr_tessdata.is_dir():
            raise FileNotFoundError(ocr_tessdata)
    destination_dir.mkdir(parents=True, exist_ok=True)
    pages: list[dict[str, Any]] = []
    with fitz.open(source) as document:
        for index, page in enumerate(document, start=1):
            textpage: fitz.TextPage | None = None
            text = page.get_text("text", sort=True).strip()
            image_count = len(page.get_images(full=True))
            image_coverage_ratio = _image_coverage_ratio(page)
            direct_quality = _quality(text, image_count, image_coverage_ratio)
            ocr_status = "NOT_NEEDED"
            ocr_error_type: str | None = None
            if direct_quality["needs_ocr"]:
                if ocr_mode == "disabled":
                    ocr_status = "DISABLED"
                else:
                    try:
                        textpage = _run_pymupdf_ocr(
                            page,
                            languages=ocr_languages,
                            dpi=ocr_dpi,
                            tessdata=ocr_tessdata,
                        )
                        ocr_text = page.get_text(
                            "text", sort=True, textpage=textpage
                        ).strip()
                        if len("".join(ocr_text.split())) > len("".join(text.split())):
                            text = ocr_text
                        ocr_status = "APPLIED"
                    except (RuntimeError, ValueError, OSError) as exc:
                        ocr_error_type = type(exc).__name__
                        ocr_status = "UNAVAILABLE"
                        if ocr_mode == "required":
                            raise OCRRequiredError(
                                f"PDF第{index}页需要OCR，但OCR不可用"
                            ) from exc
            final_quality = _quality(text, image_count, image_coverage_ratio)
            if ocr_mode == "required" and final_quality["needs_ocr"]:
                raise OCRRequiredError(f"PDF第{index}页OCR后仍未通过文本质量门禁")
            preview = destination_dir / f"page_{index:04d}.png"
            page.get_pixmap(
                matrix=fitz.Matrix(render_scale, render_scale), alpha=False
            ).save(preview)
            blocks = _structure_page(page, index, textpage=textpage)
            pages.append(
                {
                    "page": index,
                    "text": text,
                    "character_count": len(text),
                    "needs_ocr": final_quality["needs_ocr"],
                    "needs_ocr_reasons": final_quality["needs_ocr_reasons"],
                    "text_quality_score": final_quality["text_quality_score"],
                    "replacement_character_count": final_quality[
                        "replacement_character_count"
                    ],
                    "replacement_character_ratio": final_quality[
                        "replacement_character_ratio"
                    ],
                    "image_count": image_count,
                    "image_coverage_ratio": final_quality["image_coverage_ratio"],
                    "ocr_status": ocr_status,
                    "ocr_error_type": ocr_error_type,
                    "preview": preview,
                    "blocks": blocks,
                    "block_count": len(blocks),
                    "block_counts_by_kind": dict(
                        sorted(Counter(item["kind"] for item in blocks).items())
                    ),
                }
            )
    block_counts = Counter(
        block["kind"] for page in pages for block in page["blocks"]
    )
    return {
        "page_count": len(pages),
        "character_count": sum(page["character_count"] for page in pages),
        "block_count": sum(page["block_count"] for page in pages),
        "block_counts_by_kind": dict(sorted(block_counts.items())),
        "needs_ocr_page_count": sum(page["needs_ocr"] for page in pages),
        "ocr_applied_page_count": sum(
            page["ocr_status"] == "APPLIED" for page in pages
        ),
        "pages": pages,
    }


def _tokens(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens = set(_LATIN_TOKEN_PATTERN.findall(normalized))
    for sequence in _CJK_PATTERN.findall(normalized):
        tokens.update(sequence)
        if len(sequence) > 1:
            tokens.update(
                sequence[index : index + 2]
                for index in range(len(sequence) - 1)
            )
    return sorted(token for token in tokens if token)


def build_pdf_search_index(
    source_ref: str,
    extracted: dict[str, Any],
) -> dict[str, Any]:
    chunks: list[dict[str, Any]] = []
    postings: dict[str, list[str]] = {}
    for page in extracted["pages"]:
        for block in page["blocks"]:
            terms = _tokens(block["text"])
            chunk = {
                "chunk_id": f"{source_ref}:{block['block_id']}",
                "source_ref": source_ref,
                "page": block["page"],
                "block_id": block["block_id"],
                "kind": block["kind"],
                "text": block["text"],
                "terms": terms,
            }
            chunks.append(chunk)
            for term in terms:
                postings.setdefault(term, []).append(chunk["chunk_id"])
    return {
        "version": 1,
        "source_ref": source_ref,
        "page_count": extracted["page_count"],
        "chunk_count": len(chunks),
        "chunks": chunks,
        "postings": dict(sorted(postings.items())),
    }


def search_pdf_index(
    index: dict[str, Any],
    query: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    if not query.strip():
        raise ValueError("检索词不能为空")
    if not 1 <= limit <= 50:
        raise ValueError("limit 必须在1至50之间")
    query_terms = set(_tokens(query))
    normalized_query = re.sub(
        r"\s+", "", unicodedata.normalize("NFKC", query).casefold()
    )
    results: list[dict[str, Any]] = []
    for chunk in index["chunks"]:
        terms = set(chunk["terms"])
        overlap = query_terms & terms
        if not overlap:
            continue
        normalized_text = re.sub(
            r"\s+", "", unicodedata.normalize("NFKC", chunk["text"]).casefold()
        )
        exact_bonus = 10.0 if normalized_query in normalized_text else 0.0
        coverage = len(overlap) / max(1, len(query_terms))
        kind_bonus = 0.5 if chunk["kind"] in {"HEADING", "WARNING"} else 0.0
        results.append(
            {
                "chunk_id": chunk["chunk_id"],
                "source_ref": chunk["source_ref"],
                "page": chunk["page"],
                "block_id": chunk["block_id"],
                "kind": chunk["kind"],
                "score": round(exact_bonus + coverage * 5.0 + kind_bonus, 3),
                "exact_match": bool(exact_bonus),
                "matched_terms": sorted(overlap),
                "text": chunk["text"],
            }
        )
    results.sort(
        key=lambda item: (-item["score"], item["page"], item["block_id"])
    )
    return results[:limit]
