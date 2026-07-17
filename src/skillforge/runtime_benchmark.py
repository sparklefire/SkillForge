"""Measure the reproducible N31 Gold workflow and live Web rerun."""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient

from .contracts import validate_document
from .demo import ROOT
from .gold_rehearsal import run_gold_rehearsal
from .web import create_app


EXPECTED_ASSERTIONS = {
    "gold_status": "GOLD",
    "metrics_status": "FINAL",
    "workflow_state": "COMPLETED",
    "severe_before": 5,
    "severe_after": 0,
    "revision_count": 4,
    "external_model_calls": 0,
}


def _output_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _assertions(summary: dict[str, Any]) -> dict[str, Any]:
    result = {
        "gold_status": summary.get("gold_status"),
        "metrics_status": summary.get("metrics_status"),
        "workflow_state": summary.get("workflow_state"),
        "severe_before": summary.get("before", {}).get("severe_error_count"),
        "severe_after": summary.get("after", {}).get("severe_error_count"),
        "revision_count": summary.get("revision_count"),
        "external_model_calls": summary.get("external_model_calls"),
    }
    if result != EXPECTED_ASSERTIONS:
        raise ValueError(f"Gold基准断言失败: {result}")
    return result


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _timing(samples: list[float]) -> dict[str, float]:
    return {
        "minimum": round(min(samples), 3),
        "median": round(statistics.median(samples), 3),
        "p95": round(_percentile(samples, 0.95), 3),
        "maximum": round(max(samples), 3),
        "mean": round(statistics.fmean(samples), 3),
    }


def _peak_rss_bytes(usage: resource.struct_rusage) -> int:
    raw = int(usage.ru_maxrss)
    return raw if sys.platform == "darwin" else raw * 1024


def _measure(
    operation: Callable[[int, Path], tuple[dict[str, Any], Path]],
    *,
    warmup_iterations: int,
    measured_iterations: int,
    scratch: Path,
) -> tuple[list[float], list[int], dict[str, Any]]:
    for index in range(warmup_iterations):
        summary, _ = operation(index, scratch / f"warmup-{index:02d}")
        _assertions(summary)

    samples: list[float] = []
    output_sizes: list[int] = []
    last_assertions: dict[str, Any] | None = None
    for index in range(measured_iterations):
        output_dir = scratch / f"sample-{index:03d}"
        started = time.perf_counter_ns()
        summary, written_dir = operation(index, output_dir)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        last_assertions = _assertions(summary)
        samples.append(round(elapsed_ms, 3))
        output_sizes.append(_output_bytes(written_dir))
    if last_assertions is None:
        raise ValueError("至少需要一次测量迭代")
    return samples, output_sizes, last_assertions


def build_runtime_benchmark(
    *,
    root: Path = ROOT,
    execution_location: str = "LOCAL_DEVELOPMENT",
    accelerator: str = "NONE",
    warmup_iterations: int = 2,
    measured_iterations: int = 20,
    scratch_parent: Path | None = None,
) -> dict[str, Any]:
    if execution_location not in {"LOCAL_DEVELOPMENT", "DGX_SPARK"}:
        raise ValueError("execution_location 只能是 LOCAL_DEVELOPMENT 或 DGX_SPARK")
    if not 0 <= warmup_iterations <= 20:
        raise ValueError("warmup_iterations 必须在0至20之间")
    if not 1 <= measured_iterations <= 100:
        raise ValueError("measured_iterations 必须在1至100之间")

    root = root.resolve()
    parent = (scratch_parent or root / "outputs").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    case = root / "cases/n31/gold"
    demo_bundle = root / "cases/n31/demo_bundle"
    usage_before = resource.getrusage(resource.RUSAGE_SELF)

    with tempfile.TemporaryDirectory(prefix="runtime-benchmark-", dir=parent) as temp:
        scratch = Path(temp)

        def gold_operation(_: int, output_dir: Path) -> tuple[dict[str, Any], Path]:
            summary = run_gold_rehearsal(
                case / "gold_sop.json",
                case / "constraints.json",
                case / "fault_injection.json",
                output_dir,
            )
            return summary, output_dir

        def web_operation(_: int, output_dir: Path) -> tuple[dict[str, Any], Path]:
            with TestClient(create_app(output_dir, demo_bundle)) as client:
                response = client.post("/api/n31/run")
                if response.status_code != 200:
                    raise ValueError(f"Web现场重算失败: HTTP {response.status_code}")
                summary = response.json()
            return summary, output_dir / "n31_live_run"

        gold_samples, gold_sizes, gold_assertions = _measure(
            gold_operation,
            warmup_iterations=warmup_iterations,
            measured_iterations=measured_iterations,
            scratch=scratch / "gold",
        )
        web_samples, web_sizes, web_assertions = _measure(
            web_operation,
            warmup_iterations=warmup_iterations,
            measured_iterations=measured_iterations,
            scratch=scratch / "web",
        )

    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "report_id": "RUNTIME_BENCHMARK_V1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "COMPLETED",
        "execution_location": execution_location,
        "environment": {
            "os": platform.system(),
            "architecture": platform.machine(),
            "python_version": platform.python_version(),
            "cpu_logical_count": os.cpu_count() or 1,
            "accelerator": accelerator,
        },
        "configuration": {
            "warmup_iterations": warmup_iterations,
            "measured_iterations": measured_iterations,
        },
        "benchmarks": [
            {
                "benchmark_id": "GOLD_WORKFLOW",
                "transport": "DIRECT_PYTHON",
                "samples_ms": gold_samples,
                "timing_ms": _timing(gold_samples),
                "mean_output_bytes": round(statistics.fmean(gold_sizes)),
                "assertions": gold_assertions,
            },
            {
                "benchmark_id": "WEB_LIVE_RERUN",
                "transport": "IN_PROCESS_HTTP",
                "samples_ms": web_samples,
                "timing_ms": _timing(web_samples),
                "mean_output_bytes": round(statistics.fmean(web_sizes)),
                "assertions": web_assertions,
            },
        ],
        "resources": {
            "measurement_scope": "BENCHMARK_PROCESS_HIGH_WATERMARK",
            "process_peak_rss_bytes": _peak_rss_bytes(usage_after),
            "cpu_user_seconds": round(usage_after.ru_utime - usage_before.ru_utime, 6),
            "cpu_system_seconds": round(usage_after.ru_stime - usage_before.ru_stime, 6),
        },
        "data_policy": {
            "external_model_calls": 0,
            "raw_media_processed": False,
            "credentials_accessed": False,
            "network_transport": "IN_PROCESS_ONLY",
            "contains_absolute_paths": False,
        },
    }
    return validate_document(report, "runtime_benchmark.schema.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output/evaluation/runtime_benchmark_local.json",
    )
    parser.add_argument(
        "--location",
        choices=["LOCAL_DEVELOPMENT", "DGX_SPARK"],
        default="LOCAL_DEVELOPMENT",
    )
    parser.add_argument("--accelerator", default="NONE")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()
    report = build_runtime_benchmark(
        execution_location=args.location,
        accelerator=args.accelerator,
        warmup_iterations=args.warmup,
        measured_iterations=args.iterations,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
