"""Build a safe, read-only status plan for SkillForge submission closeout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .contracts import ContractValidationError, validate_document
from .demo import ROOT
from .final_recording import DEFAULT_RECORDING, final_recording_qa_issue
from .final_rehearsal import (
    FinalRehearsalError,
    final_rehearsal_qa_issue,
    verify_final_rehearsal,
)
from .human_gates import HumanGateError, HumanGateStore
from .official_rules_review import (
    OfficialRulesReviewError,
    official_rules_review_qa_issue,
    verify_official_rules_review,
)
from .publication_links import EXPECTED_TARGETS
from .release_bundle import (
    DEFAULT_ARCHIVE as DEFAULT_RELEASE_ARCHIVE,
    DEFAULT_REPORT as DEFAULT_RELEASE_QA,
    ReleaseBundleError,
    verify_saved_release_bundle_qa,
)
from .submission_receipt import (
    EXPECTED_PREFLIGHT_CHECK_IDS,
    SubmissionReceiptError,
    verify_saved_submission_receipt_qa,
)
from .team_roster import TeamRosterError, verify_team_roster
from .training_video_review import (
    TrainingVideoReviewError,
    training_video_review_qa_issue,
    verify_training_video_review,
)


DEFAULT_PRIVATE_ROOT = ROOT / "outputs/submission"
DEFAULT_OUTPUT = DEFAULT_PRIVATE_ROOT / "submission_closeout_status.json"
STAGE_ORDER = (
    "TECHNICAL_RELEASE_BUNDLE",
    "TRAINING_VIDEO_FULL_WATCH",
    "TEAM_ELIGIBILITY_CONFIRMED",
    "OFFICIAL_RULES_VERIFIED",
    "FINAL_STAGE_REHEARSAL",
    "FINAL_RECORDING_REVIEW",
    "FINAL_CLEAN_PREFLIGHT",
    "SUBMISSION_UPLOAD",
    "PUBLIC_LINK_QA",
    "SUBMISSION_RECEIPT",
)
GATE_IDS = STAGE_ORDER[1:6]


class SubmissionCloseoutError(ValueError):
    """Raised when the closeout status cannot be built or trusted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubmissionCloseoutError(f"{label}无法读取") from exc
    if not isinstance(value, dict):
        raise SubmissionCloseoutError(f"{label}必须是JSON对象")
    return value


