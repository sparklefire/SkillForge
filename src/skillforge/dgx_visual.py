"""Run privacy-gated FFmpeg sampling and native CUDA visual features on DGX."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .media import probe_media, resolve_ffmpeg


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_NAME = "dgx_visual_compute.schema.json"


class DGXVisualComputeError(RuntimeError):
    """Raised when the native DGX visual workload cannot complete safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown error")[-2000:]
        raise DGXVisualComputeError(detail)
    return completed


def extract_gpu_frames(
    source: Path,
    destination: Path,
    *,
    interval_seconds: float,
    resize_width: int,
) -> list[int]:
    """Extract grayscale PGM frames and retain their real FFmpeg presentation times."""

    if interval_seconds <= 0:
        raise ValueError("interval_seconds 必须大于0")
    if resize_width < 64:
        raise ValueError("resize_width 不能小于64")
    destination.mkdir(parents=True, exist_ok=True)
    for stale in destination.glob("frame_*.pgm"):
        stale.unlink()
    filter_chain = (
        f"fps=1/{interval_seconds},"
        f"scale={resize_width}:-2:flags=area,format=gray,showinfo"
    )
    completed = _run(
        [
            str(resolve_ffmpeg()),
            "-hide_banner",
            "-loglevel",
            "info",
            "-y",
            "-i",
            str(source),
            "-vf",
            filter_chain,
            "-fps_mode",
            "vfr",
            str(destination / "frame_%06d.pgm"),
        ]
    )
    timestamps = [
        round(float(value) * 1000)
        for value in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", completed.stderr)
    ]
    frames = sorted(destination.glob("frame_*.pgm"))
    if not frames:
        raise DGXVisualComputeError(f"没有从 {source.name} 提取到帧")
    if len(timestamps) != len(frames):
        raise DGXVisualComputeError(
            f"时间戳数量{len(timestamps)}与帧数量{len(frames)}不一致"
        )
    return timestamps


def compile_cuda_tool(
    source: Path,
    destination: Path,
    *,
    nvcc: Path,
    compiled_arch: str,
) -> Path:
    """Compile the small auditable CUDA feature extractor on the target DGX."""

    if not source.is_file():
        raise FileNotFoundError(source)
    if not nvcc.is_file() or not os.access(nvcc, os.X_OK):
        raise DGXVisualComputeError(f"nvcc不可执行: {nvcc}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(nvcc),
            "-O3",
            "--std=c++17",
            f"-arch={compiled_arch}",
            str(source),
            "-o",
            str(destination),
        ]
    )
    destination.chmod(0o700)
    return destination


def run_cuda_features(binary: Path, frame_dir: Path) -> dict[str, Any]:
    completed = _run([str(binary), str(frame_dir)])
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DGXVisualComputeError("CUDA工具没有返回合法JSON") from exc
    if result.get("backend") != "cuda_native" or not result.get("frames"):
        raise DGXVisualComputeError("CUDA工具返回结构不完整")
    return result


def select_scene_candidates(
    frames: list[dict[str, Any]],
    timestamps_ms: list[int],
    *,
    threshold: float,
    limit: int,
) -> list[dict[str, Any]]:
    """Select boundary frames and the strongest scene changes without semantic claims."""

    if len(frames) != len(timestamps_ms) or not frames:
        raise ValueError("帧指标和时间戳必须非空且一一对应")
    if not 0 <= threshold <= 1:
        raise ValueError("threshold必须在0到1之间")
    if limit < 2:
        raise ValueError("limit不能小于2")

    selected: dict[int, str] = {0: "BOUNDARY_FIRST"}
    if len(frames) > 1:
        selected[len(frames) - 1] = "BOUNDARY_LAST"
    ranked = sorted(
        range(1, len(frames)),
        key=lambda index: (-float(frames[index]["scene_change_score"]), index),
    )
    above = [
        index
        for index in ranked
        if float(frames[index]["scene_change_score"]) >= threshold
    ]
    candidates = above or ranked[:1]
    for index in candidates:
        if len(selected) >= limit:
            break
        selected.setdefault(
            index,
            "SCENE_CHANGE" if index in above else "TOP_CHANGE_FALLBACK",
        )

    result = []
    for index in sorted(selected):
        item = frames[index]
        result.append(
            {
                "frame_index": int(item["frame_index"]),
                "timestamp_ms": int(timestamps_ms[index]),
                "scene_change_score": round(float(item["scene_change_score"]), 6),
                "mean_luma": round(float(item["mean_luma"]), 6),
                "contrast": round(float(item["contrast"]), 6),
                "edge_energy": round(float(item["edge_energy"]), 6),
                "selection_reason": selected[index],
            }
        )
    return result


