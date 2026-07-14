import json

from skillforge.observability import StructuredLogger, redact


def test_redacts_credentials_recursively(tmp_path) -> None:
    assert redact({"authorization": "Bearer secret-value"}) == {
        "authorization": "[REDACTED]"
    }
    path = tmp_path / "run.jsonl"
    StructuredLogger(path).emit(
        "test",
        api_key="step-super-secret-token",
        detail="Authorization: Bearer abcdefghijklmnop",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["api_key"] == "[REDACTED]"
    assert "abcdefghijklmnop" not in path.read_text(encoding="utf-8")
