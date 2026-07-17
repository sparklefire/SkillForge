import json
from pathlib import Path

import pytest

from skillforge.contracts import validate_document
from skillforge.semantic_review import (
    DIMENSIONS,
    _validate_response,
    build_safe_review_payload,
    run_semantic_review,
)
from skillforge.observability import StructuredLogger


ROOT = Path(__file__).resolve().parents[1]
GOLD_PATH = ROOT / "cases/n31/gold/gold_sop.json"
CONSTRAINTS_PATH = ROOT / "cases/n31/gold/constraints.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _valid_response(gold: dict) -> dict:
    return {
        "case_id": gold["case_id"],
        "assessments": [
            {
                "step_id": step["step_id"],
                "verdict": "SUPPORTED",
                "reviewed_dimensions": DIMENSIONS,
                "evidence_ids": [step["evidence"][0]],
                "rationale": "给定Evidence与步骤表述、依赖和异常边界一致。",
                "risk_notes": [],
                "confidence": 0.88,
            }
            for step in gold["steps"]
        ],
        "findings": [],
    }


class FakeRouter:
    def reasoning(self, route: str) -> dict[str, str]:
        assert route == "verifier"
        return {"model": "step-3.7-flash", "reasoning_effort": "high"}


class FakeSemanticClient:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.router = FakeRouter()
        self.call_count = 0
        self.messages = []

    def chat_json(self, **kwargs):
        assert kwargs["route"] == "verifier"
        assert kwargs["schema_name"] == "semantic_review_response.schema.json"
        assert kwargs["max_tokens"] == 16_384
        self.messages.append(kwargs["messages"])
        self.call_count += 1
        return self.responses.pop(0)


def test_safe_payload_contains_only_structured_evidence_projection() -> None:
    payload = build_safe_review_payload(_read(GOLD_PATH), _read(CONSTRAINTS_PATH))
    encoded = json.dumps(payload, ensure_ascii=False)

    assert len(payload["steps"]) == 13
    assert payload["review_dimensions"] == DIMENSIONS
    assert payload["evidence"]
    assert all("keyframe" not in item["locator"] for item in payload["evidence"])
    assert "/Users/" not in encoded
    assert "/home/" not in encoded
    assert "Authorization:" not in encoded
    assert "Bearer " not in encoded


def test_semantic_review_covers_all_steps_without_changing_gold(tmp_path) -> None:
    gold = _read(GOLD_PATH)
    client = FakeSemanticClient([_valid_response(gold)])
    output = tmp_path / "semantic_review.json"

    report = run_semantic_review(
        GOLD_PATH,
        CONSTRAINTS_PATH,
        output,
        client=client,
        logger=StructuredLogger(tmp_path / "semantic_review.jsonl"),
    )

    assert output.is_file()
    assert validate_document(_read(output), "semantic_review_report.schema.json") == report
    assert report["status"] == "COMPLETED"
    assert report["model"] == "step-3.7-flash"
    assert report["reasoning_effort"] == "high"
    assert report["model_calls"] == 1
    assert report["summary"]["supported_count"] == 13
    assert report["summary"]["automatic_gold_changes"] == 0
    assert report["guardrails"]["may_override_gold"] is False
    assert report["review_scope"]["raw_media_sent"] is False


def test_unknown_evidence_triggers_one_bounded_semantic_retry(tmp_path) -> None:
    gold = _read(GOLD_PATH)
    invalid = _valid_response(gold)
    invalid["assessments"][0]["evidence_ids"] = ["E999"]
    client = FakeSemanticClient([invalid, _valid_response(gold)])

    report = run_semantic_review(
        GOLD_PATH,
        CONSTRAINTS_PATH,
        tmp_path / "semantic_review.json",
        client=client,
        logger=StructuredLogger(tmp_path / "semantic_review.jsonl"),
    )

    assert report["model_calls"] == 2
    assert client.call_count == 2
    assert "Evidence边界校验" in client.messages[1][-1]["content"]


def test_source_conflict_requires_two_independent_sources() -> None:
    gold = _read(GOLD_PATH)
    response = _valid_response(gold)
    step = gold["steps"][0]
    response["findings"] = [
        {
            "finding_id": "SF001",
            "kind": "SOURCE_CONFLICT",
            "severity": "HIGH",
            "step_ids": [step["step_id"]],
            "evidence_ids": [step["evidence"][0]],
            "description": "单一来源不能证明来源冲突。",
            "recommended_action": "HUMAN_REVIEW",
            "automatic": False,
        }
    ]

    with pytest.raises(ValueError, match="至少两个独立来源"):
        _validate_response(response, gold)
