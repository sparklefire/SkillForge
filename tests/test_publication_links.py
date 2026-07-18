from __future__ import annotations

import json
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from skillforge.contracts import ContractValidationError, validate_document
from skillforge.publication_links import (
    EXPECTED_TARGETS,
    PublicationLinksError,
    _write_private_json,
    initialize_private_input,
    verify_publication_links,
    verify_publication_links_document,
)


ROOT = Path(__file__).resolve().parents[1]


def _private_input(tmp_path: Path) -> tuple[Path, Path, dict]:
    private = tmp_path / "private"
    input_path = private / "publication_links.json"
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
    _write_private_json(document, input_path, private_root=private)
    return private, input_path, document


def _passing_transport(url: str) -> dict:
    return {
        "http_status": 200,
        "content_type": "video/mp4" if "video.example.org" in url else "text/html; charset=utf-8",
        "final_url": url,
        "redirect_count": 0,
        "remote_ip": "1.1.1.1",
    }


def test_private_link_template_has_safe_permissions_and_no_urls(tmp_path: Path) -> None:
    private = tmp_path / "private"
    path = private / "publication_links.json"
    initialize_private_input(path, private_root=private)
    document = validate_document(
        json.loads(path.read_text(encoding="utf-8")),
        "publication_links_input.schema.json",
    )

    assert document["status"] == "PENDING_INPUT"
    assert all(item["public_url"] is None for item in document["targets"])
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    with pytest.raises(PublicationLinksError, match="不会覆盖"):
        initialize_private_input(path, private_root=private)


def test_anonymous_link_check_passes_without_copying_urls_to_report(
    tmp_path: Path,
) -> None:
    private, input_path, _ = _private_input(tmp_path)
    report = verify_publication_links(
        input_path,
        private_root=private,
        transport=_passing_transport,
    )
    validate_document(report, "publication_links_qa.schema.json")

    assert report["status"] == "PASSED"
    assert report["target_count"] == 3
    assert all(item["status"] == "PASSED" for item in report["targets"])
    serialized = json.dumps(report, ensure_ascii=False)
    assert "example.org" not in serialized
    assert "https://" not in serialized
    assert report["data_policy"]["authorization_headers_sent"] is False
    assert report["data_policy"]["cookies_sent"] is False


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://example.org/project?token=secret",
        "https://example.org/project?X-Amz-Signature=secret",
        "https://example.org/project#access_token=secret",
        "https://127.0.0.1/project",
        "https://service.internal/project",
        "https://intranet/project",
    ],
)
def test_signed_private_or_credential_like_url_is_rejected(
    tmp_path: Path,
    unsafe_url: str,
) -> None:
    _, _, document = _private_input(tmp_path)
    document["targets"][0]["public_url"] = unsafe_url

    with pytest.raises(PublicationLinksError):
        verify_publication_links_document(
            document,
            input_sha256="1" * 64,
            transport=_passing_transport,
        )


def test_wrong_content_type_or_http_status_fails_target(tmp_path: Path) -> None:
    _, _, document = _private_input(tmp_path)

    def failing_transport(url: str) -> dict:
        if "code.example.org" in url:
            return {
                "http_status": 403,
                "content_type": "application/json",
                "final_url": url,
                "redirect_count": 0,
                "remote_ip": "1.1.1.1",
            }
        return _passing_transport(url)

    report = verify_publication_links_document(
        document,
        input_sha256="2" * 64,
        transport=failing_transport,
    )
    by_id = {item["target_id"]: item for item in report["targets"]}

    assert report["status"] == "FAILED"
    assert by_id["CODE_REPOSITORY"]["status"] == "FAILED"
    assert by_id["CODE_REPOSITORY"]["checks"]["anonymous_reachable"] is False
    assert by_id["CODE_REPOSITORY"]["checks"]["content_type_matches"] is False


def test_private_remote_ip_fails_without_copying_address_to_report(tmp_path: Path) -> None:
    _, _, document = _private_input(tmp_path)

    def private_transport(url: str) -> dict:
        response = _passing_transport(url)
        response["remote_ip"] = "127.0.0.1"
        return response

    report = verify_publication_links_document(
        document,
        input_sha256="5" * 64,
        transport=private_transport,
    )

    assert report["status"] == "FAILED"
    assert all(item["checks"]["remote_ip_public"] is False for item in report["targets"])
    assert "127.0.0.1" not in json.dumps(report)


def test_pending_or_duplicate_target_input_is_rejected(tmp_path: Path) -> None:
    private = tmp_path / "private"
    path = private / "publication_links.json"
    initialize_private_input(path, private_root=private)
    with pytest.raises(PublicationLinksError, match="尚未填写"):
        verify_publication_links(path, private_root=private, transport=_passing_transport)

    _, _, document = _private_input(tmp_path / "duplicate")
    document["targets"][-1] = deepcopy(document["targets"][0])
    with pytest.raises(PublicationLinksError, match="完整、唯一"):
        verify_publication_links_document(
            document,
            input_sha256="3" * 64,
            transport=_passing_transport,
        )


def test_schema_rejects_passed_report_with_failed_check(tmp_path: Path) -> None:
    _, _, document = _private_input(tmp_path)
    report = verify_publication_links_document(
        document,
        input_sha256="4" * 64,
        transport=_passing_transport,
    )
    invalid = deepcopy(report)
    invalid["targets"][0]["checks"]["anonymous_reachable"] = False

    with pytest.raises(ContractValidationError):
        validate_document(invalid, "publication_links_qa.schema.json")


def test_publication_link_script_is_executable() -> None:
    script = ROOT / "scripts/check_publication_links.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
    assert EXPECTED_TARGETS == {
        "PROJECT_PAGE": "HTML",
        "CODE_REPOSITORY": "HTML",
        "FINAL_RECORDING": "HTML_OR_VIDEO",
    }
