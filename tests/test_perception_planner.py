import json
from pathlib import Path

from skillforge.perception import PerceptionAgent
from skillforge.planner import SOPAgent


ROOT = Path(__file__).resolve().parents[1]


class FakePerceptionClient:
    def chat_json(self, **kwargs):
        content = kwargs["messages"][0]["content"]
        assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        return {
            "evidence_id": "E999",
            "source_type": "pdf",
            "source_ref": "wrong.pdf",
            "claim": "画面显示一块测试色卡，没有可见的设备操作。",
            "locator": {"page": 9},
            "classification": "SOURCE_FACT",
            "relevance": 0.8,
            "confidence": 0.9,
            "review_status": "VERIFIED",
        }


class FakePlannerClient:
    def __init__(self, reference):
        self.reference = reference

    def chat_json(self, **kwargs):
        assert kwargs["schema_name"] == "sop_draft.schema.json"
        return {
            "case_id": "model-controlled-id",
            "title": "模型草稿",
            "version": 1,
            "steps": self.reference["steps"],
        }


def test_perception_cannot_alter_canonical_provenance(tmp_path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"synthetic-jpeg-bytes")
    result = PerceptionAgent(FakePerceptionClient()).analyze_frame(
        frame,
        evidence_id="E001",
        source_ref="operation.mp4",
        keyframe_ref="frames/frame.jpg",
        start_ms=1000,
        end_ms=2000,
    )
    assert result["evidence_id"] == "E001"
    assert result["source_type"] == "video"
    assert result["locator"] == {
        "start_ms": 1000,
        "end_ms": 2000,
        "keyframe": "frames/frame.jpg",
    }
    assert result["classification"] == "MODEL_INFERENCE"
    assert result["review_status"] == "UNREVIEWED"


def test_planner_merges_canonical_evidence_catalog() -> None:
    reference = json.loads(
        (ROOT / "cases/demo_case/synthetic/reference_sop.json").read_text(
            encoding="utf-8"
        )
    )
    result = SOPAgent(FakePlannerClient(reference)).plan(
        reference["evidence_catalog"],
        case_id="CANONICAL-CASE",
        title="规范 SOP",
    )
    assert result["case_id"] == "CANONICAL-CASE"
    assert result["title"] == "规范 SOP"
    assert result["evidence_catalog"] == reference["evidence_catalog"]
    assert len(result["steps"]) == 9
