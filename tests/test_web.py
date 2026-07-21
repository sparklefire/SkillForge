from fastapi.testclient import TestClient

import json

from skillforge.demo import ROOT, run_demo
from skillforge.web import _artifact_media_type, create_app


def test_native_web_health_and_demo(tmp_path) -> None:
    client = TestClient(create_app(tmp_path, tmp_path / "missing-n31"))
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "runtime": "native-python",
        "docker_required": False,
        "n31_rehearsal_available": False,
        "training_video_available": True,
        "training_video_status": "FINAL_APPROVED",
        "artifact_stage_runner": True,
        "artifact_stage_release_available": False,
    }
    assert client.get("/api/demo").status_code == 404
    assert client.get("/api/n31/stages/current").status_code == 404
    assert client.post("/api/n31/stages/RENDERING/rerun").status_code == 409
    result = client.post("/api/demo/run")
    assert result.status_code == 200
    assert result.json()["workflow_state"] == "COMPLETED"
    payload = client.get("/api/demo").json()
    assert payload["summary"]["after"]["severe_error_count"] == 0


def test_web_prefers_explicitly_labelled_n31_rehearsal(tmp_path) -> None:
    n31_dir = tmp_path / "n31"
    run_demo(ROOT / "cases" / "demo_case" / "synthetic", n31_dir)
    summary_path = n31_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.update(
        {
            "synthetic": False,
            "gold_status": "NOT_GOLD",
            "metrics_status": "PROVISIONAL_ONLY",
            "human_review_required": True,
        }
    )
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    client = TestClient(create_app(tmp_path / "web", n31_dir))
    assert client.get("/health").json()["n31_rehearsal_available"] is True
    response = client.get("/api/n31")
    assert response.status_code == 200
    assert response.json()["summary"]["gold_status"] == "NOT_GOLD"
    review = client.post("/api/n31/review/sessions")
    assert review.status_code == 409
    assert "只有Gold最终结果" in review.json()["detail"]
    asr_correction = client.post("/api/n31/asr-corrections/sessions")
    assert asr_correction.status_code == 409
    assert "只有Gold最终结果" in asr_correction.json()["detail"]


