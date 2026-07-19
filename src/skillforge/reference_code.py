"""Statically audit participant-provided official reference materials.

The auditor never executes notebook cells and never copies source content into
its report. The workshop notebook contains service termination, state deletion,
token printing, and intentionally insecure LAN configuration intended for a
disposable training environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from .contracts import ContractValidationError, validate_document
from .demo import ROOT


DEFAULT_DROP = ROOT / "external/teacher_he_reference/drop"
DEFAULT_OUTPUT = ROOT / "outputs/reference/teacher_he_reference_audit.json"
CONTEST_TIMEZONE = ZoneInfo("Asia/Shanghai")
REQUIRED_BUNDLE_ENTRIES = (
    "scripts/env-check.sh",
    "scripts/ollama-ctl.sh",
    "scripts/comfyui-ctl.sh",
    "scripts/openclaw-ctl.sh",
    "openclaw",
    "node22",
    "ollama",
    "comfyui-app/ComfyUI",
    "sample/sample_face.jpg",
)
RISK_PATTERNS = {
    "AUTO_APPROVE_DEVICES": r"devices['\", ]+approve|def approve\(",
    "DELETE_SESSION_STATE": r"shutil\.rmtree|\.unlink\(missing_ok=True\)",
    "DISABLE_DEVICE_AUTH": r"dangerouslyDisableDeviceAuth",
    "KILL_SHARED_PORTS": r"fuser['\", -]+-k|fuser.+-k",
    "PRINT_GATEWAY_TOKEN": r"token \(URL|print\(f['\"]\s*token",
}


class ReferenceMaterialError(ValueError):
    """Raised when the official material set is missing or malformed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_text(cell: dict[str, Any]) -> str:
    source = cell.get("source", "")
    if isinstance(source, list) and all(isinstance(item, str) for item in source):
        return "".join(source)
    if isinstance(source, str):
        return source
    raise ReferenceMaterialError("Notebook单元source格式无效")


def _output_text(cell: dict[str, Any]) -> str:
    parts: list[str] = []
    for output in cell.get("outputs", []):
        if not isinstance(output, dict):
            continue
        text = output.get("text")
        if isinstance(text, list) and all(isinstance(item, str) for item in text):
            parts.extend(text)
        elif isinstance(text, str):
            parts.append(text)
        data = output.get("data")
        if isinstance(data, dict):
            for mime in ("text/plain", "text/markdown"):
                value = data.get(mime)
                if isinstance(value, list) and all(
                    isinstance(item, str) for item in value
                ):
                    parts.extend(value)
                elif isinstance(value, str):
                    parts.append(value)
    return "".join(parts)


def _detected(pattern: str, text: str, fallback: str = "NOT_DETECTED") -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else fallback


def _audit_notebook(path: Path) -> tuple[dict[str, Any], list[str]]:
    try:
        notebook = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReferenceMaterialError("官方Notebook无法读取或不是合法JSON") from exc
    if notebook.get("nbformat") != 4 or not isinstance(notebook.get("cells"), list):
        raise ReferenceMaterialError("官方Notebook不是受支持的nbformat 4")
    cells = notebook["cells"]
    if not cells or not all(isinstance(cell, dict) for cell in cells):
        raise ReferenceMaterialError("官方Notebook没有合法单元")
    code_cells = [cell for cell in cells if cell.get("cell_type") == "code"]
    markdown_cells = [cell for cell in cells if cell.get("cell_type") == "markdown"]
    if not code_cells or not markdown_cells:
        raise ReferenceMaterialError("官方Notebook缺少代码或说明单元")
    source_text = "\n".join(_source_text(cell) for cell in cells)
    output_text = "\n".join(_output_text(cell) for cell in code_cells)
    error_count = sum(
        output.get("output_type") == "error"
        for cell in code_cells
        for output in cell.get("outputs", [])
        if isinstance(output, dict)
    )
    executed = sum(isinstance(cell.get("execution_count"), int) for cell in code_cells)
    success = "skill 跑通" in output_text and "MEDIA:" in output_text and error_count == 0
    combined = f"{source_text}\n{output_text}"
    risks = sorted(
        risk_id
        for risk_id, pattern in RISK_PATTERNS.items()
        if re.search(pattern, combined, flags=re.IGNORECASE | re.DOTALL)
    )
    if set(risks) != set(RISK_PATTERNS):
        raise ReferenceMaterialError("官方Notebook风险特征集合发生变化，需要人工复审")
    expected_runtime = {
        "architecture": _detected(r"架构:\s*([^\s(]+)", output_text),
        "gpu": _detected(r"GPU:\s*([^\n]+)", output_text),
        "python": _detected(r"Python:\s*([^\s]+)", output_text),
        "node": _detected(r"Node\.js:\s*v?([^\s]+)", output_text),
        "openclaw": _detected(r"OpenClaw\s+([0-9.]+)", output_text),
        "comfyui": _detected(r'"comfyui_version":\s*"([^"]+)"', output_text),
        "pytorch": _detected(r'"pytorch_version":\s*"([^"]+)"', output_text),
        "ollama_model": "qwen3.6:35b"
        if "qwen3.6:35b" in combined
        else "NOT_DETECTED",
    }
    return (
        {
            "nbformat": 4,
            "cell_count": len(cells),
            "code_cell_count": len(code_cells),
            "markdown_cell_count": len(markdown_cells),
            "executed_code_cell_count": executed,
            "error_output_count": error_count,
            "embedded_success_evidence": success,
            "expected_runtime": expected_runtime,
        },
        risks,
    )


