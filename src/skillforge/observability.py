"""Structured JSONL logging with mandatory credential redaction."""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_SECRET_KEYS = {
    "authorization",
    "api_key",
    "step_api_key",
    "access_token",
    "refresh_token",
    "secret",
    "password",
}
_BEARER = re.compile(r"(?i)Bearer\s+[^\s,;\"']+")
_KEY_LIKE = re.compile(r"(?i)(?:sk|step)[-_][A-Za-z0-9_-]{12,}")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in _SECRET_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _KEY_LIKE.sub("[REDACTED]", _BEARER.sub("Bearer [REDACTED]", value))
    return value


class StructuredLogger:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        record = redact(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
                **fields,
            }
        )
        if self.path:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            os.chmod(self.path, 0o600)
        return record
