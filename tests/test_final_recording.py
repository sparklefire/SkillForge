from __future__ import annotations

import json
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from skillforge.contracts import ContractValidationError, validate_document
from skillforge.final_recording import (
    ROOT,
    _duration_measurements,
    evaluate_final_recording,
    final_recording_qa_issue,
    load_policy,
    write_private_report,
)


POLICY = ROOT / "config/final_recording_policy.json"


def _private_recording(tmp_path: Path) -> Path:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    private.chmod(0o700)
    recording = private / "skillforge_final_recording.mp4"
    recording.write_bytes(b"private synthetic recording fixture")
    recording.chmod(0o600)
    return recording


def _probe(*, duration_ms: int = 178000) -> dict:
    return {
        "duration_ms": duration_ms,
        "video_streams": [
            {
                "codec": "h264",
                "width": 1920,
                "height": 1080,
                "fps": 30.0,
            }
        ],
        "audio_streams": [
            {
                "codec": "aac",
                "sample_rate": 48000,
                "channels": 2,
            }
        ],
    }


def _loudness(_: Path) -> dict[str, float]:
    return {
        "integrated_lufs": -18.0,
        "loudness_range_lu": 4.0,
        "true_peak_dbtp": -1.0,
    }


def _interruptions(_: Path, __: dict) -> dict[str, int | float]:
    return {
        "silence_total_ms": 8000,
        "silence_ratio": 0.044944,
        "maximum_contiguous_silence_ms": 2500,
        "black_total_ms": 1000,
        "black_ratio": 0.005618,
        "maximum_contiguous_black_ms": 500,
    }


def _ready_report(recording: Path) -> dict:
    return evaluate_final_recording(
        recording,
        policy_path=POLICY,
        private_root=recording.parent,
        probe_fn=lambda _: _probe(),
        loudness_fn=_loudness,
        interruption_fn=_interruptions,
    )


def test_policy_is_explicitly_internal_and_not_an_official_rule() -> None:
    policy = load_policy(POLICY)

    assert policy["policy_basis"] == "INTERNAL_REHEARSAL_TARGET_NOT_OFFICIAL_RULE"
    assert policy["official_video_requirements_verified"] is False
    assert policy["duration"] == {"minimum_ms": 175000, "maximum_ms": 180500}


def test_ready_report_binds_private_media_and_keeps_human_review_pending(
    tmp_path: Path,
) -> None:
    recording = _private_recording(tmp_path)
    report = _ready_report(recording)
    validate_document(report, "final_recording_qa.schema.json")

    assert report["status"] == "READY_FOR_HUMAN_REVIEW"
    assert all(report["checks"].values())
    assert report["human_review"]["status"] == "PENDING"
    assert report["official_rules_boundary"]["official_video_requirements_verified"] is False
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(recording.resolve()) not in serialized
    assert report["data_policy"]["external_model_calls"] == 0


def test_machine_failures_are_reported_without_closing_human_review(
    tmp_path: Path,
) -> None:
    recording = _private_recording(tmp_path)
    interruptions = _interruptions(recording, {})
    interruptions["silence_ratio"] = 0.5
    report = evaluate_final_recording(
        recording,
        policy_path=POLICY,
        private_root=recording.parent,
        probe_fn=lambda _: _probe(duration_ms=160000),
        loudness_fn=_loudness,
        interruption_fn=lambda *_: interruptions,
    )

    validate_document(report, "final_recording_qa.schema.json")
    assert report["status"] == "MACHINE_QA_FAILED"
    assert report["checks"]["duration_passed"] is False
    assert report["checks"]["silence_ratio_passed"] is False
    assert report["human_review"]["status"] == "PENDING"


def test_schema_rejects_ready_status_with_failed_check(tmp_path: Path) -> None:
    report = _ready_report(_private_recording(tmp_path))
    invalid = deepcopy(report)
    invalid["checks"]["audio_stream_passed"] = False

    with pytest.raises(ContractValidationError):
        validate_document(invalid, "final_recording_qa.schema.json")


def test_private_report_permissions_and_media_binding(tmp_path: Path) -> None:
    recording = _private_recording(tmp_path)
    report = _ready_report(recording)
    destination = recording.parent / "final_recording_qa.json"
    write_private_report(report, destination)
    evidence = {
        "kind": "LOCAL_FILE",
        "sha256": report["media"]["sha256"],
        "size_bytes": report["media"]["bytes"],
    }

    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert final_recording_qa_issue(destination, evidence) is None
    destination.chmod(0o644)
    assert (
        final_recording_qa_issue(destination, evidence)
        == "FINAL_RECORDING_QA_PERMISSIONS_UNSAFE"
    )


def test_interruption_parser_sums_intervals_and_caps_at_media_duration() -> None:
    log = "silence_duration: 1.25\nsilence_duration: 2.75\nsilence_duration: 8.00"

    total, ratio, maximum = _duration_measurements(log, "silence_duration", 10000)

    assert total == 10000
    assert ratio == 1.0
    assert maximum == 8000


def test_final_recording_script_is_executable() -> None:
    script = ROOT / "scripts/check_final_recording.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
