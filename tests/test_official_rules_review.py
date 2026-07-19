from __future__ import annotations

import json
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from skillforge.contracts import ContractValidationError, validate_document
from skillforge.official_rules_review import (
    REQUIREMENT_IDS,
    OfficialRulesReviewError,
    _write_private_json,
    attach_local_source,
    attach_source_url,
    initialize_official_rules_review,
    official_rules_review_qa_issue,
    verify_official_rules_review,
)
from skillforge.submission import build_submission_preflight


ROOT = Path(__file__).resolve().parents[1]


def _complete_review_document(review_path: Path) -> dict:
    document = json.loads(review_path.read_text(encoding="utf-8"))
    document.update(
        {
            "updated_at": "2026-07-19T05:00:00+00:00",
            "status": "READY_FOR_CHECK",
            "reviewed_at": "2026-07-19T04:30:00+00:00",
            "notes": "测试记录仅用于自动化，不代表真实官方规则。",
        }
    )
    for item in document["requirements"]:
        item.update(
            {
                "finding": f"测试结论-{item['requirement_id']}",
                "source_reference": f"测试章节-{item['requirement_id']}",
                "confirmed": True,
            }
        )
    document["checks"] = {key: True for key in document["checks"]}
    return document


def _ready_review(
    tmp_path: Path,
    *,
    use_url: bool = False,
) -> tuple[Path, Path, dict]:
    private = tmp_path / "private"
    review = private / "official_rules_review.json"
    qa = private / "official_rules_review_qa.json"
    initialize_official_rules_review(review, private_root=private)
    if use_url:
        attach_source_url(
            "https://rules.example.org/hackathon/official-detail",
            review,
            private_root=private,
        )
    else:
        source = tmp_path / "official-detail.txt"
        source.write_text("official test source material", encoding="utf-8")
        attach_local_source(source, review, private_root=private)
    _write_private_json(
        _complete_review_document(review),
        review,
        private_root=private,
    )
    report = verify_official_rules_review(review, private_root=private)
    _write_private_json(report, qa, private_root=private)
    return review, qa, report


def test_template_is_exact_private_and_never_overwritten(tmp_path: Path) -> None:
    private = tmp_path / "private"
    review = private / "official_rules_review.json"

    initialize_official_rules_review(review, private_root=private)

    document = json.loads(review.read_text(encoding="utf-8"))
    validate_document(document, "official_rules_review.schema.json")
    assert document["status"] == "PENDING_INPUT"
    assert tuple(item["requirement_id"] for item in document["requirements"]) == REQUIREMENT_IDS
    assert document["source"] is None
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(review.stat().st_mode) == 0o600
    with pytest.raises(OfficialRulesReviewError, match="不会覆盖"):
        initialize_official_rules_review(review, private_root=private)


def test_local_source_review_produces_redacted_hash_bound_qa(tmp_path: Path) -> None:
    review, _, report = _ready_review(tmp_path)

    validate_document(report, "official_rules_review_qa.schema.json")
    assert report["status"] == "READY_FOR_HUMAN_CONFIRMATION"
    assert report["requirement_ids"] == list(REQUIREMENT_IDS)
    assert report["source"]["kind"] == "LOCAL_FILE"
    assert report["source"]["content_sha256"]
    serialized = json.dumps(report, ensure_ascii=False)
    private_document = json.loads(review.read_text(encoding="utf-8"))
    assert private_document["requirements"][0]["finding"] not in serialized
    assert private_document["source"]["relative_path"] not in serialized
    assert str(tmp_path.resolve()) not in serialized
    assert report["data_policy"]["contains_rule_details"] is False
    assert report["data_policy"]["contains_source_locator"] is False


def test_participant_provided_pptx_can_be_bound_as_official_source(
    tmp_path: Path,
) -> None:
    private = tmp_path / "private"
    review = private / "official_rules_review.json"
    initialize_official_rules_review(review, private_root=private)
    source = tmp_path / "opening-deck.pptx"
    source.write_bytes(b"minimal-test-pptx-placeholder")

    attach_local_source(source, review, private_root=private)

    document = json.loads(review.read_text(encoding="utf-8"))
    assert document["source"]["kind"] == "LOCAL_FILE"
    assert document["source"]["relative_path"].endswith(".pptx")
    copied = private / document["source"]["relative_path"]
    assert copied.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(copied.stat().st_mode) == 0o600


def test_safe_url_source_is_hash_bound_without_exposing_url(tmp_path: Path) -> None:
    review, _, report = _ready_review(tmp_path, use_url=True)

    private_document = json.loads(review.read_text(encoding="utf-8"))
    source_url = private_document["source"]["url"]
    assert report["source"]["kind"] == "HTTPS_URL"
    assert report["source"]["content_sha256"] is None
    assert report["source"]["bytes"] is None
    assert source_url not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/rules",
        "https://user:pass@example.org/rules",
        "https://example.org/rules?token=private",
        "https://example.org/rules#section",
    ],
)
def test_unsafe_source_url_is_rejected(tmp_path: Path, url: str) -> None:
    private = tmp_path / "private"
    review = private / "official_rules_review.json"
    initialize_official_rules_review(review, private_root=private)

    with pytest.raises(OfficialRulesReviewError, match="HTTPS"):
        attach_source_url(url, review, private_root=private)


