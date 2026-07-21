from __future__ import annotations

import json
import os
import stat
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image

from skillforge.contracts import validate_document
from skillforge.submission_form_packet import (
    SubmissionFormPacketError,
    _write_private_json,
    attach_team_photo,
    build_submission_form_packet,
    initialize_submission_form_packet,
    verify_saved_submission_form_packet_qa,
)
from skillforge.team_roster import (
    EXPECTED_ROLES,
    _write_private_json as write_team_json,
    initialize_team_roster,
    verify_team_roster,
)


ROOT = Path(__file__).resolve().parents[1]


def _ready_roster() -> dict:
    return {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": "2026-07-19T00:00:00+00:00",
        "status": "READY_FOR_CHECK",
        "members": [
            {
                "member_id": "M1",
                "name": "测试队长",
                "organization": "测试单位甲",
                "primary_contact": True,
                "registration_confirmed": True,
                "one_team_only_confirmed": True,
            },
            {
                "member_id": "M2",
                "name": "测试成员",
                "organization": "测试单位乙",
                "primary_contact": False,
                "registration_confirmed": True,
                "one_team_only_confirmed": True,
            },
        ],
        "role_assignments": [
            {"role_id": role, "member_id": "M1" if index % 2 == 0 else "M2"}
            for index, role in enumerate(sorted(EXPECTED_ROLES))
        ],
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": True,
            "git_tracked": False,
        },
    }


def _write_roster_and_qa(private: Path) -> tuple[Path, Path]:
    roster_path = private / "team_roster.json"
    qa_path = private / "team_roster_qa.json"
    initialize_team_roster(roster_path, private_root=private)
    write_team_json(_ready_roster(), roster_path, private_root=private)
    write_team_json(
        verify_team_roster(roster_path, private_root=private),
        qa_path,
        private_root=private,
    )
    return roster_path, qa_path


def _image(path: Path, image_format: str = "PNG") -> Path:
    Image.new("RGB", (640, 360), (38, 112, 79)).save(path, format=image_format)
    return path


def _transport(url: str) -> dict:
    return {
        "http_status": 200,
        "content_type": "video/mp4" if "/demo" in url else "text/html; charset=utf-8",
        "final_url": url,
        "redirect_count": 0,
        "remote_ip": "93.184.216.34",
    }


