"""Load and validate SkillForge JSON contracts."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schemas"


class ContractValidationError(ValueError):
    """Raised when a document does not satisfy a SkillForge contract."""


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"Schema 不存在: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def schema_registry() -> Registry:
    resources = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        schema_id = contents.get("$id")
        if schema_id:
            resources.append((schema_id, Resource.from_contents(contents)))
    return Registry().with_resources(resources)


def validate_document(document: Any, schema_name: str) -> Any:
    schema = load_schema(schema_name)
    validator = Draft202012Validator(
        schema,
        registry=schema_registry(),
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.path))
    if errors:
        rendered = []
        for error in errors[:8]:
            location = "/".join(str(part) for part in error.absolute_path) or "<root>"
            rendered.append(f"{location}: {error.message}")
        raise ContractValidationError("; ".join(rendered))
    return document
