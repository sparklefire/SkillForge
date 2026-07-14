from skillforge.step_plan import StepPlanClient


def test_invalid_json_is_retried_and_validated() -> None:
    calls = []
    valid_evidence = {
        "evidence_id": "E001",
        "source_type": "pdf",
        "source_ref": "synthetic.pdf",
        "claim": "模拟事实",
        "locator": {"page": 1},
        "classification": "SOURCE_FACT",
        "relevance": 1.0,
        "confidence": 1.0,
        "review_status": "VERIFIED",
    }

    def transport(payload):
        calls.append(payload)
        content = "not json" if len(calls) == 1 else valid_evidence
        return {"model": "fake", "choices": [{"message": {"content": content}}]}

    client = StepPlanClient(transport=transport)
    result = client.chat_json(
        messages=[{"role": "user", "content": "return evidence"}],
        route="fast_extractor",
        schema_name="evidence.schema.json",
        max_attempts=2,
    )
    assert result == valid_evidence
    assert len(calls) == 2
    assert "JSON Schema" in calls[1]["messages"][-1]["content"]
