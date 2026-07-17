import copy
import json
from pathlib import Path

from skillforge.contracts import validate_document
from skillforge.grounding_gate import build_grounding_gate
from skillforge.revision import revise_sop
from skillforge.verifier import verify_sop


ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "cases/n31/gold/gold_sop.json"
CONSTRAINTS = ROOT / "cases/n31/gold/constraints.json"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_grounding_gate_closes_four_isolated_tamper_scenarios(tmp_path) -> None:
    output = tmp_path / "grounding_gate.json"
    report = build_grounding_gate(GOLD, CONSTRAINTS, output_path=output)

    assert output.is_file()
    assert validate_document(_read(output), "grounding_gate_report.schema.json") == report
    assert report["status"] == "PASSED"
    assert report["summary"] == {
        "scenario_count": 4,
        "passed_count": 4,
        "detected_count": 4,
        "revised_count": 4,
        "residual_conflict_count": 0,
    }
    assert [item["scenario_id"] for item in report["scenarios"]] == [
        "CROSS_STEP_ALLOWED_TOOL",
        "ALLOWED_PARAMETER_WRONG_VALUE",
        "UNGROUNDED_WARNING",
        "ABSOLUTE_SAFETY_PROMISE",
    ]
    assert all(item["reference_evidence_ids"] for item in report["scenarios"])
    assert all(item["restored"] is True for item in report["scenarios"])


def test_globally_allowed_tool_is_rejected_outside_its_grounded_step() -> None:
    reference = _read(GOLD)
    constraints = _read(CONSTRAINTS)
    candidate = copy.deepcopy(reference)
    candidate["steps"][0]["tools"].append("本批标签纸")

    report = verify_sop(candidate, reference, constraints)
    conflict = report["conflicts"][0]

    assert conflict["kind"] == "UNSUPPORTED_TOOL"
    assert conflict["details"]["globally_allowed"] is True
    assert conflict["details"]["supported_in_step"] is False
    assert {item["evidence_id"] for item in conflict["evidence"]} == {
        "E001",
        "E096",
        "E144",
    }
    revised, audit = revise_sop(candidate, report, reference, constraints)
    assert revised["steps"][0]["tools"] == []
    assert audit["changes"][0]["evidence_ids"] == ["E001", "E096", "E144"]


def test_wrong_allowed_parameter_value_is_restored_from_evidence() -> None:
    reference = _read(GOLD)
    constraints = _read(CONSTRAINTS)
    candidate = copy.deepcopy(reference)
    candidate["steps"][0]["parameters"][0]["value"] = 999

    report = verify_sop(candidate, reference, constraints)
    conflict = report["conflicts"][0]
    assert conflict["kind"] == "UNSUPPORTED_PARAMETER"
    assert conflict["proposed_action"] == "REPLACE"
    assert conflict["details"]["reference_parameter"]["value"] == 72

    revised, audit = revise_sop(candidate, report, reference, constraints)
    assert revised["steps"][0]["parameters"][0]["value"] == 72
    assert audit["changes"][0]["action"] == "REPLACE"
    assert audit["changes"][0]["evidence_ids"] == ["E144"]
    assert verify_sop(revised, reference, constraints, iteration=2)["conflicts"] == []


def test_ungrounded_warning_and_absolute_promise_are_locally_removed() -> None:
    reference = _read(GOLD)
    constraints = _read(CONSTRAINTS)
    candidate = copy.deepcopy(reference)
    candidate["steps"][0]["warnings"].append("佩戴护目镜即可避免所有风险。")
    candidate["steps"][0]["success_check"] += " 可保证100%安全。"

    report = verify_sop(candidate, reference, constraints)
    conflicts = [
        item for item in report["conflicts"] if item["kind"] == "UNSUPPORTED_SAFETY_CLAIM"
    ]
    assert len(conflicts) == 2
    assert {item["details"]["field"] for item in conflicts} == {
        "warnings",
        "success_check",
    }

    revised, audit = revise_sop(candidate, report, reference, constraints)
    assert revised["steps"][0]["warnings"] == reference["steps"][0]["warnings"]
    assert revised["steps"][0]["success_check"] == reference["steps"][0]["success_check"]
    assert [item["action"] for item in audit["changes"]] == ["REMOVE", "REPLACE"]
    assert verify_sop(revised, reference, constraints, iteration=2)["conflicts"] == []


def test_absolute_promise_without_reference_step_requires_human_review() -> None:
    reference = _read(GOLD)
    constraints = _read(CONSTRAINTS)
    candidate = copy.deepcopy(reference)
    extra = copy.deepcopy(next(item for item in reference["steps"] if item["step_id"] == "S03"))
    extra.update(
        {
            "step_id": "S99",
            "title": "临时步骤可保证安全",
            "required": False,
            "prerequisites": [],
            "warnings": [],
            "parameters": [],
            "tools": [],
        }
    )
    candidate["steps"].append(extra)

    report = verify_sop(candidate, reference, constraints)
    conflict = next(
        item
        for item in report["conflicts"]
        if item["kind"] == "UNSUPPORTED_SAFETY_CLAIM"
    )
    assert conflict["details"]["step_id"] == "S99"
    assert conflict["automatic"] is False
    assert conflict["proposed_action"] == "REVIEW"
