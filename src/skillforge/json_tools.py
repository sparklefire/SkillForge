"""Conservative JSON extraction and repair for model responses."""

from __future__ import annotations

import json
import re
from typing import Any

from .contracts import validate_document


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")


def _slice_json(text: str) -> str:
    positions = [position for position in (text.find("{"), text.find("[")) if position >= 0]
    if not positions:
        return text
    start = min(positions)
    end = max(text.rfind("}"), text.rfind("]"))
    return text[start : end + 1] if end >= start else text[start:]


def parse_json_response(raw: str | dict[str, Any] | list[Any]) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        raise TypeError(f"模型输出类型不支持: {type(raw).__name__}")

    candidate = raw.strip().lstrip("\ufeff")
    fenced = _FENCE.match(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    attempts = [candidate]
    sliced = _slice_json(candidate)
    if sliced != candidate:
        attempts.append(sliced)
    repaired = _TRAILING_COMMA.sub(r"\1", sliced)
    if repaired not in attempts:
        attempts.append(repaired)

    last_error: json.JSONDecodeError | None = None
    for attempt in attempts:
        try:
            return json.loads(attempt)
        except json.JSONDecodeError as exc:
            last_error = exc
    assert last_error is not None
    raise ValueError(f"无法解析模型 JSON: line={last_error.lineno} column={last_error.colno}") from last_error


def parse_and_validate(raw: str | dict[str, Any] | list[Any], schema_name: str) -> Any:
    return validate_document(parse_json_response(raw), schema_name)
