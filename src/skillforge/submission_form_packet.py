"""Prepare and validate a private, manual-only official submission form packet."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError

from .contracts import ContractValidationError, validate_document
from .demo import ROOT
from .publication_links import (
    _content_type_matches,
    _curl_head,
    _safe_public_url,
)


DEFAULT_PRIVATE_ROOT = ROOT / "outputs" / "submission"
DEFAULT_INPUT = DEFAULT_PRIVATE_ROOT / "submission_form_packet.json"
DEFAULT_PREFILL = DEFAULT_PRIVATE_ROOT / "submission_form_prefill.json"
DEFAULT_REPORT = DEFAULT_PRIVATE_ROOT / "submission_form_packet_qa.json"
DEFAULT_TEAM_ROSTER = DEFAULT_PRIVATE_ROOT / "team_roster.json"
DEFAULT_TEAM_ROSTER_QA = DEFAULT_PRIVATE_ROOT / "team_roster_qa.json"
DEFAULT_FORM_SNAPSHOT = ROOT / "config" / "official_submission_form_status.json"
FORM_ASSET_DIR = "submission_form_assets"
PHOTO_LIMIT_BYTES = 20_000_000
PHOTO_FORMATS = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}
URL_FIELDS = {
    "PROJECT_REPORT_URL": ("project_report_url", "HTML"),
    "DEMO_VIDEO_URL": ("demo_video_url", "HTML_OR_VIDEO"),
    "ARTICLE_URL": ("article_url", "HTML"),
}
EXPECTED_REQUIRED_FIELDS = set(URL_FIELDS) | {
    "TEAM_NAME",
    "TEAM_MEMBERS",
    "PROJECT_NAME",
    "APPLICATION_DOMAIN",
    "TEAM_PHOTO",
}
EXPECTED_OPTIONAL_FIELDS = {"TEAM_ADDRESS"}


class SubmissionFormPacketError(ValueError):
    """Raised when form inputs cannot produce a safe manual submission packet."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _inside(path: Path, private_root: Path = DEFAULT_PRIVATE_ROOT) -> Path:
    resolved = path.expanduser().resolve()
    private_root = private_root.expanduser().resolve()
    try:
        resolved.relative_to(private_root)
    except ValueError as exc:
        raise SubmissionFormPacketError("提交表单私有文件必须保存在私有提交目录") from exc
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubmissionFormPacketError(f"{label}无法读取或不是合法JSON") from exc
    if not isinstance(document, dict):
        raise SubmissionFormPacketError(f"{label}必须是JSON对象")
    return document


def _private_file_safe(path: Path, private_root: Path) -> bool:
    try:
        _inside(path, private_root)
    except SubmissionFormPacketError:
        return False
    return (
        path.is_file()
        and stat.S_IMODE(path.stat().st_mode) == 0o600
        and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    )