def _slide_number(name: str) -> int:
    match = re.fullmatch(r"ppt/slides/slide([0-9]+)\.xml", name)
    if not match:
        raise ReferenceMaterialError("PPTX幻灯片路径无效")
    return int(match.group(1))


def _audit_deck(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            slide_names = sorted(
                (
                    name
                    for name in archive.namelist()
                    if re.fullmatch(r"ppt/slides/slide[0-9]+\.xml", name)
                ),
                key=_slide_number,
            )
            texts: dict[int, str] = {}
            for name in slide_names:
                root = ElementTree.fromstring(archive.read(name))
                texts[_slide_number(name)] = " ".join(
                    element.text or ""
                    for element in root.iter()
                    if element.tag.endswith("}t")
                )
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, KeyError) as exc:
        raise ReferenceMaterialError("官方PPTX无法安全解析") from exc
    if not slide_names:
        raise ReferenceMaterialError("官方PPTX不含幻灯片")
    scoring = re.sub(r"\s+", "", texts.get(9, ""))
    submission = re.sub(r"\s+", "", texts.get(10, ""))
    scoring_phrases = ("评审标准", "25%", "20%", "15%", "10%", "5%")
    submission_phrases = (
        "项目提交要求",
        "开源",
        "600字",
        "部署说明",
        "技术栈",
        "演示视频",
        "团队合影",
    )
    return {
        "slide_count": len(slide_names),
        "scoring_slide": 9,
        "submission_slide": 10,
        "scoring_weights_found": all(phrase in scoring for phrase in scoring_phrases),
        "submission_requirements_found": all(
            phrase in submission for phrase in submission_phrases
        ),
    }


def _select_single(drop: Path, suffix: str, label: str) -> Path:
    matches = sorted(path for path in drop.glob(f"*{suffix}") if path.is_file())
    if len(matches) != 1:
        raise ReferenceMaterialError(f"{label}必须且只能有一个")
    return matches[0]


def build_reference_material_audit(
    drop: Path = DEFAULT_DROP,
    *,
    checked_at: str | None = None,
) -> dict[str, Any]:
    drop = drop.expanduser().resolve()
    if not drop.is_dir():
        raise ReferenceMaterialError("官方参考材料投递目录不存在")
    notebook_path = _select_single(drop, ".ipynb", "官方Notebook")
    deck_path = _select_single(drop, ".pptx", "官方PPTX")
    notebook, risks = _audit_notebook(notebook_path)
    deck = _audit_deck(deck_path)
    missing = [entry for entry in REQUIRED_BUNDLE_ENTRIES if not (drop / entry).exists()]
    report = {
        "version": 1,
        "artifact_type": "OFFICIAL_REFERENCE_MATERIAL_AUDIT",
        "checked_at": checked_at
        or datetime.now(CONTEST_TIMEZONE).date().isoformat(),
        "status": "WAITING_ON_RUNTIME_BUNDLE"
        if missing
        else "READY_FOR_ISOLATED_EXECUTION",
        "sources": [
            {
                "kind": "JUPYTER_NOTEBOOK",
                "filename": notebook_path.name,
                "sha256": _sha256(notebook_path),
                "bytes": notebook_path.stat().st_size,
            },
            {
                "kind": "OPENING_DECK",
                "filename": deck_path.name,
                "sha256": _sha256(deck_path),
                "bytes": deck_path.stat().st_size,
            },
        ],
        "notebook": notebook,
        "opening_deck": deck,
        "runtime_bundle": {
            "required_entry_count": len(REQUIRED_BUNDLE_ENTRIES),
            "present_entry_count": len(REQUIRED_BUNDLE_ENTRIES) - len(missing),
            "missing_entries": missing,
        },
        "risk_flags": risks,
        "execution_claim": {
            "upstream_embedded_outputs_observed": True,
            "current_skillforge_dgx_execution_completed": False,
            "base_completion_standard_met": False,
        },
        "data_policy": {
            "contains_credentials": False,
            "contains_gateway_token": False,
            "contains_absolute_paths": False,
            "contains_source_content": False,
            "executes_notebook": False,
        },
    }
    return validate_document(report, "reference_material_audit.schema.json")


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drop", type=Path, default=DEFAULT_DROP)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--checked-at")
    args = parser.parse_args()
    try:
        report = build_reference_material_audit(
            args.drop,
            checked_at=args.checked_at,
        )
        _write_json(args.output, report)
    except (ContractValidationError, OSError, ReferenceMaterialError) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": str(exc)
                    if isinstance(exc, ReferenceMaterialError)
                    else "官方参考材料审计失败",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "embedded_success_evidence": report["notebook"][
                    "embedded_success_evidence"
                ],
                "missing_runtime_entries": len(
                    report["runtime_bundle"]["missing_entries"]
                ),
                "base_completion_standard_met": report["execution_claim"][
                    "base_completion_standard_met"
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "READY_FOR_ISOLATED_EXECUTION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
