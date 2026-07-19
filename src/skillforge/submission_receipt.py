"""Create and validate the private post-submission receipt evidence chain."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import ContractValidationError, validate_document
from .demo import ROOT
from .publication_links import EXPECTED_TARGETS
from .release_manifest import ReleaseManifestError, verify_release_manifest


DEFAULT_PRIVATE_ROOT = ROOT / "outputs/submission"
DEFAULT_INPUT = DEFAULT_PRIVATE_ROOT / "submission_receipt_review.json"
DEFAULT_REPORT = DEFAULT_PRIVATE_ROOT / "submission_receipt_qa.json"
DEFAULT_FINAL_PREFLIGHT = DEFAULT_PRIVATE_ROOT / "submission_preflight_final.json"
DEFAULT_PUBLICATION_INPUT = DEFAULT_PRIVATE_ROOT / "publication_links.json"
DEFAULT_PUBLICATION_QA = DEFAULT_PRIVATE_ROOT / "publication_links_qa.json"
DEFAULT_RELEASE_MANIFEST = ROOT / "output/submission/release_manifest_v1.json"
ALLOWED_RECEIPT_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf"}
EXPECTED_PREFLIGHT_CHECK_IDS = (
    "PROJECT_IDENTITY",
    "REQUIRED_DOCUMENTS",
    "SUBMISSION_ARTICLE",
    "OFFICIAL_RULES_STATUS",
    "OFFICIAL_RULES_REVIEW_PRIVATE_STATE",
    "RELEASE_FREEZE_MANIFEST",
    "PROJECT_BOARD_STATUS",
    "TEAM_ROSTER_PRIVATE_STATE",
    "SUBMISSION_FORM_PACKET_PRIVATE_STATE",
    "TRAINING_VIDEO_REVIEW_PRIVATE_STATE",
    "FINAL_REHEARSAL_PRIVATE_STATE",
    "FINAL_RECORDING_REVIEW_PRIVATE_STATE",
    "HUMAN_GATE_CONFIRMATIONS",
    "PITCH_PACKAGE",
    "PUBLIC_ARTIFACT_BOUNDARY",
    "GIT_WORKTREE",
    "TRACKED_SENSITIVE_PATHS",
    "ENV_AND_SECRET_SCAN",
    "AUTOMATED_TESTS",
)


class SubmissionReceiptError(ValueError):
    """Raised when post-submission evidence is incomplete, stale or unsafe."""


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


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SubmissionReceiptError("提交回执时间必须包含时区")
    return parsed.astimezone(timezone.utc)


def _inside(path: Path, root: Path = DEFAULT_PRIVATE_ROOT) -> Path:
    resolved = path.expanduser().resolve()
    root = root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise SubmissionReceiptError("提交回执记录和依据必须保存在私有提交目录") from exc
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SubmissionReceiptError(f"{label}无法读取或不是合法JSON") from exc
    if not isinstance(value, dict):
        raise SubmissionReceiptError(f"{label}必须是JSON对象")
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
        raise SubmissionReceiptError("提交回执私有目录权限必须为0700")
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


def _private_json(
    path: Path,
    label: str,
    *,
    private_root: Path,
) -> tuple[dict[str, Any], str, int]:
    path = _inside(path, private_root)
    if not path.is_file():
        raise SubmissionReceiptError(f"{label}缺失")
    if (
        stat.S_IMODE(path.parent.stat().st_mode) != 0o700
        or stat.S_IMODE(path.stat().st_mode) != 0o600
    ):
        raise SubmissionReceiptError(f"{label}权限必须为目录0700、文件0600")
    return _read_json(path, label), _sha256(path), path.stat().st_size


def initialize_submission_receipt_review(
    destination: Path = DEFAULT_INPUT,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    destination = _inside(destination, private_root)
    if destination.exists():
        raise SubmissionReceiptError("提交回执审核记录已存在；初始化不会覆盖已有内容")
    document = {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": _now(),
        "status": "PENDING_INPUT",
        "submitted_at": None,
        "reviewed_at": None,
        "platform": "NVIDIA_DGX_SPARK_HACKATHON_2",
        "submission_reference": "",
        "receipt_source": None,
        "checks": {
            "submission_success_visible": False,
            "project_identity_matches": False,
            "submission_reference_recorded": False,
            "final_version_confirmed": False,
            "project_page_reopened": False,
            "public_links_rechecked": False,
            "receipt_contains_no_credentials": False,
        },
        "notes": "",
        "data_policy": {
            "private_local_state": True,
            "contains_submission_reference": True,
            "contains_receipt_content": False,
            "contains_personal_data": True,
            "contains_credentials": False,
            "git_tracked": False,
        },
    }
    return _write_private_json(
        validate_document(document, "submission_receipt_review.schema.json"),
        destination,
        private_root=private_root,
    )


def _load_review(
    review_path: Path,
    *,
    private_root: Path,
) -> tuple[dict[str, Any], str, int]:
    document, digest, size = _private_json(
        review_path,
        "提交回执审核记录",
        private_root=private_root,
    )
    try:
        document = validate_document(
            document,
            "submission_receipt_review.schema.json",
        )
    except ContractValidationError as exc:
        raise SubmissionReceiptError("提交回执审核记录不符合严格Schema") from exc
    return document, digest, size


def attach_receipt_source(
    source_path: Path,
    review_path: Path = DEFAULT_INPUT,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    review_path = _inside(review_path, private_root)
    document, _, _ = _load_review(review_path, private_root=private_root)
    if document["receipt_source"] is not None:
        raise SubmissionReceiptError("提交回执记录已有截图或PDF；不会自动替换")
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file() or source_path.stat().st_size < 1:
        raise SubmissionReceiptError("提交成功截图或PDF不存在或为空")
    suffix = source_path.suffix.lower()
    if suffix not in ALLOWED_RECEIPT_SUFFIXES:
        raise SubmissionReceiptError("提交回执只允许PNG、JPEG或PDF")

    source_dir = _inside(private_root / "submission_receipt_sources", private_root)
    source_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(source_dir, 0o700)
    destination = source_dir / f"submission_receipt{suffix}"
    try:
        with source_path.open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        os.chmod(destination, 0o600)
        document["receipt_source"] = {
            "kind": "PDF" if suffix == ".pdf" else "IMAGE",
            "relative_path": destination.relative_to(private_root.resolve()).as_posix(),
            "sha256": _sha256(destination),
            "bytes": destination.stat().st_size,
        }
        document["updated_at"] = _now()
        _write_private_json(
            validate_document(document, "submission_receipt_review.schema.json"),
            review_path,
            private_root=private_root,
        )
    except Exception:
        destination.unlink(missing_ok=True)
        try:
            source_dir.rmdir()
        except OSError:
            pass
        raise
    return review_path


def _receipt_source_summary(
    source: dict[str, Any],
    *,
    private_root: Path,
) -> dict[str, Any]:
    path = _inside(private_root / source["relative_path"], private_root)
    expected_parent = private_root.resolve() / "submission_receipt_sources"
    if path.parent != expected_parent or not path.is_file():
        raise SubmissionReceiptError("提交回执截图或PDF缺失或位置无效")
    if (
        stat.S_IMODE(path.parent.stat().st_mode) != 0o700
        or stat.S_IMODE(path.stat().st_mode) != 0o600
    ):
        raise SubmissionReceiptError("提交回执来源权限必须为目录0700、文件0600")
    digest = _sha256(path)
    size = path.stat().st_size
    if digest != source["sha256"] or size != source["bytes"]:
        raise SubmissionReceiptError("提交回执截图或PDF内容已变化")
    return {
        "kind": source["kind"],
        "locator_sha256": _sha256_text(source["relative_path"]),
        "content_sha256": digest,
        "bytes": size,
    }


def _load_basis(
    *,
    private_root: Path,
    final_preflight_path: Path,
    publication_input_path: Path,
    publication_qa_path: Path,
    release_manifest_path: Path,
    root: Path,
) -> dict[str, Any]:
    preflight, preflight_sha256, _ = _private_json(
        final_preflight_path,
        "最终干净预检报告",
        private_root=private_root,
    )
    publication_input, publication_input_sha256, _ = _private_json(
        publication_input_path,
        "公开链接私有输入",
        private_root=private_root,
    )
    publication_qa, publication_qa_sha256, _ = _private_json(
        publication_qa_path,
        "公开链接QA报告",
        private_root=private_root,
    )
    try:
        preflight = validate_document(preflight, "submission_preflight.schema.json")
        publication_input = validate_document(
            publication_input,
            "publication_links_input.schema.json",
        )
        publication_qa = validate_document(
            publication_qa,
            "publication_links_qa.schema.json",
        )
        manifest = verify_release_manifest(
            release_manifest_path,
            root=root,
            config_path=root / "config/release_roles.json",
            runbook_path=root / "cases/n31/pitch_runbook.json",
        )
    except (ContractValidationError, ReleaseManifestError) as exc:
        raise SubmissionReceiptError("提交回执所需冻结依据无效") from exc

    input_ids = tuple(item["target_id"] for item in publication_input["targets"])
    qa_ids = tuple(item["target_id"] for item in publication_qa["targets"])
    expected_ids = tuple(EXPECTED_TARGETS)
    check_ids = [item["check_id"] for item in preflight["automatic_checks"]]
    return {
        "preflight": preflight,
        "preflight_sha256": preflight_sha256,
        "preflight_check_ids_exact": tuple(check_ids) == EXPECTED_PREFLIGHT_CHECK_IDS,
        "publication_input_sha256": publication_input_sha256,
        "publication_input_targets_exact": input_ids == expected_ids,
        "publication_qa": publication_qa,
        "publication_qa_sha256": publication_qa_sha256,
        "publication_qa_targets_exact": qa_ids == expected_ids,
        "manifest": manifest,
        "manifest_sha256": _sha256(release_manifest_path.expanduser().resolve()),
    }


def verify_submission_receipt_review_document(
    document: dict[str, Any],
    *,
    review_sha256: str,
    review_bytes: int,
    basis: dict[str, Any],
    private_root: Path,
) -> dict[str, Any]:
    try:
        validate_document(document, "submission_receipt_review.schema.json")
    except ContractValidationError as exc:
        raise SubmissionReceiptError("提交回执审核记录不符合严格Schema") from exc
    if document["status"] != "READY_FOR_CHECK":
        raise SubmissionReceiptError("提交回执审核记录尚未填写完成")
    source = document["receipt_source"]
    if source is None:
        raise SubmissionReceiptError("提交回执审核记录未绑定成功截图或PDF")
    source_summary = _receipt_source_summary(source, private_root=private_root)

    preflight = basis["preflight"]
    publication_qa = basis["publication_qa"]
    submitted_at = _timestamp(document["submitted_at"])
    reviewed_at = _timestamp(document["reviewed_at"])
    preflight_at = _timestamp(preflight["generated_at"])
    links_checked_at = _timestamp(publication_qa["checked_at"])
    final_preflight_ready = (
        preflight["status"] == "READY_FOR_SUBMISSION"
        and preflight["source_worktree_clean"] is True
        and isinstance(preflight["source_commit"], str)
        and isinstance(preflight["source_branch"], str)
        and not preflight["pending_human_gates"]
        and basis["preflight_check_ids_exact"]
        and all(
            item["status"] == "PASSED" for item in preflight["automatic_checks"]
        )
    )
    publication_links_passed = (
        publication_qa["status"] == "PASSED"
        and basis["publication_input_targets_exact"]
        and basis["publication_qa_targets_exact"]
        and all(item["status"] == "PASSED" for item in publication_qa["targets"])
    )
    checks = {
        "receipt_source_current": True,
        "final_preflight_schema_valid": True,
        "final_preflight_ready": final_preflight_ready,
        "final_preflight_precedes_submission": preflight_at <= submitted_at,
        "release_manifest_current": True,
        "publication_links_qa_schema_valid": True,
        "publication_links_qa_passed": publication_links_passed,
        "publication_links_input_current": (
            publication_qa["input_sha256"] == basis["publication_input_sha256"]
        ),
        "publication_links_checked_after_submission": links_checked_at >= submitted_at,
        **document["checks"],
    }
    if reviewed_at < submitted_at:
        raise SubmissionReceiptError("提交回执复核时间不能早于提交时间")
    if not document["submission_reference"].strip():
        raise SubmissionReceiptError("提交编号或回执标识不能为空")
    if not all(checks.values()):
        failed = ",".join(key for key, value in checks.items() if not value)
        raise SubmissionReceiptError(f"提交回执关闭检查未通过：{failed}")

    manifest = basis["manifest"]
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "SUBMISSION_RECEIPT_QA",
        "checked_at": _now(),
        "status": "READY_FOR_ARCHIVE",
        "review_sha256": review_sha256,
        "review_bytes": review_bytes,
        "submission_reference_sha256": _sha256_text(
            document["submission_reference"].strip()
        ),
        "receipt_source": source_summary,
        "final_preflight": {
            "sha256": basis["preflight_sha256"],
            "generated_at": preflight["generated_at"],
            "source_commit": preflight["source_commit"],
            "source_branch": preflight["source_branch"],
            "automatic_check_count": len(preflight["automatic_checks"]),
        },
        "release_manifest": {
            "sha256": basis["manifest_sha256"],
            "technical_freeze_digest": manifest["technical_freeze_digest"],
            "artifact_count": manifest["artifact_count"],
        },
        "publication_links": {
            "input_sha256": basis["publication_input_sha256"],
            "qa_sha256": basis["publication_qa_sha256"],
            "checked_at": publication_qa["checked_at"],
            "target_count": publication_qa["target_count"],
        },
        "checks": checks,
        "data_policy": {
            "private_local_state": True,
            "contains_receipt_content": False,
            "contains_submission_reference": False,
            "contains_urls": False,
            "contains_personal_data": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "human_confirmation_generated": False,
        },
    }
    return validate_document(report, "submission_receipt_qa.schema.json")


def verify_submission_receipt_review(
    input_path: Path = DEFAULT_INPUT,
    *,
    final_preflight_path: Path = DEFAULT_FINAL_PREFLIGHT,
    publication_input_path: Path = DEFAULT_PUBLICATION_INPUT,
    publication_qa_path: Path = DEFAULT_PUBLICATION_QA,
    release_manifest_path: Path = DEFAULT_RELEASE_MANIFEST,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
    root: Path = ROOT,
) -> dict[str, Any]:
    input_path = _inside(input_path, private_root)
    document, review_sha256, review_bytes = _load_review(
        input_path,
        private_root=private_root,
    )
    basis = _load_basis(
        private_root=private_root,
        final_preflight_path=final_preflight_path,
        publication_input_path=publication_input_path,
        publication_qa_path=publication_qa_path,
        release_manifest_path=release_manifest_path,
        root=root.expanduser().resolve(),
    )
    return verify_submission_receipt_review_document(
        document,
        review_sha256=review_sha256,
        review_bytes=review_bytes,
        basis=basis,
        private_root=private_root.expanduser().resolve(),
    )


def verify_saved_submission_receipt_qa(
    report_path: Path = DEFAULT_REPORT,
    *,
    input_path: Path = DEFAULT_INPUT,
    final_preflight_path: Path = DEFAULT_FINAL_PREFLIGHT,
    publication_input_path: Path = DEFAULT_PUBLICATION_INPUT,
    publication_qa_path: Path = DEFAULT_PUBLICATION_QA,
    release_manifest_path: Path = DEFAULT_RELEASE_MANIFEST,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
    root: Path = ROOT,
) -> dict[str, Any]:
    saved, _, _ = _private_json(
        report_path,
        "提交回执QA报告",
        private_root=private_root,
    )
    try:
        saved = validate_document(saved, "submission_receipt_qa.schema.json")
    except ContractValidationError as exc:
        raise SubmissionReceiptError("提交回执QA报告不符合严格Schema") from exc
    current = verify_submission_receipt_review(
        input_path,
        final_preflight_path=final_preflight_path,
        publication_input_path=publication_input_path,
        publication_qa_path=publication_qa_path,
        release_manifest_path=release_manifest_path,
        private_root=private_root,
        root=root,
    )
    comparable_keys = tuple(key for key in current if key != "checked_at")
    if any(saved[key] != current[key] for key in comparable_keys):
        raise SubmissionReceiptError("提交回执QA与当前审核、预检或发布状态不一致")
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--final-preflight", type=Path, default=DEFAULT_FINAL_PREFLIGHT)
    parser.add_argument("--publication-input", type=Path, default=DEFAULT_PUBLICATION_INPUT)
    parser.add_argument("--publication-qa", type=Path, default=DEFAULT_PUBLICATION_QA)
    parser.add_argument("--release-manifest", type=Path, default=DEFAULT_RELEASE_MANIFEST)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--init", action="store_true")
    action.add_argument("--attach-receipt", type=Path)
    action.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.init:
            initialize_submission_receipt_review(args.input)
            result = {"status": "PENDING_INPUT"}
        elif args.attach_receipt is not None:
            attach_receipt_source(args.attach_receipt, args.input)
            result = {"status": "RECEIPT_ATTACHED"}
        elif args.verify_only:
            report = verify_saved_submission_receipt_qa(
                args.output,
                input_path=args.input,
                final_preflight_path=args.final_preflight,
                publication_input_path=args.publication_input,
                publication_qa_path=args.publication_qa,
                release_manifest_path=args.release_manifest,
            )
            result = {
                "status": report["status"],
                "automatic_check_count": report["final_preflight"][
                    "automatic_check_count"
                ],
                "publication_target_count": report["publication_links"][
                    "target_count"
                ],
            }
        else:
            report = verify_submission_receipt_review(
                args.input,
                final_preflight_path=args.final_preflight,
                publication_input_path=args.publication_input,
                publication_qa_path=args.publication_qa,
                release_manifest_path=args.release_manifest,
            )
            _write_private_json(report, args.output)
            result = {
                "status": report["status"],
                "automatic_check_count": report["final_preflight"][
                    "automatic_check_count"
                ],
                "publication_target_count": report["publication_links"][
                    "target_count"
                ],
            }
    except (ContractValidationError, OSError, SubmissionReceiptError) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": str(exc)
                    if isinstance(exc, SubmissionReceiptError)
                    else "提交回执验证失败",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
