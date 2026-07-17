from skillforge.contracts import validate_document
from skillforge.dgx_visual import (
    build_visual_compute_report,
    select_scene_candidates,
)


def _cuda_result(scores: list[float]) -> dict:
    return {
        "backend": "cuda_native",
        "device": {
            "name": "NVIDIA GB10",
            "compute_capability": "12.1",
            "total_global_memory_bytes": 128_000_000_000,
        },
        "cuda_runtime": "13.0",
        "gpu_kernel_ms": 2.5,
        "frames": [
            {
                "frame_index": index + 1,
                "mean_luma": 100.0 + index,
                "contrast": 20.0,
                "edge_energy": 5.0,
                "scene_change_score": score,
            }
            for index, score in enumerate(scores)
        ],
    }


def test_scene_selector_keeps_boundaries_and_strongest_changes() -> None:
    cuda = _cuda_result([0.0, 0.02, 0.2, 0.1, 0.01])
    selected = select_scene_candidates(
        cuda["frames"],
        [0, 1000, 2000, 3000, 4000],
        threshold=0.08,
        limit=4,
    )
    assert [item["frame_index"] for item in selected] == [1, 3, 4, 5]
    assert selected[1]["selection_reason"] == "SCENE_CHANGE"


def test_report_proves_gpu_compute_but_limits_semantic_claims() -> None:
    source = {
        "source_id": "N31_VIDEO_TEST",
        "sha256": "a" * 64,
        "duration_ms": 4000,
        "timestamps_ms": [0, 1000, 2000, 3000],
        "cuda": _cuda_result([0.0, 0.01, 0.15, 0.02]),
    }
    report = build_visual_compute_report(
        case_id="n31_media_change",
        source_results=[source],
        excluded_source_count=2,
        sample_interval_seconds=1.0,
        resize_width=320,
        scene_change_threshold=0.08,
        selected_frame_limit=6,
        compiled_arch="sm_121",
        elapsed_seconds=2.0,
        generated_at="2026-07-17T00:00:00+00:00",
    )
    validate_document(report, "dgx_visual_compute.schema.json")
    assert report["actual_gpu_compute"] is True
    assert report["processing_location"] == "DGX_SPARK_LOCAL"
    assert report["semantic_claim_scope"] == "CANDIDATE_SELECTION_ONLY"
    assert report["summary"]["semantic_model_used"] is False
    assert report["source_policy"]["external_api_processing_authorized"] is False
    assert report["source_policy"]["dgx_processing_authorized"] is True
    assert report["source_policy"]["third_party_reference_processed"] is False
    assert report["agent_trace"][-1]["agent"] == "VERIFIER_AGENT"
