import copy
import json
import stat
from pathlib import Path

import pytest

from skillforge.conflict_adjudication import (
    HUMAN_REQUIRED_KINDS,
    ConflictDecisionStore,
    route_conflict,
)
from skillforge.demo import ROOT
from skillforge.revision import revise_sop
from skillforge.verifier import verify_sop


def _read(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _gold_sources() -> tuple[dict, dict, dict, dict]:
    proposed = _read("cases/n31/demo_bundle/after_sop.json")
    proposed["evidence_catalog"] = _read("cases/n31/gold/gold_sop.json")[
        "evidence_catalog"
    ]
    return (
        _read("cases/n31/demo_bundle/initial_conflicts.json"),
        _read("cases/n31/demo_bundle/revision_audit.json"),
        proposed,
        _read("cases/n31/demo_bundle/final_conflicts.json"),
    )


def _safety_sources() -> tuple[dict, dict, dict, dict]:
    reference = _read("cases/n31/gold/gold_sop.json")
    constraints = _read("cases/n31/gold/constraints.json")
    draft = copy.deepcopy(reference)
    draft["steps"][0]["title"] += "，保证100%安全"
    initial = verify_sop(draft, reference, constraints, iteration=1)
    proposed, audit = revise_sop(draft, initial, reference, constraints, iteration=1)
    final = verify_sop(proposed, reference, constraints, iteration=2)
    return initial, audit, proposed, final


def test_gold_conflicts_are_auto_finalized_with_final_results(tmp_path) -> None:
    store = ConflictDecisionStore(tmp_path / "conflict-decisions")
    session = store.create(*_gold_sources())
    assert session["status"] == "AUTO_FINALIZED"
    assert session["finalization"] == {
        "publishable": True,
        "final_sop_sha256": session["source_bindings"]["proposed_sop_sha256"],
        "proposed_residual_conflict_count": 0,
        "adopted_unresolved_conflict_count": 0,
        "adopted_conflict_count": 5,
        "rejected_conflict_count": 0,
        "pending_conflict_count": 0,
    }
    assert all(item["route"] == "AUTO" for item in session["decisions"])
    assert all(item["human_decision"] == "NOT_REQUIRED" for item in session["decisions"])
    assert {item["final_result"] for item in session["decisions"]} == {
        "ADOPTED",
        "RESOLVED_BY_RELATED_CHANGE",
    }
    assert stat.S_IMODE((tmp_path / "conflict-decisions").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "conflict-decisions" / f"{session['session_id']}.json").stat().st_mode) == 0o600


def test_safety_conflict_overrides_automatic_flag_and_requires_operator(tmp_path) -> None:
    sources = _safety_sources()
    conflict = sources[0]["conflicts"][0]
    assert conflict["kind"] == "UNSUPPORTED_SAFETY_CLAIM"
    assert conflict["automatic"] is True
    assert route_conflict(conflict)[0] == "HUMAN"
    assert set(HUMAN_REQUIRED_KINDS) == {
        "UNSUPPORTED_SAFETY_CLAIM",
        "MISSING_EVIDENCE",
        "INVALID_EVIDENCE",
    }

    store = ConflictDecisionStore(tmp_path / "decisions")
    session = store.create(*sources)
    decision = session["decisions"][0]
    assert session["status"] == "AWAITING_HUMAN"
    assert session["finalization"]["publishable"] is False
    assert decision["safety_override"] is True
    assert decision["automatic_decision"] == "DEFER_TO_HUMAN"
    assert decision["human_decision"] == "PENDING"

    approved = store.decide(
        session["session_id"],
        decision["conflict_id"],
        approved=True,
        comment="确认删除无来源的绝对安全承诺；" + "Bearer " + "private-token-value",
        initial_report=sources[0],
        revision_audit=sources[1],
        proposed_sop=sources[2],
        final_report=sources[3],
    )
    assert approved["status"] == "FINALIZED"
    assert approved["finalization"]["publishable"] is True
    assert approved["decisions"][0]["confirmed_by"] == "OPERATOR"
    assert "private-token-value" not in json.dumps(approved, ensure_ascii=False)


def test_rejected_safety_decision_is_not_publishable_but_can_be_reopened(tmp_path) -> None:
    sources = _safety_sources()
    store = ConflictDecisionStore(tmp_path / "decisions")
    session = store.create(*sources)
    conflict_id = session["decisions"][0]["conflict_id"]
    rejected = store.decide(
        session["session_id"],
        conflict_id,
        approved=False,
        comment="暂不采用，需重新核对设备安全措辞",
        initial_report=sources[0],
        revision_audit=sources[1],
        proposed_sop=sources[2],
        final_report=sources[3],
    )
    assert rejected["status"] == "NEEDS_REVIEW"
    assert rejected["finalization"]["publishable"] is False
    assert rejected["finalization"]["adopted_unresolved_conflict_count"] == 1
    assert rejected["decisions"][0]["final_result"] == "KEPT_ORIGINAL"

    reopened = store.decide(
        session["session_id"],
        conflict_id,
        approved=True,
        comment="复核Evidence后同意删除绝对安全承诺",
        initial_report=sources[0],
        revision_audit=sources[1],
        proposed_sop=sources[2],
        final_report=sources[3],
    )
    assert reopened["status"] == "FINALIZED"
    assert any(item["event_type"] == "REOPENED" for item in reopened["events"])


def test_decision_rejects_changed_sources_and_auto_route_manual_override(tmp_path) -> None:
    sources = _safety_sources()
    store = ConflictDecisionStore(tmp_path / "decisions")
    session = store.create(*sources)
    changed = copy.deepcopy(sources[0])
    changed["conflicts"][0]["message"] += "（已变化）"
    with pytest.raises(ValueError, match="来源已经变化"):
        store.decide(
            session["session_id"],
            session["decisions"][0]["conflict_id"],
            approved=True,
            comment="同意",
            initial_report=changed,
            revision_audit=sources[1],
            proposed_sop=sources[2],
            final_report=sources[3],
        )

    auto = ConflictDecisionStore(tmp_path / "auto").create(*_gold_sources())
    with pytest.raises(ValueError, match="不能伪装成人工裁决"):
        ConflictDecisionStore(tmp_path / "auto").decide(
            auto["session_id"],
            auto["decisions"][0]["conflict_id"],
            approved=True,
            comment="不允许人工覆盖自动路由",
            initial_report=_gold_sources()[0],
            revision_audit=_gold_sources()[1],
            proposed_sop=_gold_sources()[2],
            final_report=_gold_sources()[3],
        )
