import json
from pathlib import Path

import pytest

import skillforge.runtime_benchmark as benchmark_module
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
    assert all(item["successful_iterations"] == 2 for item in report["benchmarks"])
    assert all(item["failure_count"] == 0 for item in report["benchmarks"])
    assert all(
        item["unique_semantic_fingerprint_count"] == 1
        for item in report["benchmarks"]
    )
    fingerprints = {
        item["semantic_fingerprint_sha256"] for item in report["benchmarks"]
    }
    assert len(fingerprints) == 1
    assert report["stability"] == {
        "total_measured_iterations": 4,
        "all_measured_iterations_succeeded": True,
        "gold_semantics_stable": True,
        "web_semantics_stable": True,
        "gold_and_web_semantics_equal": True,
        "unique_semantic_fingerprint_count": 1,
        "semantic_fingerprint_sha256": next(iter(fingerprints)),
    }
    assert report["data_policy"] == {
        "external_model_calls": 0,
        "raw_media_processed": False,
        "credentials_accessed": False,
        "network_transport": "IN_PROCESS_ONLY",
        "contains_absolute_paths": False,
        "network_requests": 0,
        "automatic_human_confirmations": 0,
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


def test_runtime_benchmark_readme_matches_current_dgx_report() -> None:
    report = json.loads(
        (ROOT / "output/evaluation/runtime_benchmark_dgx.json").read_text(
            encoding="utf-8"
        )
    )
    readme = (ROOT / "output/evaluation/README.md").read_text(encoding="utf-8")
    benchmarks = {item["benchmark_id"]: item for item in report["benchmarks"]}

    for benchmark_id in ("GOLD_WORKFLOW", "WEB_LIVE_RERUN"):
        timing = benchmarks[benchmark_id]["timing_ms"]
        assert f'{timing["median"]:.3f} ms' in readme
        assert f'{timing["p95"]:.3f} ms' in readme
    assert f'{report["resources"]["process_peak_rss_bytes"]:,}' in readme
    assert "40轮当前全部成功、失败0次、唯一语义指纹1个" in readme


def test_runtime_benchmark_rejects_semantic_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fingerprints = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(
        benchmark_module,
        "semantic_fingerprint_from_directory",
        lambda _path: next(fingerprints),
    )
    with pytest.raises(ValueError, match="语义指纹发生漂移"):
        build_runtime_benchmark(
            root=ROOT,
            warmup_iterations=0,
            measured_iterations=2,
            scratch_parent=tmp_path,
        )
