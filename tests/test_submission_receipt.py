from __future__ import annotations

import json
import stat
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from skillforge.contracts import ContractValidationError, validate_document
from skillforge.publication_links import (
    _write_private_json as _write_publication_json,
    initialize_private_input,
    verify_publication_links,
)
from skillforge.submission import build_submission_preflight
from skillforge.submission_receipt import (
    SubmissionReceiptError,
    _write_private_json,
    attach_receipt_source,
    initialize_submission_receipt_review,
    verify_saved_submission_receipt_qa,
    verify_submission_receipt_review,
)


ROOT = Path(__file__).resolve().parents[1]


def _passing_transport(url: str) -> dict:
    return {
        "http_status": 200,
        "content_type": "video/mp4" if "video.example.org" in url else "text/html",
        "final_url": url,
        "redirect_count": 0,
        "remote_ip": "1.1.1.1",
    }


def _ready_publication_links(private: Path) -> tuple[Path, Path]:
    input_path = private / "publication_links.json"
    qa_path = private / "publication_links_qa.json"
    initialize_private_input(input_path, private_root=private)
    document = json.loads(input_path.read_text(encoding="utf-8"))
    document["status"] = "READY_FOR_CHECK"
    urls = {
        "PROJECT_PAGE": "https://contest.example.org/projects/skillforge",
        "CODE_REPOSITORY": "https://code.example.org/team/skillforge",
        "FINAL_RECORDING": "https://video.example.org/watch?id=public-demo",
    }
    for item in document["targets"]:
        item["public_url"] = urls[item["target_id"]]
    _write_publication_json(document, input_path, private_root=private)
    report = verify_publication_links(
        input_path,
        private_root=private,
        transport=_passing_transport,
    )
    _write_publication_json(report, qa_path, private_root=private)
    return input_path, qa_path


def _ready_final_preflight(private: Path) -> Path:
    path = private / "submission_preflight_final.json"
    report = build_submission_preflight(
        root=ROOT,
        run_tests=False,
        allow_dirty=True,
        allow_missing_git=True,
    )
    report.update(
        {
            "generated_at": "2000-01-01T00:00:00+00:00",
            "status": "READY_FOR_SUBMISSION",
            "source_commit": "a" * 40,
            "source_branch": "main",
            "source_worktree_clean": True,
            "pending_human_gates": [],
        }
    )
    for check in report["automatic_checks"]:
        check["status"] = "PASSED"
    validate_document(report, "submission_preflight.schema.json")
    _write_private_json(report, path, private_root=private)
    return path


def _ready_review(
    tmp_path: Path,
    *,
    suffix: str = ".png",
) -> tuple[Path, Path, Path, Path, dict]:
    private = tmp_path / "private"
    publication_input, publication_qa = _ready_publication_links(private)
    final_preflight = _ready_final_preflight(private)
    review = private / "submission_receipt_review.json"
    qa = private / "submission_receipt_qa.json"
    initialize_submission_receipt_review(review, private_root=private)
    receipt = tmp_path / f"receipt{suffix}"
    receipt.write_bytes(b"synthetic private submission receipt evidence")
    attach_receipt_source(receipt, review, private_root=private)
    document = json.loads(review.read_text(encoding="utf-8"))
    document.update(
        {
            "updated_at": "2001-01-01T00:10:00+00:00",
            "status": "READY_FOR_CHECK",
            "submitted_at": "2001-01-01T00:00:00+00:00",
            "reviewed_at": "2001-01-01T00:05:00+00:00",
            "submission_reference": "PRIVATE-RECEIPT-REFERENCE-123",
            "notes": "仅用于测试的私有回执备注",
        }
    )
    document["checks"] = {key: True for key in document["checks"]}
    _write_private_json(document, review, private_root=private)
    report = verify_submission_receipt_review(
        review,
        final_preflight_path=final_preflight,
        publication_input_path=publication_input,
        publication_qa_path=publication_qa,
        private_root=private,
    )
    return review, qa, final_preflight, publication_input, report


