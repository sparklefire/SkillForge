#!/usr/bin/env python3
"""Build a Git-safe, asset-free N31 Web demo fallback bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from skillforge.contracts import validate_document


REQUIRED = (
    "summary",
    "before_sop",
    "after_sop",
    "initial_conflicts",
    "final_conflicts",
    "revision_audit",
)
OPTIONAL = (
    "sop_views",
    "checklist",
    "quiz",
    "workflow",
    "grounding_gate",
    "semantic_review",
    "selective_rebuild",
)
OPTIONAL_SCHEMAS = {
    "sop_views": "sop_views.schema.json",
    "checklist": "mobile_checklist.schema.json",
    "quiz": "training_quiz.schema.json",
    "grounding_gate": "grounding_gate_report.schema.json",
    "semantic_review": "semantic_review_report.schema.json",
    "selective_rebuild": "selective_rebuild_report.schema.json",
}


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compact_sop(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": payload["case_id"],
        "title": payload["title"],
        "version": payload["version"],
        "steps": payload["steps"],
    }


def _validate_optional(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    schema = OPTIONAL_SCHEMAS.get(name)
    if schema is None:
        return payload
    document = validate_document(payload, schema)
    if name == "grounding_gate" and document["status"] != "PASSED":
        raise ValueError("只允许发布通过复检的确定性门禁报告")
    if name == "semantic_review" and document["summary"]["automatic_gold_changes"] != 0:
        raise ValueError("语义复核不得自动修改Gold")
    if name == "selective_rebuild" and (
        document["status"] != "PASSED"
        or not all(document["verification"].values())
    ):
        raise ValueError("只允许发布通过边界验证的选择性重建报告")
    return document


def build_bundle(
    source: Path,
    output: Path,
    *,
    grounding_gate: Path | None = None,
    semantic_review: Path | None = None,
    selective_rebuild: Path | None = None,
) -> dict[str, Any]:
    missing = [name for name in REQUIRED if not (source / f"{name}.json").is_file()]
    if missing:
        raise FileNotFoundError(f"缺少Gold演示输出: {missing}")
    output.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED + OPTIONAL:
        path = source / f"{name}.json"
        if not path.is_file():
            continue
        payload = _read(path)
        if name in {"before_sop", "after_sop"}:
            payload = _compact_sop(payload)
        elif name in OPTIONAL_SCHEMAS:
            payload = _validate_optional(name, payload)
        _write(output / f"{name}.json", payload)
    if grounding_gate is not None:
        gate = _validate_optional("grounding_gate", _read(grounding_gate))
        _write(output / "grounding_gate.json", gate)
    if semantic_review is not None:
        review = _validate_optional("semantic_review", _read(semantic_review))
        _write(output / "semantic_review.json", review)
    if selective_rebuild is not None:
        rebuild = _validate_optional("selective_rebuild", _read(selective_rebuild))
        _write(output / "selective_rebuild.json", rebuild)
    summary = _read(output / "summary.json")
    if (
        summary.get("gold_status") != "GOLD"
        or summary.get("metrics_status") != "FINAL"
        or summary.get("after", {}).get("severe_error_count") != 0
    ):
        raise ValueError("只允许发布通过复检的Gold最终结果")
    files = sorted(path for path in output.glob("*.json") if path.name != "bundle.json")
    manifest = {
        "version": 1,
        "case_id": summary["case_id"],
        "bundle_type": "ASSET_FREE_WEB_FALLBACK",
        "gold_status": summary["gold_status"],
        "metrics_status": summary["metrics_status"],
        "contains_raw_media": False,
        "contains_credentials": False,
        "files": [
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in files
        ],
    }
    _write(output / "bundle.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("cases/n31/output/gold_rehearsal_v1"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("cases/n31/demo_bundle"),
    )
    parser.add_argument(
        "--grounding-gate",
        type=Path,
        default=Path("cases/n31/evaluations/deterministic_grounding_gate_v1.json"),
    )
    parser.add_argument(
        "--semantic-review",
        type=Path,
        default=Path("cases/n31/evaluations/semantic_review_v1.json"),
    )
    parser.add_argument(
        "--selective-rebuild",
        type=Path,
        default=Path("cases/n31/evaluations/selective_rebuild_v1.json"),
    )
    args = parser.parse_args()
    print(
        json.dumps(
            build_bundle(
                args.source,
                args.output,
                grounding_gate=args.grounding_gate,
                semantic_review=args.semantic_review,
                selective_rebuild=args.selective_rebuild,
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