def _ready_packet(private: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    input_path = private / "submission_form_packet.json"
    initialize_submission_form_packet(input_path, private_root=private)
    attach_team_photo(
        _image(tmp_path / "team.png"),
        input_path,
        private_root=private,
    )
    document = json.loads(input_path.read_text(encoding="utf-8"))
    document["status"] = "READY_FOR_CHECK"
    document["fields"].update(
        {
            "team_name": "SkillForge 测试队",
            "team_address": "测试地址",
            "project_report_url": "https://example.com/report",
            "demo_video_url": "https://example.com/demo",
            "article_url": "https://example.com/article",
        }
    )
    _write_private_json(document, input_path, private_root=private)
    roster_path, roster_qa_path = _write_roster_and_qa(private)
    return input_path, roster_path, roster_qa_path


def test_template_is_private_manual_only_and_not_overwritten(tmp_path: Path) -> None:
    private = tmp_path / "submission"
    path = private / "submission_form_packet.json"
    initialize_submission_form_packet(path, private_root=private)
    document = validate_document(
        json.loads(path.read_text(encoding="utf-8")),
        "submission_form_packet.schema.json",
    )

    assert document["status"] == "PENDING_INPUT"
    assert document["fields"]["application_domain"] == "制造业"
    assert document["fields"]["project_report_url"].endswith("/SkillForge")
    assert document["data_policy"]["browser_form_filled"] is False
    assert document["data_policy"]["browser_form_submitted"] is False
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(SubmissionFormPacketError, match="不会覆盖"):
        initialize_submission_form_packet(path, private_root=private)


def test_photo_is_decoded_copied_privately_and_requires_explicit_replace(
    tmp_path: Path,
) -> None:
    private = tmp_path / "submission"
    path = private / "submission_form_packet.json"
    initialize_submission_form_packet(path, private_root=private)
    attach_team_photo(_image(tmp_path / "one.png"), path, private_root=private)
    document = json.loads(path.read_text(encoding="utf-8"))
    photo_path = private / document["team_photo"]["relative_path"]

    assert photo_path.is_file()
    assert document["team_photo"]["mime_type"] == "image/png"
    assert stat.S_IMODE(photo_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(photo_path.stat().st_mode) == 0o600
    with pytest.raises(SubmissionFormPacketError, match="replace-photo"):
        attach_team_photo(_image(tmp_path / "two.png"), path, private_root=private)


def test_ready_packet_builds_private_prefill_and_redacted_qa(tmp_path: Path) -> None:
    private = tmp_path / "submission"
    input_path, roster_path, roster_qa_path = _ready_packet(private, tmp_path)
    prefill_path = private / "submission_form_prefill.json"
    report_path = private / "submission_form_packet_qa.json"
    prefill, report = build_submission_form_packet(
        input_path,
        prefill_path=prefill_path,
        report_path=report_path,
        roster_path=roster_path,
        roster_qa_path=roster_qa_path,
        private_root=private,
        transport=_transport,
    )

    validate_document(prefill, "submission_form_prefill.schema.json")
    validate_document(report, "submission_form_packet_qa.schema.json")
    assert prefill["required_fields"]["team_members"] == "队长：测试队长；成员：测试成员"
    assert prefill["submission_mode"] == "MANUAL_COPY_ONLY"
    assert report["status"] == "READY_FOR_HUMAN_SUBMISSION"
    assert report["required_field_count"] == 8
    assert len(report["url_checks"]) == 3
    assert report["data_policy"]["network_requests"] == 3
    assert report["data_policy"]["browser_form_submitted"] is False
    assert stat.S_IMODE(prefill_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    serialized = json.dumps(report, ensure_ascii=False)
    for private_value in (
        "测试队长",
        "测试成员",
        "测试单位甲",
        "测试单位乙",
        "SkillForge 测试队",
        "测试地址",
        "example.com",
        "https://",
        "submission_form_assets",
    ):
        assert private_value not in serialized

    saved = verify_saved_submission_form_packet_qa(
        report_path,
        input_path=input_path,
        prefill_path=prefill_path,
        roster_path=roster_path,
        roster_qa_path=roster_qa_path,
        private_root=private,
    )
    assert saved == report


def test_duplicate_or_unsafe_urls_are_rejected_before_network(tmp_path: Path) -> None:
    private = tmp_path / "submission"
    input_path, roster_path, roster_qa_path = _ready_packet(private, tmp_path)
    document = json.loads(input_path.read_text(encoding="utf-8"))
    document["fields"]["article_url"] = document["fields"]["project_report_url"]
    _write_private_json(document, input_path, private_root=private)
    calls = []

    with pytest.raises(SubmissionFormPacketError, match="互不相同"):
        build_submission_form_packet(
            input_path,
            prefill_path=private / "submission_form_prefill.json",
            report_path=private / "submission_form_packet_qa.json",
            roster_path=roster_path,
            roster_qa_path=roster_qa_path,
            private_root=private,
            transport=lambda url: calls.append(url),
        )
    assert calls == []

    document["fields"]["article_url"] = "https://localhost/article"
    _write_private_json(document, input_path, private_root=private)
    with pytest.raises(SubmissionFormPacketError, match="安全公开HTTPS"):
        build_submission_form_packet(
            input_path,
            prefill_path=private / "submission_form_prefill.json",
            report_path=private / "submission_form_packet_qa.json",
            roster_path=roster_path,
            roster_qa_path=roster_qa_path,
            private_root=private,
            transport=_transport,
        )


def test_failed_anonymous_url_check_is_not_reported_as_ready(tmp_path: Path) -> None:
    private = tmp_path / "submission"
    input_path, roster_path, roster_qa_path = _ready_packet(private, tmp_path)

    def failed_transport(url: str) -> dict:
        response = _transport(url)
        if "/article" in url:
            response["http_status"] = 403
        return response

    with pytest.raises(SubmissionFormPacketError, match="ARTICLE_URL"):
        build_submission_form_packet(
            input_path,
            prefill_path=private / "submission_form_prefill.json",
            report_path=private / "submission_form_packet_qa.json",
            roster_path=roster_path,
            roster_qa_path=roster_qa_path,
            private_root=private,
            transport=failed_transport,
        )
    assert not (private / "submission_form_packet_qa.json").exists()


def test_stale_roster_or_saved_packet_drift_is_rejected(tmp_path: Path) -> None:
    private = tmp_path / "submission"
    input_path, roster_path, roster_qa_path = _ready_packet(private, tmp_path)
    changed = deepcopy(json.loads(roster_path.read_text(encoding="utf-8")))
    changed["updated_at"] = "2026-07-19T01:00:00+00:00"
    write_team_json(changed, roster_path, private_root=private)
    with pytest.raises(SubmissionFormPacketError, match="漂移"):
        build_submission_form_packet(
            input_path,
            prefill_path=private / "submission_form_prefill.json",
            report_path=private / "submission_form_packet_qa.json",
            roster_path=roster_path,
            roster_qa_path=roster_qa_path,
            private_root=private,
            transport=_transport,
        )

    write_team_json(
        verify_team_roster(roster_path, private_root=private),
        roster_qa_path,
        private_root=private,
    )
    prefill_path = private / "submission_form_prefill.json"
    report_path = private / "submission_form_packet_qa.json"
    build_submission_form_packet(
        input_path,
        prefill_path=prefill_path,
        report_path=report_path,
        roster_path=roster_path,
        roster_qa_path=roster_qa_path,
        private_root=private,
        transport=_transport,
    )
    packet = json.loads(input_path.read_text(encoding="utf-8"))
    packet["updated_at"] = "2026-07-19T02:00:00+00:00"
    _write_private_json(packet, input_path, private_root=private)
    with pytest.raises(SubmissionFormPacketError, match="依赖已漂移"):
        verify_saved_submission_form_packet_qa(
            report_path,
            input_path=input_path,
            prefill_path=prefill_path,
            roster_path=roster_path,
            roster_qa_path=roster_qa_path,
            private_root=private,
        )


def test_invalid_or_oversized_photo_is_rejected(tmp_path: Path) -> None:
    private = tmp_path / "submission"
    path = private / "submission_form_packet.json"
    initialize_submission_form_packet(path, private_root=private)
    invalid = tmp_path / "invalid.png"
    invalid.write_text("not an image", encoding="utf-8")
    with pytest.raises(SubmissionFormPacketError, match="解码"):
        attach_team_photo(invalid, path, private_root=private)

    oversized = tmp_path / "oversized.png"
    with oversized.open("wb") as handle:
        handle.seek(20_000_000)
        handle.write(b"x")
    with pytest.raises(SubmissionFormPacketError, match="20MB"):
        attach_team_photo(oversized, path, private_root=private)


def test_submission_form_packet_script_is_executable() -> None:
    script = ROOT / "scripts/check_submission_form_packet.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111


def test_incomplete_input_guidance_lists_empty_fields(tmp_path: Path) -> None:
    from skillforge.submission_form_packet import _incomplete_input_guidance

    input_path = tmp_path / "packet.json"
    input_path.write_text(
        json.dumps(
            {
                "status": "PENDING_INPUT",
                "fields": {
                    "team_name": "测试队名",
                    "article_url": "",
                    "demo_video_url": "PENDING_INPUT",
                },
                "team_photo": None,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    joined = "\n".join(_incomplete_input_guidance(input_path))
    assert "仍为空的字段：article_url、demo_video_url" in joined
    assert "status=PENDING_INPUT" in joined
    assert "--attach-photo" in joined
    assert "READY_FOR_CHECK" in joined
    # privacy: filled-in values never appear in the guidance
    assert "测试队名" not in joined
