import json
from pathlib import Path

from skillforge.provisional_rehearsal import run_provisional_rehearsal


ROOT = Path(__file__).resolve().parents[1]


def test_provisional_rehearsal_is_explicitly_not_gold(tmp_path) -> None:
    demo_case = ROOT / "cases" / "demo_case" / "synthetic"
    reference = json.loads(
        (demo_case / "reference_sop.json").read_text(encoding="utf-8")
    )
    reference["case_id"] = "real_source_test"
    reference["title"] = "Real-source candidate, not Gold"
    reference_path = tmp_path / "candidate.json"
    reference_path.write_text(json.dumps(reference), encoding="utf-8")

    constraints = json.loads(
        (demo_case / "constraints.json").read_text(encoding="utf-8")
    )
    constraints["evaluation_basis"] = "CANDIDATE_NOT_GOLD"
    constraints_path = tmp_path / "constraints.json"
    constraints_path.write_text(json.dumps(constraints), encoding="utf-8")

    faults = json.loads(
        (demo_case / "fault_injection.json").read_text(encoding="utf-8")
    )
    faults["controlled_rehearsal"] = True
    faults_path = tmp_path / "faults.json"
    faults_path.write_text(json.dumps(faults), encoding="utf-8")

    summary = run_provisional_rehearsal(
        reference_path,
        constraints_path,
        faults_path,
        tmp_path / "output",
    )
    assert summary["synthetic"] is False
    assert summary["evaluation_basis"] == "CANDIDATE_NOT_GOLD"
    assert summary["gold_status"] == "NOT_GOLD"
    assert summary["metrics_status"] == "PROVISIONAL_ONLY"
    assert summary["human_review_required"] is True
    assert summary["after"]["severe_error_count"] == 0