@pytest.mark.parametrize(
    "mutation,match",
    [
        (
            lambda document: document["requirements"].reverse(),
            "requirement_set_exact",
        ),
        (
            lambda document: document["requirements"][0].update({"finding": "   "}),
            "all_findings_nonblank",
        ),
        (
            lambda document: document["requirements"][0].update(
                {"source_reference": "   "}
            ),
            "all_source_references_nonblank",
        ),
        (
            lambda document: document["checks"].update(
                {"submission_plan_updated": False}
            ),
            "严格Schema",
        ),
    ],
)
def test_incomplete_or_reordered_review_is_rejected(
    tmp_path: Path,
    mutation,
    match: str,
) -> None:
    private = tmp_path / "private"
    review = private / "official_rules_review.json"
    initialize_official_rules_review(review, private_root=private)
    source = tmp_path / "official.txt"
    source.write_text("official source", encoding="utf-8")
    attach_local_source(source, review, private_root=private)
    document = _complete_review_document(review)
    mutation(document)
    _write_private_json(document, review, private_root=private)

    with pytest.raises(OfficialRulesReviewError, match=match):
        verify_official_rules_review(review, private_root=private)


def test_source_permission_or_content_drift_invalidates_review(tmp_path: Path) -> None:
    review, _, _ = _ready_review(tmp_path)
    private_document = json.loads(review.read_text(encoding="utf-8"))
    source = review.parent / private_document["source"]["relative_path"]

    source.chmod(0o644)
    with pytest.raises(OfficialRulesReviewError, match="权限"):
        verify_official_rules_review(review, private_root=review.parent)
    source.chmod(0o600)
    source.write_text("changed source material", encoding="utf-8")
    source.chmod(0o600)
    with pytest.raises(OfficialRulesReviewError, match="内容已变化"):
        verify_official_rules_review(review, private_root=review.parent)


def test_qa_issue_requires_fixed_local_current_record(tmp_path: Path) -> None:
    review, qa, report = _ready_review(tmp_path)
    evidence = {
        "kind": "LOCAL_FILE",
        "locator": str(review),
        "sha256": report["review_sha256"],
        "size_bytes": report["review_bytes"],
    }

    assert official_rules_review_qa_issue(qa, evidence) is None
    assert (
        official_rules_review_qa_issue(
            qa,
            {
                "kind": "HTTPS_URL",
                "locator": "https://example.org/rules",
                "sha256": None,
                "size_bytes": None,
            },
        )
        == "OFFICIAL_RULES_REVIEW_REQUIRES_LOCAL_FILE"
    )
    qa.unlink()
    assert (
        official_rules_review_qa_issue(qa, evidence)
        == "OFFICIAL_RULES_REVIEW_QA_MISSING"
    )


def test_submission_preflight_tracks_absent_ready_and_stale_private_review(
    tmp_path: Path,
) -> None:
    absent = build_submission_preflight(
        root=ROOT,
        run_tests=False,
        allow_dirty=True,
        allow_missing_git=True,
        official_rules_review_path=tmp_path / "absent" / "official_rules_review.json",
        official_rules_review_qa_path=tmp_path / "absent" / "official_rules_review_qa.json",
    )
    absent_check = {
        item["check_id"]: item for item in absent["automatic_checks"]
    }["OFFICIAL_RULES_REVIEW_PRIVATE_STATE"]
    assert absent_check["status"] == "PASSED"
    assert "ABSENT" in absent_check["details"][0]

    review, qa, _ = _ready_review(tmp_path)
    ready = build_submission_preflight(
        root=ROOT,
        run_tests=False,
        allow_dirty=True,
        allow_missing_git=True,
        official_rules_review_path=review,
        official_rules_review_qa_path=qa,
    )
    ready_check = {
        item["check_id"]: item for item in ready["automatic_checks"]
    }["OFFICIAL_RULES_REVIEW_PRIVATE_STATE"]
    assert ready_check["status"] == "PASSED"
    assert "六项机器检查通过" in ready_check["details"][0]
    assert str(review) not in json.dumps(ready, ensure_ascii=False)

    qa.unlink()
    stale = build_submission_preflight(
        root=ROOT,
        run_tests=False,
        allow_dirty=True,
        allow_missing_git=True,
        official_rules_review_path=review,
        official_rules_review_qa_path=qa,
    )
    stale_check = {
        item["check_id"]: item for item in stale["automatic_checks"]
    }["OFFICIAL_RULES_REVIEW_PRIVATE_STATE"]
    assert stale_check["status"] == "FAILED"
    assert stale["status"] == "NOT_READY"


def test_qa_schema_rejects_failed_check(tmp_path: Path) -> None:
    _, _, report = _ready_review(tmp_path)
    invalid = deepcopy(report)
    invalid["checks"]["source_current"] = False
    with pytest.raises(ContractValidationError):
        validate_document(invalid, "official_rules_review_qa.schema.json")


def test_official_rules_review_script_is_executable() -> None:
    script = ROOT / "scripts/check_official_rules_review.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