def _write_private_json(
    document: dict[str, Any],
    destination: Path,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    destination = _inside(destination, private_root)
    parent_existed = destination.parent.exists()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed:
        os.chmod(destination.parent, 0o700)
    elif stat.S_IMODE(destination.parent.stat().st_mode) != 0o700:
        raise SubmissionFormPacketError("提交表单私有目录权限必须为0700")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _load_form_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    path = path.expanduser().resolve()
    try:
        document = validate_document(
            _read_json(path, "官方提交表单快照"),
            "official_submission_form_status.schema.json",
        )
    except ContractValidationError as exc:
        raise SubmissionFormPacketError("官方提交表单快照无效") from exc
    return document, _sha256(path)


def initialize_submission_form_packet(
    destination: Path = DEFAULT_INPUT,
    *,
    form_snapshot_path: Path = DEFAULT_FORM_SNAPSHOT,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    destination = _inside(destination, private_root)
    if destination.exists():
        raise SubmissionFormPacketError("提交表单私有输入已存在；初始化不会覆盖")
    _, snapshot_sha256 = _load_form_snapshot(form_snapshot_path)
    document = {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": _now(),
        "status": "PENDING_INPUT",
        "form_snapshot_sha256": snapshot_sha256,
        "fields": {
            "team_name": "",
            "team_address": None,
            "project_name": "SkillForge（匠传）",
            "application_domain": "制造业",
            "project_report_url": "https://github.com/sparklefire/SkillForge",
            "demo_video_url": None,
            "article_url": None,
        },
        "team_photo": None,
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": True,
            "contains_public_urls": True,
            "contains_team_photo_locator": True,
            "git_tracked": False,
            "browser_form_filled": False,
            "browser_form_submitted": False,
        },
    }
    return _write_private_json(
        validate_document(document, "submission_form_packet.schema.json"),
        destination,
        private_root=private_root,
    )


def _inspect_photo(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size < 1 or size > PHOTO_LIMIT_BYTES:
        raise SubmissionFormPacketError("团队照片必须大于0且不超过20MB")
    try:
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            image_format = str(image.format or "").upper()
            width, height = image.size
    except (OSError, UnidentifiedImageError) as exc:
        raise SubmissionFormPacketError("团队照片无法解码") from exc
    if image_format not in PHOTO_FORMATS or width < 1 or height < 1:
        raise SubmissionFormPacketError("团队照片必须是可解码的JPEG、PNG或WebP")
    extension, mime_type = PHOTO_FORMATS[image_format]
    return {
        "extension": extension,
        "sha256": _sha256(path),
        "bytes": size,
        "mime_type": mime_type,
        "width": width,
        "height": height,
    }


def attach_team_photo(
    source_path: Path,
    input_path: Path = DEFAULT_INPUT,
    *,
    replace: bool = False,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    input_path = _inside(input_path, private_root)
    if not _private_file_safe(input_path, private_root):
        raise SubmissionFormPacketError("提交表单私有输入缺失或权限不安全")
    document = validate_document(
        _read_json(input_path, "提交表单私有输入"),
        "submission_form_packet.schema.json",
    )
    if document["team_photo"] is not None and not replace:
        raise SubmissionFormPacketError("团队照片已绑定；替换时必须显式使用--replace-photo")
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file():
        raise SubmissionFormPacketError("团队照片源文件不存在")
    details = _inspect_photo(source_path)

    asset_dir = _inside(private_root / FORM_ASSET_DIR, private_root)
    if asset_dir.exists() and stat.S_IMODE(asset_dir.stat().st_mode) != 0o700:
        raise SubmissionFormPacketError("团队照片私有目录权限必须为0700")
    asset_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(asset_dir, 0o700)
    destination = asset_dir / f"team_photo.{details['extension']}"
    old_relative = (
        document["team_photo"]["relative_path"]
        if document["team_photo"] is not None
        else None
    )
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".team_photo.", dir=asset_dir
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as target, source_path.open("rb") as source:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        copied = _inspect_photo(temporary)
        if copied != details:
            raise SubmissionFormPacketError("团队照片复制后校验不一致")
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        document["team_photo"] = {
            "relative_path": destination.relative_to(private_root.resolve()).as_posix(),
            **{key: details[key] for key in ("sha256", "bytes", "mime_type", "width", "height")},
        }
        document["updated_at"] = _now()
        _write_private_json(
            validate_document(document, "submission_form_packet.schema.json"),
            input_path,
            private_root=private_root,
        )
        if old_relative and old_relative != document["team_photo"]["relative_path"]:
            old_path = _inside(private_root / old_relative, private_root)
            if old_path.parent == asset_dir:
                old_path.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)
    return input_path


def _load_team_sources(
    roster_path: Path,
    roster_qa_path: Path,
    *,
    private_root: Path,
) -> tuple[dict[str, Any], str, str]:
    roster_path = _inside(roster_path, private_root)
    roster_qa_path = _inside(roster_qa_path, private_root)
    if not _private_file_safe(roster_path, private_root) or not _private_file_safe(
        roster_qa_path, private_root
    ):
        raise SubmissionFormPacketError("团队名单或名单QA缺失/权限不安全")
    try:
        roster = validate_document(
            _read_json(roster_path, "团队名单"), "team_roster.schema.json"
        )
        roster_qa = validate_document(
            _read_json(roster_qa_path, "团队名单QA"), "team_roster_qa.schema.json"
        )
    except ContractValidationError as exc:
        raise SubmissionFormPacketError("团队名单或名单QA不符合严格Schema") from exc
    roster_sha256 = _sha256(roster_path)
    roster_qa_sha256 = _sha256(roster_qa_path)
    if (
        roster["status"] != "READY_FOR_CHECK"
        or roster_qa["status"] != "READY_FOR_HUMAN_CONFIRMATION"
        or roster_qa["roster_sha256"] != roster_sha256
        or roster_qa["team_size"] != len(roster["members"])
    ):
        raise SubmissionFormPacketError("团队名单QA未通过或已漂移")
    return roster, roster_sha256, roster_qa_sha256


def _current_photo(
    photo: dict[str, Any] | None,
    *,
    private_root: Path,
) -> tuple[Path, dict[str, Any]]:
    if photo is None:
        raise SubmissionFormPacketError("团队照片尚未绑定")
    path = _inside(private_root / photo["relative_path"], private_root)
    expected_parent = private_root.resolve() / FORM_ASSET_DIR
    if path.parent != expected_parent or not _private_file_safe(path, private_root):
        raise SubmissionFormPacketError("团队照片位置或权限不安全")
    details = _inspect_photo(path)
    for key in ("sha256", "bytes", "mime_type", "width", "height"):
        if details[key] != photo[key]:
            raise SubmissionFormPacketError("团队照片内容或元数据已漂移")
    return path, details


def _url_checks(
    fields: dict[str, Any],
    *,
    transport: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    safe_values: dict[str, str] = {}
    for field_id, (key, _) in URL_FIELDS.items():
        value = fields.get(key)
        if not isinstance(value, str):
            raise SubmissionFormPacketError(f"{field_id}尚未填写")
        try:
            safe_values[field_id] = _safe_public_url(value)
        except ValueError as exc:
            raise SubmissionFormPacketError(f"{field_id}不是安全公开HTTPS网址") from exc
    if len(set(safe_values.values())) != len(safe_values):
        raise SubmissionFormPacketError("三个提交网址必须互不相同")

    results = []
    failed: list[str] = []
    for field_id, safe_url in safe_values.items():
        _, expected_surface = URL_FIELDS[field_id]
        response = transport(safe_url)
        final_raw = response.get("final_url")
        final_url = None
        final_url_safe = False
        if final_raw:
            try:
                final_url = _safe_public_url(str(final_raw))
                final_url_safe = True
            except ValueError:
                final_url_safe = False
        http_status = int(response.get("http_status") or 0)
        content_type = str(response.get("content_type") or "")[:200]
        remote_ip_public = False
        try:
            remote_ip_public = ipaddress.ip_address(
                str(response.get("remote_ip") or "")
            ).is_global
        except ValueError:
            remote_ip_public = False
        checks = {
            "input_url_safe": True,
            "anonymous_reachable": 200 <= http_status <= 299,
            "content_type_matches": _content_type_matches(
                expected_surface, content_type
            ),
            "final_url_safe": final_url_safe,
            "remote_ip_public": remote_ip_public,
        }
        status = "PASSED" if all(checks.values()) else "FAILED"
        if status == "FAILED":
            failed.append(field_id)
        results.append(
            {
                "field_id": field_id,
                "url_sha256": _sha256_text(safe_url),
                "final_url_sha256": _sha256_text(final_url) if final_url else None,
                "http_status": http_status,
                "content_type": content_type,
                "redirect_count": max(0, int(response.get("redirect_count") or 0)),
                "status": status,
                "checks": checks,
            }
        )
    if failed:
        raise SubmissionFormPacketError(f"提交网址匿名检查未通过: {failed}")
    return results


def build_submission_form_packet(
    input_path: Path = DEFAULT_INPUT,
    *,
    prefill_path: Path = DEFAULT_PREFILL,
    report_path: Path = DEFAULT_REPORT,
    form_snapshot_path: Path = DEFAULT_FORM_SNAPSHOT,
    roster_path: Path = DEFAULT_TEAM_ROSTER,
    roster_qa_path: Path = DEFAULT_TEAM_ROSTER_QA,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
    transport: Callable[[str], dict[str, Any]] = _curl_head,
) -> tuple[dict[str, Any], dict[str, Any]]:
    input_path = _inside(input_path, private_root)
    prefill_path = _inside(prefill_path, private_root)
    report_path = _inside(report_path, private_root)
    if not _private_file_safe(input_path, private_root):
        raise SubmissionFormPacketError("提交表单私有输入缺失或权限不安全")
    try:
        packet = validate_document(
            _read_json(input_path, "提交表单私有输入"),
            "submission_form_packet.schema.json",
        )
    except ContractValidationError as exc:
        raise SubmissionFormPacketError("提交表单私有输入不符合严格Schema") from exc
    if packet["status"] != "READY_FOR_CHECK":
        raise SubmissionFormPacketError("提交表单私有输入尚未填写完成")
    snapshot, snapshot_sha256 = _load_form_snapshot(form_snapshot_path)
    if packet["form_snapshot_sha256"] != snapshot_sha256:
        raise SubmissionFormPacketError("官方提交表单快照已变化；私有输入需重新核对")
    if (
        set(snapshot["required_fields"]) != EXPECTED_REQUIRED_FIELDS
        or set(snapshot["optional_fields"]) != EXPECTED_OPTIONAL_FIELDS
    ):
        raise SubmissionFormPacketError("官方提交表单字段集合与冻结定义不一致")
    fields = packet["fields"]
    if fields["application_domain"] not in snapshot["application_domains"]:
        raise SubmissionFormPacketError("项目应用领域不在官方表单选项内")
    if not fields["team_name"].strip() or fields["team_name"] != fields["team_name"].strip():
        raise SubmissionFormPacketError("团队名称不能为空或包含首尾空格")

    roster, roster_sha256, roster_qa_sha256 = _load_team_sources(
        roster_path,
        roster_qa_path,
        private_root=private_root,
    )
    photo_path, photo = _current_photo(packet["team_photo"], private_root=private_root)
    url_checks = _url_checks(fields, transport=transport)
    members = sorted(
        roster["members"],
        key=lambda item: (not item["primary_contact"], item["member_id"]),
    )
    leader = next(item for item in members if item["primary_contact"])
    teammates = [item for item in members if not item["primary_contact"]]
    member_text = (
        f"队长：{leader['name']}；成员："
        + "、".join(item["name"] for item in teammates)
    )
    packet_sha256 = _sha256(input_path)
    prefill = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "SUBMISSION_FORM_PREFILL",
        "generated_at": _now(),
        "status": "READY_FOR_HUMAN_SUBMISSION",
        "required_fields": {
            "team_name": fields["team_name"],
            "team_members": member_text,
            "project_name": fields["project_name"],
            "application_domain": fields["application_domain"],
            "project_report_url": fields["project_report_url"],
            "demo_video_url": fields["demo_video_url"],
            "article_url": fields["article_url"],
            "team_photo_relative_path": photo_path.relative_to(
                private_root.resolve()
            ).as_posix(),
        },
        "optional_fields": {"team_address": fields["team_address"]},
        "team_member_count": len(members),
        "source_hashes": {
            "packet": packet_sha256,
            "form_snapshot": snapshot_sha256,
            "team_roster": roster_sha256,
            "team_roster_qa": roster_qa_sha256,
        },
        "submission_mode": "MANUAL_COPY_ONLY",
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": True,
            "contains_public_urls": True,
            "contains_team_photo_locator": True,
            "git_tracked": False,
            "browser_form_filled": False,
            "browser_form_submitted": False,
        },
    }
    validate_document(prefill, "submission_form_prefill.schema.json")
    _write_private_json(prefill, prefill_path, private_root=private_root)
    prefill_sha256 = _sha256(prefill_path)
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "SUBMISSION_FORM_PACKET_QA",
        "checked_at": _now(),
        "status": "READY_FOR_HUMAN_SUBMISSION",
        "packet_sha256": packet_sha256,
        "prefill_sha256": prefill_sha256,
        "form_snapshot_sha256": snapshot_sha256,
        "team_roster_sha256": roster_sha256,
        "team_roster_qa_sha256": roster_qa_sha256,
        "required_field_count": len(snapshot["required_fields"]),
        "optional_field_count": len(snapshot["optional_fields"]),
        "team_member_count": len(members),
        "application_domain_sha256": _sha256_text(fields["application_domain"]),
        "url_checks": url_checks,
        "team_photo": {
            key: photo[key]
            for key in ("sha256", "bytes", "mime_type", "width", "height")
        },
        "checks": {
            "form_snapshot_current": True,
            "required_fields_complete": True,
            "application_domain_allowed": True,
            "team_roster_schema_valid": True,
            "team_roster_qa_current": True,
            "team_size_valid": 2 <= len(members) <= 5,
            "team_photo_current": True,
            "team_photo_under_20mb": photo["bytes"] <= PHOTO_LIMIT_BYTES,
            "team_photo_decodable": True,
            "three_urls_unique": True,
            "three_urls_safe": True,
            "three_urls_anonymous_reachable": True,
            "three_url_content_types_match": True,
            "browser_form_not_filled": True,
            "browser_form_not_submitted": True,
        },
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": False,
            "contains_member_names": False,
            "contains_organizations": False,
            "contains_urls": False,
            "contains_team_photo_locator": False,
            "contains_team_photo_bytes": False,
            "contains_form_url": False,
            "contains_credentials": False,
            "browser_form_filled": False,
            "browser_form_submitted": False,
            "network_requests": 3,
        },
    }
    validate_document(report, "submission_form_packet_qa.schema.json")
    _write_private_json(report, report_path, private_root=private_root)
    return prefill, report


