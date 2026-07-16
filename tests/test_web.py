from fastapi.testclient import TestClient

import json

from skillforge.demo import ROOT, run_demo
from skillforge.web import create_app


def test_native_web_health_and_demo(tmp_path) -> None:
    client = TestClient(create_app(tmp_path, tmp_path / "missing-n31"))
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "runtime": "native-python",
        "docker_required": False,
        "n31_rehearsal_available": False,
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
