from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from skillforge.contracts import validate_document
from skillforge.public_checkout import (
    DEFAULT_REPORT,
    PublicCheckoutError,
    _safe_member,
    _write_private_report,
    verify_public_checkout,
)
from skillforge.submission import _check_public_checkout


ROOT = Path(__file__).resolve().parents[1]


def test_member_boundary_allows_only_placeholders_in_private_roots() -> None:
    assert _safe_member("README.md").as_posix() == "README.md"
    assert (
        _safe_member("output/video/n31_training_video_v1.mp4").as_posix()
        == "output/video/n31_training_video_v1.mp4"
    )
    assert (
        _safe_member("cases/n31/input", is_directory=True).as_posix()
        == "cases/n31/input"
    )
    assert (
        _safe_member("cases/n31/input/.gitkeep").as_posix()
        == "cases/n31/input/.gitkeep"
    )
    for unsafe in (
        "../escape",
        ".env",
        "outputs/report.json",
        "cases/n31/input/private.mp4",
        "cases/n31/derived/frame.jpg",
        "cases/n31/output/cache.json",
        "external/teacher_he_reference/drop/reference.ipynb",
        "docs/._status.md",
    ):
        with pytest.raises(PublicCheckoutError):
            _safe_member(unsafe)


def test_public_commit_reproduces_without_private_state(tmp_path: Path) -> None:
    git_probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if git_probe.returncode != 0 or git_probe.stdout.strip() != "true":
        pytest.skip("公开提交复现集成测试需要Git元数据")

    private = tmp_path / "reproducibility"
    report_path = private / "public_checkout_reproducibility.json"

    report = verify_public_checkout(
        report_path=report_path,
        private_root=private,
        allow_dirty=True,
    )

    validate_document(report, "public_checkout_reproducibility.schema.json")
    assert report["status"] in {"PASSED", "DEVELOPMENT_CHECK"}
    assert report["archive"]["file_count"] == report["source"][
        "tracked_file_count"
    ]
    assert report["results"] == {
        "runtime": "native-python",
        "docker_required": False,
        "gold_status": "GOLD",
        "metrics_status": "FINAL",
        "workflow_state": "COMPLETED",
        "severe_before": 5,
        "severe_after": 0,
        "revision_count": 4,
        "web_health_status": 200,
        "web_payload_status": 200,
        "release_artifact_count": 18,
        "release_member_count": 20,
        "pitch_status": "READY_WITH_HUMAN_GATES",
        "pending_human_gate_count": 5,
        "network_requests": 0,
        "external_model_calls": 0,
        "automatic_human_confirmations": 0,
    }
    assert all(report["checks"].values())
    serialized = json.dumps(report, ensure_ascii=False)
    assert "/Users/" not in serialized
    assert "/home/" not in serialized
    assert "file://" not in serialized
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert [item.name for item in private.iterdir()] == [report_path.name]


def test_report_cannot_escape_private_root(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    with pytest.raises(PublicCheckoutError, match="必须位于私有输出目录"):
        _write_private_report(
            {},
            tmp_path / "outside.json",
            private_root=private,
        )


def test_submission_check_requires_current_report_only_in_formal_mode(
    tmp_path: Path,
) -> None:
    development = _check_public_checkout(
        ROOT,
        allow_dirty=True,
        allow_missing_git=False,
    )
    deployment = _check_public_checkout(
        ROOT,
        allow_dirty=False,
        allow_missing_git=True,
    )
    missing = _check_public_checkout(
        tmp_path,
        allow_dirty=False,
        allow_missing_git=False,
    )

    assert development["status"] == "SKIPPED"
    assert deployment["status"] == "SKIPPED"
    assert missing["status"] == "FAILED"


def test_script_is_executable_and_default_report_is_ignored() -> None:
    script = ROOT / "scripts/check_public_checkout.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK)
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(DEFAULT_REPORT.relative_to(ROOT))],
        cwd=ROOT,
        check=False,
    )
    assert ignored.returncode == 0