def build_visual_compute_report(
    *,
    case_id: str,
    source_results: list[dict[str, Any]],
    excluded_source_count: int,
    sample_interval_seconds: float,
    resize_width: int,
    scene_change_threshold: float,
    selected_frame_limit: int,
    compiled_arch: str,
    elapsed_seconds: float,
    external_api_processing_authorized: bool = False,
    generated_at: str | None = None,
) -> dict[str, Any]:
    if not source_results:
        raise ValueError("至少需要一个GPU处理结果")
    device = source_results[0]["cuda"]["device"]
    cuda_runtime = source_results[0]["cuda"]["cuda_runtime"]
    for item in source_results[1:]:
        if item["cuda"]["device"] != device:
            raise DGXVisualComputeError("同一报告中检测到不同GPU")
        if item["cuda"]["cuda_runtime"] != cuda_runtime:
            raise DGXVisualComputeError("同一报告中检测到不同CUDA运行时")

    sources = []
    all_frames: list[dict[str, Any]] = []
    for item in source_results:
        cuda = item["cuda"]
        selected = select_scene_candidates(
            cuda["frames"],
            item["timestamps_ms"],
            threshold=scene_change_threshold,
            limit=selected_frame_limit,
        )
        all_frames.extend(cuda["frames"])
        sources.append(
            {
                "source_id": item["source_id"],
                "sha256": item["sha256"],
                "duration_ms": item["duration_ms"],
                "sampled_frame_count": len(cuda["frames"]),
                "gpu_kernel_ms": round(float(cuda["gpu_kernel_ms"]), 6),
                "selected_frames": selected,
            }
        )

    sampled_count = sum(item["sampled_frame_count"] for item in sources)
    selected_count = sum(len(item["selected_frames"]) for item in sources)
    kernel_ms = sum(item["gpu_kernel_ms"] for item in sources)
    generated = generated_at or datetime.now(timezone.utc).isoformat()
    report = {
        "version": 1,
        "case_id": case_id,
        "run_id": "dgx-visual-" + generated.replace(":", "").replace("+00:00", "Z"),
        "generated_at": generated,
        "status": "COMPLETED",
        "processing_location": "DGX_SPARK_LOCAL",
        "backend": "CUDA_NATIVE",
        "actual_gpu_compute": True,
        "semantic_claim_scope": "CANDIDATE_SELECTION_ONLY",
        "source_policy": {
            "external_api_processing_authorized": external_api_processing_authorized,
            "dgx_processing_authorized": True,
            "accepted_privacy_status": "LOCAL_QA_PASSED",
            "processed_source_count": len(sources),
            "excluded_source_count": excluded_source_count,
            "third_party_reference_processed": False,
        },
        "gpu": {
            "device_name": device["name"],
            "compute_capability": device["compute_capability"],
            "cuda_runtime": cuda_runtime,
            "compiled_arch": compiled_arch,
            "total_global_memory_bytes": int(device["total_global_memory_bytes"]),
        },
        "configuration": {
            "sample_interval_seconds": sample_interval_seconds,
            "resize_width": resize_width,
            "scene_change_threshold": scene_change_threshold,
            "selected_frame_limit": selected_frame_limit,
            "timestamp_basis": "FFMPEG_SHOWINFO_PTS",
        },
        "sources": sources,
        "summary": {
            "processed_video_count": len(sources),
            "sampled_frame_count": sampled_count,
            "selected_frame_count": selected_count,
            "gpu_kernel_ms": round(kernel_ms, 6),
            "end_to_end_elapsed_seconds": round(elapsed_seconds, 6),
            "end_to_end_frames_per_second": round(sampled_count / elapsed_seconds, 6),
            "max_scene_change_score": round(
                max(float(item["scene_change_score"]) for item in all_frames), 6
            ),
            "semantic_model_used": False,
        },
        "agent_trace": [
            {
                "event_id": "T01",
                "agent": "PERCEPTION_AGENT",
                "action": "FILTER_AUTHORIZED_SOURCES",
                "tool": "INGEST_MANIFEST_POLICY_GATE",
                "decision": "只接收LOCAL_QA_PASSED的自摄安全派生视频",
                "outcome": f"接收{len(sources)}个来源，排除{excluded_source_count}个其他来源",
            },
            {
                "event_id": "T02",
                "agent": "PERCEPTION_AGENT",
                "action": "EXTRACT_TIMESTAMPED_FRAMES",
                "tool": "FFMPEG_SHOWINFO",
                "decision": "按固定间隔生成灰度帧并保留真实PTS",
                "outcome": f"生成{sampled_count}帧",
            },
            {
                "event_id": "T03",
                "agent": "PERCEPTION_AGENT",
                "action": "COMPUTE_VISUAL_FEATURES",
                "tool": "CUDA_NATIVE_GB10",
                "decision": "在DGX本地计算亮度、对比度、边缘能量和相邻帧变化",
                "outcome": f"GPU核耗时{kernel_ms:.3f}毫秒",
            },
            {
                "event_id": "T04",
                "agent": "PERCEPTION_AGENT",
                "action": "RANK_SCENE_CANDIDATES",
                "tool": "DETERMINISTIC_SCENE_SELECTOR",
                "decision": "保留首尾帧及超过阈值的最强场景变化帧",
                "outcome": f"选择{selected_count}个候选帧",
            },
            {
                "event_id": "T05",
                "agent": "VERIFIER_AGENT",
                "action": "LIMIT_SEMANTIC_SCOPE",
                "tool": "CLAIM_SCOPE_GATE",
                "decision": "GPU特征只用于候选帧筛选，不自动证明SOP动作成立",
                "outcome": "未新增任何语义支持结论，候选帧仍需视觉模型或人工复核",
            },
        ],
    }
    return validate_document(report, SCHEMA_NAME)


