import json
from pathlib import Path

import pytest

from skillforge.contracts import validate_document
from skillforge.runtime_benchmark import build_runtime_benchmark


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_benchmark_is_reproducible_and_safe(tmp_path) -> None:
    report = build_runtime_benchmark(
        root=ROOT,
        execution_location="LOCAL_DEVELOPMENT",
        accelerator="NONE",
        warmup_iterations=0,
        measured_iterations=2,
        scratch_parent=tmp_path,
    )
    validate_document(report, "runtime_benchmark.schema.json")

    assert report["status"] == "COMPLETED"
    assert [item["benchmark_id"] for item in report["benchmarks"]] == [
        "GOLD_WORKFLOW",
        "WEB_LIVE_RERUN",
    ]
    assert all(len(item["samples_ms"]) == 2 for item in report["benchmarks"])
    assert all(item["timing_ms"]["median"] > 0 for item in report["benchmarks"])
    assert report["data_policy"] == {
        "external_model_calls": 0,
        "raw_media_processed": False,
        "credentials_accessed": False,
        "network_transport": "IN_PROCESS_ONLY",
        "contains_absolute_paths": False,
    }
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(ROOT) not in serialized
    assert "sk-" not in serialized


def test_runtime_benchmark_rejects_invalid_iterations(tmp_path) -> None:
    with pytest.raises(ValueError, match="measured_iterations"):
        build_runtime_benchmark(
            root=ROOT,
            measured_iterations=0,
            scratch_parent=tmp_path,
        )


def test_runtime_benchmark_schema_requires_both_benchmark_ids(tmp_path) -> None:
    report = build_runtime_benchmark(
        root=ROOT,
        warmup_iterations=0,
        measured_iterations=1,
        scratch_parent=tmp_path,
    )
    report["benchmarks"][1]["benchmark_id"] = "GOLD_WORKFLOW"
    with pytest.raises(ValueError):
        validate_document(report, "runtime_benchmark.schema.json")
