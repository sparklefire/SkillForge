"""Merge reviewed keyframe intervals into auditable candidate action windows."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .demo import ROOT


SCHEMA_NAME = "temporal_action_windows.schema.json"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def merge_frame_intervals(
    frames: list[dict[str, Any]],
    *,
    merge_gap_ms: int,
) -> list[dict[str, Any]]:
    """Merge intervals only within the same source timeline."""

    if not 0 <= merge_gap_ms <= 10_000:
        raise ValueError("merge_gap_ms 必须在0至10000之间")
    source_order: list[str] = []
    by_source: dict[str, list[dict[str, Any]]] = {}
    for frame in frames:
        source = frame["source_ref"]
        if source not in by_source:
            source_order.append(source)
            by_source[source] = []
        by_source[source].append(frame)

    merged: list[dict[str, Any]] = []
    for source in source_order:
        ordered = sorted(
            by_source[source],
            key=lambda item: (item["start_ms"], item["end_ms"], item["evidence_id"]),
        )
        current: dict[str, Any] | None = None
        for frame in ordered:
            start_ms = int(frame["start_ms"])
            end_ms = int(frame["end_ms"])
            if end_ms <= start_ms:
                raise ValueError(f"{frame['evidence_id']} 时间区间无效")
            if current is None or start_ms > current["end_ms"] + merge_gap_ms:
                current = {
                    "source_ref": source,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "evidence_ids": [frame["evidence_id"]],
                    "selected_frame_count": 1,
                }
                merged.append(current)
            else:
                current["end_ms"] = max(current["end_ms"], end_ms)
                if frame["evidence_id"] not in current["evidence_ids"]:
                    current["evidence_ids"].append(frame["evidence_id"])
                current["selected_frame_count"] += 1
    return merged


def _validate_input_alignment(
    gold: dict[str, Any],
    visual: dict[str, Any],
    dgx: dict[str, Any],
) -> None:
    gold_steps = {item["step_id"]: item for item in gold["steps"]}
    assessments = {item["step_id"]: item for item in visual["assessments"]}
    if set(gold_steps) != set(assessments):
        raise ValueError("视觉复核步骤与Gold步骤集合不一致")
    evidence = {item["evidence_id"]: item for item in gold["evidence_catalog"]}
    dgx_sources = {item["source_id"] for item in dgx["sources"]}
    for step_id, assessment in assessments.items():
        step = gold_steps[step_id]
        if assessment["title"] != step["title"] or assessment["required"] != step["required"]:
            raise ValueError(f"{step_id} 标题或必要性与Gold不一致")
        if assessment["model_result"]["step_id"] != step_id:
            raise ValueError(f"{step_id} 模型结果编号不一致")
        for frame in assessment["frames"]:
            item = evidence.get(frame["evidence_id"])
            if not item or item["source_type"] != "video":
                raise ValueError(f"{frame['evidence_id']} 不是Gold视频Evidence")
            locator = item["locator"]
            expected = (
                item["source_ref"],
                int(locator["start_ms"]),
                int(locator["end_ms"]),
            )
            actual = (
                frame["source_ref"],
                int(frame["start_ms"]),
                int(frame["end_ms"]),
            )
            if actual != expected:
                raise ValueError(f"{frame['evidence_id']} 时间或来源与Gold不一致")
            if frame["source_ref"] not in dgx_sources:
                raise ValueError(f"{frame['source_ref']} 不存在于DGX报告")


def build_temporal_action_windows(
    gold_sop_path: Path,
    visual_review_path: Path,
    dgx_visual_path: Path,
    *,
    merge_gap_ms: int = 1_000,
    candidate_padding_ms: int = 1_000,
) -> dict[str, Any]:
    if not 0 <= candidate_padding_ms <= 10_000:
        raise ValueError("candidate_padding_ms 必须在0至10000之间")
    gold = validate_document(_read_json(gold_sop_path), "sop.schema.json")
    visual = validate_document(
        _read_json(visual_review_path), "visual_review_report.schema.json"
    )
    dgx = validate_document(
        _read_json(dgx_visual_path), "dgx_visual_compute.schema.json"
    )
    if not dgx["actual_gpu_compute"] or dgx["semantic_claim_scope"] != "CANDIDATE_SELECTION_ONLY":
        raise ValueError("DGX输入必须是真实GPU候选筛选报告")
    _validate_input_alignment(gold, visual, dgx)

    assessment_by_step = {item["step_id"]: item for item in visual["assessments"]}
    dgx_by_source = {item["source_id"]: item for item in dgx["sources"]}
    windows: list[dict[str, Any]] = []
    for step in gold["steps"]:
        assessment = assessment_by_step[step["step_id"]]
        merged = merge_frame_intervals(
            assessment["frames"],
            merge_gap_ms=merge_gap_ms,
        )
        for index, window in enumerate(merged, start=1):
            lower = max(0, window["start_ms"] - candidate_padding_ms)
            upper = window["end_ms"] + candidate_padding_ms
            candidate_times = sorted(
                {
                    int(item["timestamp_ms"])
                    for item in dgx_by_source[window["source_ref"]]["selected_frames"]
                    if lower <= int(item["timestamp_ms"]) <= upper
                }
            )
            windows.append(
                {
                    "window_id": f"{step['step_id']}-W{index:02d}",
                    "step_id": step["step_id"],
                    "title": step["title"],
                    "required": step["required"],
                    "source_ref": window["source_ref"],
                    "start_ms": window["start_ms"],
                    "end_ms": window["end_ms"],
                    "duration_ms": window["end_ms"] - window["start_ms"],
                    "evidence_ids": window["evidence_ids"],
                    "selected_frame_count": window["selected_frame_count"],
                    "dgx_candidate_timestamps_ms": candidate_times,
                    "dgx_candidate_count": len(candidate_times),
                    "visual_verdict": assessment["model_result"]["verdict"],
                    "visual_confidence": assessment["model_result"]["confidence"],
                    "privacy_observation": assessment["model_result"][
                        "privacy_observation"
                    ],
                }
            )

    step_window_counts = Counter(item["step_id"] for item in windows)
    source_ids = sorted({item["source_ref"] for item in windows})
    unique_candidates = {
        (item["source_ref"], timestamp)
        for item in windows
        for timestamp in item["dgx_candidate_timestamps_ms"]
    }
    report = {
        "version": 1,
        "case_id": gold["case_id"],
        "report_id": "N31_TEMPORAL_ACTION_WINDOWS_V1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "COMPLETED",
        "semantic_claim_scope": "GOLD_ALIGNED_CANDIDATE_WINDOW_ONLY",
        "model_calls": 0,
        "upstream_visual_model": visual["model"],
        "upstream_visual_model_calls": visual["model_calls"],
        "input_bindings": {
            "gold_sop_sha256": _sha256(gold_sop_path),
            "visual_review_sha256": _sha256(visual_review_path),
            "dgx_visual_sha256": _sha256(dgx_visual_path),
        },
        "configuration": {
            "merge_gap_ms": merge_gap_ms,
            "candidate_padding_ms": candidate_padding_ms,
        },
        "windows": windows,
        "summary": {
            "step_count": len(gold["steps"]),
            "required_step_count": sum(item["required"] for item in gold["steps"]),
            "window_count": len(windows),
            "source_count": len(source_ids),
            "source_ids": source_ids,
            "multi_source_step_count": sum(
                count > 1 for count in step_window_counts.values()
            ),
            "selected_frame_reference_count": sum(
                item["selected_frame_count"] for item in windows
            ),
            "window_with_dgx_candidate_count": sum(
                item["dgx_candidate_count"] > 0 for item in windows
            ),
            "unique_dgx_candidate_count": len(unique_candidates),
            "supported_step_count": visual["summary"]["supported_count"],
            "partial_step_count": visual["summary"]["partial_count"],
            "not_visible_step_count": visual["summary"]["not_visible_count"],
            "contradicted_step_count": visual["summary"]["contradicted_count"],
        },
        "data_policy": {
            "contains_raw_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "external_model_calls_for_window_generation": 0,
        },
    }
    return validate_document(report, SCHEMA_NAME)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gold",
        type=Path,
        default=ROOT / "cases/n31/gold/gold_sop.json",
    )
    parser.add_argument(
        "--visual-review",
        type=Path,
        default=ROOT / "cases/n31/evaluations/visual_sequence_review_v1.json",
    )
    parser.add_argument(
        "--dgx-visual",
        type=Path,
        default=ROOT / "cases/n31/evaluations/dgx_visual_compute_v1.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "cases/n31/evaluations/temporal_action_windows_v1.json",
    )
    parser.add_argument("--merge-gap-ms", type=int, default=1_000)
    parser.add_argument("--candidate-padding-ms", type=int, default=1_000)
    args = parser.parse_args()
    report = build_temporal_action_windows(
        args.gold,
        args.visual_review,
        args.dgx_visual,
        merge_gap_ms=args.merge_gap_ms,
        candidate_padding_ms=args.candidate_padding_ms,
    )
    _write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
