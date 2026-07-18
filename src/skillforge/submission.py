"""Run the local-only SkillForge submission preflight."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .contracts import ContractValidationError, validate_document
from .demo import ROOT
from .human_gates import HumanGateStore
from .pitch import build_readiness
from .project_board import ProjectBoardError, build_project_board_status
from .release_manifest import ReleaseManifestError, verify_release_manifest
from .team_roster import TeamRosterError, verify_team_roster


REQUIRED_DOCUMENTS = [
    "README.md",
    "docs/架构与数据边界.md",
    "docs/参赛提交材料.md",
    "docs/三分钟路演脚本.md",
    "docs/现场演示与录屏操作单.md",
    "docs/赛事要求对齐矩阵.md",
    "docs/环境与接入.md",
    "docs/执行状态.md",
    "docs/SkillForge任务拆解.md",
]
ABSOLUTE_PATH_MARKERS = (b"/Users/", b"/home/Developer/", b"file://")
SECRET_KEY_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION")
PRIVATE_NAME_MARKERS = (
    "_private",
    "private_review",
    "previous_shipping_label",
    "面单_sf",
)
EXPECTED_PUBLIC_RULE_FACTS = {
    "EVENT_THEME",
    "MULTIMODAL_AGENT_SCOPE",
    "DGX_SPARK_PLATFORM",
    "TEAM_SIZE_2_TO_5",
    "ONE_TEAM_PER_PERSON",
    "SUBMISSION_DEADLINE_2026_07_22",
    "JUDGING_2026_07_23_TO_26",
    "FINAL_2026_08_02",
}
EXPECTED_UNRESOLVED_RULE_REQUIREMENTS = {
    "SCORING_WEIGHTS",
    "SUBMISSION_FIELDS",
    "VIDEO_REQUIREMENTS",
    "EXTERNAL_API_POLICY",
    "OPEN_SOURCE_POLICY",
    "ON_SITE_RUNTIME_REQUIREMENT",
}
EXPECTED_RULE_SOURCES = {
    "NVIDIA_CSDN_EVENT_PAGE": "https://nvidia.csdn.net/6a4476b3662f9a54cb87233d.html",
    "NVIDIA_TRAINING_PAGE": (
        "https://scrm.nvidia.cn/lp/dgx-spark-hackathon-multi-agents-20260712"
    ),
}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path = path.expanduser().resolve()
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not parent_existed or path.parent == (ROOT / "outputs/submission").resolve():
        os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _check(check_id: str, status: str, *details: str) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": status,
        "details": list(details) or ["无附加信息"],
    }


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _tracked_files(root: Path) -> list[str] | None:
    result = _git(root, "ls-files", "-z")
    if result.returncode != 0:
        return None
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def _find_sensitive_tracked_paths(paths: Iterable[str]) -> list[str]:
    findings: list[str] = []
    for path in paths:
        lowered = path.lower()
        parts = Path(path).parts
        if path == ".env":
            findings.append(path)
            continue
        if len(parts) >= 4 and parts[0] == "cases" and parts[2] in {
            "input",
            "derived",
            "output",
        }:
            if parts[-1] != ".gitkeep":
                findings.append(path)
                continue
        if any(marker in lowered for marker in PRIVATE_NAME_MARKERS):
            findings.append(path)
    return sorted(set(findings))


def _secret_values(env_path: Path) -> list[bytes]:
    if not env_path.is_file():
        return []
    values: list[bytes] = []
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not any(marker in key.upper() for marker in SECRET_KEY_MARKERS):
            continue
        normalized = value.strip().strip("\"'")
        if len(normalized) >= 8:
            values.append(normalized.encode("utf-8"))
    return sorted(set(values))


def _contains_needles(path: Path, needles: Iterable[bytes]) -> bool:
    targets = tuple(item for item in needles if item)
    if not targets or not path.is_file():
        return False
    overlap = max(len(item) for item in targets) - 1
    previous = b""
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            payload = previous + chunk
            if any(item in payload for item in targets):
                return True
            previous = payload[-overlap:] if overlap > 0 else b""
    return False


def _find_secret_value_leaks(
    root: Path,
    tracked_paths: Iterable[str],
    values: Iterable[bytes],
) -> list[str]:
    needles = tuple(values)
    if not needles:
        return []
    findings = []
    for relative in tracked_paths:
        path = root / relative
        if _contains_needles(path, needles):
            findings.append(relative)
    return findings


def _artifact_contains_markers(path: Path, markers: Iterable[bytes]) -> bool:
    targets = tuple(markers)
    if path.suffix.lower() == ".pptx":
        try:
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if name.endswith("/"):
                        continue
                    payload = archive.read(name)
                    if any(marker in payload for marker in targets):
                        return True
        except zipfile.BadZipFile:
            return True
        return False
    return _contains_needles(path, targets)


def _check_project_identity(root: Path) -> dict[str, Any]:
    readme = (root / "README.md").read_text(encoding="utf-8")
    runbook = _read_json(root / "cases/n31/pitch_runbook.json")
    assertions = {
        "project_name": "匠传 SkillForge" in readme,
        "positioning": "可追溯、可验证、会自我修订" in readme,
        "case_id": runbook.get("case_id") == "n31_media_change",
        "deadline": "2026-07-22" in readme,
    }
    failed = [name for name, passed in assertions.items() if not passed]
    return _check(
        "PROJECT_IDENTITY",
        "PASSED" if not failed else "FAILED",
        "项目名、定位、案例编号和截止日一致"
        if not failed
        else f"不一致字段: {','.join(failed)}",
    )


def _check_required_documents(root: Path) -> dict[str, Any]:
    missing = [
        relative
        for relative in REQUIRED_DOCUMENTS
        if not (root / relative).is_file() or (root / relative).stat().st_size == 0
    ]
    return _check(
        "REQUIRED_DOCUMENTS",
        "PASSED" if not missing else "FAILED",
        f"{len(REQUIRED_DOCUMENTS)}份说明文档存在且非空"
        if not missing
        else f"缺少文档: {','.join(missing)}",
    )


def _check_official_rules_status(root: Path) -> dict[str, Any]:
    try:
        status = validate_document(
            _read_json(root / "config/official_rules_status.json"),
            "official_rules_status.schema.json",
        )
    except (OSError, json.JSONDecodeError, ContractValidationError) as exc:
        return _check(
            "OFFICIAL_RULES_STATUS",
            "FAILED",
            f"官方规则核验快照缺失或无效；错误类型={type(exc).__name__}",
        )

    confirmed = set(status["publicly_confirmed"])
    unresolved = set(status["unresolved_requirements"])
    sources = {item["source_id"]: item["url"] for item in status["sources"]}
    audit = status["public_access_audit"]
    current_snapshot_ok = (
        status["verification_status"] == "OFFICIAL_DETAIL_REQUIRED"
        and status["checked_at"] == "2026-07-18"
        and confirmed == EXPECTED_PUBLIC_RULE_FACTS
        and unresolved == EXPECTED_UNRESOLVED_RULE_REQUIREMENTS
        and len(status["sources"]) == len(sources)
        and sources == EXPECTED_RULE_SOURCES
        and audit["event_summary_public"] is True
        and audit["rules_session_listed"] is True
        and audit["public_rule_material_available"] is False
        and audit["official_detail_obtained"] is False
        and audit["technical_lecture_download_count"] == 3
        and audit["inspection_method"] == "ANONYMOUS_INTERACTIVE_PAGE"
        and audit["authentication_or_organizer_material_required"] is True
    )
    if not current_snapshot_ok:
        return _check(
            "OFFICIAL_RULES_STATUS",
            "FAILED",
            "规则核验状态与2026-07-18公开访问复核结论不一致；必须重新核验后更新代码与快照",
        )
    return _check(
        "OFFICIAL_RULES_STATUS",
        "PASSED",
        (
            f"公开确认={len(confirmed)}项；待官方细则={len(unresolved)}项；"
            "规则人工门禁保持待确认"
        ),
    )


def _check_release_manifest(root: Path) -> dict[str, Any]:
    try:
        manifest = verify_release_manifest(
            root / "output/submission/release_manifest_v1.json",
            root=root,
            config_path=root / "config/release_roles.json",
            runbook_path=root / "cases/n31/pitch_runbook.json",
        )
    except (OSError, ReleaseManifestError, ContractValidationError) as exc:
        return _check(
            "RELEASE_FREEZE_MANIFEST",
            "FAILED",
            f"发布冻结清单缺失、过期或无效；错误类型={type(exc).__name__}",
        )
    return _check(
        "RELEASE_FREEZE_MANIFEST",
        "PASSED",
        (
            f"{manifest['artifact_count']}项成果完成角色、终检和哈希冻结；"
            f"角色={len(manifest['roles'])}; 公开入口={len(manifest['publication_targets'])}; "
            "成员姓名与链接仍为私有待填写"
        ),
    )


def _check_project_board(root: Path) -> dict[str, Any]:
    try:
        report = build_project_board_status(
            board_path=root / "config/project_board.json",
            runbook_path=root / "cases/n31/pitch_runbook.json",
        )
    except (OSError, ProjectBoardError, ContractValidationError) as exc:
        return _check(
            "PROJECT_BOARD_STATUS",
            "FAILED",
            f"P0任务看板缺失或无效；错误类型={type(exc).__name__}",
        )
    return _check(
        "PROJECT_BOARD_STATUS",
        "PASSED" if report["status"] == "ON_TRACK" else "FAILED",
        (
            f"状态={report['status']}; 完成={report['completed_count']}; "
            f"技术就绪={report['ready_count']}; 等待人={report['awaiting_human_count']}; "
            f"等待外部={report['awaiting_external_count']}; "
            f"实现目标受阻={str(report['implementation_goal_blocked']).lower()}"
        ),
    )


def _check_team_roster_private_state(
    root: Path,
    confirmed_gate_ids: set[str] | None = None,
    roster_path: Path | None = None,
) -> dict[str, Any]:
    confirmed_gate_ids = confirmed_gate_ids or set()
    roster_path = (
        roster_path.resolve()
        if roster_path is not None
        else (root / "outputs/submission/team_roster.json").resolve()
    )
    private_root = roster_path.parent
    if not roster_path.exists():
        if "TEAM_ELIGIBILITY_CONFIRMED" in confirmed_gate_ids:
            return _check(
                "TEAM_ROSTER_PRIVATE_STATE",
                "FAILED",
                "团队资格已确认，但2–5人私有名单和六类职责映射缺失",
            )
        return _check(
            "TEAM_ROSTER_PRIVATE_STATE",
            "PASSED",
            "私有团队名单状态=ABSENT；2–5人资格人工门禁保持待确认",
        )
    try:
        report = verify_team_roster(roster_path, private_root=private_root)
    except (OSError, TeamRosterError, ContractValidationError) as exc:
        return _check(
            "TEAM_ROSTER_PRIVATE_STATE",
            "FAILED",
            f"私有团队名单不完整或无效；错误类型={type(exc).__name__}",
        )
    return _check(
        "TEAM_ROSTER_PRIVATE_STATE",
        "PASSED",
        (
            f"私有团队名单机器检查通过；成员={report['team_size']}; "
            f"角色={report['role_assignment_count']}; 团队资格人工门禁="
            f"{'CONFIRMED' if 'TEAM_ELIGIBILITY_CONFIRMED' in confirmed_gate_ids else 'PENDING'}"
        ),
    )


def _check_pitch_package(
    root: Path,
    confirmed_gate_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    readiness = build_readiness(
        root / "cases/n31/pitch_runbook.json",
        root=root,
        confirmed_gate_ids=confirmed_gate_ids,
    )
    passed = readiness["status"] != "NOT_READY" and all(
        item["status"] == "PASSED" for item in readiness["checks"]
    )
    artifact_check = next(
        item for item in readiness["checks"] if item["check_id"] == "REQUIRED_ARTIFACTS"
    )
    return (
        _check(
            "PITCH_PACKAGE",
            "PASSED" if passed else "FAILED",
            f"{len(artifact_check['items'])}项成果通过；路演状态={readiness['status']}",
        ),
        readiness,
    )


def _check_human_gate_confirmations(
    root: Path,
    confirmations_path: Path,
) -> tuple[dict[str, Any], set[str]]:
    store = HumanGateStore(
        confirmations_path,
        runbook_path=root / "cases/n31/pitch_runbook.json",
    )
    audit = store.audit()
    if audit["valid"]:
        detail = (
            f"人工门禁有效={audit['summary']['passed']}/{audit['summary']['total']}; "
            f"待确认={audit['summary']['pending']}; 状态={audit['store_state']}"
        )
        return _check("HUMAN_GATE_CONFIRMATIONS", "PASSED", detail), set(
            audit["confirmed_gate_ids"]
        )
    issue_codes = ",".join(audit["issues"][:5])
    return (
        _check(
            "HUMAN_GATE_CONFIRMATIONS",
            "FAILED",
            f"私有人工确认无效；状态={audit['store_state']}; 问题={issue_codes}",
        ),
        set(),
    )


def _check_public_artifacts(
    root: Path,
    secret_values: Iterable[bytes],
) -> dict[str, Any]:
    runbook = validate_document(
        _read_json(root / "cases/n31/pitch_runbook.json"),
        "pitch_runbook.schema.json",
    )
    markers = (*ABSOLUTE_PATH_MARKERS, *tuple(secret_values))
    findings = []
    for artifact in runbook["required_artifacts"]:
        path = (root / artifact["path"]).resolve()
        if path != root and root not in path.parents:
            findings.append(artifact["path"])
        elif _artifact_contains_markers(path, markers):
            findings.append(artifact["path"])
    return _check(
        "PUBLIC_ARTIFACT_BOUNDARY",
        "PASSED" if not findings else "FAILED",
        f"{len(runbook['required_artifacts'])}项成果未发现绝对路径或本地密钥值"
        if not findings
        else f"发现需复核的成果: {','.join(findings)}",
    )


def _check_tests(root: Path, *, run_tests: bool) -> dict[str, Any]:
    if not run_tests:
        return _check("AUTOMATED_TESTS", "SKIPPED", "开发检查未执行全量测试")
    result = subprocess.run(
        [sys.executable, "-m", "pytest"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        text=True,
    )
    match = re.search(r"(\d+) passed", result.stdout)
    count = int(match.group(1)) if match else 0
    return _check(
        "AUTOMATED_TESTS",
        "PASSED" if result.returncode == 0 and count > 0 else "FAILED",
        f"{count}项自动测试通过"
        if result.returncode == 0 and count > 0
        else f"自动测试退出码={result.returncode}",
    )


def _check_git_and_secrets(
    root: Path,
    *,
    allow_dirty: bool,
    allow_missing_git: bool,
) -> tuple[list[dict[str, Any]], str | None, str | None, bool | None, list[bytes]]:
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != b"true":
        status = "SKIPPED" if allow_missing_git else "FAILED"
        detail = "运行目录没有Git元数据" if allow_missing_git else "无法验证Git提交边界"
        return [
            _check("GIT_WORKTREE", status, detail),
            _check("TRACKED_SENSITIVE_PATHS", status, detail),
            _check("ENV_AND_SECRET_SCAN", status, detail),
        ], None, None, None, []

    branch_result = _git(root, "branch", "--show-current")
    commit_result = _git(root, "rev-parse", "HEAD")
    status_result = _git(root, "status", "--porcelain", "--untracked-files=all")
    branch = branch_result.stdout.decode("utf-8").strip() or None
    commit = commit_result.stdout.decode("utf-8").strip() or None
    clean = status_result.returncode == 0 and not status_result.stdout.strip()
    if clean:
        worktree_check = _check("GIT_WORKTREE", "PASSED", "工作树干净")
    elif allow_dirty:
        worktree_check = _check(
            "GIT_WORKTREE", "SKIPPED", "开发模式允许未提交差异；正式提交前必须清空"
        )
    else:
        worktree_check = _check("GIT_WORKTREE", "FAILED", "存在未提交或未跟踪文件")

    tracked = _tracked_files(root) or []
    sensitive_paths = _find_sensitive_tracked_paths(tracked)
    path_check = _check(
        "TRACKED_SENSITIVE_PATHS",
        "PASSED" if not sensitive_paths else "FAILED",
        f"{len(tracked)}个Git文件未包含原始输入、私有派生物或.env"
        if not sensitive_paths
        else f"敏感路径: {','.join(sensitive_paths)}",
    )

    env_path = root / ".env"
    values = _secret_values(env_path)
    env_mode_ok = not env_path.exists() or stat.S_IMODE(env_path.stat().st_mode) == 0o600
    ignored = not env_path.exists() or _git(root, "check-ignore", "-q", ".env").returncode == 0
    leaks = _find_secret_value_leaks(root, tracked, values)
    secret_ok = env_mode_ok and ignored and not leaks
    secret_check = _check(
        "ENV_AND_SECRET_SCAN",
        "PASSED" if secret_ok else "FAILED",
        (
            f"env={'present' if env_path.exists() else 'absent'}; "
            f"mode_600={str(env_mode_ok).lower()}; ignored={str(ignored).lower()}; "
            f"secret_values={len(values)}; tracked_leaks={len(leaks)}"
        ),
    )
    return [worktree_check, path_check, secret_check], commit, branch, clean, values


def build_submission_preflight(
    *,
    root: Path = ROOT,
    run_tests: bool = True,
    allow_dirty: bool = False,
    allow_missing_git: bool = False,
    confirmations_path: Path | None = None,
    team_roster_path: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    confirmation_check, confirmed_gate_ids = _check_human_gate_confirmations(
        root,
        (confirmations_path or root / "outputs/submission/human_gate_confirmations.json").resolve(),
    )
    git_checks, commit, branch, clean, values = _check_git_and_secrets(
        root,
        allow_dirty=allow_dirty,
        allow_missing_git=allow_missing_git,
    )
    pitch_check, readiness = _check_pitch_package(root, confirmed_gate_ids)
    checks = [
        _check_project_identity(root),
        _check_required_documents(root),
        _check_official_rules_status(root),
        _check_release_manifest(root),
        _check_project_board(root),
        _check_team_roster_private_state(
            root,
            confirmed_gate_ids,
            roster_path=team_roster_path,
        ),
        confirmation_check,
        pitch_check,
        _check_public_artifacts(root, values),
        *git_checks,
        _check_tests(root, run_tests=run_tests),
    ]
    failed = any(item["status"] == "FAILED" for item in checks)
    skipped = any(item["status"] == "SKIPPED" for item in checks)
    pending = readiness["pending_human_gates"]
    if failed:
        status_value = "NOT_READY"
    elif skipped:
        status_value = "DEVELOPMENT_CHECK"
    elif pending:
        status_value = "READY_WITH_HUMAN_GATES"
    else:
        status_value = "READY_FOR_SUBMISSION"
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status_value,
        "source_commit": commit,
        "source_branch": branch,
        "source_worktree_clean": clean,
        "automatic_checks": checks,
        "pending_human_gates": pending,
        "data_policy": {
            "contains_credentials": False,
            "contains_raw_media": False,
            "contains_absolute_paths": False,
        },
    }
    return validate_document(report, "submission_preflight.schema.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "outputs/submission/submission_preflight_latest.json",
    )
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--allow-missing-git", action="store_true")
    parser.add_argument(
        "--confirmations",
        type=Path,
        default=ROOT / "outputs/submission/human_gate_confirmations.json",
    )
    args = parser.parse_args()
    report = build_submission_preflight(
        run_tests=not args.skip_tests,
        allow_dirty=args.allow_dirty,
        allow_missing_git=args.allow_missing_git,
        confirmations_path=args.confirmations,
    )
    _write_json(args.output, report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if report["status"] == "READY_FOR_SUBMISSION":
        return 0
    if report["status"] == "NOT_READY":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
