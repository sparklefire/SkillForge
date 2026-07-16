import json
from pathlib import Path

from skillforge.contracts import validate_document
from skillforge.expert_gold import align_expert_answers


ROOT = Path(__file__).resolve().parents[1]


def test_align_expert_answers_uses_timestamped_stream() -> None:
    transcription = {
        "text": "角色和经验N31实际操作者纸张形态折叠纸",
        "segments": [
            {"text": "角色和经验", "start_ms": 100, "end_ms": 500},
            {"text": "N31实际操作者", "start_ms": 500, "end_ms": 900},
            {"text": "纸张形态", "start_ms": 900, "end_ms": 1200},
            {"text": "折叠纸", "start_ms": 1200, "end_ms": 1500},
        ],
    }
    plan = {
        "corrections": [],
        "answers": [
            {
                "question_id": "Q01",
                "topic": "角色",
                "anchor_variants": ["角色和经验"],
                "required_terms": ["N31", "实际操作者"],
                "verified_claim": "操作者确认经验。",
                "bind_step_ids": [],
            },
            {
                "question_id": "Q02",
                "topic": "纸张",
                "anchor_variants": ["纸张形态"],
                "required_terms": ["折叠纸"],
                "verified_claim": "介质是折叠纸。",
                "bind_step_ids": ["S01"],
            },
        ],
    }
    answers = align_expert_answers(transcription, plan)
    assert [(item["start_ms"], item["end_ms"]) for item in answers] == [
        (100, 900),
        (900, 1500),
    ]
    assert all(all(item["required_term_checks"].values()) for item in answers)


def test_checked_in_n31_gold_is_schema_valid_and_audio_grounded() -> None:
    gold = json.loads(
        (ROOT / "cases/n31/gold/gold_sop.json").read_text(encoding="utf-8")
    )
    validate_document(gold, "sop.schema.json")
    assert len(gold["steps"]) == 13
    assert all(item["status"] == "VERIFIED" for item in gold["steps"])
    assert [item["step_id"] for item in gold["steps"] if not item["required"]] == [
        "S05",
        "S06",
        "S11",
    ]
    audio = [
        item
        for item in gold["evidence_catalog"]
        if item["source_ref"] == "N31_EXPERT_INTERVIEW"
    ]
    assert len(audio) == 12
    assert all(item["review_status"] == "VERIFIED" for item in audio)
    assert gold["steps"][0]["parameters"][0]["evidence_ids"] == ["E144"]
    assert gold["steps"][9]["parameters"] == []