def verify_saved_submission_form_packet_qa(
    report_path: Path = DEFAULT_REPORT,
    *,
    input_path: Path = DEFAULT_INPUT,
    prefill_path: Path = DEFAULT_PREFILL,
    form_snapshot_path: Path = DEFAULT_FORM_SNAPSHOT,
    roster_path: Path = DEFAULT_TEAM_ROSTER,
    roster_qa_path: Path = DEFAULT_TEAM_ROSTER_QA,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> dict[str, Any]:
    paths = [report_path, input_path, prefill_path, roster_path, roster_qa_path]
    if any(not _private_file_safe(_inside(path, private_root), private_root) for path in paths):
        raise SubmissionFormPacketError("提交表单包或其依赖缺失/权限不安全")
    try:
        report = validate_document(
            _read_json(report_path, "提交表单包QA"),
            "submission_form_packet_qa.schema.json",
        )
        packet = validate_document(
            _read_json(input_path, "提交表单输入"),
            "submission_form_packet.schema.json",
        )
        prefill = validate_document(
            _read_json(prefill_path, "提交表单预填包"),
            "submission_form_prefill.schema.json",
        )
    except ContractValidationError as exc:
        raise SubmissionFormPacketError("提交表单包保存结果不符合严格Schema") from exc
    _, snapshot_sha256 = _load_form_snapshot(form_snapshot_path)
    expected = {
        "packet_sha256": _sha256(input_path),
        "prefill_sha256": _sha256(prefill_path),
        "form_snapshot_sha256": snapshot_sha256,
        "team_roster_sha256": _sha256(roster_path),
        "team_roster_qa_sha256": _sha256(roster_qa_path),
    }
    mismatched = [key for key, value in expected.items() if report[key] != value]
    if mismatched:
        raise SubmissionFormPacketError(f"提交表单包依赖已漂移: {mismatched}")
    if prefill["source_hashes"]["packet"] != expected["packet_sha256"]:
        raise SubmissionFormPacketError("提交表单预填包未绑定当前输入")
    prefill_expected = {
        "packet": expected["packet_sha256"],
        "form_snapshot": expected["form_snapshot_sha256"],
        "team_roster": expected["team_roster_sha256"],
        "team_roster_qa": expected["team_roster_qa_sha256"],
    }
    if prefill["source_hashes"] != prefill_expected:
        raise SubmissionFormPacketError("提交表单预填包来源绑定已漂移")
    _current_photo(packet["team_photo"], private_root=private_root)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--prefill", type=Path, default=DEFAULT_PREFILL)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--team-roster", type=Path, default=DEFAULT_TEAM_ROSTER)
    parser.add_argument("--team-roster-qa", type=Path, default=DEFAULT_TEAM_ROSTER_QA)
    parser.add_argument("--form-snapshot", type=Path, default=DEFAULT_FORM_SNAPSHOT)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--attach-photo", type=Path)
    parser.add_argument("--replace-photo", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.init:
            initialize_submission_form_packet(
                args.input, form_snapshot_path=args.form_snapshot
            )
            print(json.dumps({"status": "PENDING_INPUT"}, ensure_ascii=False))
            return 0
        if args.attach_photo:
            attach_team_photo(
                args.attach_photo,
                args.input,
                replace=args.replace_photo,
            )
            print(json.dumps({"status": "PHOTO_ATTACHED"}, ensure_ascii=False))
            return 0
        if args.verify_only:
            report = verify_saved_submission_form_packet_qa(
                args.output,
                input_path=args.input,
                prefill_path=args.prefill,
                form_snapshot_path=args.form_snapshot,
                roster_path=args.team_roster,
                roster_qa_path=args.team_roster_qa,
            )
        else:
            _, report = build_submission_form_packet(
                args.input,
                prefill_path=args.prefill,
                report_path=args.output,
                form_snapshot_path=args.form_snapshot,
                roster_path=args.team_roster,
                roster_qa_path=args.team_roster_qa,
            )
    except (ContractValidationError, OSError, SubmissionFormPacketError) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": (
                        str(exc)
                        if isinstance(exc, SubmissionFormPacketError)
                        else "提交表单包验证失败"
                    ),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "required_field_count": report["required_field_count"],
                "team_member_count": report["team_member_count"],
                "url_check_count": len(report["url_checks"]),
                "browser_form_submitted": report["data_policy"][
                    "browser_form_submitted"
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