def _private_file_safe(path: Path, private_root: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(private_root.expanduser().resolve())
    except ValueError:
        return False
    return (
        path.is_file()
        and stat.S_IMODE(path.stat().st_mode) == 0o600
        and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    )


def _stage(
    stage_id: str,
    category: str,
    status: str,
    evidence_state: str,
    *,
    human_gate_id: str | None = None,
    depends_on: tuple[str, ...] = (),
    next_action: str | None = None,
    next_command: str | None = None,
) -> dict[str, Any]:
    return {
        "stage_id": stage_id,
        "category": category,
        "status": status,
        "evidence_state": evidence_state,
        "human_gate_id": human_gate_id,
        "depends_on": list(depends_on),
        "next_action": next_action,
        "next_command": next_command,
    }


def _technical_bundle_stage(
    root: Path,
    archive_path: Path,
    qa_path: Path,
) -> dict[str, Any]:
    exists = archive_path.exists() or qa_path.exists()
    if not exists:
        return _stage(
            "TECHNICAL_RELEASE_BUNDLE",
            "TECHNICAL",
            "READY",
            "ABSENT",
            next_action="生成并反向核验18项成果技术交付包",
            next_command="bash scripts/build_public_release_bundle.sh",
        )
    try:
        verify_saved_release_bundle_qa(
            qa_path,
            archive_path=archive_path,
            root=root,
            release_manifest_path=root / "output/submission/release_manifest_v1.json",
        )
    except (ReleaseBundleError, OSError, ContractValidationError):
        return _stage(
            "TECHNICAL_RELEASE_BUNDLE",
            "TECHNICAL",
            "NEEDS_REVIEW",
            "INVALID" if archive_path.exists() and qa_path.exists() else "PARTIAL",
            next_action="重新生成技术交付包并检查成果漂移",
            next_command="bash scripts/build_public_release_bundle.sh",
        )
    return _stage(
        "TECHNICAL_RELEASE_BUNDLE",
        "TECHNICAL",
        "COMPLETED",
        "VERIFIED",
    )


def _absent_or_partial(
    input_path: Path,
    qa_path: Path,
    *,
    category: str,
    init_action: str,
    init_command: str,
) -> dict[str, str] | None:
    if not input_path.exists() and not qa_path.exists():
        return {
            "status": "AWAITING_EXTERNAL" if category == "EXTERNAL" else "AWAITING_HUMAN",
            "evidence_state": "ABSENT",
            "next_action": init_action,
            "next_command": init_command,
        }
    if not input_path.exists() and qa_path.exists():
        return {
            "status": "NEEDS_REVIEW",
            "evidence_state": "INVALID",
            "next_action": "清理失去来源绑定的QA后重新开始",
            "next_command": init_command,
        }
    return None


def _probe_training_video(root: Path, private_root: Path) -> dict[str, str]:
    review = private_root / "training_video_review.json"
    qa = private_root / "training_video_review_qa.json"
    early = _absent_or_partial(
        review,
        qa,
        category="HUMAN",
        init_action="完整观看80秒成片并初始化私有审核表",
        init_command="bash scripts/check_training_video_review.sh --init",
    )
    if early:
        return early
    try:
        report = verify_training_video_review(
            review,
            manifest_path=root / "output/video/n31_training_video_manifest_v1.json",
            video_path=root / "output/video/n31_training_video_v1.mp4",
            private_root=private_root,
        )
    except (TrainingVideoReviewError, ContractValidationError, OSError):
        return {
            "status": "AWAITING_HUMAN",
            "evidence_state": "DRAFT",
            "next_action": "完成完整观看审核表中的全部人工检查",
            "next_command": "bash scripts/check_training_video_review.sh",
        }
    if not qa.exists():
        return {
            "status": "READY",
            "evidence_state": "INPUT_READY",
            "next_action": "运行成片观看机器检查生成安全QA",
            "next_command": "bash scripts/check_training_video_review.sh",
        }
    issue = training_video_review_qa_issue(
        qa,
        {
            "kind": "LOCAL_FILE",
            "locator": str(review),
            "sha256": report["review_sha256"],
            "size_bytes": report["review_bytes"],
        },
        manifest_path=root / "output/video/n31_training_video_manifest_v1.json",
        video_path=root / "output/video/n31_training_video_v1.mp4",
    )
    return (
        {
            "status": "READY_FOR_CONFIRMATION",
            "evidence_state": "MACHINE_READY",
            "next_action": "人工复核后显式确认完整观看门禁",
            "next_command": "bash scripts/manage_human_gates.sh status",
        }
        if issue is None
        else {
            "status": "NEEDS_REVIEW",
            "evidence_state": "INVALID",
            "next_action": "重新运行成片观看检查并处理QA漂移",
            "next_command": "bash scripts/check_training_video_review.sh",
        }
    )


def _probe_team_roster(root: Path, private_root: Path) -> dict[str, str]:
    roster = private_root / "team_roster.json"
    qa = private_root / "team_roster_qa.json"
    early = _absent_or_partial(
        roster,
        qa,
        category="HUMAN",
        init_action="初始化并填写2至5人私有团队名单",
        init_command="bash scripts/check_team_roster.sh --init",
    )
    if early:
        return early
    try:
        current = verify_team_roster(roster, private_root=private_root)
    except (TeamRosterError, ContractValidationError, OSError):
        return {
            "status": "AWAITING_HUMAN",
            "evidence_state": "DRAFT",
            "next_action": "完成成员资格声明和六类职责映射",
            "next_command": "bash scripts/check_team_roster.sh",
        }
    if not qa.exists():
        return {
            "status": "READY",
            "evidence_state": "INPUT_READY",
            "next_action": "运行团队名单机器检查生成安全QA",
            "next_command": "bash scripts/check_team_roster.sh",
        }
    try:
        if not _private_file_safe(qa, private_root):
            raise SubmissionCloseoutError("团队名单QA权限无效")
        saved = validate_document(_read_json(qa, "团队名单QA"), "team_roster_qa.schema.json")
        for key in current:
            if key != "checked_at" and saved[key] != current[key]:
                raise SubmissionCloseoutError("团队名单QA已漂移")
    except (SubmissionCloseoutError, ContractValidationError, KeyError):
        return {
            "status": "NEEDS_REVIEW",
            "evidence_state": "INVALID",
            "next_action": "重新运行团队名单检查并处理QA漂移",
            "next_command": "bash scripts/check_team_roster.sh",
        }
    return {
        "status": "READY_FOR_CONFIRMATION",
        "evidence_state": "MACHINE_READY",
        "next_action": "对照报名页后显式确认团队资格门禁",
        "next_command": "bash scripts/manage_human_gates.sh status",
    }


def _probe_official_rules(root: Path, private_root: Path) -> dict[str, str]:
    review = private_root / "official_rules_review.json"
    qa = private_root / "official_rules_review_qa.json"
    early = _absent_or_partial(
        review,
        qa,
        category="EXTERNAL",
        init_action="取得官方规则原文后初始化六项私有审核",
        init_command="bash scripts/check_official_rules_review.sh --init",
    )
    if early:
        return early
    try:
        report = verify_official_rules_review(
            review,
            public_snapshot_path=root / "config/official_rules_status.json",
            private_root=private_root,
        )
    except (OfficialRulesReviewError, ContractValidationError, OSError):
        return {
            "status": "AWAITING_EXTERNAL",
            "evidence_state": "DRAFT",
            "next_action": "绑定官方来源并完成六项规则审核",
            "next_command": "bash scripts/check_official_rules_review.sh",
        }
    if not qa.exists():
        return {
            "status": "READY",
            "evidence_state": "INPUT_READY",
            "next_action": "运行官方规则机器检查生成安全QA",
            "next_command": "bash scripts/check_official_rules_review.sh",
        }
    issue = official_rules_review_qa_issue(
        qa,
        {
            "kind": "LOCAL_FILE",
            "locator": str(review),
            "sha256": report["review_sha256"],
            "size_bytes": report["review_bytes"],
        },
        public_snapshot_path=root / "config/official_rules_status.json",
    )
    return (
        {
            "status": "READY_FOR_CONFIRMATION",
            "evidence_state": "MACHINE_READY",
            "next_action": "人工复核原文后显式确认官方规则门禁",
            "next_command": "bash scripts/manage_human_gates.sh status",
        }
        if issue is None
        else {
            "status": "NEEDS_REVIEW",
            "evidence_state": "INVALID",
            "next_action": "重新运行规则审核并处理来源或QA漂移",
            "next_command": "bash scripts/check_official_rules_review.sh",
        }
    )


def _probe_rehearsal(root: Path, private_root: Path) -> dict[str, str]:
    record = private_root / "final_stage_rehearsal.json"
    qa = private_root / "final_stage_rehearsal_qa.json"
    early = _absent_or_partial(
        record,
        qa,
        category="HUMAN",
        init_action="初始化并完成一次连续180秒真人彩排",
        init_command="bash scripts/check_final_rehearsal.sh --init",
    )
    if early:
        return early
    try:
        report = verify_final_rehearsal(
            record,
            runbook_path=root / "cases/n31/pitch_runbook.json",
            policy_path=root / "config/final_rehearsal_policy.json",
            private_root=private_root,
        )
    except (FinalRehearsalError, ContractValidationError, OSError):
        return {
            "status": "AWAITING_HUMAN",
            "evidence_state": "DRAFT",
            "next_action": "完成7段连续计时彩排和人工检查",
            "next_command": "bash scripts/check_final_rehearsal.sh",
        }
    if not qa.exists():
        return {
            "status": "READY",
            "evidence_state": "INPUT_READY",
            "next_action": "运行彩排机器检查生成安全QA",
            "next_command": "bash scripts/check_final_rehearsal.sh",
        }
    issue = final_rehearsal_qa_issue(
        qa,
        {
            "kind": "LOCAL_FILE",
            "locator": str(record),
            "sha256": report["record_sha256"],
            "size_bytes": report["record_bytes"],
        },
        runbook_path=root / "cases/n31/pitch_runbook.json",
        policy_path=root / "config/final_rehearsal_policy.json",
    )
    return (
        {
            "status": "READY_FOR_CONFIRMATION",
            "evidence_state": "MACHINE_READY",
            "next_action": "人工复核彩排后显式确认门禁",
            "next_command": "bash scripts/manage_human_gates.sh status",
        }
        if issue is None
        else {
            "status": "NEEDS_REVIEW",
            "evidence_state": "INVALID",
            "next_action": "重新运行彩排检查并处理QA漂移",
            "next_command": "bash scripts/check_final_rehearsal.sh",
        }
    )


def _probe_final_recording(root: Path, private_root: Path) -> dict[str, str]:
    recording = private_root / DEFAULT_RECORDING.name
    qa = private_root / "final_recording_qa.json"
    if not recording.exists() and not qa.exists():
        return {
            "status": "AWAITING_HUMAN",
            "evidence_state": "ABSENT",
            "next_action": "录制最终有声字幕视频并保存到固定私有文件名",
            "next_command": "bash scripts/check_final_recording.sh",
        }
    if not recording.exists() or not _private_file_safe(recording, private_root):
        return {
            "status": "NEEDS_REVIEW",
            "evidence_state": "INVALID",
            "next_action": "恢复固定文件名和700/600权限后重新检查录屏",
            "next_command": "bash scripts/check_final_recording.sh",
        }
    if not qa.exists():
        return {
            "status": "READY",
            "evidence_state": "INPUT_READY",
            "next_action": "运行最终录屏机器QA",
            "next_command": "bash scripts/check_final_recording.sh",
        }
    issue = final_recording_qa_issue(
        qa,
        {
            "kind": "LOCAL_FILE",
            "locator": str(recording),
            "sha256": _sha256(recording),
            "size_bytes": recording.stat().st_size,
        },
    )
    return (
        {
            "status": "READY_FOR_CONFIRMATION",
            "evidence_state": "MACHINE_READY",
            "next_action": "完整观看录屏后显式确认最终录屏门禁",
            "next_command": "bash scripts/manage_human_gates.sh status",
        }
        if issue is None
        else {
            "status": "NEEDS_REVIEW",
            "evidence_state": "INVALID",
            "next_action": "重新运行录屏QA并处理媒体或策略漂移",
            "next_command": "bash scripts/check_final_recording.sh",
        }
    )


GATE_PROBES: dict[str, tuple[str, Callable[[Path, Path], dict[str, str]]]] = {
    "TRAINING_VIDEO_FULL_WATCH": ("HUMAN", _probe_training_video),
    "TEAM_ELIGIBILITY_CONFIRMED": ("HUMAN", _probe_team_roster),
    "OFFICIAL_RULES_VERIFIED": ("EXTERNAL", _probe_official_rules),
    "FINAL_STAGE_REHEARSAL": ("HUMAN", _probe_rehearsal),
    "FINAL_RECORDING_REVIEW": ("HUMAN", _probe_final_recording),
}


def _gate_stages(
    root: Path,
    private_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    store = HumanGateStore(
        private_root / "human_gate_confirmations.json",
        runbook_path=root / "cases/n31/pitch_runbook.json",
        final_recording_qa_path=private_root / "final_recording_qa.json",
        final_rehearsal_qa_path=private_root / "final_stage_rehearsal_qa.json",
        team_roster_path=private_root / "team_roster.json",
        training_video_review_qa_path=private_root / "training_video_review_qa.json",
        training_video_manifest_path=root / "output/video/n31_training_video_manifest_v1.json",
        training_video_path=root / "output/video/n31_training_video_v1.mp4",
        official_rules_review_qa_path=private_root / "official_rules_review_qa.json",
        official_rules_snapshot_path=root / "config/official_rules_status.json",
    )
    try:
        audit = store.audit()
    except (HumanGateError, ContractValidationError, OSError):
        audit = {
            "store_state": "INVALID",
            "valid": False,
            "confirmed_gate_ids": [],
            "issues": ["AUDIT_FAILED"],
            "summary": {"passed": 0, "pending": 5, "total": 5},
        }
    confirmed = set(audit["confirmed_gate_ids"])
    stages: list[dict[str, Any]] = []
    for gate_id in GATE_IDS:
        category, probe = GATE_PROBES[gate_id]
        if gate_id in confirmed and audit["valid"]:
            stages.append(
                _stage(
                    gate_id,
                    category,
                    "COMPLETED",
                    "CONFIRMED",
                    human_gate_id=gate_id,
                    depends_on=("TECHNICAL_RELEASE_BUNDLE",),
                )
            )
            continue
        result = probe(root, private_root)
        if not audit["valid"]:
            result = {
                "status": "NEEDS_REVIEW",
                "evidence_state": "INVALID",
                "next_action": "检查并处理失效的人工门禁确认记录",
                "next_command": "bash scripts/manage_human_gates.sh status",
            }
        stages.append(
            _stage(
                gate_id,
                category,
                result["status"],
                result["evidence_state"],
                human_gate_id=gate_id,
                depends_on=("TECHNICAL_RELEASE_BUNDLE",),
                next_action=result["next_action"],
                next_command=result["next_command"],
            )
        )
    return stages, audit


def _git_state(root: Path) -> tuple[bool, str | None, bool | None]:
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return False, None, None
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return True, head, not bool(dirty)


def _preflight_stage(
    root: Path,
    private_root: Path,
    *,
    dependencies_complete: bool,
) -> dict[str, Any]:
    path = private_root / "submission_preflight_final.json"
    dependencies = tuple(GATE_IDS)
    if not path.exists():
        return _stage(
            "FINAL_CLEAN_PREFLIGHT",
            "TECHNICAL",
            "READY" if dependencies_complete else "WAITING_ON_DEPENDENCIES",
            "ABSENT",
            depends_on=dependencies,
            next_action="五项门禁确认后固定最终干净预检",
            next_command=(
                "bash scripts/check_submission.sh --output "
                "outputs/submission/submission_preflight_final.json"
            ),
        )
    try:
        if not _private_file_safe(path, private_root):
            raise SubmissionCloseoutError("最终预检权限无效")
        report = validate_document(
            _read_json(path, "最终预检"), "submission_preflight.schema.json"
        )
        check_ids = [item["check_id"] for item in report["automatic_checks"]]
        if (
            report["status"] != "READY_FOR_SUBMISSION"
            or report["source_worktree_clean"] is not True
            or report["pending_human_gates"]
            or tuple(check_ids) != EXPECTED_PREFLIGHT_CHECK_IDS
            or any(item["status"] != "PASSED" for item in report["automatic_checks"])
            or not dependencies_complete
        ):
            raise SubmissionCloseoutError("最终预检不是提交就绪状态")
        has_git, head, clean = _git_state(root)
        if not has_git or not clean or report["source_commit"] != head:
            raise SubmissionCloseoutError("最终预检不绑定当前干净提交")
    except (SubmissionCloseoutError, ContractValidationError, KeyError, OSError):
        return _stage(
            "FINAL_CLEAN_PREFLIGHT",
            "TECHNICAL",
            "NEEDS_REVIEW",
            "INVALID",
            depends_on=dependencies,
            next_action="重新运行最终干净预检并处理提交或门禁漂移",
            next_command="bash scripts/check_submission.sh",
        )
    return _stage(
        "FINAL_CLEAN_PREFLIGHT",
        "TECHNICAL",
        "COMPLETED",
        "VERIFIED",
        depends_on=dependencies,
    )


def _publication_state(private_root: Path) -> tuple[dict[str, Any], bool]:
    input_path = private_root / "publication_links.json"
    qa_path = private_root / "publication_links_qa.json"
    if not input_path.exists() and not qa_path.exists():
        return (
            _stage(
                "PUBLIC_LINK_QA",
                "SUBMISSION",
                "WAITING_ON_DEPENDENCIES",
                "ABSENT",
                depends_on=("SUBMISSION_UPLOAD",),
                next_action="上传后填写三个公开入口并运行匿名检查",
                next_command="bash scripts/check_publication_links.sh --init",
            ),
            False,
        )
    if not input_path.exists() or not _private_file_safe(input_path, private_root):
        return (
            _stage(
                "PUBLIC_LINK_QA",
                "SUBMISSION",
                "NEEDS_REVIEW",
                "INVALID",
                depends_on=("SUBMISSION_UPLOAD",),
                next_action="恢复安全的三个公开入口私有输入",
                next_command="bash scripts/check_publication_links.sh --init",
            ),
            False,
        )
    try:
        document = validate_document(
            _read_json(input_path, "公开链接输入"),
            "publication_links_input.schema.json",
        )
        input_ready = document["status"] == "READY_FOR_CHECK" and all(
            item["public_url"] for item in document["targets"]
        )
        target_ids = [item["target_id"] for item in document["targets"]]
        if set(target_ids) != set(EXPECTED_TARGETS) or len(set(target_ids)) != 3:
            raise SubmissionCloseoutError("公开入口集合不完整")
    except (SubmissionCloseoutError, ContractValidationError, KeyError):
        input_ready = False
    if not qa_path.exists():
        return (
            _stage(
                "PUBLIC_LINK_QA",
                "SUBMISSION",
                "AWAITING_HUMAN",
                "INPUT_READY" if input_ready else "DRAFT",
                depends_on=("SUBMISSION_UPLOAD",),
                next_action=(
                    "运行三个公开入口匿名检查"
                    if input_ready
                    else "填写作品页、代码仓库和最终录屏公开网址"
                ),
                next_command="bash scripts/check_publication_links.sh",
            ),
            input_ready,
        )
    try:
        if not input_ready or not _private_file_safe(qa_path, private_root):
            raise SubmissionCloseoutError("公开链接QA权限或输入无效")
        qa = validate_document(
            _read_json(qa_path, "公开链接QA"), "publication_links_qa.schema.json"
        )
        qa_ids = [item["target_id"] for item in qa["targets"]]
        if (
            qa["status"] != "PASSED"
            or qa["input_sha256"] != _sha256(input_path)
            or set(qa_ids) != set(EXPECTED_TARGETS)
            or len(set(qa_ids)) != 3
            or any(item["status"] != "PASSED" for item in qa["targets"])
        ):
            raise SubmissionCloseoutError("公开链接QA未通过或已漂移")
    except (SubmissionCloseoutError, ContractValidationError, KeyError, OSError):
        return (
            _stage(
                "PUBLIC_LINK_QA",
                "SUBMISSION",
                "NEEDS_REVIEW",
                "INVALID",
                depends_on=("SUBMISSION_UPLOAD",),
                next_action="重新运行公开链接匿名检查并处理QA漂移",
                next_command="bash scripts/check_publication_links.sh",
            ),
            input_ready,
        )
    return (
        _stage(
            "PUBLIC_LINK_QA",
            "SUBMISSION",
            "COMPLETED",
            "VERIFIED",
            depends_on=("SUBMISSION_UPLOAD",),
        ),
        True,
    )


def _receipt_stage(
    root: Path,
    private_root: Path,
    *,
    publication_complete: bool,
) -> dict[str, Any]:
    review = private_root / "submission_receipt_review.json"
    qa = private_root / "submission_receipt_qa.json"
    sources = private_root / "submission_receipt_sources"
    if not qa.exists():
        partial = review.exists() or sources.exists()
        return _stage(
            "SUBMISSION_RECEIPT",
            "SUBMISSION",
            "AWAITING_HUMAN" if publication_complete or partial else "WAITING_ON_DEPENDENCIES",
            "DRAFT" if partial else "ABSENT",
            depends_on=("PUBLIC_LINK_QA",),
            next_action="保存提交成功截图、复查作品页并完成私有回执QA",
            next_command="bash scripts/check_submission_receipt.sh --init",
        )
    try:
        verify_saved_submission_receipt_qa(
            qa,
            input_path=review,
            final_preflight_path=private_root / "submission_preflight_final.json",
            publication_input_path=private_root / "publication_links.json",
            publication_qa_path=private_root / "publication_links_qa.json",
            release_manifest_path=root / "output/submission/release_manifest_v1.json",
            private_root=private_root,
            root=root,
        )
    except (SubmissionReceiptError, ContractValidationError, OSError):
        return _stage(
            "SUBMISSION_RECEIPT",
            "SUBMISSION",
            "NEEDS_REVIEW",
            "INVALID",
            depends_on=("PUBLIC_LINK_QA",),
            next_action="重新检查回执、最终预检和公开链接绑定",
            next_command="bash scripts/check_submission_receipt.sh --verify-only",
        )
    return _stage(
        "SUBMISSION_RECEIPT",
        "SUBMISSION",
        "COMPLETED",
        "VERIFIED",
        depends_on=("PUBLIC_LINK_QA",),
    )


def _overall_status(stages: list[dict[str, Any]]) -> str:
    by_id = {item["stage_id"]: item for item in stages}
    if any(item["status"] == "NEEDS_REVIEW" for item in stages):
        return "NEEDS_REVIEW"
    if by_id["SUBMISSION_RECEIPT"]["status"] == "COMPLETED":
        return "READY_FOR_ARCHIVE"
    if by_id["PUBLIC_LINK_QA"]["status"] == "COMPLETED":
        return "SUBMISSION_RECEIPT_PENDING"
    if by_id["SUBMISSION_UPLOAD"]["status"] == "COMPLETED":
        return "PUBLIC_LINK_QA_PENDING"
    if by_id["FINAL_CLEAN_PREFLIGHT"]["status"] == "COMPLETED":
        return "READY_FOR_UPLOAD"
    if all(by_id[gate_id]["status"] == "COMPLETED" for gate_id in GATE_IDS):
        return "FINAL_PREFLIGHT_PENDING"
    if by_id["TECHNICAL_RELEASE_BUNDLE"]["status"] != "COMPLETED":
        return "TECHNICAL_PACKAGE_PENDING"
    return "TECHNICAL_READY_HUMAN_GATES_PENDING"


def _next_action(stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    needs_review = [item for item in stages if item["status"] == "NEEDS_REVIEW"]
    candidates = needs_review or [
        item
        for item in stages
        if item["status"] not in {"COMPLETED", "WAITING_ON_DEPENDENCIES"}
    ]
    if not candidates:
        candidates = [item for item in stages if item["status"] != "COMPLETED"]
    if not candidates:
        return None
    selected = candidates[0]
    return {
        "stage_id": selected["stage_id"],
        "action": selected["next_action"] or "复核当前收尾状态",
        "command": selected["next_command"],
    }


def build_submission_closeout_status(
    *,
    root: Path = ROOT,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
    release_archive: Path = DEFAULT_RELEASE_ARCHIVE,
    release_qa: Path = DEFAULT_RELEASE_QA,
    generated_at: str | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    private_root = private_root.expanduser().resolve()
    technical = _technical_bundle_stage(root, release_archive, release_qa)
    gates, audit = _gate_stages(root, private_root)
    dependencies_complete = technical["status"] == "COMPLETED" and all(
        item["status"] == "COMPLETED" for item in gates
    )
    preflight = _preflight_stage(
        root,
        private_root,
        dependencies_complete=dependencies_complete,
    )
    publication, publication_input_ready = _publication_state(private_root)
    preflight_complete = preflight["status"] == "COMPLETED"
    if not preflight_complete and publication["status"] == "COMPLETED":
        publication = _stage(
            "PUBLIC_LINK_QA",
            "SUBMISSION",
            "NEEDS_REVIEW",
            "INVALID",
            depends_on=("SUBMISSION_UPLOAD",),
            next_action="最终预检缺失或过期，需按提交顺序重新核验公开链接",
            next_command="bash scripts/check_submission.sh",
        )
    elif not preflight_complete and publication_input_ready:
        publication["status"] = "WAITING_ON_DEPENDENCIES"
        publication["next_action"] = "先关闭五项门禁并完成最终干净预检"
        publication["next_command"] = "bash scripts/check_submission.sh"
    upload_complete = preflight_complete and (
        publication_input_ready or publication["status"] == "COMPLETED"
    )
    upload = _stage(
        "SUBMISSION_UPLOAD",
        "SUBMISSION",
        (
            "COMPLETED"
            if upload_complete
            else "AWAITING_HUMAN"
            if preflight["status"] == "COMPLETED"
            else "WAITING_ON_DEPENDENCIES"
        ),
        "VERIFIED" if upload_complete else "NOT_APPLICABLE",
        depends_on=("FINAL_CLEAN_PREFLIGHT",),
        next_action="按官方字段上传冻结成果并保存提交状态",
        next_command=None,
    )
    if upload_complete and publication["status"] == "WAITING_ON_DEPENDENCIES":
        publication["status"] = "AWAITING_HUMAN"
    receipt = _receipt_stage(
        root,
        private_root,
        publication_complete=publication["status"] == "COMPLETED",
    )
    stages = [technical, *gates, preflight, upload, publication, receipt]
    if tuple(item["stage_id"] for item in stages) != STAGE_ORDER:
        raise SubmissionCloseoutError("收尾阶段顺序与冻结定义不一致")
    status = _overall_status(stages)
    document = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "SUBMISSION_CLOSEOUT_STATUS",
        "generated_at": generated_at or _now(),
        "status": status,
        "implementation_goal_blocked": False,
        "formal_submission_ready": preflight["status"] == "COMPLETED",
        "submission_archived": receipt["status"] == "COMPLETED",
        "stage_count": len(stages),
        "completed_stage_count": sum(
            item["status"] == "COMPLETED" for item in stages
        ),
        "human_gate_summary": {
            "confirmed": audit["summary"]["passed"],
            "pending": audit["summary"]["pending"],
            "total": audit["summary"]["total"],
            "store_state": audit["store_state"],
            "valid": audit["valid"],
            "issue_count": len(audit["issues"]),
        },
        "next_action": _next_action(stages),
        "stages": stages,
        "data_policy": {
            "private_local_state": True,
            "contains_credentials": False,
            "contains_personal_data": False,
            "contains_urls": False,
            "contains_evidence_paths": False,
            "contains_private_notes": False,
            "contains_raw_media": False,
            "automatic_human_confirmations": 0,
            "network_requests": 0,
        },
    }
    return validate_document(document, "submission_closeout_status.schema.json")


def write_closeout_status(
    document: dict[str, Any],
    destination: Path = DEFAULT_OUTPUT,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    validate_document(document, "submission_closeout_status.schema.json")
    destination = destination.expanduser().resolve()
    private_root = private_root.expanduser().resolve()
    if destination.parent != private_root:
        raise SubmissionCloseoutError("收尾状态报告必须直接保存在私有提交目录")
    existed = private_root.exists()
    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not existed:
        os.chmod(private_root, 0o700)
    elif stat.S_IMODE(private_root.stat().st_mode) != 0o700:
        raise SubmissionCloseoutError("私有提交目录权限必须为0700")
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


def verify_saved_closeout_status(
    output_path: Path = DEFAULT_OUTPUT,
    *,
    root: Path = ROOT,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
    release_archive: Path = DEFAULT_RELEASE_ARCHIVE,
    release_qa: Path = DEFAULT_RELEASE_QA,
) -> dict[str, Any]:
    output_path = output_path.expanduser().resolve()
    if not _private_file_safe(output_path, private_root):
        raise SubmissionCloseoutError("收尾状态报告缺失或权限无效")
    try:
        saved = validate_document(
            _read_json(output_path, "收尾状态报告"),
            "submission_closeout_status.schema.json",
        )
    except ContractValidationError as exc:
        raise SubmissionCloseoutError("收尾状态报告不符合严格Schema") from exc
    current = build_submission_closeout_status(
        root=root,
        private_root=private_root,
        release_archive=release_archive,
        release_qa=release_qa,
        generated_at=saved["generated_at"],
    )
    if saved != current:
        raise SubmissionCloseoutError("收尾状态报告与当前门禁或提交状态不一致")
    return saved


def _exit_code(status: str) -> int:
    if status == "READY_FOR_ARCHIVE":
        return 0
    if status == "NEEDS_REVIEW":
        return 1
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.verify_only:
            document = verify_saved_closeout_status(args.output)
        else:
            document = build_submission_closeout_status()
            write_closeout_status(document, args.output)
            verify_saved_closeout_status(args.output)
    except (
        SubmissionCloseoutError,
        ContractValidationError,
        HumanGateError,
        OSError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": "提交收尾状态生成或验证失败",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    next_action = document["next_action"]
    print(
        json.dumps(
            {
                "status": document["status"],
                "implementation_goal_blocked": document[
                    "implementation_goal_blocked"
                ],
                "completed_stage_count": document["completed_stage_count"],
                "stage_count": document["stage_count"],
                "human_gates_confirmed": document["human_gate_summary"]["confirmed"],
                "human_gates_pending": document["human_gate_summary"]["pending"],
                "next_stage_id": next_action["stage_id"] if next_action else None,
                "next_command": next_action["command"] if next_action else None,
                "automatic_human_confirmations": 0,
                "network_requests": 0,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return _exit_code(document["status"])


if __name__ == "__main__":
    raise SystemExit(main())
