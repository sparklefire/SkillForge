"""Validate the reproducible three-minute N31 pitch package."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from .contracts import validate_document
from .demo import ROOT
from .web import create_app


PHASE_ORDER = [
    "PROBLEM",
    "INPUTS",
    "LOCAL_EXTRACTION",
    "VERIFY_AND_REVISE",
    "OUTPUTS",
    "METRICS",
    "PLATFORM_VALUE",
]
MODE_ORDER = ["LIVE", "PREPROCESSED", "OFFLINE"]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_timeline(runbook: dict[str, Any]) -> dict[str, Any]:
    segments = runbook["segments"]
    phase_order = [segment["phase"] for segment in segments]
    errors: list[str] = []
    if phase_order != PHASE_ORDER:
        errors.append("路演阶段顺序不符合冻结顺序")
    cursor = 0
    for segment in segments:
        if segment["start_ms"] != cursor:
            errors.append(f"{segment['phase']} 与上一段不连续")
        if segment["end_ms"] <= segment["start_ms"]:
            errors.append(f"{segment['phase']} 时长必须大于0")
        cursor = segment["end_ms"]
    if cursor != runbook["total_duration_ms"] or cursor != 180_000:
        errors.append("路演总时长必须精确为180秒")
    return {
        "check_id": "THREE_MINUTE_TIMELINE",
        "status": "PASSED" if not errors else "FAILED",
        "details": errors or ["7个阶段连续覆盖0至180秒"],
    }


def _check_artifact(path: Path, kind: str) -> tuple[bool, str]:
    if not path.is_file() or path.stat().st_size == 0:
        return False, "文件不存在或为空"
    try:
        if kind == "JSON":
            _read_json(path)
        elif kind == "PDF":
            if not path.read_bytes()[:5] == b"%PDF-":
                return False, "不是有效PDF文件头"
        elif kind == "MP4":
            if b"ftyp" not in path.read_bytes()[:64]:
                return False, "不是有效MP4文件头"
        elif kind == "PPTX":
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if "ppt/presentation.xml" not in names:
                    return False, "PPTX缺少presentation.xml"
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        return False, f"读取失败: {exc.__class__.__name__}"
    return True, f"{path.stat().st_size} bytes · sha256={_sha256(path)}"


def _check_artifacts(runbook: dict[str, Any], root: Path) -> dict[str, Any]:
    items = []
    passed = True
    for artifact in runbook["required_artifacts"]:
        path = (root / artifact["path"]).resolve()
        within_root = path == root or root in path.parents
        ok, detail = (
            _check_artifact(path, artifact["kind"])
            if within_root
            else (False, "路径越出项目根目录")
        )
        passed &= ok
        items.append(
            {
                "artifact_id": artifact["artifact_id"],
                "path": artifact["path"],
                "status": "PASSED" if ok else "FAILED",
                "detail": detail,
            }
        )
    return {
        "check_id": "REQUIRED_ARTIFACTS",
        "status": "PASSED" if passed else "FAILED",
        "items": items,
    }


def _check_metrics(root: Path) -> dict[str, Any]:
    summary = _read_json(root / "cases/n31/demo_bundle/summary.json")
    multisource = _read_json(
        root / "cases/n31/evaluations/multisource_comparison_v1.json"
    )
    dgx = validate_document(
        _read_json(root / "cases/n31/evaluations/dgx_visual_compute_v1.json"),
        "dgx_visual_compute.schema.json",
    )
    temporal = validate_document(
        _read_json(root / "cases/n31/evaluations/temporal_action_windows_v1.json"),
        "temporal_action_windows.schema.json",
    )
    pdf_structure = validate_document(
        _read_json(root / "cases/n31/evaluations/pdf_structure_v1.json"),
        "pdf_structure_report.schema.json",
    )
    source_candidates = validate_document(
        _read_json(root / "cases/n31/evaluations/source_candidate_synthesis_v1.json"),
        "source_candidate_synthesis.schema.json",
    )
    manifest_path = root / "output/video/n31_training_video_manifest_v1.json"
    manifest = validate_document(
        _read_json(manifest_path), "training_video_manifest.schema.json"
    )
    video_path = root / "output/video/n31_training_video_v1.mp4"
    assertions = {
        "gold_final": summary.get("gold_status") == "GOLD"
        and summary.get("metrics_status") == "FINAL",
        "errors_5_to_0": summary.get("before", {}).get("severe_error_count") == 5
        and summary.get("after", {}).get("severe_error_count") == 0,
        "revision_count_4": summary.get("revision_count") == 4,
        "coverage_90_to_100": summary.get("before", {}).get(
            "required_step_coverage"
        )
        == 0.9
        and summary.get("after", {}).get("required_step_coverage") == 1.0,
        "multisource_100": multisource["source_ablation"][
            "two_or_more_source_types"
        ]["coverage"]
        == 1.0,
        "dgx_cuda_native": dgx["actual_gpu_compute"] is True
        and dgx["summary"]["processed_video_count"] == 6
        and dgx["summary"]["sampled_frame_count"] == 420
        and dgx["summary"]["selected_frame_count"] == 50,
        "temporal_windows_bounded": temporal["semantic_claim_scope"]
        == "GOLD_ALIGNED_CANDIDATE_WINDOW_ONLY"
        and temporal["model_calls"] == 0
        and temporal["summary"]["step_count"] == 13
        and temporal["summary"]["window_count"] == 19
        and temporal["summary"]["source_count"] == 6
        and temporal["summary"]["selected_frame_reference_count"] == 51
        and temporal["summary"]["window_with_dgx_candidate_count"] == 12
        and temporal["summary"]["unique_dgx_candidate_count"] == 41
        and any(
            item["step_id"] == "S04"
            and item["visual_verdict"] == "NOT_VISIBLE"
            and item["start_ms"] == 60_000
            and item["end_ms"] == 75_000
            for item in temporal["windows"]
        ),
        "pdf_structure_grounded": pdf_structure["status"] == "COMPLETED"
        and pdf_structure["external_model_calls"] == 0
        and pdf_structure["summary"]["source_count"] == 2
        and pdf_structure["summary"]["page_count"] == 58
        and pdf_structure["summary"]["block_count"] == 607
        and pdf_structure["summary"]["needs_ocr_page_count"] == 0
        and pdf_structure["summary"]["ocr_applied_page_count"] == 9
        and pdf_structure["summary"]["search_chunk_count"] == 607
        and pdf_structure["summary"]["passed_query_count"]
        == pdf_structure["summary"]["query_count"]
        == 3
        and {
            item["query_id"]: (
                item["status"],
                item["top_hits"][0]["source_ref"],
                item["top_hits"][0]["page"],
            )
            for item in pdf_structure["queries"]
        }
        == {
            "Q01": ("PASSED", "N31_MANUAL_REV1_0", 14),
            "Q02": ("PASSED", "N31_MANUAL_REV1_0", 20),
            "Q03": ("PASSED", "N31_MANUAL_REV1_0", 20),
        },
        "source_candidates_grounded": source_candidates["status"] == "NEEDS_REVIEW"
        and source_candidates["uses_gold_step_text"] is False
        and source_candidates["data_policy"]["external_model_calls"] == 0
        and source_candidates["summary"]["source_candidate_counts"]
        == {"video": 18, "pdf": 7, "audio": 8}
        and source_candidates["summary"]["ordered_step_count"] == 13
        and source_candidates["summary"]["coarse_candidate_count"] == 8
        and source_candidates["summary"]["fine_candidate_count"] == 8
        and source_candidates["summary"]["coarse_split_group_count"] == 10
        and source_candidates["summary"]["fine_merge_group_count"] == 4
        and source_candidates["summary"]["synonym_merge_group_count"] == 12
        and source_candidates["summary"]["multi_source_step_count"] == 12
        and source_candidates["summary"]["graph_acyclic"] is True
        and any(
            item["step_id"] == "S04"
            and item["source_types"] == ["pdf"]
            and "E014" not in item["evidence_ids"]
            and item["confidence_assessment"]["band"] == "LOW"
            and item["confidence_assessment"]["route"]
            == "HUMAN_REVIEW_REQUIRED"
            and item["confidence_assessment"]["observation_ids"] == ["NO001"]
            for item in source_candidates["ordered_steps"]
        ),
        "video_manifest_bound": video_path.is_file()
        and manifest["output"]["sha256"] == _sha256(video_path)
        and manifest["coverage"]["covered_gold_step_count"]
        == manifest["coverage"]["gold_step_count"]
        == 13,
    }
    return {
        "check_id": "PITCH_CLAIMS",
        "status": "PASSED" if all(assertions.values()) else "FAILED",
        "assertions": assertions,
    }


def _check_runtime_benchmark(root: Path) -> dict[str, Any]:
    report = validate_document(
        _read_json(root / "output/evaluation/runtime_benchmark_dgx.json"),
        "runtime_benchmark.schema.json",
    )
    benchmarks = {item["benchmark_id"]: item for item in report["benchmarks"]}
    gold = benchmarks.get("GOLD_WORKFLOW", {})
    web = benchmarks.get("WEB_LIVE_RERUN", {})
    expected_assertions = {
        "gold_status": "GOLD",
        "metrics_status": "FINAL",
        "workflow_state": "COMPLETED",
        "severe_before": 5,
        "severe_after": 0,
        "revision_count": 4,
        "external_model_calls": 0,
    }
    assertions = {
        "dgx_environment": report["execution_location"] == "DGX_SPARK"
        and report["environment"]["architecture"] == "aarch64"
        and report["environment"]["accelerator"] == "NVIDIA GB10",
        "twenty_measured_runs": report["configuration"]["measured_iterations"] == 20
        and len(gold.get("samples_ms", [])) == 20
        and len(web.get("samples_ms", [])) == 20,
        "gold_workflow_measured": gold.get("timing_ms", {}).get("median", 0) > 0
        and gold.get("assertions") == expected_assertions,
        "web_live_rerun_measured": web.get("timing_ms", {}).get("median", 0) > 0
        and web.get("assertions") == expected_assertions,
        "resource_recorded": report["resources"]["process_peak_rss_bytes"] > 0,
        "safe_measurement_scope": report["data_policy"]
        == {
            "external_model_calls": 0,
            "raw_media_processed": False,
            "credentials_accessed": False,
            "network_transport": "IN_PROCESS_ONLY",
            "contains_absolute_paths": False,
        },
    }
    return {
        "check_id": "RUNTIME_BENCHMARK",
        "status": "PASSED" if all(assertions.values()) else "FAILED",
        "assertions": assertions,
        "metrics": {
            "gold_workflow_median_ms": gold.get("timing_ms", {}).get("median"),
            "web_live_rerun_median_ms": web.get("timing_ms", {}).get("median"),
            "process_peak_rss_bytes": report["resources"]["process_peak_rss_bytes"],
        },
    }


def _check_demo_modes(runbook: dict[str, Any], root: Path) -> dict[str, Any]:
    ordered_modes = [
        item["mode"]
        for item in sorted(
            runbook["demo_modes"], key=lambda item: item["priority"]
        )
    ]
    client = TestClient(
        create_app(root / "outputs/pitch_web_check", root / "cases/n31/demo_bundle")
    )
    health = client.get("/health")
    payload = client.get("/api/n31")
    rerun = client.post("/api/n31/run")
    offline_bundle = _read_json(root / "cases/n31/demo_bundle/bundle.json")
    assertions = {
        "mode_priority": ordered_modes == MODE_ORDER,
        "health": health.status_code == 200
        and health.json().get("runtime") == "native-python"
        and health.json().get("docker_required") is False,
        "offline_bundle_safe": offline_bundle.get("contains_raw_media") is False
        and offline_bundle.get("contains_credentials") is False,
        "offline_payload": payload.status_code == 200
        and payload.json()["summary"].get("gold_status") == "GOLD",
        "live_rerun": rerun.status_code == 200
        and rerun.json().get("before", {}).get("severe_error_count") == 5
        and rerun.json().get("after", {}).get("severe_error_count") == 0
        and rerun.json().get("revision_count") == 4,
        "entry_script": (root / "scripts/run_demo_mode.sh").is_file(),
    }
    return {
        "check_id": "DEMO_FALLBACKS",
        "status": "PASSED" if all(assertions.values()) else "FAILED",
        "assertions": assertions,
    }


def build_readiness(
    runbook_path: Path,
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    root = root.resolve()
    runbook = validate_document(_read_json(runbook_path), "pitch_runbook.schema.json")
    checks = [
        _check_timeline(runbook),
        _check_artifacts(runbook, root),
        _check_metrics(root),
        _check_runtime_benchmark(root),
        _check_demo_modes(runbook, root),
    ]
    checks_passed = all(check["status"] == "PASSED" for check in checks)
    pending_gates = [
        gate["gate_id"] for gate in runbook["human_gates"] if gate["status"] != "PASSED"
    ]
    if not checks_passed:
        status = "NOT_READY"
    elif pending_gates:
        status = "READY_WITH_HUMAN_GATES"
    else:
        status = "READY_FOR_SUBMISSION"
    return {
        "version": 1,
        "case_id": runbook["case_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "total_duration_ms": runbook["total_duration_ms"],
        "checks": checks,
        "pending_human_gates": pending_gates,
        "contains_credentials": False,
        "contains_raw_media": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runbook",
        type=Path,
        default=ROOT / "cases/n31/pitch_runbook.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output/presentation/n31_pitch_readiness_v1.json",
    )
    args = parser.parse_args()
    readiness = build_readiness(args.runbook)
    _write_json(args.output, readiness)
    print(json.dumps(readiness, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if readiness["status"] != "NOT_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
