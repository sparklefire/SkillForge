from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from skillforge.contracts import validate_document
from skillforge.step_discovery import (
    StepDiscoveryAgent,
    StepDiscoveryError,
    build_safe_discovery_payload,
    validate_discovery_response,
)
from skillforge.step_discovery_eval import evaluate_step_discovery
from skillforge.step_plan import StepPlanClient


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "cases" / "demo_case" / "synthetic"


def _read(name: str) -> Any:
    return json.loads((CASE / name).read_text(encoding="utf-8"))


def _client(*responses: dict[str, Any]) -> StepPlanClient:
    pending = list(responses)

    def transport(_payload: dict[str, Any]) -> dict[str, Any]:
        response = pending.pop(0)
        return {
            "model": "test-fixture",
            "choices": [
                {"message": {"content": json.dumps(response, ensure_ascii=False)}}
            ],
            "usage": {"total_tokens": 0},
        }

    return StepPlanClient(transport=transport, retry_sleep=lambda _: None)


def test_offline_discovery_builds_strict_review_only_report() -> None:
    evidence = _read("discovery_evidence.json")
    response = _read("discovery_response_fixture.json")
    report = StepDiscoveryAgent(_client(response)).discover(
        evidence,
        case_id="SYNTHETIC-DISCOVERY-001",
        title="虚构候选发现",
        planning_attempts=1,
        external_processing=False,
    )

    validate_document(report, "step_discovery_report.schema.json")
    assert report["status"] == "NEEDS_REVIEW"
    assert report["uses_gold_step_text"] is False
    assert report["semantic_spec_provided"] is False
    assert report["model"] == "offline-fixture"
    assert report["model_calls"] == 0
    assert report["data_policy"]["external_model_calls"] == 0
    assert report["summary"]["step_count"] == 9
    assert report["summary"]["used_evidence_count"] == 9
    assert {
        item["review_status"] for item in report["ordered_candidates"]
    } == {"HUMAN_REVIEW_REQUIRED"}
    assert report["guardrails"]["may_override_gold"] is False
    assert report["guardrails"]["may_publish"] is False


def test_safe_payload_omits_source_paths_and_keyframes() -> None:
    evidence = _read("discovery_evidence.json")
    evidence[0]["locator"]["keyframe"] = "frames/private.jpg"
    payload = build_safe_discovery_payload(evidence)

    encoded = json.dumps(payload, ensure_ascii=False)
    assert "source_ref" not in encoded
    assert "keyframe" not in encoded
    assert "discovery_demo.mp4" not in encoded
    assert payload["evidence"][0]["locator"] == {"start_ms": 0, "end_ms": 6000}


def test_absolute_path_and_rejected_evidence_fail_closed() -> None:
    evidence = _read("discovery_evidence.json")
    absolute = copy.deepcopy(evidence)
    absolute[0]["source_ref"] = "/Users/example/private.mp4"
    with pytest.raises(StepDiscoveryError, match="禁止标记"):
        build_safe_discovery_payload(absolute)

    rejected = copy.deepcopy(evidence)
    rejected[0]["review_status"] = "REJECTED"
    with pytest.raises(StepDiscoveryError, match="已驳回Evidence"):
        build_safe_discovery_payload(rejected)


def test_schema_retry_rejects_model_controlled_provenance() -> None:
    evidence = _read("discovery_evidence.json")
    invalid = _read("discovery_response_fixture.json")
    invalid["steps"][0]["source_types"] = ["audio"]
    valid = _read("discovery_response_fixture.json")
    client = _client(invalid, valid)

    report = StepDiscoveryAgent(client).discover(
        evidence,
        case_id="SYNTHETIC-DISCOVERY-001",
        title="虚构候选发现",
        planning_attempts=1,
        external_processing=False,
    )

    assert client.call_count == 2
    assert report["ordered_candidates"][0]["source_types"] == ["video"]


