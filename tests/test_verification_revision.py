import json
from pathlib import Path

from skillforge.contracts import validate_document
from skillforge.revision import revise_sop
from skillforge.synthetic_case import inject_faults
from skillforge.verifier import metrics, verify_sop


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "cases/demo_case/synthetic"


def load(name: str):
    return json.loads((CASE / name).read_text(encoding="utf-8"))


def test_controlled_errors_are_found_and_locally_revised() -> None:
    reference = load("reference_sop.json")
    constraints = load("constraints.json")
    draft = inject_faults(reference, load("fault_injection.json"))
    validate_document(draft, "sop.schema.json")

    before = verify_sop(draft, reference, constraints)
    kinds = {item["kind"] for item in before["conflicts"]}
    assert {
        "MISSING_STEP",
        "MISSING_PREREQUISITE",
        "ORDER_ERROR",
        "UNSUPPORTED_TOOL",
        "UNSUPPORTED_PARAMETER",
    }.issubset(kinds)
    assert metrics(draft, before, constraints)["severe_error_count"] == 5

    revised, audit = revise_sop(draft, before, reference, constraints)
    after = verify_sop(revised, reference, constraints, iteration=2)

    assert after["conflicts"] == []
    assert [step["step_id"] for step in revised["steps"]] == constraints["expected_order"]
    assert len(audit["changes"]) == 4
    assert metrics(revised, after, constraints)["required_step_coverage"] == 1.0
    final_step = revised["steps"][-1]
    assert "torque wrench" not in final_step["tools"]
    assert final_step["parameters"] == []
