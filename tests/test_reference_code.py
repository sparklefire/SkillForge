from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from skillforge.contracts import validate_document
from skillforge.reference_code import (
    REQUIRED_BUNDLE_ENTRIES,
    ReferenceMaterialError,
    build_reference_material_audit,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_notebook(path: Path) -> None:
    notebook = {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {},
        "cells": [
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": ["# Official workshop\n"],
            },
            {
                "cell_type": "code",
                "metadata": {},
                "execution_count": 1,
                "source": [
                    "import shutil\n",
                    "shutil.rmtree('openclaw-home')\n",
                    "# fuser -k -n tcp 3030\n",
                    "dangerouslyDisableDeviceAuth = True\n",
                    "def approve(device): pass\n",
                    "print(f'token (URL 已带): {token}')\n",
                    "model = 'qwen3.6:35b'\n",
                ],
                "outputs": [
                    {
                        "output_type": "stream",
                        "name": "stdout",
                        "text": [
                            "架构: aarch64\nGPU: NVIDIA GB10\nPython: 3.12.3\n",
                            "Node.js: v22.20.0\nOpenClaw 2026.5.19\n",
                            '\"comfyui_version\": \"0.18.1\"\n',
                            '\"pytorch_version\": \"2.11.0+cu130\"\n',
                            "MEDIA:/private/result.png\n✅ skill 跑通\n",
                        ],
                    }
                ],
            },
        ],
    }
    path.write_text(json.dumps(notebook, ensure_ascii=False), encoding="utf-8")


def _write_deck(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for slide_number in range(1, 13):
            text = f"Slide {slide_number}"
            if slide_number == 9:
                text = "评审标准 25% 25% 20% 15% 10% 5%"
            if slide_number == 10:
                text = "项目提交要求 开源 600字 部署说明 技术栈 演示视频 团队合影"
            archive.writestr(
                f"ppt/slides/slide{slide_number}.xml",
                (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><p:cSld>'
                    f"<a:t>{text}</a:t>"
                    "</p:cSld></p:sld>"
                ),
            )


def _material_set(tmp_path: Path) -> Path:
    drop = tmp_path / "drop"
    drop.mkdir()
    _write_notebook(drop / "workshop.ipynb")
    _write_deck(drop / "opening.pptx")
    return drop


def test_static_audit_detects_success_risks_and_missing_bundle(tmp_path: Path) -> None:
    drop = _material_set(tmp_path)

    report = build_reference_material_audit(drop, checked_at="2026-07-19")

    validate_document(report, "reference_material_audit.schema.json")
    assert report["status"] == "WAITING_ON_RUNTIME_BUNDLE"
    assert report["notebook"]["embedded_success_evidence"] is True
    assert report["notebook"]["error_output_count"] == 0
    assert report["opening_deck"]["scoring_weights_found"] is True
    assert report["opening_deck"]["submission_requirements_found"] is True
    assert set(report["risk_flags"]) == {
        "AUTO_APPROVE_DEVICES",
        "DELETE_SESSION_STATE",
        "DISABLE_DEVICE_AUTH",
        "KILL_SHARED_PORTS",
        "PRINT_GATEWAY_TOKEN",
    }
    assert report["runtime_bundle"]["missing_entries"] == list(
        REQUIRED_BUNDLE_ENTRIES
    )
    assert report["execution_claim"]["base_completion_standard_met"] is False
    serialized = json.dumps(report, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    assert "MEDIA:/private/result.png" not in serialized


def test_complete_bundle_only_marks_ready_for_isolated_execution(tmp_path: Path) -> None:
    drop = _material_set(tmp_path)
    for entry in REQUIRED_BUNDLE_ENTRIES:
        path = drop / entry
        if Path(entry).suffix or entry == "openclaw":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("placeholder", encoding="utf-8")
        else:
            path.mkdir(parents=True, exist_ok=True)

    report = build_reference_material_audit(drop, checked_at="2026-07-19")

    assert report["status"] == "READY_FOR_ISOLATED_EXECUTION"
    assert report["runtime_bundle"]["missing_entries"] == []
    assert report["execution_claim"][
        "current_skillforge_dgx_execution_completed"
    ] is False
    assert report["execution_claim"]["base_completion_standard_met"] is False


def test_missing_or_corrupt_material_fails_safely(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    drop.mkdir()
    _write_notebook(drop / "workshop.ipynb")
    (drop / "opening.pptx").write_text("not a zip", encoding="utf-8")

    with pytest.raises(ReferenceMaterialError, match="PPTX"):
        build_reference_material_audit(drop, checked_at="2026-07-19")


def test_reference_check_script_is_executable() -> None:
    script = ROOT / "scripts/check_teacher_he_reference.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
