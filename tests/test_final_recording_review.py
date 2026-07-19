from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from skillforge.contracts import ContractValidationError, validate_document
from skillforge.final_recording import evaluate_final_recording, write_private_report
from skillforge.final_recording_review import (
    FinalRecordingReviewError,
    _write_private_json,
    final_recording_review_qa_issue,
    initialize_final_recording_review,
    verify_final_recording_review,
)
from skillforge.submission import build_submission_preflight
from skillforge.submission_closeout import _probe_final_recording


ROOT = Path(__file__).resolve().parents[1]
STORYBOARD = ROOT / "config/final_recording_storyboard.json"
POLICY = ROOT / "config/final_recording_policy.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _basis(tmp_path: Path) -> dict[str, Path]:
    private = tmp_path / "submission"
    private.mkdir(mode=0o700)
    recording = private / "skillforge_final_recording.mp4"
    recording.write_bytes(b"synthetic private final recording")
    recording.chmod(0o600)
    qa = evaluate_final_recording(
        recording,
        private_root=private,
        probe_fn=lambda _: {
            "duration_ms": 178000,
            "video_streams": [
                {"codec": "h264", "width": 1920, "height": 1080, "fps": 30.0}
            ],
            "audio_streams": [
                {"codec": "aac", "sample_rate": 48000, "channels": 2}
            ],
        },
        loudness_fn=lambda _: {
            "integrated_lufs": -18.0,
            "loudness_range_lu": 3.0,
            "true_peak_dbtp": -1.0,
        },
        interruption_fn=lambda *_: {
            "silence_total_ms": 4000,
            "silence_ratio": 0.022472,
            "maximum_contiguous_silence_ms": 1500,
            "black_total_ms": 500,
            "black_ratio": 0.002809,
            "maximum_contiguous_black_ms": 500,
        },
    )
    machine_qa = private / "final_recording_qa.json"
    write_private_report(qa, machine_qa)
    digest = "a" * 64
    build = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "FINAL_RECORDING_BUILD",
        "generated_at": "2026-07-19T00:00:00+00:00",
        "status": "READY_FOR_HUMAN_REVIEW",
        "storyboard_sha256": _sha256(STORYBOARD),
        "scene_count": 9,
        "target_duration_ms": 178000,
        "media": {
            "filename": recording.name,
            "sha256": _sha256(recording),
            "bytes": recording.stat().st_size,
            "duration_ms": 178000,
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "video_codec": "h264",
            "audio_codec": "aac",
        },
        "scenes": [
            {
                "scene_id": f"R{order:02d}",
                "order": order,
                "duration_ms": 15000,
                "visual_kind": "SCREENSHOT",
                "visual_source_sha256": digest,
                "narration_sha256": digest,
                "tts_audio_sha256": digest,
                "rendered_sha256": digest,
                "output_probe_ms": order * 10000,
                "difference_hash_distance": 0,
                "sequence_match": True,
            }
            for order in range(1, 10)
        ],
        "tts": {
            "model": "stepaudio-2.5-tts",
            "voice": "zhixingjiejie",
            "scene_count": 9,
            "generated_count": 0,
            "reused_count": 9,
            "external_model_calls": 0,
            "text_only": True,
        },
        "machine_qa": {
            "status": "READY_FOR_HUMAN_REVIEW",
            "report_sha256": _sha256(machine_qa),
            "all_checks_passed": True,
            "scene_sequence_all_matched": True,
        },
        "human_review": {
            "required": True,
            "status": "PENDING",
            "automatic_approval": False,
        },
        "data_policy": {
            "private_local_state": True,
            "screenshot_assets_private": True,
            "raw_media_sent_to_tts": False,
            "tts_text_only": True,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "automatic_human_approval": False,
        },
    }
    validate_document(build, "final_recording_build.schema.json")
    build_path = private / "final_recording_build.json"
    _write_private_json(build, build_path, private_root=private)
    return {
        "private": private,
        "recording": recording,
        "machine_qa": machine_qa,
        "build": build_path,
    }