def test_web_accepts_operator_reviewed_gold_result(tmp_path) -> None:
    n31_dir = ROOT / "cases" / "n31" / "demo_bundle"
    client = TestClient(create_app(tmp_path / "web", n31_dir))
    home = client.get("/")
    assert home.status_code == 200
    assert "无来源内容拒绝门禁" in home.text
    assert "/api/n31/artifacts/grounding-gate" in home.text
    assert "高推理语义质检" in home.text
    assert "/api/n31/artifacts/semantic-review" in home.text
    assert "受影响范围与选择性重建" in home.text
    assert "/api/n31/artifacts/selective-rebuild" in home.text
    assert "交付配置与低码率素材预览" in home.text
    assert "<video controls" in home.text
    assert "五类 Agent 与可审计工具链" in home.text
    assert "操作者 SOP 审核台" in home.text
    assert "专家口述 ASR 快速修正" in home.text
    assert "重新计算Evidence绑定摘要" in home.text
    assert "冲突裁决与最终采用" in home.text
    assert "安全声明、缺失证据、无效证据" in home.text
    assert "单步重建" in home.text
    assert "/api/n31/evidence/" in home.text
    assert "可恢复工作流检查点" in home.text
    assert "只复用上游并重建下游" in home.text
    response = client.get("/api/n31")
    assert response.status_code == 200
    assert response.json()["summary"]["gold_status"] == "GOLD"
    assert response.json()["summary"]["metrics_status"] == "FINAL"
    agent_trace = response.json()["agent_tool_trace"]
    assert agent_trace["summary"]["agent_count"] == 5
    assert agent_trace["summary"]["tool_count"] == 13
    assert client.get("/api/n31/agents").json() == agent_trace
    assert response.json()["workflow"]["version"] == 1
    assert response.json()["workflow"]["state"] == "COMPLETED"
    assert response.json()["workflow"]["stage_attempts"]["VERIFYING"] == 2
    assert response.json()["workflow"]["last_failure"] is None
    assert (
        response.json()["multisource_comparison"]["source_ablation"][
            "two_or_more_source_types"
        ]["coverage"]
        == 1.0
    )
    assert response.json()["visual_review"]["summary"]["contradicted_count"] == 0
    training_video = response.json()["training_video"]
    assert training_video["status"] == "FINAL_APPROVED"
    assert training_video["output"]["duration_ms"] == 80_000
    assert training_video["coverage"]["covered_gold_step_count"] == 13
    assert training_video["final_human_review_required"] is False
    dgx_path = ROOT / "cases/n31/evaluations/dgx_visual_compute_v1.json"
    if dgx_path.is_file():
        dgx = response.json()["dgx_visual_compute"]
        assert dgx["actual_gpu_compute"] is True
        assert dgx["semantic_claim_scope"] == "CANDIDATE_SELECTION_ONLY"
    temporal = response.json()["temporal_action_windows"]
    assert temporal["semantic_claim_scope"] == "GOLD_ALIGNED_CANDIDATE_WINDOW_ONLY"
    assert temporal["summary"]["step_count"] == 13
    assert temporal["summary"]["window_count"] == 19
    s04 = next(item for item in temporal["windows"] if item["step_id"] == "S04")
    assert s04["visual_verdict"] == "NOT_VISIBLE"
    pdf_structure = response.json()["pdf_structure"]
    assert pdf_structure["status"] == "COMPLETED"
    assert pdf_structure["summary"]["page_count"] == 58
    assert pdf_structure["summary"]["needs_ocr_page_count"] == 0
    assert pdf_structure["summary"]["passed_query_count"] == 3
    source_candidates = response.json()["source_candidate_synthesis"]
    assert source_candidates["summary"]["source_candidate_counts"] == {
        "video": 18,
        "pdf": 7,
        "audio": 8,
    }
    assert source_candidates["summary"]["ordered_step_count"] == 13
    assert source_candidates["summary"]["multi_source_step_count"] == 12
    assert source_candidates["summary"]["low_confidence_step_ids"] == ["S04"]
    assert source_candidates["summary"]["review_route_counts"] == {
        "AUTO_VERIFY": 6,
        "VERIFIER_QUEUE": 6,
        "HUMAN_REVIEW_REQUIRED": 1,
    }
    output_profile = response.json()["output_profile"]
    assert output_profile["audience"]["primary_role"] == "NEW_OPERATOR"
    assert output_profile["language"]["locale"] == "zh-CN"
    assert output_profile["duration"]["training_video_target_seconds"] == 80
    previews = response.json()["video_previews"]
    assert previews["manifest"]["summary"]["source_count"] == 6
    assert previews["manifest"]["summary"]["all_checks_passed"] is True
    assert len(previews["availability"]) == 6
    assert 0 <= previews["available_count"] <= 6
    grounding_gate = response.json()["grounding_gate"]
    assert grounding_gate["status"] == "PASSED"
    assert grounding_gate["summary"] == {
        "scenario_count": 4,
        "passed_count": 4,
        "detected_count": 4,
        "revised_count": 4,
        "residual_conflict_count": 0,
    }
    semantic_review = response.json()["semantic_review"]
    assert semantic_review["status"] == "COMPLETED"
    assert semantic_review["model"] == "step-3.7-flash"
    assert semantic_review["reasoning_effort"] == "high"
    assert semantic_review["summary"]["supported_count"] == 13
    assert semantic_review["summary"]["finding_count"] == 0
    assert semantic_review["summary"]["automatic_gold_changes"] == 0
    assert semantic_review["review_scope"]["raw_media_sent"] is False
    selective = response.json()["selective_rebuild"]
    assert selective["status"] == "PASSED"
    assert selective["summary"]["affected_step_count"] == 7
    assert selective["summary"]["quiz_question_count"] == 1
    assert selective["summary"]["video_scene_count"] == 7
    assert all(selective["verification"].values())
    assert len(response.json()["checklist"]["items"]) == 13
    assert set(response.json()["sop_views"]["views"]) == {
        "concise",
        "detailed",
        "evidence",
    }
    assert len(response.json()["quiz"]["questions"]) == 5
    assert [item["category"] for item in response.json()["quiz"]["questions"]] == [
        "ORDERING",
        "TOOL_SELECTION",
        "RISK_RESPONSE",
        "STATUS_RECOGNITION",
        "ERROR_JUDGMENT",
    ]
    assert response.json()["quiz"]["coverage"]["all_answers_grounded"] is True
    checklist = client.get("/api/n31/artifacts/checklist")
    assert checklist.status_code == 200
    assert checklist.json()["case_id"] == "n31_media_change"
    assert "attachment" in checklist.headers["content-disposition"]
    sop_views = client.get("/api/n31/artifacts/sop-views")
    assert sop_views.status_code == 200
    assert sop_views.json()["artifact_type"] == "SOP_VIEWS"
    quiz = client.get("/api/n31/artifacts/quiz")
    assert quiz.status_code == 200
    assert quiz.json()["artifact_type"] == "TRAINING_QUIZ"
    assert quiz.json()["coverage"]["category_count"] == 5
    session = client.post("/api/n31/checklist/sessions")
    assert session.status_code == 200
    session_id = session.json()["session_id"]
    completed = client.patch(
        f"/api/n31/checklist/sessions/{session_id}/items/S01",
        json={"completed": True},
    )
    assert completed.status_code == 200
    assert completed.json()["progress"]["completed_items"] == 1
    feedback = client.patch(
        f"/api/n31/checklist/sessions/{session_id}/items/S01",
        json={
            "feedback_category": "EVIDENCE_ISSUE",
            "feedback_comment": "需要重新核对来源定位",
        },
    )
    assert feedback.status_code == 200
    assert len(feedback.json()["feedback_log"]) == 1
    persisted = client.get(f"/api/n31/checklist/sessions/{session_id}")
    assert persisted.status_code == 200
    assert persisted.json()["items"][0]["completed"] is True
    assert client.get("/api/n31/checklist/keyframes/E999").status_code == 404
    preview = client.get("/api/n31/checklist/previews/S01")
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"
    assert preview.content.startswith(b"\xff\xd8")
    assert client.get("/api/n31/checklist/previews/S99").status_code == 404
    thumbnail_manifest = client.get("/api/n31/artifacts/checklist-thumbnails")
    assert thumbnail_manifest.status_code == 200
    assert len(thumbnail_manifest.json()["items"]) == 13
    poster = client.get("/api/n31/artifacts/poster")
    assert poster.status_code == 200
    assert poster.headers["content-type"] == "application/pdf"
    assert poster.content.startswith(b"%PDF-")
    video = client.get(
        "/api/n31/media/training-video", headers={"Range": "bytes=0-31"}
    )
    assert video.status_code == 206
    assert video.headers["content-type"] == "video/mp4"
    assert b"ftyp" in video.content
    manifest = client.get("/api/n31/artifacts/training-video-manifest")
    assert manifest.status_code == 200
    assert manifest.json()["output"]["duration_ms"] == 80_000
    evidence_pack = client.get("/api/n31/artifacts/training-video-evidence")
    assert evidence_pack.status_code == 200
    assert evidence_pack.json()["artifact_type"] == "TRAINING_VIDEO_EVIDENCE_PACK"
    assert evidence_pack.json()["contains_raw_media"] is False
    temporal_download = client.get("/api/n31/artifacts/temporal-windows")
    assert temporal_download.status_code == 200
    assert temporal_download.json()["summary"]["window_count"] == 19
    pdf_download = client.get("/api/n31/artifacts/pdf-structure")
    assert pdf_download.status_code == 200
    assert pdf_download.json()["summary"]["block_count"] == 607
    candidate_download = client.get("/api/n31/artifacts/source-candidates")
    assert candidate_download.status_code == 200
    assert candidate_download.json()["summary"]["coarse_candidate_count"] == 8
    grounding_download = client.get("/api/n31/artifacts/grounding-gate")
    assert grounding_download.status_code == 200
    assert grounding_download.json()["report_id"] == "DETERMINISTIC_GROUNDING_GATE_V1"
    semantic_download = client.get("/api/n31/artifacts/semantic-review")
    assert semantic_download.status_code == 200
    assert semantic_download.json()["report_id"] == "N31_SEMANTIC_REVIEW_V1"
    selective_download = client.get("/api/n31/artifacts/selective-rebuild")
    assert selective_download.status_code == 200
    assert selective_download.json()["report_id"] == "N31_SELECTIVE_REBUILD_V1"
    evidence = client.get("/api/n31/evidence/E144")
    assert evidence.status_code == 200
    assert evidence.json()["navigation"] == {
        "kind": "AUDIO_TIME",
        "label": "40.7–68.7秒",
        "safe_preview_url": None,
        "raw_source_url": None,
    }
    video_evidence = client.get("/api/n31/evidence/E001")
    assert video_evidence.status_code == 200
    assert "keyframe" not in video_evidence.json()["locator"]
    assert video_evidence.json()["navigation"]["safe_preview_url"].startswith(
        "/api/n31/checklist/previews/S"
    )
    assert client.get("/api/n31/evidence/E999").status_code == 404

    asr_session = client.post("/api/n31/asr-corrections/sessions")
    assert asr_session.status_code == 200
    asr_session_id = asr_session.json()["session_id"]
    assert asr_session.json()["status"] == "OPEN"
    assert asr_session.json()["summary"] == {
        "answer_count": 12,
        "corrected_answer_count": 0,
        "correction_event_count": 0,
        "evidence_binding_count": 12,
    }
    q02 = next(
        item
        for item in asr_session.json()["answers"]
        if item["question_id"] == "Q02"
    )
    corrected = client.patch(
        f"/api/n31/asr-corrections/sessions/{asr_session_id}/answers/Q02",
        json={
            "corrected_text": q02["effective_corrected_text"] + " 人工复听确认。",
            "operator": "实际操作者",
            "reason": "复听后补充确认",
        },
    )
    assert corrected.status_code == 200
    corrected_q02 = next(
        item for item in corrected.json()["answers"] if item["question_id"] == "Q02"
    )
    assert corrected.json()["status"] == "CORRECTED"
    assert corrected.json()["summary"]["corrected_answer_count"] == 1
    assert corrected_q02["correction_count"] == 1
    assert corrected_q02["latest_operator"] == "实际操作者"
    assert (
        corrected_q02["evidence_binding"]["current_sha256"]
        != corrected_q02["evidence_binding"]["baseline_sha256"]
    )
    persisted_asr = client.get(
        f"/api/n31/asr-corrections/sessions/{asr_session_id}"
    )
    assert persisted_asr.status_code == 200
    invalid_asr = client.patch(
        f"/api/n31/asr-corrections/sessions/{asr_session_id}/answers/Q02",
        json={
            "corrected_text": "删除全部必要术语",
            "operator": "实际操作者",
            "reason": "错误尝试",
        },
    )
    assert invalid_asr.status_code == 400
    assert "必要术语" in invalid_asr.json()["detail"]
    assert client.get("/api/n31/asr-corrections/sessions/not-found").status_code == 404

    conflict_session = client.post("/api/n31/conflicts/sessions")
    assert conflict_session.status_code == 200
    conflict_id = conflict_session.json()["session_id"]
    assert conflict_session.json()["status"] == "AUTO_FINALIZED"
    assert conflict_session.json()["finalization"] == {
        "publishable": True,
        "final_sop_sha256": conflict_session.json()["source_bindings"][
            "proposed_sop_sha256"
        ],
        "proposed_residual_conflict_count": 0,
        "adopted_unresolved_conflict_count": 0,
        "adopted_conflict_count": 5,
        "rejected_conflict_count": 0,
        "pending_conflict_count": 0,
    }
    assert all(
        item["final_result"] in {"ADOPTED", "RESOLVED_BY_RELATED_CHANGE"}
        for item in conflict_session.json()["decisions"]
    )
    assert client.get(f"/api/n31/conflicts/sessions/{conflict_id}").status_code == 200
    manual_override = client.patch(
        f"/api/n31/conflicts/sessions/{conflict_id}/decisions/C001",
        json={"human_decision": "APPROVED", "comment": "错误尝试人工覆盖"},
    )
    assert manual_override.status_code == 400
    assert "不能伪装成人工裁决" in manual_override.json()["detail"]
    assert client.get("/api/n31/conflicts/sessions/not-found").status_code == 404

    review = client.post("/api/n31/review/sessions")
    assert review.status_code == 200
    review_id = review.json()["session_id"]
    assert len(review.json()["steps"]) == 13
    rebuilt = client.post(
        f"/api/n31/review/sessions/{review_id}/steps/S12/rebuild"
    )
    assert rebuilt.status_code == 200
    assert rebuilt.json()["scope"]["quiz_question_ids"] == ["Q03"]
    assert rebuilt.json()["scope"]["unchanged_step_count"] == 12
    reordered = client.post(
        f"/api/n31/review/sessions/{review_id}/reorder",
        json={"step_id": "S11", "target_position": 12},
    )
    assert reordered.status_code == 200
    assert [item["step_id"] for item in reordered.json()["steps"]][10:13] == [
        "S12",
        "S11",
        "S13",
    ]
    invalid_order = client.post(
        f"/api/n31/review/sessions/{review_id}/reorder",
        json={"step_id": "S09", "target_position": 8},
    )
    assert invalid_order.status_code == 400
    assert "S08 必须先于 S09" in invalid_order.json()["detail"]
    locked = client.patch(
        f"/api/n31/review/sessions/{review_id}/steps/S01",
        json={"locked": True},
    )
    assert locked.status_code == 200
    confirmed = client.patch(
        f"/api/n31/review/sessions/{review_id}/steps/S01",
        json={"confirmed": True},
    )
    assert confirmed.status_code == 200
    s01 = next(
        item for item in confirmed.json()["steps"] if item["step_id"] == "S01"
    )
    assert s01["locked"] is True and s01["confirmed"] is True
    assert client.get(f"/api/n31/review/sessions/{review_id}").status_code == 200
    assert client.get("/api/n31/artifacts/private-video").status_code == 404
    rerun = client.post("/api/n31/run")
    assert rerun.status_code == 200
    assert rerun.json()["gold_status"] == "GOLD"
    assert rerun.json()["before"]["severe_error_count"] == 5
    assert rerun.json()["after"]["severe_error_count"] == 0
    assert rerun.json()["stage_run"]["rebuilt_stages"] == [
        "INGESTING",
        "EXTRACTING",
        "PLANNING",
        "CREATING",
        "VERIFYING_INITIAL",
        "REVISING",
        "VERIFYING_FINAL",
        "RENDERING",
    ]
    current_run = client.get("/api/n31/stages/current")
    assert current_run.status_code == 200
    assert current_run.json()["published"] is True
    assert current_run.json()["status"] == "COMPLETED"
    stage_rerun = client.post("/api/n31/stages/RENDERING/rerun")
    assert stage_rerun.status_code == 200
    assert stage_rerun.json()["reused_stages"] == [
        "INGESTING",
        "EXTRACTING",
        "PLANNING",
        "CREATING",
        "VERIFYING_INITIAL",
        "REVISING",
        "VERIFYING_FINAL",
    ]
    assert stage_rerun.json()["rebuilt_stages"] == ["RENDERING"]
    active = client.get("/api/n31").json()
    assert active["summary"]["metrics_status"] == "FINAL"
    assert len(active["checklist"]["items"]) == 13
    assert active["stage_run"]["start_stage"] == "RENDERING"
    assert all(
        stage["metrics"]["process_peak_rss_bytes"] > 0
        and stage["metrics"]["output_bytes"]
        == sum(item["size_bytes"] for item in stage["outputs"])
        for stage in active["stage_run"]["stages"]
    )


def test_artifact_media_type_is_explicit() -> None:
    assert _artifact_media_type(ROOT / "artifact.json") == "application/json"
    assert _artifact_media_type(ROOT / "artifact.pdf") == "application/pdf"
    assert _artifact_media_type(ROOT / "artifact.mp4") == "video/mp4"
    assert _artifact_media_type(ROOT / "artifact.bin") == "application/octet-stream"


def test_upload_requires_at_least_one_asset(tmp_path) -> None:
    client = TestClient(create_app(tmp_path))
    response = client.post("/api/ingest")
    assert response.status_code == 400


def test_asr_requires_explicit_external_processing_authorization(tmp_path) -> None:
    client = TestClient(create_app(tmp_path))
    response = client.post(
        "/api/ingest",
        data={"transcribe": "true"},
        files={"audio": ("expert.wav", b"not-a-real-wave", "audio/wav")},
    )
    assert response.status_code == 400
    assert "授权" in response.json()["detail"]
