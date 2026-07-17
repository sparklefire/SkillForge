import copy
import json
import stat
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skillforge.contracts import validate_document
from skillforge.media import resolve_ffmpeg
from skillforge.video_preview import (
    generate_previews,
    validate_output_profile,
    verified_preview_path,
)
from skillforge.web import create_app


ROOT = Path(__file__).resolve().parents[1]


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_video(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(resolve_ffmpeg()),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=48000",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _profile(case_id: str) -> dict:
    payload = json.loads(
        (ROOT / "cases/n31/output_profile.json").read_text(encoding="utf-8")
    )
    payload["case_id"] = case_id
    return payload


def _ingest(case_id: str, privacy_status: str = "LOCAL_QA_PASSED") -> dict:
    return {
        "version": 1,
        "case_id": case_id,
        "sources": [
            {
                "source_id": "TEST_VIDEO",
                "type": "video",
                "role": "CONTINUOUS_OPERATION",
                "path": "input/source.mp4",
                "approved_for_local_ingest": True,
                "privacy_status": privacy_status,
            }
        ],
    }


def test_generates_private_low_bitrate_preview_with_timeline_mapping(tmp_path) -> None:
    source = tmp_path / "input/source.mp4"
    _make_video(source)
    profile = _profile("n31_media_change")
    _write(tmp_path / "profile.json", profile)
    _write(tmp_path / "ingest.json", _ingest("n31_media_change"))
    output_dir = tmp_path / "private/previews"
    report_path = tmp_path / "evaluations/preview.json"

    manifest = generate_previews(
        project_root=tmp_path,
        ingest_manifest_path=Path("ingest.json"),
        output_profile_path=Path("profile.json"),
        output_dir=Path("private/previews"),
        report_path=Path("evaluations/preview.json"),
    )
    validate_document(manifest, "video_preview_manifest.schema.json")
    assert manifest["status"] == "COMPLETED"
    assert manifest["summary"]["source_count"] == 1
    assert manifest["summary"]["all_checks_passed"] is True
    item = manifest["items"][0]
    assert item["source_id"] == "TEST_VIDEO"
    assert item["width"] <= 854 and item["height"] <= 480
    assert item["fps"] <= 15.1
    assert item["video_codec"] == "h264"
    assert item["audio_codec"] == "aac"
    assert item["average_total_bitrate_kbps"] <= 600
    assert item["duration_delta_ms"] <= 250
    assert item["timeline_mapping"] == [
        {
            "source_start_ms": 0,
            "source_end_ms": item["source_duration_ms"],
            "preview_start_ms": 0,
            "preview_end_ms": item["preview_duration_ms"],
        }
    ]
    assert all(item["checks"].values())
    preview, verified_item = verified_preview_path(
        manifest,
        source_id="TEST_VIDEO",
        output_dir=output_dir,
        output_profile=profile,
    )
    assert verified_item == item
    assert preview.is_file()
    assert stat.S_IMODE(output_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(preview.stat().st_mode) == 0o600
    assert report_path.is_file()

    client = TestClient(
        create_app(
            tmp_path / "web",
            ROOT / "cases/n31/demo_bundle",
            n31_preview_dir=output_dir,
            n31_preview_manifest_path=report_path,
            n31_output_profile_path=tmp_path / "profile.json",
        )
    )
    payload = client.get("/api/n31")
    assert payload.status_code == 200
    assert payload.json()["output_profile"]["audience"]["primary_role"] == "NEW_OPERATOR"
    assert payload.json()["video_previews"]["available_count"] == 1
    assert payload.json()["video_previews"]["availability"] == [
        {
            "source_id": "TEST_VIDEO",
            "available": True,
            "media_url": "/api/n31/previews/TEST_VIDEO",
        }
    ]
    streamed = client.get(
        "/api/n31/previews/TEST_VIDEO", headers={"Range": "bytes=0-31"}
    )
    assert streamed.status_code == 206
    assert streamed.headers["content-type"] == "video/mp4"
    assert b"ftyp" in streamed.content
    assert client.get("/api/n31/previews/UNKNOWN").status_code == 404

    with preview.open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(ValueError, match="大小或SHA-256"):
        verified_preview_path(
            manifest,
            source_id="TEST_VIDEO",
            output_dir=output_dir,
            output_profile=profile,
        )


def test_preview_rejects_video_without_local_privacy_qa(tmp_path) -> None:
    _make_video(tmp_path / "input/source.mp4")
    _write(tmp_path / "profile.json", _profile("preview_test"))
    _write(tmp_path / "ingest.json", _ingest("preview_test", "PENDING"))
    with pytest.raises(ValueError, match="隐私QA"):
        generate_previews(
            project_root=tmp_path,
            ingest_manifest_path=Path("ingest.json"),
            output_profile_path=Path("profile.json"),
            output_dir=Path("private/previews"),
            report_path=Path("evaluations/preview.json"),
        )


def test_checked_in_output_profile_is_complete_and_matches_storyboard() -> None:
    profile = json.loads(
        (ROOT / "cases/n31/output_profile.json").read_text(encoding="utf-8")
    )
    storyboard = json.loads(
        (ROOT / "cases/n31/training_video_storyboard.json").read_text(
            encoding="utf-8"
        )
    )
    validate_output_profile(profile)
    assert profile["audience"] == {
        "primary_role": "NEW_OPERATOR",
        "experience_level": "BEGINNER",
        "usage_context": "ON_DEVICE_TASK_SUPPORT",
    }
    assert profile["language"]["locale"] == "zh-CN"
    assert (
        profile["duration"]["training_video_target_seconds"]
        == storyboard["target_duration_seconds"]
        == 80
    )
    assert {item["output_type"] for item in profile["output_types"]} == {
        "STRUCTURED_SOP",
        "MOBILE_CHECKLIST",
        "TRAINING_QUIZ",
        "A4_POSTER",
        "TRAINING_VIDEO",
        "REVISION_AUDIT",
    }

    invalid = copy.deepcopy(profile)
    invalid["output_types"].pop()
    with pytest.raises(ValueError, match="P0输出类型"):
        validate_output_profile(invalid)
