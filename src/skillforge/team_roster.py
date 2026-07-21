"""Privately validate the final 2-5 person team and release-role mapping."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cli_hints import print_error_hints
from .contracts import ContractValidationError, validate_document
from .demo import ROOT


DEFAULT_PRIVATE_ROOT = ROOT / "outputs/submission"
DEFAULT_INPUT = DEFAULT_PRIVATE_ROOT / "team_roster.json"
DEFAULT_REPORT = DEFAULT_PRIVATE_ROOT / "team_roster_qa.json"
EXPECTED_ROLES = {
    "TECHNICAL_OWNER",
    "EVIDENCE_OWNER",
    "CONTENT_OWNER",
    "DEMO_OPERATOR",
    "SUBMISSION_OWNER",
    "FINAL_REVIEWER",
}


class TeamRosterError(ValueError):
    """Raised when the private roster is missing, incomplete or unsafe."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, root: Path = DEFAULT_PRIVATE_ROOT) -> Path:
    resolved = path.expanduser().resolve()
    root = root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise TeamRosterError("团队名单和报告必须保存在私有提交目录") from exc
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TeamRosterError("团队名单无法读取或不是合法JSON") from exc
    if not isinstance(value, dict):
        raise TeamRosterError("团队名单必须是JSON对象")
    return value


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
        raise TeamRosterError("团队名单私有目录权限必须为0700")
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


def initialize_team_roster(
    destination: Path = DEFAULT_INPUT,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    destination = _inside(destination, private_root)
    if destination.exists():
        raise TeamRosterError("团队名单已存在；初始化不会覆盖已有内容")
    document = {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": _now(),
        "status": "PENDING_INPUT",
        "members": [],
        "role_assignments": [
            {"role_id": role_id, "member_id": None}
            for role_id in sorted(EXPECTED_ROLES)
        ],
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": True,
            "git_tracked": False,
        },
    }
    return _write_private_json(
        validate_document(document, "team_roster.schema.json"),
        destination,
        private_root=private_root,
    )


def verify_team_roster_document(
    document: dict[str, Any],
    *,
    roster_sha256: str,
) -> dict[str, Any]:
    try:
        validate_document(document, "team_roster.schema.json")
    except ContractValidationError as exc:
        raise TeamRosterError("团队名单不符合严格Schema") from exc
    if document["status"] != "READY_FOR_CHECK":
        raise TeamRosterError("团队名单尚未填写完成")

    members = document["members"]
    assignments = document["role_assignments"]
    member_ids = [item["member_id"] for item in members]
    member_id_set = set(member_ids)
    role_ids = [item["role_id"] for item in assignments]
    checks = {
        "team_size_valid": 2 <= len(members) <= 5,
        "member_ids_unique": len(member_ids) == len(member_id_set),
        "member_fields_nonblank": all(
            item["name"].strip() == item["name"]
            and item["organization"].strip() == item["organization"]
            and bool(item["name"])
            and bool(item["organization"])
            for item in members
        ),
        "one_primary_contact": sum(item["primary_contact"] for item in members) == 1,
        "all_members_registration_confirmed": all(
            item["registration_confirmed"] for item in members
        ),
        "all_members_one_team_only_confirmed": all(
            item["one_team_only_confirmed"] for item in members
        ),
        "all_roles_assigned_once": len(role_ids) == len(set(role_ids))
        and set(role_ids) == EXPECTED_ROLES,
        "role_members_exist": all(
            item["member_id"] in member_id_set for item in assignments
        ),
    }
    if not all(checks.values()):
        raise TeamRosterError("团队人数、资格声明、主联系人或角色映射尚未通过")

    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "TEAM_ROSTER_QA",
        "checked_at": _now(),
        "status": "READY_FOR_HUMAN_CONFIRMATION",
        "roster_sha256": roster_sha256,
        "team_size": len(members),
        "role_assignment_count": len(assignments),
        "human_gate_status": "PENDING",
        "checks": checks,
        "data_policy": {
            "private_local_state": True,
            "contains_personal_data": False,
            "contains_member_names": False,
            "contains_organizations": False,
            "contains_member_ids": False,
            "human_confirmation_generated": False,
        },
    }
    return validate_document(report, "team_roster_qa.schema.json")


def verify_team_roster(
    input_path: Path = DEFAULT_INPUT,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> dict[str, Any]:
    input_path = _inside(input_path, private_root)
    if not input_path.is_file():
        raise TeamRosterError("团队名单私有输入不存在；请先使用--init")
    if (
        stat.S_IMODE(input_path.parent.stat().st_mode) != 0o700
        or stat.S_IMODE(input_path.stat().st_mode) != 0o600
    ):
        raise TeamRosterError("团队名单权限必须为目录0700、文件0600")
    return verify_team_roster_document(
        _read_json(input_path),
        roster_sha256=_sha256_file(input_path),
    )


_TEAM_ROSTER_ERROR_HINTS = {
    "团队名单私有输入不存在；请先使用--init": [
        "── 团队名单待办（私有） ──",
        "  1. 初始化空白名单：bash scripts/check_team_roster.sh --init",
        "  2. 用编辑器打开私有名单，填写成员、资格声明、主联系人与角色映射，"
        "并把 status 改为 READY_FOR_CHECK",
        "  3. 重新运行 bash scripts/check_team_roster.sh 生成私有名单报告",
    ],
    "团队名单尚未填写完成": [
        "  提示：用编辑器打开私有名单，补全成员、资格声明、主联系人与角色映射，"
        "并把 status 改为 READY_FOR_CHECK，再重新运行本脚本。",
    ],
    "团队人数、资格声明、主联系人或角色映射尚未通过": [
        "  提示：用编辑器打开私有名单，补全未通过的字段，"
        "并把 status 改为 READY_FOR_CHECK，再重新运行本脚本。",
    ],
    "团队名单不符合严格Schema": [
        "  提示：对照私有名单模板的字段名与取值检查并修正，再重新运行本脚本。",
    ],
    "团队名单私有目录权限必须为0700": [
        "  提示：私有提交目录权限应为 0700；修正后重新运行本脚本。",
    ],
    "团队名单权限必须为目录0700、文件0600": [
        "  提示：私有目录应为 0700、名单文件应为 0600；修正后重新运行本脚本。",
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    try:
        if args.init:
            initialize_team_roster(args.input)
            print(json.dumps({"status": "PENDING_INPUT"}, ensure_ascii=False))
            return 0
        report = verify_team_roster(args.input)
        _write_private_json(report, args.output)
    except (ContractValidationError, OSError, TeamRosterError) as exc:
        if isinstance(exc, TeamRosterError):
            print_error_hints(str(exc), exact_hints=_TEAM_ROSTER_ERROR_HINTS)
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": str(exc)
                    if isinstance(exc, TeamRosterError)
                    else "团队名单验证失败",
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
                "team_size": report["team_size"],
                "role_assignment_count": report["role_assignment_count"],
                "human_gate_status": report["human_gate_status"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
