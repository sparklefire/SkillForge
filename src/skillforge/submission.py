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
from .final_rehearsal import (
    FinalRehearsalError,
    final_rehearsal_qa_issue,
    verify_final_rehearsal,
)
from .final_recording_review import (
    FinalRecordingReviewError,
    final_recording_review_qa_issue,
    verify_final_recording_review,
)
from .human_gates import HumanGateStore
from .official_rules_review import (
    OfficialRulesReviewError,
    official_rules_review_qa_issue,
    verify_official_rules_review,
)
from .pitch import build_readiness
from .project_board import ProjectBoardError, build_project_board_status
from .release_manifest import ReleaseManifestError, verify_release_manifest
from .team_roster import TeamRosterError, verify_team_roster
from .submission_form_packet import (
    SubmissionFormPacketError,
    verify_saved_submission_form_packet_qa,
)
from .submission_article import (
    SubmissionArticleError,
    verify_saved_submission_article_qa,
)
from .training_video_review import (
    TrainingVideoReviewError,
    training_video_review_qa_issue,
    verify_training_video_review,
)


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
    "docs/官方参考代码复现.md",
    "docs/赛事征文.md",
    "docs/最终录屏候选制作.md",
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
    "VIDEO_REQUIREMENTS",
    "EXTERNAL_API_POLICY",
    "ON_SITE_RUNTIME_REQUIREMENT",
}
EXPECTED_OFFICIAL_MATERIAL_FACTS = {
    "SCORING_WEIGHTS",
    "SUBMISSION_FIELDS",
    "OPEN_SOURCE_POLICY",
    "REFERENCE_CODE_BASELINE",
}
EXPECTED_RULE_SOURCES = {
    "NVIDIA_CSDN_EVENT_PAGE": "https://nvidia.csdn.net/6a4476b3662f9a54cb87233d.html",
    "NVIDIA_TRAINING_PAGE": (
        "https://scrm.nvidia.cn/lp/dgx-spark-hackathon-multi-agents-20260712"
    ),
    "NVIDIA_PARTICIPANT_MATERIAL": (
        "https://scrm.nvidia.cn/assets/"
        "download-dgx-spark-hackathon-multi-agents-20260712-04"
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


def _check_submission_article(root: Path) -> dict[str, Any]:
    try:
        report = verify_saved_submission_article_qa(
            root / "output/submission/submission_article_qa_v1.json",
            root=root,
            policy_path=root / "config/submission_article_policy.json",
        )
    except (OSError, SubmissionArticleError, ContractValidationError) as exc:
        return _check(
            "SUBMISSION_ARTICLE",
            "FAILED",
            f"赛事征文缺失、无效或已漂移；错误类型={type(exc).__name__}",
        )
    return _check(
        "SUBMISSION_ARTICLE",
        "PASSED",
        (
            f"征文内容可人工发布；中文字符={report['chinese_character_count']}；"
            f"事实主张={len(report['claim_checks'])}项；来源={len(report['source_checks'])}项；"
            "公开网址仍需人工发布后填写"
        ),
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
    official_material = set(status["official_material_confirmed"])
    unresolved = set(status["unresolved_requirements"])
    sources = {item["source_id"]: item["url"] for item in status["sources"]}
    audit = status["public_access_audit"]
    current_snapshot_ok = (
        status["verification_status"] == "OFFICIAL_DETAIL_REQUIRED"
        and status["checked_at"] == "2026-07-19"
        and confirmed == EXPECTED_PUBLIC_RULE_FACTS
        and official_material == EXPECTED_OFFICIAL_MATERIAL_FACTS
        and unresolved == EXPECTED_UNRESOLVED_RULE_REQUIREMENTS
        and len(status["sources"]) == len(sources)
        and sources == EXPECTED_RULE_SOURCES
        and audit["event_summary_public"] is True
        and audit["rules_session_listed"] is True
        and audit["public_rule_material_available"] is False
        and audit["official_detail_obtained"] is False
        and audit["technical_lecture_download_count"] == 3
        and audit["inspection_method"] == "PARTICIPANT_PROVIDED_OFFICIAL_MATERIAL"
        and audit["authentication_or_organizer_material_required"] is True
    )
    if not current_snapshot_ok:
        return _check(
            "OFFICIAL_RULES_STATUS",
            "FAILED",
            "规则核验状态与2026-07-19公开页面及参赛者官方材料复核结论不一致；必须重新核验后更新代码与快照",
        )
    return _check(
        "OFFICIAL_RULES_STATUS",
        "PASSED",
        (
            f"公开确认={len(confirmed)}项；官方材料确认={len(official_material)}项；"
            f"待官方细则={len(unresolved)}项；"
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


def _check_pending_private_draft(
    input_path: Path,
    qa_path: Path,
    *,
    schema_name: str,
    check_id: str,
    gate_id: str,
    confirmed_gate_ids: set[str],
    label: str,
) -> dict[str, Any] | None:
    """Accept a valid unfilled template without treating it as human evidence."""

    try:
        input_safe = (
            input_path.is_file()
            and stat.S_IMODE(input_path.stat().st_mode) == 0o600
            and stat.S_IMODE(input_path.parent.stat().st_mode) == 0o700
        )
        if not input_safe:
            raise ValueError("unsafe private draft permissions")
        document = validate_document(_read_json(input_path), schema_name)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ContractValidationError,
        ValueError,
    ) as exc:
        return _check(
            check_id,
            "FAILED",
            f"{label}无法读取、权限不安全或不符合Schema；错误类型={type(exc).__name__}",
        )
    if document["status"] != "PENDING_INPUT":
        return None
    if qa_path.exists() or gate_id in confirmed_gate_ids:
        return _check(
            check_id,
            "FAILED",
            f"{label}仍为PENDING_INPUT，但QA或人工确认已经存在",
        )
    return _check(
        check_id,
        "PASSED",
        f"{label}状态=PENDING_INPUT；模板已安全初始化，人工门禁保持待确认",
    )


def _check_team_roster_private_state(
    root: Path,
    confirmed_gate_ids: set[str] | None = None,
    roster_path: Path | None = None,
    qa_path: Path | None = None,
) -> dict[str, Any]:
    confirmed_gate_ids = confirmed_gate_ids or set()
    roster_path = (
        roster_path.resolve()
        if roster_path is not None
        else (root / "outputs/submission/team_roster.json").resolve()
    )
    private_root = roster_path.parent
    qa_path = (
        qa_path.resolve()
        if qa_path is not None
        else private_root / "team_roster_qa.json"
    )
    if not roster_path.exists():
        if qa_path.exists() or "TEAM_ELIGIBILITY_CONFIRMED" in confirmed_gate_ids:
            return _check(
                "TEAM_ROSTER_PRIVATE_STATE",
                "FAILED",
                "团队名单QA或人工确认存在，但2–5人私有名单和六类职责映射缺失",
            )
        return _check(
            "TEAM_ROSTER_PRIVATE_STATE",
            "PASSED",
            "私有团队名单状态=ABSENT；2–5人资格人工门禁保持待确认",
        )
    draft = _check_pending_private_draft(
        roster_path,
        qa_path,
        schema_name="team_roster.schema.json",
        check_id="TEAM_ROSTER_PRIVATE_STATE",
        gate_id="TEAM_ELIGIBILITY_CONFIRMED",
        confirmed_gate_ids=confirmed_gate_ids,
        label="私有团队名单",
    )
    if draft is not None:
        return draft
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


def _check_submission_form_packet_private_state(
    root: Path,
    confirmed_gate_ids: set[str] | None = None,
    input_path: Path | None = None,
    prefill_path: Path | None = None,
    qa_path: Path | None = None,
) -> dict[str, Any]:
    confirmed_gate_ids = confirmed_gate_ids or set()
    required_gates = {
        "TRAINING_VIDEO_FULL_WATCH",
        "TEAM_ELIGIBILITY_CONFIRMED",
        "OFFICIAL_RULES_VERIFIED",
        "FINAL_STAGE_REHEARSAL",
        "FINAL_RECORDING_REVIEW",
    }
    gates_complete = required_gates.issubset(confirmed_gate_ids)
    input_path = (
        input_path.resolve()
        if input_path is not None
        else (root / "outputs/submission/submission_form_packet.json").resolve()
    )
    private_root = input_path.parent
    prefill_path = (
        prefill_path.resolve()
        if prefill_path is not None
        else private_root / "submission_form_prefill.json"
    )
    qa_path = (
        qa_path.resolve()
        if qa_path is not None
        else private_root / "submission_form_packet_qa.json"
    )
    if not input_path.exists():
        if prefill_path.exists() or qa_path.exists() or gates_complete:
            return _check(
                "SUBMISSION_FORM_PACKET_PRIVATE_STATE",
                "FAILED",
                "提交表单预填包/QA或五项确认已存在，但私有表单输入缺失",
            )
        return _check(
            "SUBMISSION_FORM_PACKET_PRIVATE_STATE",
            "PASSED",
            "私有官方提交表单材料包状态=ABSENT；不会自动填写或提交浏览器表单",
        )
    try:
        document = validate_document(
            _read_json(input_path), "submission_form_packet.schema.json"
        )
    except (OSError, ContractValidationError, json.JSONDecodeError):
        return _check(
            "SUBMISSION_FORM_PACKET_PRIVATE_STATE",
            "FAILED",
            "私有官方提交表单输入无法读取或不符合严格Schema",
        )
    if document["status"] == "PENDING_INPUT":
        if prefill_path.exists() or qa_path.exists() or gates_complete:
            return _check(
                "SUBMISSION_FORM_PACKET_PRIVATE_STATE",
                "FAILED",
                "官方提交表单输入仍为PENDING_INPUT，但预填包/QA或五项确认已存在",
            )
        return _check(
            "SUBMISSION_FORM_PACKET_PRIVATE_STATE",
            "PASSED",
            "私有官方提交表单材料包状态=PENDING_INPUT；人工复制提交，不执行浏览器写入",
        )
    try:
        report = verify_saved_submission_form_packet_qa(
            qa_path,
            input_path=input_path,
            prefill_path=prefill_path,
            form_snapshot_path=root / "config/official_submission_form_status.json",
            roster_path=private_root / "team_roster.json",
            roster_qa_path=private_root / "team_roster_qa.json",
            private_root=private_root,
        )
    except (OSError, SubmissionFormPacketError, ContractValidationError) as exc:
        return _check(
            "SUBMISSION_FORM_PACKET_PRIVATE_STATE",
            "FAILED",
            f"官方提交表单材料包不完整或已漂移；错误类型={type(exc).__name__}",
        )
    return _check(
        "SUBMISSION_FORM_PACKET_PRIVATE_STATE",
        "PASSED",
        (
            f"官方提交表单8项必填字段机器QA通过；网址={len(report['url_checks'])}; "
            "提交方式=MANUAL_COPY_ONLY; 浏览器提交=false"
        ),
    )


def _check_final_rehearsal_private_state(
    root: Path,
    confirmed_gate_ids: set[str] | None = None,
    rehearsal_path: Path | None = None,
    qa_path: Path | None = None,
) -> dict[str, Any]:
    confirmed_gate_ids = confirmed_gate_ids or set()
    rehearsal_path = (
        rehearsal_path.resolve()
        if rehearsal_path is not None
        else (root / "outputs/submission/final_stage_rehearsal.json").resolve()
    )
    qa_path = (
        qa_path.resolve()
        if qa_path is not None
        else rehearsal_path.parent / "final_stage_rehearsal_qa.json"
    )
    if not rehearsal_path.exists():
        if qa_path.exists() or "FINAL_STAGE_REHEARSAL" in confirmed_gate_ids:
            return _check(
                "FINAL_REHEARSAL_PRIVATE_STATE",
                "FAILED",
                "彩排QA或人工确认存在，但绑定的私有计时记录缺失",
            )
        return _check(
            "FINAL_REHEARSAL_PRIVATE_STATE",
            "PASSED",
            "私有180秒彩排记录状态=ABSENT；彩排人工门禁保持待确认",
        )
    draft = _check_pending_private_draft(
        rehearsal_path,
        qa_path,
        schema_name="final_rehearsal_record.schema.json",
        check_id="FINAL_REHEARSAL_PRIVATE_STATE",
        gate_id="FINAL_STAGE_REHEARSAL",
        confirmed_gate_ids=confirmed_gate_ids,
        label="私有180秒彩排记录",
    )
    if draft is not None:
        return draft
    try:
        report = verify_final_rehearsal(
            rehearsal_path,
            runbook_path=root / "cases/n31/pitch_runbook.json",
            policy_path=root / "config/final_rehearsal_policy.json",
            private_root=rehearsal_path.parent,
        )
        issue = final_rehearsal_qa_issue(
            qa_path,
            {
                "kind": "LOCAL_FILE",
                "locator": str(rehearsal_path),
                "sha256": report["record_sha256"],
                "size_bytes": report["record_bytes"],
            },
            runbook_path=root / "cases/n31/pitch_runbook.json",
            policy_path=root / "config/final_rehearsal_policy.json",
        )
        if issue:
            raise FinalRehearsalError(issue)
    except (OSError, FinalRehearsalError, ContractValidationError) as exc:
        return _check(
            "FINAL_REHEARSAL_PRIVATE_STATE",
            "FAILED",
            f"私有彩排记录不完整或已失效；错误类型={type(exc).__name__}",
        )
    return _check(
        "FINAL_REHEARSAL_PRIVATE_STATE",
        "PASSED",
        (
            f"私有彩排机器检查通过；分段={report['phase_count']}; "
            f"总时长={report['duration']['actual_ms']}毫秒; "
            "彩排人工门禁="
            f"{'CONFIRMED' if 'FINAL_STAGE_REHEARSAL' in confirmed_gate_ids else 'PENDING'}"
        ),
    )


def _check_training_video_review_private_state(
    root: Path,
    confirmed_gate_ids: set[str] | None = None,
    review_path: Path | None = None,
    qa_path: Path | None = None,
) -> dict[str, Any]:
    confirmed_gate_ids = confirmed_gate_ids or set()
    review_path = (
        review_path.resolve()
        if review_path is not None
        else (root / "outputs/submission/training_video_review.json").resolve()
    )
    qa_path = (
        qa_path.resolve()
        if qa_path is not None
        else review_path.parent / "training_video_review_qa.json"
    )
    if not review_path.exists():
        if qa_path.exists() or "TRAINING_VIDEO_FULL_WATCH" in confirmed_gate_ids:
            return _check(
                "TRAINING_VIDEO_REVIEW_PRIVATE_STATE",
                "FAILED",
                "观看QA或人工确认存在，但绑定的私有完整观看记录缺失",
            )
        return _check(
            "TRAINING_VIDEO_REVIEW_PRIVATE_STATE",
            "PASSED",
            "私有80秒成片观看记录状态=ABSENT；完整观看人工门禁保持待确认",
        )
    draft = _check_pending_private_draft(
        review_path,
        qa_path,
        schema_name="training_video_review.schema.json",
        check_id="TRAINING_VIDEO_REVIEW_PRIVATE_STATE",
        gate_id="TRAINING_VIDEO_FULL_WATCH",
        confirmed_gate_ids=confirmed_gate_ids,
        label="私有80秒成片观看记录",
    )
    if draft is not None:
        return draft
    try:
        report = verify_training_video_review(
            review_path,
            manifest_path=root / "output/video/n31_training_video_manifest_v1.json",
            video_path=root / "output/video/n31_training_video_v1.mp4",
            private_root=review_path.parent,
        )
        issue = training_video_review_qa_issue(
            qa_path,
            {
                "kind": "LOCAL_FILE",
                "locator": str(review_path),
                "sha256": report["review_sha256"],
                "size_bytes": report["review_bytes"],
            },
            manifest_path=root / "output/video/n31_training_video_manifest_v1.json",
            video_path=root / "output/video/n31_training_video_v1.mp4",
        )
        if issue:
            raise TrainingVideoReviewError(issue)
    except (OSError, TrainingVideoReviewError, ContractValidationError) as exc:
        return _check(
            "TRAINING_VIDEO_REVIEW_PRIVATE_STATE",
            "FAILED",
            f"私有完整观看记录不完整或已失效；错误类型={type(exc).__name__}",
        )
    return _check(
        "TRAINING_VIDEO_REVIEW_PRIVATE_STATE",
        "PASSED",
        (
            f"当前80秒成片完整观看机器检查通过；时长={report['video']['duration_ms']}毫秒; "
            "观看人工门禁="
            f"{'CONFIRMED' if 'TRAINING_VIDEO_FULL_WATCH' in confirmed_gate_ids else 'PENDING'}"
        ),
    )


def _check_final_recording_review_private_state(
    root: Path,
    confirmed_gate_ids: set[str] | None = None,
    review_path: Path | None = None,
    qa_path: Path | None = None,
) -> dict[str, Any]:
    confirmed_gate_ids = confirmed_gate_ids or set()
    review_path = (
        review_path.resolve()
        if review_path is not None
        else (root / "outputs/submission/final_recording_review.json").resolve()
    )
    qa_path = (
        qa_path.resolve()
        if qa_path is not None
        else review_path.parent / "final_recording_review_qa.json"
    )
    if not review_path.exists():
        if qa_path.exists() or "FINAL_RECORDING_REVIEW" in confirmed_gate_ids:
            return _check(
                "FINAL_RECORDING_REVIEW_PRIVATE_STATE",
                "FAILED",
                "最终录屏观看QA或人工确认存在，但绑定的私有完整观看记录缺失",
            )
        return _check(
            "FINAL_RECORDING_REVIEW_PRIVATE_STATE",
            "PASSED",
            "私有178秒最终录屏观看记录状态=ABSENT；最终录屏人工门禁保持待确认",
        )
    draft = _check_pending_private_draft(
        review_path,
        qa_path,
        schema_name="final_recording_review.schema.json",
        check_id="FINAL_RECORDING_REVIEW_PRIVATE_STATE",
        gate_id="FINAL_RECORDING_REVIEW",
        confirmed_gate_ids=confirmed_gate_ids,
        label="私有178秒最终录屏观看记录",
    )
    if draft is not None:
        return draft
    recording_path = review_path.parent / "skillforge_final_recording.mp4"
    try:
        report = verify_final_recording_review(
            review_path,
            recording_path=recording_path,
            machine_qa_path=review_path.parent / "final_recording_qa.json",
            build_report_path=review_path.parent / "final_recording_build.json",
            storyboard_path=root / "config/final_recording_storyboard.json",
            policy_path=root / "config/final_recording_policy.json",
            private_root=review_path.parent,
        )
        issue = final_recording_review_qa_issue(
            qa_path,
            {
                "kind": "LOCAL_FILE",
                "locator": str(recording_path),
                "sha256": report["recording"]["sha256"],
                "size_bytes": report["recording"]["bytes"],
            },
            review_path=review_path,
            recording_path=recording_path,
            machine_qa_path=review_path.parent / "final_recording_qa.json",
            build_report_path=review_path.parent / "final_recording_build.json",
            storyboard_path=root / "config/final_recording_storyboard.json",
            policy_path=root / "config/final_recording_policy.json",
        )
        if issue:
            raise FinalRecordingReviewError(issue)
    except (OSError, FinalRecordingReviewError, ContractValidationError) as exc:
        return _check(
            "FINAL_RECORDING_REVIEW_PRIVATE_STATE",
            "FAILED",
            f"私有最终录屏完整观看记录不完整或已失效；错误类型={type(exc).__name__}",
        )
    return _check(
        "FINAL_RECORDING_REVIEW_PRIVATE_STATE",
        "PASSED",
        (
            f"当前最终录屏完整观看机器检查通过；时长={report['recording']['duration_ms']}毫秒; "
            "最终录屏人工门禁="
            f"{'CONFIRMED' if 'FINAL_RECORDING_REVIEW' in confirmed_gate_ids else 'PENDING'}"
        ),
    )


def _check_official_rules_review_private_state(
    root: Path,
    confirmed_gate_ids: set[str] | None = None,
    review_path: Path | None = None,
    qa_path: Path | None = None,
) -> dict[str, Any]:
    confirmed_gate_ids = confirmed_gate_ids or set()
    review_path = (
        review_path.resolve()
        if review_path is not None
        else (root / "outputs/submission/official_rules_review.json").resolve()
    )
    qa_path = (
        qa_path.resolve()
        if qa_path is not None
        else review_path.parent / "official_rules_review_qa.json"
    )
    if not review_path.exists():
        if qa_path.exists() or "OFFICIAL_RULES_VERIFIED" in confirmed_gate_ids:
            return _check(
                "OFFICIAL_RULES_REVIEW_PRIVATE_STATE",
                "FAILED",
                "规则审核QA或人工确认存在，但绑定的私有六项审核记录缺失",
            )
        return _check(
            "OFFICIAL_RULES_REVIEW_PRIVATE_STATE",
            "PASSED",
            "私有官方规则六项审核状态=ABSENT；官方规则人工门禁保持待确认",
        )
    draft = _check_pending_private_draft(
        review_path,
        qa_path,
        schema_name="official_rules_review.schema.json",
        check_id="OFFICIAL_RULES_REVIEW_PRIVATE_STATE",
        gate_id="OFFICIAL_RULES_VERIFIED",
        confirmed_gate_ids=confirmed_gate_ids,
        label="私有官方规则六项审核",
    )
    if draft is not None:
        return draft
    try:
        report = verify_official_rules_review(
            review_path,
            public_snapshot_path=root / "config/official_rules_status.json",
            private_root=review_path.parent,
        )
        issue = official_rules_review_qa_issue(
            qa_path,
            {
                "kind": "LOCAL_FILE",
                "locator": str(review_path),
                "sha256": report["review_sha256"],
                "size_bytes": report["review_bytes"],
            },
            public_snapshot_path=root / "config/official_rules_status.json",
        )
        if issue:
            raise OfficialRulesReviewError(issue)
    except (OSError, OfficialRulesReviewError, ContractValidationError) as exc:
        return _check(
            "OFFICIAL_RULES_REVIEW_PRIVATE_STATE",
            "FAILED",
            f"私有官方规则审核不完整或已失效；错误类型={type(exc).__name__}",
        )
    return _check(
        "OFFICIAL_RULES_REVIEW_PRIVATE_STATE",
        "PASSED",
        (
            f"官方规则六项机器检查通过；来源类型={report['source']['kind']}; "
            "官方规则人工门禁="
            f"{'CONFIRMED' if 'OFFICIAL_RULES_VERIFIED' in confirmed_gate_ids else 'PENDING'}"
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
    *,
    official_rules_review_qa_path: Path | None = None,
) -> tuple[dict[str, Any], set[str]]:
    store = HumanGateStore(
        confirmations_path,
        runbook_path=root / "cases/n31/pitch_runbook.json",
        training_video_manifest_path=(
            root / "output/video/n31_training_video_manifest_v1.json"
        ),
        training_video_path=root / "output/video/n31_training_video_v1.mp4",
        official_rules_review_qa_path=official_rules_review_qa_path,
        official_rules_snapshot_path=root / "config/official_rules_status.json",
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
    final_rehearsal_path: Path | None = None,
    final_rehearsal_qa_path: Path | None = None,
    training_video_review_path: Path | None = None,
    training_video_review_qa_path: Path | None = None,
    final_recording_review_path: Path | None = None,
    final_recording_review_qa_path: Path | None = None,
    official_rules_review_path: Path | None = None,
    official_rules_review_qa_path: Path | None = None,
    submission_form_packet_path: Path | None = None,
    submission_form_prefill_path: Path | None = None,
    submission_form_packet_qa_path: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    confirmation_check, confirmed_gate_ids = _check_human_gate_confirmations(
        root,
        (confirmations_path or root / "outputs/submission/human_gate_confirmations.json").resolve(),
        official_rules_review_qa_path=official_rules_review_qa_path,
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
        _check_submission_article(root),
        _check_official_rules_status(root),
        _check_official_rules_review_private_state(
            root,
            confirmed_gate_ids,
            review_path=official_rules_review_path,
            qa_path=official_rules_review_qa_path,
        ),
        _check_release_manifest(root),
        _check_project_board(root),
        _check_team_roster_private_state(
            root,
            confirmed_gate_ids,
            roster_path=team_roster_path,
        ),
        _check_submission_form_packet_private_state(
            root,
            confirmed_gate_ids,
            input_path=submission_form_packet_path,
            prefill_path=submission_form_prefill_path,
            qa_path=submission_form_packet_qa_path,
        ),
        _check_training_video_review_private_state(
            root,
            confirmed_gate_ids,
            review_path=training_video_review_path,
            qa_path=training_video_review_qa_path,
        ),
        _check_final_rehearsal_private_state(
            root,
            confirmed_gate_ids,
            rehearsal_path=final_rehearsal_path,
            qa_path=final_rehearsal_qa_path,
        ),
        _check_final_recording_review_private_state(
            root,
            confirmed_gate_ids,
            review_path=final_recording_review_path,
            qa_path=final_recording_review_qa_path,
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
