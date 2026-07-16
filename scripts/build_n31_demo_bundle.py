#!/usr/bin/env python3
"""Build a Git-safe, asset-free N31 Web demo fallback bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


REQUIRED = (
    "summary",
    "before_sop",
    "after_sop",
    "initial_conflicts",
    "final_conflicts",
    "revision_audit",
)
OPTIONAL = ("checklist", "quiz", "workflow")


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


def build_bundle(source: Path, output: Path) -> dict[str, Any]:
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
        _write(output / f"{name}.json", payload)
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
    args = parser.parse_args()
    print(
        json.dumps(
            build_bundle(args.source, args.output),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
