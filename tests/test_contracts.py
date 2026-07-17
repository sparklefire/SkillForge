import json
from pathlib import Path

import pytest

import skillforge.contracts as contracts
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


def test_schema_registry_ignores_macos_appledouble_files(
    tmp_path, monkeypatch
) -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://skillforge.local/schemas/test.schema.json",
        "type": "object",
    }
    (tmp_path / "test.schema.json").write_text(json.dumps(schema), encoding="utf-8")
    (tmp_path / "._test.schema.json").write_bytes(b"\x00\x05\x16\x07Mac OS X")
    contracts.schema_registry.cache_clear()
    monkeypatch.setattr(contracts, "SCHEMA_DIR", tmp_path)
    try:
        registry = contracts.schema_registry()
        assert registry.get(schema["$id"]) is not None
    finally:
        contracts.schema_registry.cache_clear()