def _verify(
    review: Path,
    *,
    final_preflight: Path | None = None,
    publication_input: Path | None = None,
    publication_qa: Path | None = None,
) -> dict:
    private = review.parent
    return verify_submission_receipt_review(
        review,
        final_preflight_path=final_preflight
        or private / "submission_preflight_final.json",
        publication_input_path=publication_input or private / "publication_links.json",
        publication_qa_path=publication_qa or private / "publication_links_qa.json",
        private_root=private,
    )


def test_template_is_private_empty_and_never_overwritten(tmp_path: Path) -> None:
    private = tmp_path / "private"
    review = private / "submission_receipt_review.json"

    initialize_submission_receipt_review(review, private_root=private)

    document = validate_document(
        json.loads(review.read_text(encoding="utf-8")),
        "submission_receipt_review.schema.json",
    )
    assert document["status"] == "PENDING_INPUT"
    assert document["receipt_source"] is None
    assert document["submission_reference"] == ""
    assert all(value is False for value in document["checks"].values())
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(review.stat().st_mode) == 0o600
    with pytest.raises(SubmissionReceiptError, match="不会覆盖"):
        initialize_submission_receipt_review(review, private_root=private)


@pytest.mark.parametrize(
    ("suffix", "kind"),
    [(".png", "IMAGE"), (".jpg", "IMAGE"), (".jpeg", "IMAGE"), (".pdf", "PDF")],
)
def test_ready_receipt_binds_all_final_basis_without_private_content(
    tmp_path: Path,
    suffix: str,
    kind: str,
) -> None:
    review, _, _, _, report = _ready_review(tmp_path, suffix=suffix)

    validate_document(report, "submission_receipt_qa.schema.json")
    assert report["status"] == "READY_FOR_ARCHIVE"
    assert report["receipt_source"]["kind"] == kind
    assert report["final_preflight"]["automatic_check_count"] >= 18
    assert report["release_manifest"]["artifact_count"] == 18
    assert report["publication_links"]["target_count"] == 3
    assert all(report["checks"].values())
    serialized = json.dumps(report, ensure_ascii=False)
    assert "PRIVATE-RECEIPT-REFERENCE-123" not in serialized
    assert "example.org" not in serialized
    assert "https://" not in serialized
    assert "仅用于测试" not in serialized
    assert str(tmp_path.resolve()) not in serialized
    source = json.loads(review.read_text(encoding="utf-8"))["receipt_source"]
    assert source["relative_path"] not in serialized


def test_receipt_source_type_and_replacement_are_restricted(tmp_path: Path) -> None:
    private = tmp_path / "private"
    review = private / "submission_receipt_review.json"
    initialize_submission_receipt_review(review, private_root=private)
    unsupported = tmp_path / "receipt.txt"
    unsupported.write_text("not allowed", encoding="utf-8")
    with pytest.raises(SubmissionReceiptError, match="只允许"):
        attach_receipt_source(unsupported, review, private_root=private)

    image = tmp_path / "receipt.png"
    image.write_bytes(b"receipt")
    attach_receipt_source(image, review, private_root=private)
    with pytest.raises(SubmissionReceiptError, match="不会自动替换"):
        attach_receipt_source(image, review, private_root=private)


def test_receipt_source_permission_or_content_drift_is_rejected(
    tmp_path: Path,
) -> None:
    review, _, _, _, _ = _ready_review(tmp_path)
    source_info = json.loads(review.read_text(encoding="utf-8"))["receipt_source"]
    source = review.parent / source_info["relative_path"]

    source.chmod(0o644)
    with pytest.raises(SubmissionReceiptError, match="权限"):
        _verify(review)
    source.chmod(0o600)
    source.write_bytes(b"changed receipt evidence")
    source.chmod(0o600)
    with pytest.raises(SubmissionReceiptError, match="内容已变化"):
        _verify(review)


