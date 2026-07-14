import json
from pathlib import Path

import pytest

from skillforge.contracts import ContractValidationError, validate_document


ROOT = Path(__file__).resolve().parents[1]


def test_reference_sop_matches_schema() -> None:
    document = json.loads(
        (ROOT / "cases/demo_case/synthetic/reference_sop.json").read_text(
            encoding="utf-8"
        )
    )
    assert validate_document(document, "sop.schema.json") is document


def test_invalid_sop_is_rejected() -> None:
    with pytest.raises(ContractValidationError, match="steps"):
        validate_document(
            {
                "case_id": "invalid",
                "title": "invalid",
                "version": 1,
                "evidence_catalog": [],
                "steps": [],
            },
            "sop.schema.json",
        )
