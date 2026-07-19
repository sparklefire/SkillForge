from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skillforge.contracts import validate_document
from skillforge.final_recording import evaluate_final_recording, write_private_report
from skillforge.final_recording_review import (
    initialize_final_recording_review,
    _write_private_json as _write_final_recording_json,
)
from skillforge.final_rehearsal import initialize_final_rehearsal
from skillforge.guided_human_review import (
    FINAL_RECORDING_CHECK_PROMPTS,
    REHEARSAL_COMPLETION_PROMPTS,
    REHEARSAL_SEGMENT_KEYS,
    TRAINING_CHECK_PROMPTS,
    GuidedHumanReviewDeclined,
    GuidedHumanReviewError,
    PlaybackResult,
    _safe_player_environment,
    complete_final_recording_review,
    complete_final_rehearsal,
    complete_training_video_review,
)
from skillforge.training_video_review import (
    DEFAULT_MANIFEST,
    TrainingVideoReviewError,
    _write_private_json as _write_training_json,
    initialize_training_video_review,
    migrate_pending_training_video_review,
)


ROOT = Path(__file__).resolve().parents[1]
STORYBOARD = ROOT / "config/final_recording_storyboard.json"
FINAL_POLICY = ROOT / "config/final_recording_policy.json"
RUNBOOK = ROOT / "cases/n31/pitch_runbook.json"
REHEARSAL_POLICY = ROOT / "config/final_rehearsal_policy.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _playback(duration_ms: int) -> PlaybackResult:
    started = datetime(2026, 7, 19, 5, 0, tzinfo=timezone.utc)
    return PlaybackResult(
        started_at=started,
        completed_at=started + timedelta(milliseconds=duration_ms),
        elapsed_ms=duration_ms,
    )


def _training_basis(tmp_path: Path) -> tuple[Path, Path]:
    public = tmp_path / "public"
    public.mkdir(parents=True)
    video = public / "n31_training_video_v1.mp4"
    video.write_bytes(b"synthetic guided training video")
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    manifest["output"]["sha256"] = _sha256(video)
    manifest["output"]["bytes"] = video.stat().st_size
    manifest_path = public / "n31_training_video_manifest_v1.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    validate_document(manifest, "training_video_manifest.schema.json")
    return manifest_path, video


def _final_recording_basis(tmp_path: Path) -> dict[str, Path]:
    private = tmp_path / "submission"
    private.mkdir(mode=0o700)
    recording = private / "skillforge_final_recording.mp4"
    recording.write_bytes(b"synthetic guided final recording")
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
    _write_final_recording_json(build, build_path, private_root=private)
    return {
        "private": private,
        "recording": recording,
        "machine_qa": machine_qa,
        "build": build_path,
    }


def test_guided_training_review_records_watch_timing_and_qa(tmp_path: Path) -> None:
    manifest, video = _training_basis(tmp_path)
    private = tmp_path / "submission"
    review = private / "training_video_review.json"
    qa = private / "training_video_review_qa.json"
    initialize_training_video_review(
        review,
        manifest_path=manifest,
        video_path=video,
        private_root=private,
    )
    answers = {key: True for key in TRAINING_CHECK_PROMPTS}

    report = complete_training_video_review(
        _playback(80000),
        answers,
        input_path=review,
        report_path=qa,
        manifest_path=manifest,
        video_path=video,
    )

    saved = json.loads(review.read_text(encoding="utf-8"))
    assert saved["status"] == "READY_FOR_CHECK"
    assert saved["watch_started_at"] == "2026-07-19T05:00:00+00:00"
    assert saved["watch_completed_at"] == "2026-07-19T05:01:20+00:00"
    assert report["watch_elapsed_ms"] == 80000
    assert report["human_gate_status"] == "PENDING"
    assert report["data_policy"]["human_confirmation_generated"] is False
    assert stat.S_IMODE(qa.stat().st_mode) == 0o600


def test_guided_training_decline_or_short_playback_does_not_write(
    tmp_path: Path,
) -> None:
    manifest, video = _training_basis(tmp_path)
    private = tmp_path / "submission"
    review = private / "training_video_review.json"
    qa = private / "training_video_review_qa.json"
    initialize_training_video_review(
        review,
        manifest_path=manifest,
        video_path=video,
        private_root=private,
    )
    before = review.read_bytes()
    declined = {key: True for key in TRAINING_CHECK_PROMPTS}
    declined["narration_pacing_acceptable"] = False
    with pytest.raises(GuidedHumanReviewDeclined):
        complete_training_video_review(
            _playback(80000),
            declined,
            input_path=review,
            report_path=qa,
            manifest_path=manifest,
            video_path=video,
        )
    assert review.read_bytes() == before
    with pytest.raises(GuidedHumanReviewError, match="播放时长不足"):
        complete_training_video_review(
            _playback(30000),
            {key: True for key in TRAINING_CHECK_PROMPTS},
            input_path=review,
            report_path=qa,
            manifest_path=manifest,
            video_path=video,
        )
    assert review.read_bytes() == before
    assert not qa.exists()