def test_nonfinal_preflight_cannot_back_a_submission_receipt(tmp_path: Path) -> None:
    review, _, final_preflight, _, _ = _ready_review(tmp_path)
    preflight = json.loads(final_preflight.read_text(encoding="utf-8"))
    preflight["status"] = "READY_WITH_HUMAN_GATES"
    preflight["pending_human_gates"] = ["OFFICIAL_RULES_VERIFIED"]
    _write_private_json(preflight, final_preflight, private_root=review.parent)

    with pytest.raises(SubmissionReceiptError, match="final_preflight_ready"):
        _verify(review)

    preflight["status"] = "READY_FOR_SUBMISSION"
    preflight["pending_human_gates"] = []
    preflight["automatic_checks"][0]["check_id"] = "SUBSTITUTED_CHECK"
    _write_private_json(preflight, final_preflight, private_root=review.parent)
    with pytest.raises(SubmissionReceiptError, match="final_preflight_ready"):
        _verify(review)


def test_stale_or_failed_publication_links_cannot_close_receipt(
    tmp_path: Path,
) -> None:
    review, _, _, publication_input, _ = _ready_review(tmp_path)
    document = json.loads(publication_input.read_text(encoding="utf-8"))
    document["targets"][0]["public_url"] = "https://changed.example.org/project"
    _write_publication_json(document, publication_input, private_root=review.parent)
    with pytest.raises(SubmissionReceiptError, match="publication_links_input_current"):
        _verify(review)

    review2, _, _, _, _ = _ready_review(tmp_path / "failed")
    qa_path = review2.parent / "publication_links_qa.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    qa["status"] = "FAILED"
    qa["targets"][0]["status"] = "FAILED"
    qa["targets"][0]["checks"]["anonymous_reachable"] = False
    _write_publication_json(qa, qa_path, private_root=review2.parent)
    with pytest.raises(SubmissionReceiptError, match="publication_links_qa_passed"):
        _verify(review2)


def test_time_order_and_manual_checks_are_strict(tmp_path: Path) -> None:
    review, _, _, _, _ = _ready_review(tmp_path)
    document = json.loads(review.read_text(encoding="utf-8"))
    document["reviewed_at"] = "2000-12-31T23:59:00+00:00"
    _write_private_json(document, review, private_root=review.parent)
    with pytest.raises(SubmissionReceiptError, match="复核时间"):
        _verify(review)

    document["reviewed_at"] = "2001-01-01T00:05:00+00:00"
    document["checks"]["project_page_reopened"] = False
    _write_private_json(document, review, private_root=review.parent)
    with pytest.raises(SubmissionReceiptError, match="严格Schema"):
        _verify(review)


def test_saved_qa_detects_review_or_basis_drift(tmp_path: Path) -> None:
    review, qa_path, final_preflight, publication_input, report = _ready_review(tmp_path)
    _write_private_json(report, qa_path, private_root=review.parent)

    verified = verify_saved_submission_receipt_qa(
        qa_path,
        input_path=review,
        final_preflight_path=final_preflight,
        publication_input_path=publication_input,
        publication_qa_path=review.parent / "publication_links_qa.json",
        private_root=review.parent,
    )
    assert verified["status"] == "READY_FOR_ARCHIVE"

    document = json.loads(review.read_text(encoding="utf-8"))
    document["notes"] = "审核备注变化"
    _write_private_json(document, review, private_root=review.parent)
    with pytest.raises(SubmissionReceiptError, match="不一致"):
        verify_saved_submission_receipt_qa(
            qa_path,
            input_path=review,
            final_preflight_path=final_preflight,
            publication_input_path=publication_input,
            publication_qa_path=review.parent / "publication_links_qa.json",
            private_root=review.parent,
        )


def test_qa_schema_rejects_failed_check(tmp_path: Path) -> None:
    _, _, _, _, report = _ready_review(tmp_path)
    invalid = deepcopy(report)
    invalid["checks"]["receipt_source_current"] = False
    with pytest.raises(ContractValidationError):
        validate_document(invalid, "submission_receipt_qa.schema.json")


def test_missing_default_receipt_fails_safely_and_script_is_executable() -> None:
    script = ROOT / "scripts/check_submission_receipt.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
    completed = subprocess.run(
        ["bash", str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "/Users/" not in completed.stdout
    assert "READY_FOR_ARCHIVE" not in completed.stdout
