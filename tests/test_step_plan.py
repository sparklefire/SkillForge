import json
import subprocess

import pytest

from skillforge.observability import StructuredLogger
from skillforge.step_plan import (
    StepPlanClient,
    StepPlanError,
    StepPlanTransportError,
)


def _valid_evidence() -> dict:
    return {
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


def test_invalid_json_is_retried_and_validated() -> None:
    calls = []
    valid_evidence = _valid_evidence()

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
    assert client.call_count == 2
    assert "JSON Schema" in calls[1]["messages"][-1]["content"]


def test_retryable_rate_limit_is_bounded_and_preserves_messages() -> None:
    calls = []
    delays = []

    def transport(payload):
        calls.append(payload)
        if len(calls) == 1:
            raise StepPlanTransportError(
                "rate limited",
                category="RATE_LIMIT",
                retryable=True,
                status_code=429,
            )
        return {"model": "fake", "choices": [{"message": {"content": _valid_evidence()}}]}

    client = StepPlanClient(transport=transport, retry_sleep=delays.append)
    result = client.chat_json(
        messages=[{"role": "user", "content": "return evidence"}],
        route="fast_extractor",
        schema_name="evidence.schema.json",
        max_attempts=3,
    )
    assert result == _valid_evidence()
    assert len(calls) == 2
    assert calls[0]["messages"] == calls[1]["messages"]
    assert delays == [0.5]
    assert client.call_count == 2


def test_timeout_exhaustion_stops_after_three_attempts_and_logs_safely(tmp_path) -> None:
    calls = []
    delays = []
    log_path = tmp_path / "step-plan.jsonl"

    def transport(payload):
        calls.append(payload)
        raise TimeoutError("Authorization: Bearer should-never-be-logged")

    client = StepPlanClient(
        transport=transport,
        retry_sleep=delays.append,
        logger=StructuredLogger(log_path),
    )
    with pytest.raises(StepPlanError, match="TIMEOUT"):
        client.chat_json(
            messages=[{"role": "user", "content": "return evidence"}],
            route="fast_extractor",
            schema_name="evidence.schema.json",
            max_attempts=3,
        )
    assert len(calls) == 3
    assert delays == [0.5, 1.0]
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [item["event"] for item in records].count("step_plan.transport_error") == 3
    assert [item["event"] for item in records].count("step_plan.retry") == 2
    assert "should-never-be-logged" not in log_path.read_text()


def test_non_retryable_client_error_fails_without_extra_calls() -> None:
    calls = []

    def transport(payload):
        calls.append(payload)
        raise StepPlanTransportError(
            "bad request",
            category="HTTP_CLIENT_ERROR",
            retryable=False,
            status_code=400,
        )

    client = StepPlanClient(transport=transport, retry_sleep=lambda _: None)
    with pytest.raises(StepPlanError, match="HTTP_CLIENT_ERROR"):
        client.chat_json(
            messages=[{"role": "user", "content": "return evidence"}],
            route="fast_extractor",
            schema_name="evidence.schema.json",
            max_attempts=3,
        )
    assert len(calls) == 1


def test_curl_transport_classifies_429_without_exposing_response(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=22,
            stdout='{"error":"private detail"}\n429',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    client = StepPlanClient(retry_sleep=lambda _: None)
    client.api_key = "test-key-not-real"
    with pytest.raises(StepPlanTransportError) as captured:
        client._curl_transport({"model": "fake"})
    assert captured.value.category == "RATE_LIMIT"
    assert captured.value.retryable is True
    assert captured.value.status_code == 429
    assert "private detail" not in str(captured.value)