def _initialize(paths: dict[str, Path]) -> Path:
    review = paths["private"] / "final_recording_review.json"
    initialize_final_recording_review(
        review,
        recording_path=paths["recording"],
        machine_qa_path=paths["machine_qa"],
        build_report_path=paths["build"],
        storyboard_path=STORYBOARD,
        policy_path=POLICY,
        private_root=paths["private"],
    )
    return review


def _complete(paths: dict[str, Path], review: Path) -> dict:
    document = json.loads(review.read_text(encoding="utf-8"))
    document.update(
        {
            "updated_at": "2026-07-19T00:03:01+00:00",
            "status": "READY_FOR_CHECK",
            "watch_started_at": "2026-07-19T00:00:00+00:00",
            "watch_completed_at": "2026-07-19T00:03:00+00:00",
            "playback_method": "LOCAL_PLAYER",
            "notes": "private reviewer note",
        }
    )
    document["checks"] = {key: True for key in document["checks"]}
    _write_private_json(document, review, private_root=paths["private"])
    return verify_final_recording_review(
        review,
        recording_path=paths["recording"],
        machine_qa_path=paths["machine_qa"],
        build_report_path=paths["build"],
        storyboard_path=STORYBOARD,
        policy_path=POLICY,
        private_root=paths["private"],
    )


def test_initialization_binds_current_candidate_and_stays_pending(
    tmp_path: Path,
) -> None:
    paths = _basis(tmp_path)
    review = _initialize(paths)
    document = validate_document(
        json.loads(review.read_text(encoding="utf-8")),
        "final_recording_review.schema.json",
    )

    assert document["status"] == "PENDING_INPUT"
    assert document["recording"]["sha256"] == _sha256(paths["recording"])
    assert all(value is False for value in document["checks"].values())
    assert stat.S_IMODE(paths["private"].stat().st_mode) == 0o700
    assert stat.S_IMODE(review.stat().st_mode) == 0o600
    assert not (paths["private"] / "final_recording_review_qa.json").exists()


def test_completed_review_generates_anonymous_qa_without_closing_gate(
    tmp_path: Path,
) -> None:
    paths = _basis(tmp_path)
    review = _initialize(paths)
    report = _complete(paths, review)
    validate_document(report, "final_recording_review_qa.schema.json")

    assert report["status"] == "READY_FOR_HUMAN_CONFIRMATION"
    assert report["watch_elapsed_ms"] == 180000
    assert all(report["checks"].values())
    assert report["human_gate_status"] == "PENDING"
    serialized = json.dumps(report, ensure_ascii=False)
    assert "private reviewer note" not in serialized
    assert "2026-07-19T00:00:00" not in serialized


def test_review_rejects_short_or_reversed_watch_interval(tmp_path: Path) -> None:
    paths = _basis(tmp_path)
    review = _initialize(paths)
    document = json.loads(review.read_text(encoding="utf-8"))
    document.update(
        {
            "updated_at": "2026-07-19T00:01:01+00:00",
            "status": "READY_FOR_CHECK",
            "watch_started_at": "2026-07-19T00:00:00+00:00",
            "watch_completed_at": "2026-07-19T00:01:00+00:00",
            "playback_method": "LOCAL_PLAYER",
        }
    )
    document["checks"] = {key: True for key in document["checks"]}
    _write_private_json(document, review, private_root=paths["private"])

    with pytest.raises(FinalRecordingReviewError, match="观看时长不足"):
        verify_final_recording_review(
            review,
            recording_path=paths["recording"],
            machine_qa_path=paths["machine_qa"],
            build_report_path=paths["build"],
            storyboard_path=STORYBOARD,
            policy_path=POLICY,
            private_root=paths["private"],
        )