def test_legacy_training_template_migrates_only_when_pristine(tmp_path: Path) -> None:
    manifest, video = _training_basis(tmp_path)
    private = tmp_path / "submission"
    review = private / "training_video_review.json"
    initialize_training_video_review(
        review,
        manifest_path=manifest,
        video_path=video,
        private_root=private,
    )
    legacy = json.loads(review.read_text(encoding="utf-8"))
    legacy.pop("watch_started_at")
    legacy.pop("watch_completed_at")
    review.write_text(json.dumps(legacy), encoding="utf-8")
    review.chmod(0o600)

    assert migrate_pending_training_video_review(review, private_root=private) is True
    migrated = json.loads(review.read_text(encoding="utf-8"))
    assert migrated["watch_started_at"] is None
    assert migrated["watch_completed_at"] is None
    assert migrate_pending_training_video_review(review, private_root=private) is False

    legacy.pop("watch_started_at", None)
    legacy.pop("watch_completed_at", None)
    legacy["notes"] = "已有人工内容"
    review.write_text(json.dumps(legacy), encoding="utf-8")
    review.chmod(0o600)
    with pytest.raises(TrainingVideoReviewError, match="拒绝自动迁移"):
        migrate_pending_training_video_review(review, private_root=private)


def test_guided_final_recording_review_records_qa_without_confirmation(
    tmp_path: Path,
) -> None:
    paths = _final_recording_basis(tmp_path)
    review = paths["private"] / "final_recording_review.json"
    qa = paths["private"] / "final_recording_review_qa.json"
    initialize_final_recording_review(
        review,
        recording_path=paths["recording"],
        machine_qa_path=paths["machine_qa"],
        build_report_path=paths["build"],
        storyboard_path=STORYBOARD,
        policy_path=FINAL_POLICY,
        private_root=paths["private"],
    )

    report = complete_final_recording_review(
        _playback(180000),
        {key: True for key in FINAL_RECORDING_CHECK_PROMPTS},
        input_path=review,
        report_path=qa,
        recording_path=paths["recording"],
        machine_qa_path=paths["machine_qa"],
        build_report_path=paths["build"],
        storyboard_path=STORYBOARD,
        policy_path=FINAL_POLICY,
    )

    assert report["status"] == "READY_FOR_HUMAN_CONFIRMATION"
    assert report["watch_elapsed_ms"] == 180000
    assert report["human_gate_status"] == "PENDING"
    assert report["official_rules_boundary"][
        "official_video_requirements_verified"
    ] is False


def test_guided_rehearsal_records_contiguous_boundaries_and_qa(
    tmp_path: Path,
) -> None:
    private = tmp_path / "submission"
    record = private / "final_stage_rehearsal.json"
    qa = private / "final_stage_rehearsal_qa.json"
    initialize_final_rehearsal(record, runbook_path=RUNBOOK, private_root=private)
    boundaries = [0, 20000, 40000, 70000, 110000, 140000, 160000, 178000]
    segment_checks = [
        {key: True for key in REHEARSAL_SEGMENT_KEYS} for _ in range(7)
    ]
    completion = {key: True for key in REHEARSAL_COMPLETION_PROMPTS}

    report = complete_final_rehearsal(
        boundaries,
        segment_checks,
        completion,
        started_at=datetime(2026, 7, 19, 6, 0, tzinfo=timezone.utc),
        input_path=record,
        report_path=qa,
        runbook_path=RUNBOOK,
        policy_path=REHEARSAL_POLICY,
    )

    saved = json.loads(record.read_text(encoding="utf-8"))
    assert report["duration"]["actual_ms"] == 178000
    assert report["human_gate_status"] == "PENDING"
    assert [item["actual_start_ms"] for item in saved["segments"]] == boundaries[:-1]
    assert [item["actual_end_ms"] for item in saved["segments"]] == boundaries[1:]


def test_guided_rehearsal_rejects_bad_duration_or_human_check_without_write(
    tmp_path: Path,
) -> None:
    private = tmp_path / "submission"
    record = private / "final_stage_rehearsal.json"
    qa = private / "final_stage_rehearsal_qa.json"
    initialize_final_rehearsal(record, runbook_path=RUNBOOK, private_root=private)
    before = record.read_bytes()
    segment_checks = [
        {key: True for key in REHEARSAL_SEGMENT_KEYS} for _ in range(7)
    ]
    completion = {key: True for key in REHEARSAL_COMPLETION_PROMPTS}
    with pytest.raises(GuidedHumanReviewError, match="175至180秒"):
        complete_final_rehearsal(
            [0, 20000, 40000, 70000, 110000, 140000, 160000, 181000],
            segment_checks,
            completion,
            started_at=datetime.now(timezone.utc),
            input_path=record,
            report_path=qa,
            runbook_path=RUNBOOK,
            policy_path=REHEARSAL_POLICY,
        )
    declined = deepcopy(segment_checks)
    declined[2]["proof_points_verified"] = False
    with pytest.raises(GuidedHumanReviewDeclined):
        complete_final_rehearsal(
            [0, 20000, 40000, 70000, 110000, 140000, 160000, 178000],
            declined,
            completion,
            started_at=datetime.now(timezone.utc),
            input_path=record,
            report_path=qa,
            runbook_path=RUNBOOK,
            policy_path=REHEARSAL_POLICY,
        )
    assert record.read_bytes() == before
    assert not qa.exists()


def test_guided_status_script_is_safe_and_player_env_excludes_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEP_API_KEY", "do-not-forward")
    assert "STEP_API_KEY" not in _safe_player_environment()
    script = ROOT / "scripts/run_guided_human_review.sh"
    assert os.access(script, os.X_OK)
    result = subprocess.run(
        ["bash", str(script), "status"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["automatic_human_confirmations"] == 0
    assert str(ROOT) not in result.stdout
