import hashlib
from pathlib import Path

import fitz

from skillforge.poster import generate_poster


ROOT = Path(__file__).resolve().parents[1]


def test_generates_deterministic_single_page_a4_poster(tmp_path) -> None:
    gold = ROOT / "cases/n31/gold/gold_sop.json"
    first = generate_poster(gold, tmp_path / "first.pdf")
    second = generate_poster(gold, tmp_path / "second.pdf")
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(
        second.read_bytes()
    ).digest()
    document = fitz.open(first)
    assert len(document) == 1
    page = document[0]
    assert abs(page.rect.width - 595.28) < 1
    assert abs(page.rect.height - 841.89) < 1
    text = page.get_text()
    assert "N31" in text
    assert "S01" in text
    assert "S13" in text
    document.close()
