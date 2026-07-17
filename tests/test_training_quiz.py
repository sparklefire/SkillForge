import copy
import json
from pathlib import Path

import pytest

from skillforge.contracts import ContractValidationError, validate_document
from skillforge.creator import _validate_quiz_integrity, create_quiz


ROOT = Path(__file__).resolve().parents[1]


def _sop(name: str = "n31") -> dict:
    path = (
        ROOT / "cases/n31/gold/gold_sop.json"
        if name == "n31"
        else ROOT / "cases/demo_case/synthetic/reference_sop.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def test_quiz_covers_five_distinct_grounded_categories() -> None:
    quiz = create_quiz(_sop())
    validate_document(quiz, "training_quiz.schema.json")
    assert [item["category"] for item in quiz["questions"]] == [
        "ORDERING",
        "TOOL_SELECTION",
        "RISK_RESPONSE",
        "STATUS_RECOGNITION",
        "ERROR_JUDGMENT",
    ]
    assert [item["type"] for item in quiz["questions"]] == [
        "ORDERING",
        "MULTIPLE_SELECT",
        "SINGLE_CHOICE",
        "SINGLE_CHOICE",
        "TRUE_FALSE",
    ]
    for question in quiz["questions"]:
        evidence_ids = set(question["evidence_ids"])
        assert set(question["answer_evidence_ids"]) <= evidence_ids
        assert set(question["explanation_evidence_ids"]) <= evidence_ids
        assert {item["evidence_id"] for item in question["evidence_details"]} == (
            evidence_ids
        )
        assert all(set(option["evidence_ids"]) <= evidence_ids for option in question["options"])


def test_quiz_answers_match_sop_facts() -> None:
    sop = _sop()
    by_step = {item["step_id"]: item for item in sop["steps"]}
    questions = {item["category"]: item for item in create_quiz(sop)["questions"]}
    tool = questions["TOOL_SELECTION"]
    assert set(tool["answer"]) == set(by_step[tool["step_ids"][0]]["tools"])
    risk = questions["RISK_RESPONSE"]
    assert risk["answer"] in by_step[risk["step_ids"][0]]["warnings"]
    status = questions["STATUS_RECOGNITION"]
    assert status["answer"] == by_step[status["step_ids"][0]]["success_check"]
    assert questions["ERROR_JUDGMENT"]["answer"] is False


def test_ordering_answer_respects_selected_dependencies() -> None:
    sop = _sop()
    by_step = {item["step_id"]: item for item in sop["steps"]}
    ordering = create_quiz(sop)["questions"][0]
    positions = {step_id: index for index, step_id in enumerate(ordering["answer"])}
    for step_id in ordering["answer"]:
        for prerequisite in by_step[step_id]["prerequisites"]:
            if prerequisite in positions:
                assert positions[prerequisite] < positions[step_id]


def test_quiz_generator_also_supports_synthetic_regression_case() -> None:
    quiz = create_quiz(_sop("synthetic"))
    assert quiz["coverage"]["question_count"] == 5
    assert quiz["coverage"]["all_answers_grounded"] is True


def test_quiz_integrity_rejects_tampered_answer_and_evidence() -> None:
    sop = _sop()
    quiz = create_quiz(sop)
    wrong_tool = copy.deepcopy(quiz)
    wrong_tool["questions"][1]["answer"] = [wrong_tool["questions"][1]["options"][1]["value"]]
    with pytest.raises(ValueError, match="工具题答案"):
        _validate_quiz_integrity(wrong_tool, sop)

    wrong_evidence = copy.deepcopy(quiz)
    wrong_evidence["questions"][0]["answer_evidence_ids"] = ["E999"]
    with pytest.raises(ValueError, match="答案来源越界"):
        _validate_quiz_integrity(wrong_evidence, sop)


def test_quiz_schema_rejects_missing_category() -> None:
    quiz = create_quiz(_sop())
    quiz["questions"][4]["category"] = "STATUS_RECOGNITION"
    with pytest.raises(ContractValidationError):
        validate_document(quiz, "training_quiz.schema.json")