def test_local_grounding_retry_rejects_unknown_evidence() -> None:
    evidence = _read("discovery_evidence.json")
    invalid = _read("discovery_response_fixture.json")
    invalid["steps"][0]["evidence_ids"] = ["E999"]
    invalid["steps"][0]["tools"][0]["evidence_ids"] = ["E999"]
    invalid["steps"][0]["success_check"]["evidence_ids"] = ["E999"]
    valid = _read("discovery_response_fixture.json")
    client = _client(invalid, valid)

    report = StepDiscoveryAgent(client).discover(
        evidence,
        case_id="SYNTHETIC-DISCOVERY-001",
        title="虚构候选发现",
        planning_attempts=2,
        external_processing=False,
    )

    assert client.call_count == 2
    assert report["summary"]["all_references_grounded"] is True


def test_nested_claims_must_be_in_step_evidence_set() -> None:
    evidence = _read("discovery_evidence.json")
    response = _read("discovery_response_fixture.json")
    response["steps"][0]["tools"][0]["evidence_ids"] = ["E002"]

    with pytest.raises(StepDiscoveryError, match="未列入步骤"):
        validate_discovery_response(response, evidence)


def test_graph_phase_and_evidence_accounting_fail_closed() -> None:
    evidence = _read("discovery_evidence.json")

    forward = _read("discovery_response_fixture.json")
    forward["steps"][1]["prerequisites"] = ["P03"]
    with pytest.raises(StepDiscoveryError, match="较早候选"):
        validate_discovery_response(forward, evidence)

    phase_rollback = _read("discovery_response_fixture.json")
    phase_rollback["steps"][7]["phase"] = "PREPARATION"
    with pytest.raises(StepDiscoveryError, match="阶段顺序发生回退"):
        validate_discovery_response(phase_rollback, evidence)

    unaccounted = _read("discovery_response_fixture.json")
    unaccounted["steps"][8]["evidence_ids"] = ["E008"]
    unaccounted["steps"][8]["success_check"]["evidence_ids"] = ["E008"]
    with pytest.raises(StepDiscoveryError, match="未被使用或说明排除"):
        validate_discovery_response(unaccounted, evidence)


def test_discovery_evaluation_is_reference_separated_and_reproducible() -> None:
    evidence = _read("discovery_evidence.json")
    response = _read("discovery_response_fixture.json")
    discovery = StepDiscoveryAgent(_client(response)).discover(
        evidence,
        case_id="SYNTHETIC-DISCOVERY-001",
        title="虚构候选发现",
        planning_attempts=1,
        external_processing=False,
    )
    evaluation = evaluate_step_discovery(
        discovery,
        _read("discovery_evaluation_spec.json"),
    )

    assert evaluation["status"] == "PASSED"
    assert evaluation["uses_gold_as_model_input"] is False
    assert evaluation["evaluation_reference_not_sent_to_model"] is True
    assert evaluation["semantic_text_quality_evaluated"] is False
    assert evaluation["metrics"]["evidence_recall"] == 1.0
    assert evaluation["metrics"]["ordering_violation_count"] == 0
    assert evaluation["metrics"]["source_type_coverage"] == 1.0
    assert evaluation["metrics"]["review_containment_rate"] == 1.0


def test_discovery_evaluation_detects_evidence_order_regression() -> None:
    evidence = _read("discovery_evidence.json")
    response = _read("discovery_response_fixture.json")
    discovery = StepDiscoveryAgent(_client(response)).discover(
        evidence,
        case_id="SYNTHETIC-DISCOVERY-001",
        title="虚构候选发现",
        planning_attempts=1,
        external_processing=False,
    )
    discovery["ordered_candidates"][0]["evidence_ids"] = ["E002"]
    discovery["ordered_candidates"][0]["tools"][0]["evidence_ids"] = ["E002"]
    discovery["ordered_candidates"][0]["success_check"]["evidence_ids"] = ["E002"]
    discovery["ordered_candidates"][0]["source_types"] = ["pdf"]
    discovery["ordered_candidates"][1]["evidence_ids"] = ["E001"]
    discovery["ordered_candidates"][1]["success_check"]["evidence_ids"] = ["E001"]
    discovery["ordered_candidates"][1]["source_types"] = ["video"]

    evaluation = evaluate_step_discovery(
        discovery,
        _read("discovery_evaluation_spec.json"),
    )

    assert evaluation["status"] == "FAILED"
    assert evaluation["metrics"]["ordering_violation_count"] == 1
    assert any("E001>E002" in item for item in evaluation["failures"])
