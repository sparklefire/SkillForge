from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import skillforge.demo_mode_parity as parity_module
from skillforge.contracts import validate_document
from skillforge.demo_mode_parity import (
    DEFAULT_OUTPUT,
    DemoModeParityError,
    build_demo_mode_parity,
    verify_saved_demo_mode_parity,
    write_demo_mode_parity,
)


ROOT = Path(__file__).resolve().parents[1]
PREPROCESSED = ROOT / "cases/n31/output/gold_rehearsal_v1"
OFFLINE = ROOT / "cases/n31/demo_bundle"


@pytest.fixture()
def mode_dirs(tmp_path: Path) -> tuple[Path, Path]:
    preprocessed = tmp_path / "preprocessed"
    offline = tmp_path / "offline"
    shutil.copytree(PREPROCESSED, preprocessed)
    shutil.copytree(OFFLINE, offline)
    return preprocessed, offline


def test_three_modes_have_equal_closed_loop_semantics(
    tmp_path: Path, mode_dirs: tuple[Path, Path]
) -> None:
    preprocessed, offline = mode_dirs
    private_root = tmp_path / "private"
    report = build_demo_mode_parity(
        private_root=private_root,
        preprocessed_dir=preprocessed,
        offline_dir=offline,
    )
    validate_document(report, "demo_mode_parity.schema.json")
    assert report["status"] == "PASSED"
    assert [item["mode"] for item in report["modes"]] == [
        "live",
        "preprocessed",
        "offline",
    ]
    assert [item["priority"] for item in report["modes"]] == [1, 2, 3]
    assert report["modes"][0]["source_state"] == "RECOMPUTED_FROM_GOLD"
    assert report["modes"][1]["source_state"] == "PREPARED_OUTPUT_VERIFIED"
    assert report["modes"][2]["source_state"] == "TRACKED_OFFLINE_BUNDLE"
    for key in (
        "summary_projection_sha256",
        "final_step_projection_sha256",
        "initial_conflict_projection_sha256",
        "revision_projection_sha256",
    ):
        assert len({item[key] for item in report["modes"]}) == 1
    assert report["parity"]["external_model_calls"] == 0
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "/Users/" not in serialized
    assert "/home/" not in serialized


def test_changed_step_projection_is_rejected(
    tmp_path: Path, mode_dirs: tuple[Path, Path]
) -> None:
    preprocessed, offline = mode_dirs
    path = preprocessed / "after_sop.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["steps"][0]["action"] += " 非预期改动"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(DemoModeParityError, match="核心语义结果不一致"):
        build_demo_mode_parity(
            private_root=tmp_path / "private",
            preprocessed_dir=preprocessed,
            offline_dir=offline,
        )


def test_missing_required_mode_file_is_rejected(
    tmp_path: Path, mode_dirs: tuple[Path, Path]
) -> None:
    preprocessed, offline = mode_dirs
    (offline / "workflow.json").unlink()
    with pytest.raises(DemoModeParityError, match="缺少必要结构化产物"):
        build_demo_mode_parity(
            private_root=tmp_path / "private",
            preprocessed_dir=preprocessed,
            offline_dir=offline,
        )


def test_private_report_permissions_and_current_state_verification(
    tmp_path: Path, mode_dirs: tuple[Path, Path]
) -> None:
    preprocessed, offline = mode_dirs
    private_root = tmp_path / "private"
    output = private_root / "demo_mode_parity.json"
    report = build_demo_mode_parity(
        private_root=private_root,
        preprocessed_dir=preprocessed,
        offline_dir=offline,
    )
    write_demo_mode_parity(report, output, private_root=private_root)
    assert stat.S_IMODE(private_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert (
        verify_saved_demo_mode_parity(
            output,
            private_root=private_root,
            preprocessed_dir=preprocessed,
            offline_dir=offline,
        )
        == report
    )

    summary_path = preprocessed / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["after"]["severe_error_count"] = 1
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(DemoModeParityError):
        verify_saved_demo_mode_parity(
            output,
            private_root=private_root,
            preprocessed_dir=preprocessed,
            offline_dir=offline,
        )


def test_refresh_runner_is_explicit_and_recorded(
    tmp_path: Path, mode_dirs: tuple[Path, Path]
) -> None:
    preprocessed, offline = mode_dirs
    calls: list[Path] = []
    report = build_demo_mode_parity(
        private_root=tmp_path / "private",
        preprocessed_dir=preprocessed,
        offline_dir=offline,
        refresh_preprocessed=True,
        refresh_runner=lambda root: calls.append(root),
    )
    assert calls == [ROOT]
    assert report["preprocessed_refresh_performed"] is True
    assert report["modes"][1]["source_state"] == "REFRESHED_FROM_LOCAL_INPUTS"


def test_real_refresh_runner_forces_offline_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(parity_module.subprocess, "run", fake_run)
    parity_module._refresh_preprocessed(ROOT)
    assert captured["args"] == ["bash", "scripts/run_n31_local.sh"]
    assert captured["cwd"] == ROOT
    assert captured["env"]["SKILLFORGE_OFFLINE_OCR"] == "1"
    setup_script = (ROOT / "scripts/setup_ocr_languages.sh").read_text(
        encoding="utf-8"
    )
    local_script = (ROOT / "scripts/run_n31_local.sh").read_text(encoding="utf-8")
    assert "--offline" in setup_script
    assert "离线模式拒绝下载" in setup_script
    assert "SKILLFORGE_OFFLINE_OCR" in local_script
    assert "progress()" in local_script


def test_cli_report_is_private_and_script_is_executable(
    tmp_path: Path, mode_dirs: tuple[Path, Path]
) -> None:
    preprocessed, offline = mode_dirs
    private_root = tmp_path / "private"
    output = private_root / "demo_mode_parity.json"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "skillforge.demo_mode_parity",
            "--private-root",
            str(private_root),
            "--preprocessed-dir",
            str(preprocessed),
            "--offline-dir",
            str(offline),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASSED"
    assert payload["mode_count"] == 3
    assert payload["before_severe_errors"] == 5
    assert payload["after_severe_errors"] == 0
    assert payload["external_model_calls"] == 0
    assert payload["network_requests"] == 0
    assert "/Users/" not in result.stdout
    assert "https://" not in result.stdout
    assert output.is_file()
    assert stat.S_IMODE(private_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    git_state = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if git_state.returncode == 0:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(DEFAULT_OUTPUT.relative_to(ROOT))],
            cwd=ROOT,
            check=False,
        )
        assert ignored.returncode == 0
    else:
        assert "outputs/*" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    script = ROOT / "scripts/check_demo_mode_parity.sh"
    assert script.is_file() and os.access(script, os.X_OK)