def run_manifest(
    manifest_path: Path,
    source_root: Path,
    frame_root: Path,
    binary: Path,
    cuda_source: Path,
    nvcc: Path,
    output: Path,
    *,
    sample_interval_seconds: float = 1.0,
    resize_width: int = 320,
    scene_change_threshold: float = 0.08,
    selected_frame_limit: int = 12,
    compiled_arch: str = "sm_121",
    dgx_processing_authorized: bool = False,
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not dgx_processing_authorized:
        raise DGXVisualComputeError("必须显式允许在DGX处理安全派生素材")
    accepted = [
        item
        for item in manifest.get("sources", [])
        if item.get("privacy_status") == "LOCAL_QA_PASSED"
        and item.get("path", "").lower().endswith(".mp4")
    ]
    excluded = len(manifest.get("sources", [])) - len(accepted)
    if not accepted:
        raise DGXVisualComputeError("清单中没有LOCAL_QA_PASSED视频")

    compile_cuda_tool(
        cuda_source,
        binary,
        nvcc=nvcc,
        compiled_arch=compiled_arch,
    )
    started = time.perf_counter()
    results = []
    for item in accepted:
        source = source_root / Path(item["path"]).name
        if not source.is_file():
            raise FileNotFoundError(source)
        source_frames = frame_root / item["source_id"]
        timestamps = extract_gpu_frames(
            source,
            source_frames,
            interval_seconds=sample_interval_seconds,
            resize_width=resize_width,
        )
        cuda = run_cuda_features(binary, source_frames)
        if len(cuda["frames"]) != len(timestamps):
            raise DGXVisualComputeError("CUDA帧结果与FFmpeg时间戳数量不一致")
        duration_ms = probe_media(source).get("duration_ms")
        if not duration_ms:
            raise DGXVisualComputeError(f"无法读取视频时长: {source.name}")
        results.append(
            {
                "source_id": item["source_id"],
                "sha256": _sha256(source),
                "duration_ms": int(duration_ms),
                "timestamps_ms": timestamps,
                "cuda": cuda,
            }
        )
    elapsed = time.perf_counter() - started
    report = build_visual_compute_report(
        case_id=manifest["case_id"],
        source_results=results,
        excluded_source_count=excluded,
        sample_interval_seconds=sample_interval_seconds,
        resize_width=resize_width,
        scene_change_threshold=scene_change_threshold,
        selected_frame_limit=selected_frame_limit,
        compiled_arch=compiled_arch,
        elapsed_seconds=elapsed,
        external_api_processing_authorized=bool(
            manifest.get("external_processing_authorized")
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--frame-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--cuda-source",
        type=Path,
        default=ROOT / "native" / "dgx_frame_features.cu",
    )
    parser.add_argument(
        "--binary",
        type=Path,
        default=Path.home() / "skillforge" / "bin" / "dgx_frame_features",
    )
    parser.add_argument(
        "--nvcc", type=Path, default=Path("/usr/local/cuda-13.0/bin/nvcc")
    )
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--resize-width", type=int, default=320)
    parser.add_argument("--scene-threshold", type=float, default=0.08)
    parser.add_argument("--selected-limit", type=int, default=12)
    parser.add_argument("--allow-dgx-safe-derivatives", action="store_true")
    args = parser.parse_args()
    report = run_manifest(
        args.manifest,
        args.source_root,
        args.frame_root,
        args.binary,
        args.cuda_source,
        args.nvcc,
        args.output,
        sample_interval_seconds=args.sample_interval,
        resize_width=args.resize_width,
        scene_change_threshold=args.scene_threshold,
        selected_frame_limit=args.selected_limit,
        dgx_processing_authorized=args.allow_dgx_safe_derivatives,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
