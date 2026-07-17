from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from skillforge.contracts import ContractValidationError, validate_document
from skillforge.submission import (
    _find_secret_value_leaks,
    _find_sensitive_tracked_paths,
    build_submission_preflight,
)


ROOT = Path(__file__).resolve().parents[1]


def test_submission_preflight_preserves_human_gates() -> None:
    report = build_submission_preflight(
        root=ROOT,
        run_tests=False,
        allow_dirty=True,
        allow_missing_git=True,
    )
    validate_document(report, "submission_preflight.schema.json")
    checks = {item["check_id"]: item for item in report["automatic_checks"]}

    assert report["status"] == "DEVELOPMENT_CHECK"
    assert checks["PROJECT_IDENTITY"]["status"] == "PASSED"
    assert checks["REQUIRED_DOCUMENTS"]["status"] == "PASSED"
    assert checks["PITCH_PACKAGE"]["status"] == "PASSED"
    assert checks["PUBLIC_ARTIFACT_BOUNDARY"]["status"] == "PASSED"
    assert checks["TRACKED_SENSITIVE_PATHS"]["status"] in {"PASSED", "SKIPPED"}
    assert checks["ENV_AND_SECRET_SCAN"]["status"] in {"PASSED", "SKIPPED"}
    assert checks["AUTOMATED_TESTS"]["status"] == "SKIPPED"
    assert set(report["pending_human_gates"]) == {
        "TRAINING_VIDEO_FULL_WATCH",
        "FINAL_STAGE_REHEARSAL",
        "FINAL_RECORDING_REVIEW",
        "TEAM_ELIGIBILITY_CONFIRMED",
        "OFFICIAL_RULES_VERIFIED",
    }
    assert report["data_policy"]["contains_credentials"] is False
    assert report["data_policy"]["contains_raw_media"] is False


def test_sensitive_tracked_path_detection() -> None:
    findings = _find_sensitive_tracked_paths(
        [
            ".env",
            "cases/n31/input/.gitkeep",
            "cases/n31/input/private.mp4",
            "cases/n31/derived/frame.jpg",
            "cases/n31/output/result.json",
            "output/video/n31_training_video_v1.mp4",
            "notes/private_review_v2.md",
        ]
    )
    assert findings == [
        ".env",
        "cases/n31/derived/frame.jpg",
        "cases/n31/input/private.mp4",
        "cases/n31/output/result.json",
        "notes/private_review_v2.md",
    ]


def test_exact_env_secret_leak_detection_does_not_expose_value(tmp_path: Path) -> None:
    secret = b"test-secret-value-123456"
    (tmp_path / "safe.txt").write_text("public", encoding="utf-8")
    (tmp_path / "leak.txt").write_bytes(b"prefix " + secret + b" suffix")

    assert _find_secret_value_leaks(
        tmp_path,
        ["safe.txt", "leak.txt"],
        [secret],
    ) == ["leak.txt"]


def test_submission_report_rejects_unsafe_data_policy() -> None:
    report = build_submission_preflight(
        root=ROOT,
        run_tests=False,
        allow_dirty=True,
        allow_missing_git=True,
    )
    invalid = deepcopy(report)
    invalid["data_policy"]["contains_raw_media"] = True
    with pytest.raises(ContractValidationError):
        validate_document(invalid, "submission_preflight.schema.json")


def test_submission_script_is_executable() -> None:
    script = ROOT / "scripts/check_submission.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
