import copy
import json
import stat
from pathlib import Path

import pytest

from skillforge.asr_corrections import AsrCorrectionStore
from skillforge.contracts import validate_document


ROOT = Path(__file__).resolve().parents[1]


def _sources() -> tuple[dict, dict]:
    transcript = json.loads(
        (ROOT / "cases/n31/gold/expert_transcript.json").read_text(encoding="utf-8")
    )
    gold = json.loads(
        (ROOT / "cases/n31/gold/gold_sop.json").read_text(encoding="utf-8")
    )
    return transcript, gold


def test_creates_private_source_bound_asr_correction_session(tmp_path) -> None:
    transcript, gold = _sources()
    store = AsrCorrectionStore(tmp_path / "asr_corrections")
    session = store.create(transcript, gold)

    validate_document(session, "asr_correction_session.schema.json")
    assert session["status"] == "OPEN"
    assert session["summary"] == {
        "answer_count": 12,
        "corrected_answer_count": 0,
        "correction_event_count": 0,
        "evidence_binding_count": 12,
    }
    assert [item["evidence_id"] for item in session["answers"]] == [
        f"E{number:03d}" for number in range(143, 155)
    ]
    assert all(
        item["raw_asr_text"]
        and item["effective_corrected_text"] == item["baseline_corrected_text"]
        and item["evidence_binding"]["current_sha256"]
        == item["evidence_binding"]["baseline_sha256"]
        for item in session["answers"]
    )
    session_path = store.root / f"{session['session_id']}.json"
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(session_path.stat().st_mode) == 0o600


def test_correction_records_operator_reason_and_rebinds_evidence_digest(tmp_path) -> None:
    transcript, gold = _sources()
    store = AsrCorrectionStore(tmp_path / "asr_corrections")
    session = store.create(transcript, gold)
    q02 = next(item for item in session["answers"] if item["question_id"] == "Q02")
    baseline = q02["baseline_corrected_text"]
    previous_digest = q02["evidence_binding"]["current_sha256"]

    updated = store.correct(
        session["session_id"],
        "Q02",
        corrected_text=baseline + " 人工复听确认。",
        operator="实际操作者",
        reason="复听后补充句末确认；Bearer " + "private-token-value",
        transcript=transcript,
        gold_sop=gold,
    )
    answer = next(item for item in updated["answers"] if item["question_id"] == "Q02")
    assert updated["status"] == "CORRECTED"
    assert updated["summary"]["corrected_answer_count"] == 1
    assert updated["summary"]["correction_event_count"] == 1
    assert answer["raw_asr_text"] == q02["raw_asr_text"]
    assert answer["correction_state"] == "CORRECTED"
    assert answer["latest_operator"] == "实际操作者"
    assert answer["latest_reason"].endswith("Bearer [REDACTED]")
    assert answer["evidence_binding"]["current_sha256"] != previous_digest
    history = answer["corrections"][0]
    assert history["before_text"] == baseline
    assert history["after_text"] == baseline + " 人工复听确认。"
    assert history["previous_evidence_binding_sha256"] == previous_digest
    assert history["evidence_binding_sha256"] == answer["evidence_binding"]["current_sha256"]
    persisted = json.dumps(store.get(session["session_id"]), ensure_ascii=False)
    assert "private-token-value" not in persisted


def test_correction_can_revert_but_keeps_audit_chain(tmp_path) -> None:
    transcript, gold = _sources()
    store = AsrCorrectionStore(tmp_path / "asr_corrections")
    session = store.create(transcript, gold)
    answer = next(item for item in session["answers"] if item["question_id"] == "Q05")
    baseline = answer["baseline_corrected_text"]
    first = store.correct(
        session["session_id"],
        "Q05",
        corrected_text=baseline + " 已复听。",
        operator="实际操作者",
        reason="第一次复听修正",
        transcript=transcript,
        gold_sop=gold,
    )
    reverted = store.correct(
        session["session_id"],
        "Q05",
        corrected_text=baseline,
        operator="实际操作者",
        reason="再次复听后恢复基线文本",
        transcript=transcript,
        gold_sop=gold,
    )
    current = next(item for item in reverted["answers"] if item["question_id"] == "Q05")
    assert first["summary"]["corrected_answer_count"] == 1
    assert reverted["status"] == "CORRECTED"
    assert reverted["summary"]["corrected_answer_count"] == 0
    assert reverted["summary"]["correction_event_count"] == 2
    assert current["correction_state"] == "UNCHANGED"
    assert current["correction_count"] == 2
    assert current["evidence_binding"]["current_sha256"] == current["evidence_binding"][
        "baseline_sha256"
    ]


def test_correction_rejects_missing_verified_terms_and_changed_sources(tmp_path) -> None:
    transcript, gold = _sources()
    store = AsrCorrectionStore(tmp_path / "asr_corrections")
    session = store.create(transcript, gold)
    with pytest.raises(ValueError, match="必要术语"):
        store.correct(
            session["session_id"],
            "Q02",
            corrected_text="只保留一段不完整的修正文本",
            operator="实际操作者",
            reason="错误尝试",
            transcript=transcript,
            gold_sop=gold,
        )

    changed = copy.deepcopy(transcript)
    changed["answers"][0]["topic"] += "（已变化）"
    with pytest.raises(ValueError, match="已经变化"):
        store.correct(
            session["session_id"],
            "Q02",
            corrected_text=next(
                item["baseline_corrected_text"]
                for item in session["answers"]
                if item["question_id"] == "Q02"
            )
            + " 人工复听确认。",
            operator="实际操作者",
            reason="来源变化后不应继续",
            transcript=changed,
            gold_sop=gold,
        )