def test_review_and_qa_detect_artifact_or_record_drift(tmp_path: Path) -> None:
    paths = _basis(tmp_path)
    review = _initialize(paths)
    report = _complete(paths, review)
    report_path = paths["private"] / "final_recording_review_qa.json"
    _write_private_json(report, report_path, private_root=paths["private"])
    evidence = {
        "kind": "LOCAL_FILE",
        "locator": str(paths["recording"]),
        "sha256": _sha256(paths["recording"]),
        "size_bytes": paths["recording"].stat().st_size,
    }
    kwargs = {
        "review_path": review,
        "recording_path": paths["recording"],
        "machine_qa_path": paths["machine_qa"],
        "build_report_path": paths["build"],
        "storyboard_path": STORYBOARD,
        "policy_path": POLICY,
    }

    assert final_recording_review_qa_issue(report_path, evidence, **kwargs) is None
    forged_report = dict(report)
    forged_report["watch_elapsed_ms"] += 1
    _write_private_json(forged_report, report_path, private_root=paths["private"])
    assert (
        final_recording_review_qa_issue(report_path, evidence, **kwargs)
        == "FINAL_RECORDING_REVIEW_QA_INVALID"
    )
    _write_private_json(report, report_path, private_root=paths["private"])
    review.write_text(review.read_text(encoding="utf-8") + " ", encoding="utf-8")
    review.chmod(0o600)
    assert (
        final_recording_review_qa_issue(report_path, evidence, **kwargs)
        == "FINAL_RECORDING_REVIEW_RECORD_CHANGED"
    )


def test_ready_schema_rejects_false_human_check(tmp_path: Path) -> None:
    paths = _basis(tmp_path)
    review = _initialize(paths)
    document = json.loads(review.read_text(encoding="utf-8"))
    document.update(
        {
            "status": "READY_FOR_CHECK",
            "watch_started_at": "2026-07-19T00:00:00+00:00",
            "watch_completed_at": "2026-07-19T00:03:00+00:00",
            "playback_method": "LOCAL_PLAYER",
        }
    )
    document["checks"] = {key: True for key in document["checks"]}
    document["checks"]["no_private_content_or_personal_ui"] = False

    with pytest.raises(ContractValidationError):
        validate_document(document, "final_recording_review.schema.json")


def test_pending_review_is_valid_preflight_state(tmp_path: Path) -> None:
    paths = _basis(tmp_path)
    review = _initialize(paths)
    absent = tmp_path / "absent"
    report = build_submission_preflight(
        root=ROOT,
        run_tests=False,
        allow_dirty=True,
        allow_missing_git=True,
        confirmations_path=absent / "human_gate_confirmations.json",
        team_roster_path=absent / "team_roster.json",
        final_rehearsal_path=absent / "final_stage_rehearsal.json",
        final_rehearsal_qa_path=absent / "final_stage_rehearsal_qa.json",
        training_video_review_path=absent / "training_video_review.json",
        training_video_review_qa_path=absent / "training_video_review_qa.json",
        final_recording_review_path=review,
        final_recording_review_qa_path=paths["private"]
        / "final_recording_review_qa.json",
        official_rules_review_path=absent / "official_rules_review.json",
        official_rules_review_qa_path=absent / "official_rules_review_qa.json",
    )
    checks = {item["check_id"]: item for item in report["automatic_checks"]}

    assert checks["FINAL_RECORDING_REVIEW_PRIVATE_STATE"]["status"] == "PASSED"
    assert "PENDING_INPUT" in checks["FINAL_RECORDING_REVIEW_PRIVATE_STATE"][
        "details"
    ][0]
    assert "FINAL_RECORDING_REVIEW" in report["pending_human_gates"]


def test_closeout_requires_review_before_final_recording_confirmation(
    tmp_path: Path,
) -> None:
    paths = _basis(tmp_path)
    before = _probe_final_recording(ROOT, paths["private"])
    assert before == {
        "status": "AWAITING_HUMAN",
        "evidence_state": "MACHINE_READY",
        "next_action": "初始化并填写最终录屏完整观看记录",
        "next_command": "bash scripts/check_final_recording_review.sh --init",
    }

    review = _initialize(paths)
    pending = _probe_final_recording(ROOT, paths["private"])
    assert pending["status"] == "AWAITING_HUMAN"
    assert pending["evidence_state"] == "DRAFT"
    report = _complete(paths, review)
    _write_private_json(
        report,
        paths["private"] / "final_recording_review_qa.json",
        private_root=paths["private"],
    )
    ready = _probe_final_recording(ROOT, paths["private"])
    assert ready["status"] == "READY_FOR_CONFIRMATION"
    assert ready["evidence_state"] == "MACHINE_READY"


def test_final_recording_review_script_is_executable() -> None:
    script = ROOT / "scripts/check_final_recording_review.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK)
