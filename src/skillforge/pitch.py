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
from .conflict_adjudication import route_conflict
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
    gold = validate_document(
        _read_json(root / "cases/n31/gold/gold_sop.json"),
        "sop.schema.json",
    )
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
    grounding_gate = validate_document(
        _read_json(
            root / "cases/n31/evaluations/deterministic_grounding_gate_v1.json"
        ),
        "grounding_gate_report.schema.json",
    )
    semantic_review = validate_document(
        _read_json(root / "cases/n31/evaluations/semantic_review_v1.json"),
        "semantic_review_report.schema.json",
    )
    selective_rebuild = validate_document(
        _read_json(root / "cases/n31/evaluations/selective_rebuild_v1.json"),
        "selective_rebuild_report.schema.json",
    )
    sop_views = validate_document(
        _read_json(root / "cases/n31/demo_bundle/sop_views.json"),
        "sop_views.schema.json",
    )
    checklist = validate_document(
        _read_json(root / "cases/n31/demo_bundle/checklist.json"),
        "mobile_checklist.schema.json",
    )
    quiz = validate_document(
        _read_json(root / "cases/n31/demo_bundle/quiz.json"),
        "training_quiz.schema.json",
    )
    checklist_thumbnails = validate_document(
        _read_json(root / "output/checklist_thumbnails/manifest.json"),
        "checklist_thumbnail_manifest.schema.json",
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
        "grounding_gate_closed": grounding_gate["status"] == "PASSED"
        and grounding_gate["model_calls"] == 0
        and grounding_gate["summary"]
        == {
            "scenario_count": 4,
            "passed_count": 4,
            "detected_count": 4,
            "revised_count": 4,
            "residual_conflict_count": 0,
        }
        and [item["scenario_id"] for item in grounding_gate["scenarios"]]
        == [
            "CROSS_STEP_ALLOWED_TOOL",
            "ALLOWED_PARAMETER_WRONG_VALUE",
            "UNGROUNDED_WARNING",
            "ABSOLUTE_SAFETY_PROMISE",
        ]
        and all(
            item["status"] == "PASSED"
            and item["detected_conflict_ids"]
            and item["reference_evidence_ids"]
            and item["restored"] is True
            and item["residual_conflict_count"] == 0
            for item in grounding_gate["scenarios"]
        ),
        "semantic_review_grounded": semantic_review["status"] == "COMPLETED"
        and semantic_review["model"] == "step-3.7-flash"
        and semantic_review["reasoning_effort"] == "high"
        and semantic_review["model_calls"] >= 1
        and semantic_review["review_scope"]
        == {
            "step_count": 13,
            "evidence_count": 36,
            "dimensions": [
                "SOURCE_DISTORTION",
                "SOURCE_CONFLICT",
                "ORDERING_RISK",
                "EXCEPTION_OMISSION",
            ],
            "structured_sop_sent": True,
            "evidence_claims_sent": True,
            "raw_media_sent": False,
            "full_transcript_sent": False,
            "manual_pages_sent": False,
            "local_paths_sent": False,
            "credentials_sent": False,
        }
        and semantic_review["summary"]
        == {
            "step_count": 13,
            "supported_count": 13,
            "partial_count": 0,
            "conflict_count": 0,
            "needs_review_count": 0,
            "finding_count": 0,
            "high_severity_count": 0,
            "finding_kind_counts": {
                "SOURCE_DISTORTION": 0,
                "SOURCE_CONFLICT": 0,
                "ORDERING_RISK": 0,
                "EXCEPTION_OMISSION": 0,
            },
            "human_review_finding_ids": [],
            "automatic_gold_changes": 0,
        }
        and semantic_review["guardrails"]["may_override_gold"] is False
        and semantic_review["guardrails"]["model_output_classification"]
        == "MODEL_INFERENCE"
        and {
            item["step_id"] for item in semantic_review["assessments"]
        }
        == {item["step_id"] for item in gold["steps"]}
        and all(
            set(assessment["evidence_ids"])
            <= set(
                next(
                    step
                    for step in gold["steps"]
                    if step["step_id"] == assessment["step_id"]
                )["evidence"]
            )
            for assessment in semantic_review["assessments"]
        ),
        "selective_rebuild_bounded": selective_rebuild["status"] == "PASSED"
        and selective_rebuild["summary"]
        == {
            "affected_step_count": 7,
            "content_changed_step_count": 3,
            "position_changed_step_count": 7,
            "rebuild_artifact_count": 6,
            "skipped_artifact_count": 0,
            "quiz_question_count": 1,
            "video_scene_count": 7,
            "whole_artifact_count": 1,
        }
        and selective_rebuild["verification"]
        == {
            "sop_patch_reproduces_after": True,
            "quiz_unchanged_questions_identical": True,
            "video_scene_ids_exist": True,
            "no_unaffected_video_scene_selected": True,
            "poster_dependency_declared": True,
        }
        and selective_rebuild["data_policy"]
        == {
            "external_model_calls": 0,
            "contains_raw_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
        }
        and {
            plan["artifact_type"]: plan["units"]
            for plan in selective_rebuild["artifact_plans"]
        }["TRAINING_QUIZ"]
        == ["Q02"]
        and {
            plan["artifact_type"]: plan["units"]
            for plan in selective_rebuild["artifact_plans"]
        }["TRAINING_VIDEO"]
        == ["V07", "V08", "V09", "V10", "V11", "V12", "V13"],
        "training_package_traceable": set(sop_views["views"])
        == {"concise", "detailed", "evidence"}
        and all(len(view["steps"]) == 13 for view in sop_views["views"].values())
        and all(
            {"action", "reason", "completion_marker", "risks", "sources"}
            <= set(step)
            for view in sop_views["views"].values()
            for step in view["steps"]
        )
        and checklist["interaction_mode"] == "ONE_STEP_PER_SCREEN"
        and len(checklist["items"]) == 13
        and next(
            item for item in checklist["items"] if item["step_id"] == "S04"
        )["keyframe"]["visual_status"]
        == "NOT_VISIBLE",
        "checklist_previews_public": len(checklist_thumbnails["items"]) == 13
        and checklist_thumbnails["source_video"]["sha256"] == _sha256(video_path)
        and all(
            (root / item["preview_path"]).is_file()
            and (root / item["preview_path"]).stat().st_size == item["bytes"]
            and _sha256(root / item["preview_path"]) == item["sha256"]
            for item in checklist_thumbnails["items"]
        )
        and {
            item["step_id"]: item["preview_path"]
            for item in checklist_thumbnails["items"]
        }
        == {
            item["step_id"]: item["keyframe"]["preview_path"]
            for item in checklist["items"]
        },
        "training_quiz_grounded": quiz["coverage"]
        == {
            "question_count": 5,
            "category_count": 5,
            "categories": [
                "ORDERING",
                "TOOL_SELECTION",
                "RISK_RESPONSE",
                "STATUS_RECOGNITION",
                "ERROR_JUDGMENT",
            ],
            "all_answers_grounded": True,
            "all_explanations_grounded": True,
        }
        and [item["type"] for item in quiz["questions"]]
        == [
            "ORDERING",
            "MULTIPLE_SELECT",
            "SINGLE_CHOICE",
            "SINGLE_CHOICE",
            "TRUE_FALSE",
        ]
        and all(
            set(item["answer_evidence_ids"]) <= set(item["evidence_ids"])
            and set(item["explanation_evidence_ids"]) <= set(item["evidence_ids"])
            and {detail["evidence_id"] for detail in item["evidence_details"]}
            == set(item["evidence_ids"])
            and all(
                set(option["evidence_ids"]) <= set(item["evidence_ids"])
                for option in item["options"]
            )
            for item in quiz["questions"]
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
    conflict_session = client.post("/api/n31/conflicts/sessions")
    asr_session = client.post("/api/n31/asr-corrections/sessions")
    asr_session_payload = asr_session.json() if asr_session.status_code == 200 else {}
    asr_q02 = next(
        (
            item
            for item in asr_session_payload.get("answers", [])
            if item.get("question_id") == "Q02"
        ),
        None,
    )
    asr_correction = (
        client.patch(
            f"/api/n31/asr-corrections/sessions/{asr_session_payload['session_id']}/answers/Q02",
            json={
                "corrected_text": asr_q02["effective_corrected_text"]
                + " 人工复听确认。",
                "operator": "路演自动验收操作者",
                "reason": "验证ASR修正历史和Evidence摘要重绑",
            },
        )
        if asr_q02 is not None
        else None
    )
    asr_correction_payload = (
        asr_correction.json()
        if asr_correction is not None and asr_correction.status_code == 200
        else {}
    )
    asr_corrected_q02 = next(
        (
            item
            for item in asr_correction_payload.get("answers", [])
            if item.get("question_id") == "Q02"
        ),
        None,
    )
    safety_probe = dict(payload.json()["initial_conflicts"]["conflicts"][0])
    safety_probe.update(
        {
            "kind": "UNSUPPORTED_SAFETY_CLAIM",
            "automatic": True,
            "proposed_action": "REPLACE",
            "status": "OPEN",
        }
    )
    safety_route = route_conflict(safety_probe)
    rerun = client.post("/api/n31/run")
    stage_before = client.get("/api/n31/stages/current")
    stage_rerun = client.post("/api/n31/stages/RENDERING/rerun")
    stage_after = client.get("/api/n31/stages/current")
    evidence = client.get("/api/n31/evidence/E144")
    review = client.post("/api/n31/review/sessions")
    review_id = review.json().get("session_id") if review.status_code == 200 else None
    rebuild = (
        client.post(f"/api/n31/review/sessions/{review_id}/steps/S12/rebuild")
        if review_id
        else None
    )
    reordered = (
        client.post(
            f"/api/n31/review/sessions/{review_id}/reorder",
            json={"step_id": "S11", "target_position": 12},
        )
        if review_id
        else None
    )
    invalid_order = (
        client.post(
            f"/api/n31/review/sessions/{review_id}/reorder",
            json={"step_id": "S09", "target_position": 8},
        )
        if review_id
        else None
    )
    locked = (
        client.patch(
            f"/api/n31/review/sessions/{review_id}/steps/S01",
            json={"locked": True},
        )
        if review_id
        else None
    )
    confirmed = (
        client.patch(
            f"/api/n31/review/sessions/{review_id}/steps/S01",
            json={"confirmed": True},
        )
        if review_id and locked is not None and locked.status_code == 200
        else None
    )
    offline_bundle = _read_json(root / "cases/n31/demo_bundle/bundle.json")
    before_manifest = stage_before.json() if stage_before.status_code == 200 else {}
    after_manifest = stage_after.json() if stage_after.status_code == 200 else {}
    before_hashes = {
        item["stage_id"]: {output["name"]: output["sha256"] for output in item["outputs"]}
        for item in before_manifest.get("stages", [])
    }
    after_hashes = {
        item["stage_id"]: {output["name"]: output["sha256"] for output in item["outputs"]}
        for item in after_manifest.get("stages", [])
    }
    assertions = {
        "mode_priority": ordered_modes == MODE_ORDER,
        "health": health.status_code == 200
        and health.json().get("runtime") == "native-python"
        and health.json().get("docker_required") is False,
        "offline_bundle_safe": offline_bundle.get("contains_raw_media") is False
        and offline_bundle.get("contains_credentials") is False,
        "offline_payload": payload.status_code == 200
        and payload.json()["summary"].get("gold_status") == "GOLD",
        "conflict_decision_auditable": conflict_session.status_code == 200
        and conflict_session.json().get("status") == "AUTO_FINALIZED"
        and conflict_session.json().get("finalization")
        == {
            "publishable": True,
            "final_sop_sha256": conflict_session.json()
            .get("source_bindings", {})
            .get("proposed_sop_sha256"),
            "proposed_residual_conflict_count": 0,
            "adopted_unresolved_conflict_count": 0,
            "adopted_conflict_count": 5,
            "rejected_conflict_count": 0,
            "pending_conflict_count": 0,
        }
        and all(
            item.get("route") == "AUTO"
            and item.get("human_decision") == "NOT_REQUIRED"
            and item.get("final_result")
            in {"ADOPTED", "RESOLVED_BY_RELATED_CHANGE"}
            for item in conflict_session.json().get("decisions", [])
        ),
        "safety_conflict_human_gate": safety_route[0] == "HUMAN"
        and safety_route[2] is True
        and conflict_session.json()
        .get("routing_policy", {})
        .get("safety_override_enabled")
        is True
        and conflict_session.json()
        .get("routing_policy", {})
        .get("human_required_kinds")
        == [
            "UNSUPPORTED_SAFETY_CLAIM",
            "MISSING_EVIDENCE",
            "INVALID_EVIDENCE",
        ],
        "asr_correction_auditable": asr_session.status_code == 200
        and asr_q02 is not None
        and asr_correction is not None
        and asr_correction.status_code == 200
        and asr_corrected_q02 is not None
        and asr_correction_payload.get("status") == "CORRECTED"
        and asr_correction_payload.get("summary", {}).get("corrected_answer_count")
        == 1
        and asr_correction_payload.get("summary", {}).get("correction_event_count")
        == 1
        and asr_corrected_q02["raw_asr_text"] == asr_q02["raw_asr_text"]
        and asr_corrected_q02["evidence_binding"]["current_sha256"]
        != asr_q02["evidence_binding"]["baseline_sha256"]
        and asr_correction_payload.get("data_policy")
        == {
            "storage_scope": "LOCAL_PRIVATE_ONLY",
            "external_model_calls": 0,
            "contains_raw_transcript_snippets": True,
            "contains_raw_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
        },
        "workflow_checkpoint_safe": payload.status_code == 200
        and payload.json().get("workflow", {}).get("version") == 1
        and payload.json().get("workflow", {}).get("state") == "COMPLETED"
        and payload.json().get("workflow", {}).get("stage_attempts", {}).get(
            "VERIFYING"
        )
        == 2
        and payload.json().get("workflow", {}).get("last_failure") is None
        and payload.json().get("workflow", {}).get("data_policy", {}).get(
            "contains_credentials"
        )
        is False,
        "live_rerun": rerun.status_code == 200
        and rerun.json().get("before", {}).get("severe_error_count") == 5
        and rerun.json().get("after", {}).get("severe_error_count") == 0
        and rerun.json().get("revision_count") == 4,
        "artifact_stage_rerun": stage_before.status_code == 200
        and stage_rerun.status_code == 200
        and stage_after.status_code == 200
        and after_manifest.get("source_run_id") == before_manifest.get("run_id")
        and after_manifest.get("start_stage") == "RENDERING"
        and stage_rerun.json().get("reused_stages")
        == [
            "INGESTING",
            "EXTRACTING",
            "PLANNING",
            "CREATING",
            "VERIFYING_INITIAL",
            "REVISING",
            "VERIFYING_FINAL",
        ]
        and stage_rerun.json().get("rebuilt_stages") == ["RENDERING"]
        and all(
            before_hashes.get(stage) == after_hashes.get(stage)
            for stage in stage_rerun.json().get("reused_stages", [])
        )
        and after_manifest.get("published") is True
        and after_manifest.get("data_policy")
        == {
            "external_model_calls": 0,
            "contains_raw_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
        },
        "evidence_locator_safe": evidence.status_code == 200
        and evidence.json().get("evidence_id") == "E144"
        and evidence.json().get("navigation", {}).get("kind") == "AUDIO_TIME"
        and evidence.json().get("navigation", {}).get("raw_source_url") is None
        and evidence.json().get("data_policy", {}).get("contains_raw_media") is False,
        "operator_review_controls": review.status_code == 200
        and len(review.json().get("steps", [])) == 13
        and rebuild is not None
        and rebuild.status_code == 200
        and rebuild.json().get("scope", {}).get("unchanged_step_count") == 12
        and rebuild.json().get("scope", {}).get("external_model_calls") == 0
        and reordered is not None
        and reordered.status_code == 200
        and invalid_order is not None
        and invalid_order.status_code == 400
        and locked is not None
        and locked.status_code == 200
        and confirmed is not None
        and confirmed.status_code == 200
        and any(
            item.get("step_id") == "S01"
            and item.get("locked") is True
            and item.get("confirmed") is True
            for item in confirmed.json().get("steps", [])
        ),
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
