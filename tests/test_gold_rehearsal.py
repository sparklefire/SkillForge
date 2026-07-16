from pathlib import Path

from skillforge.gold_rehearsal import GOLD_BASIS, run_gold_rehearsal


ROOT = Path(__file__).resolve().parents[1]


def test_n31_gold_rehearsal_detects_and_repairs_controlled_errors(tmp_path) -> None:
    case = ROOT / "cases/n31/gold"
    summary = run_gold_rehearsal(
        case / "gold_sop.json",
        case / "constraints.json",
        case / "fault_injection.json",
        tmp_path / "output",
    )
    assert summary["evaluation_basis"] == GOLD_BASIS
    assert summary["gold_status"] == "GOLD"
    assert summary["metrics_status"] == "FINAL"
    assert summary["human_review_required"] is False
    assert summary["before"]["severe_error_count"] == 5
    assert summary["after"]["severe_error_count"] == 0
    assert summary["after"]["required_step_coverage"] == 1.0
    assert summary["after"]["evidence_supported_required_steps"] == 1.0
