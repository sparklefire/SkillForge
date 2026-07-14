"""Page-preserving native PDF extraction and preview rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz


def extract_pdf(
    source: Path,
    destination_dir: Path,
    *,
    render_scale: float = 1.25,
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    destination_dir.mkdir(parents=True, exist_ok=True)
    pages: list[dict[str, Any]] = []
    with fitz.open(source) as document:
        for index, page in enumerate(document, start=1):
            text = page.get_text("text", sort=True).strip()
            preview = destination_dir / f"page_{index:04d}.png"
            page.get_pixmap(matrix=fitz.Matrix(render_scale, render_scale), alpha=False).save(
                preview
            )
            pages.append(
                {
                    "page": index,
                    "text": text,
                    "character_count": len(text),
                    "needs_ocr": len(text) < 20,
                    "preview": preview,
                }
            )
    return {"page_count": len(pages), "pages": pages}
