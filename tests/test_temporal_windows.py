from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from skillforge.contracts import validate_document
from skillforge.temporal_windows import (
    build_temporal_action_windows,
    merge_frame_intervals,
)


ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "cases/n31/gold/gold_sop.json"
VISUAL = ROOT / "cases/n31/evaluations/visual_sequence_review_v1.json"
DGX = ROOT / "cases/n31/evaluations/dgx_visual_compute_v1.json"


def _report() -> dict:
    return build_temporal_action_windows(GOLD, VISUAL, DGX)


def test_merge_frame_intervals_never_crosses_source_timelines() -> None:
    frames = [
        {"evidence_id": "E001", "source_ref": "A", "start_ms": 0, "end_ms": 5000},
        {"evidence_id": "E002", "source_ref": "A", "start_ms": 5000, "end_ms": 10000},
        {"evidence_id": "E003", "source_ref": "A", "start_ms": 12000, "end_ms": 15000},
        {"evidence_id": "E004", "source_ref": "B", "start_ms": 0, "end_ms": 5000},
    ]
    windows = merge_frame_intervals(frames, merge_gap_ms=1000)
    assert windows == [
        {
            "source_ref": "A",
            "start_ms": 0,
            "end_ms": 10000,
            "evidence_ids": ["E001", "E002"],
            "selected_frame_count": 2,
        },
        {
            "source_ref": "A",
            "start_ms": 12000,
            "end_ms": 15000,
            "evidence_ids": ["E003"],
            "selected_frame_count": 1,
        },
        {
            "source_ref": "B",
            "start_ms": 0,
            "end_ms": 5000,
            "evidence_ids": ["E004"],
            "selected_frame_count": 1,
        },
    ]


def test_n31_temporal_windows_are_schema_valid_and_bounded() -> None:
    report = _report()
    validate_document(report, "temporal_action_windows.schema.json")
    assert report["semantic_claim_scope"] == "GOLD_ALIGNED_CANDIDATE_WINDOW_ONLY"
    assert report["model_calls"] == 0
    assert report["summary"] == {
        "step_count": 13,
        "required_step_count": 10,
        "window_count": 19,
        "source_count": 6,
        "source_ids": [
            "N31_VIDEO_GUIDES",
            "N31_VIDEO_LEARNING",
            "N31_VIDEO_MEDIA_FEED",
            "N31_VIDEO_MEDIA_TYPE",
            "N31_VIDEO_OPERATION_FULL",
            "N31_VIDEO_PRINT_RESULT",
        ],
        "multi_source_step_count": 6,
        "selected_frame_reference_count": 51,
        "window_with_dgx_candidate_count": 12,
        "unique_dgx_candidate_count": 41,
        "supported_step_count": 0,
        "partial_step_count": 12,
        "not_visible_step_count": 1,
        "contradicted_step_count": 0,
    }
    assert len({item["window_id"] for item in report["windows"]}) == 19
    assert report["data_policy"] == {
        "contains_raw_media": False,
        "contains_credentials": False,
        "contains_absolute_paths": False,
        "external_model_calls_for_window_generation": 0,
    }


def test_s04_window_preserves_not_visible_verdict_and_dgx_candidates() -> None:
    s04 = next(item for item in _report()["windows"] if item["step_id"] == "S04")
    assert (s04["start_ms"], s04["end_ms"]) == (60_000, 75_000)
    assert s04["evidence_ids"] == ["E013", "E014", "E015"]
    assert s04["dgx_candidate_timestamps_ms"] == [62_000, 63_000, 67_000]
    assert s04["visual_verdict"] == "NOT_VISIBLE"


def test_temporal_window_generation_is_deterministic_except_timestamp() -> None:
    first = _report()
    second = _report()
    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


def test_temporal_windows_reject_stale_visual_timestamps(tmp_path: Path) -> None:
    visual = json.loads(VISUAL.read_text(encoding="utf-8"))
    stale = deepcopy(visual)
    stale["assessments"][0]["frames"][0]["start_ms"] = 1
    path = tmp_path / "stale_visual.json"
    path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="时间或来源与Gold不一致"):
        build_temporal_action_windows(GOLD, path, DGX)
