from pathlib import Path

from skillforge.visual_review import (
    VisualSequenceAgent,
    select_visual_windows,
)


class FakeVisualClient:
    def chat_json(self, **kwargs):
        assert kwargs["schema_name"] == "visual_assessment.schema.json"
        content = kwargs["messages"][0]["content"]
        assert sum(item["type"] == "image_url" for item in content) == 3
        return {
            "step_id": "S99",
            "verdict": "PARTIAL",
            "observed_claim": "画面可见纸张沿导轨移动。",
            "visible_actions": ["纸张进入导轨"],
            "missing_or_uncertain": ["无法确认停止位置"],
            "cited_evidence_ids": ["E001", "E002"],
            "confidence": 0.72,
            "privacy_observation": "NO_SENSITIVE_CONTENT_VISIBLE",
        }


def _evidence(evidence_id: str, start_ms: int, name: str) -> dict:
    return {
        "evidence_id": evidence_id,
        "source_type": "video",
        "source_ref": "VIDEO",
        "claim": "关键帧候选",
        "locator": {
            "start_ms": start_ms,
            "end_ms": start_ms + 1000,
            "keyframe": f"frames/{name}",
        },
        "classification": "MODEL_INFERENCE",
        "relevance": 0.5,
        "confidence": 0.5,
        "review_status": "UNREVIEWED",
    }


def test_selects_direct_frame_and_adjacent_sequence(tmp_path) -> None:
    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    for name in ("one.jpg", "two.jpg", "three.jpg"):
        (frame_dir / name).write_bytes(b"fake-jpeg")
    sop = {
        "evidence_catalog": [
            _evidence("E001", 0, "one.jpg"),
            _evidence("E002", 1000, "two.jpg"),
            _evidence("E003", 2000, "three.jpg"),
        ],
        "steps": [
            {
                "step_id": "S01",
                "title": "进纸",
                "action": "把纸张送入导轨。",
                "success_check": "纸张平直进入。",
                "required": True,
                "evidence": ["E002"],
            }
        ],
    }
    windows = select_visual_windows(sop, tmp_path)
    assert [item["evidence_id"] for item in windows[0]["frames"]] == [
        "E001",
        "E002",
        "E003",
    ]
    result = VisualSequenceAgent(FakeVisualClient()).review(windows[0])
    assert result["step_id"] == "S01"
    assert result["cited_evidence_ids"] == ["E001", "E002"]
