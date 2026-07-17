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
        "training_video_status": "READY_FOR_HUMAN_REVIEW",
    }
    assert client.get("/api/demo").status_code == 404
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


def test_web_accepts_operator_reviewed_gold_result(tmp_path) -> None:
    n31_dir = ROOT / "cases" / "n31" / "demo_bundle"
    client = TestClient(create_app(tmp_path / "web", n31_dir))
    response = client.get("/api/n31")
    assert response.status_code == 200
    assert response.json()["summary"]["gold_status"] == "GOLD"
    assert response.json()["summary"]["metrics_status"] == "FINAL"
    assert (
        response.json()["multisource_comparison"]["source_ablation"][
            "two_or_more_source_types"
        ]["coverage"]
        == 1.0
    )
    assert response.json()["visual_review"]["summary"]["contradicted_count"] == 0
    training_video = response.json()["training_video"]
    assert training_video["status"] == "READY_FOR_HUMAN_REVIEW"
    assert training_video["output"]["duration_ms"] == 80_000
    assert training_video["coverage"]["covered_gold_step_count"] == 13
    assert training_video["final_human_review_required"] is True
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
    assert len(response.json()["checklist"]["items"]) == 13
    assert len(response.json()["quiz"]["questions"]) == 5
    checklist = client.get("/api/n31/artifacts/checklist")
    assert checklist.status_code == 200
    assert checklist.json()["case_id"] == "n31_media_change"
    assert "attachment" in checklist.headers["content-disposition"]
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
    assert client.get("/api/n31/artifacts/private-video").status_code == 404
    rerun = client.post("/api/n31/run")
    assert rerun.status_code == 200
    assert rerun.json()["gold_status"] == "GOLD"
    assert rerun.json()["before"]["severe_error_count"] == 5
    assert rerun.json()["after"]["severe_error_count"] == 0
    active = client.get("/api/n31").json()
    assert active["summary"]["metrics_status"] == "FINAL"
    assert len(active["checklist"]["items"]) == 13


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
