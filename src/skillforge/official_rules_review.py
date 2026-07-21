"""Create and validate the private official-rules review evidence chain."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .cli_hints import print_error_hints
from .contracts import ContractValidationError, validate_document
from .demo import ROOT


DEFAULT_PUBLIC_SNAPSHOT = ROOT / "config/official_rules_status.json"
DEFAULT_PRIVATE_ROOT = ROOT / "outputs/submission"
DEFAULT_INPUT = DEFAULT_PRIVATE_ROOT / "official_rules_review.json"
DEFAULT_REPORT = DEFAULT_PRIVATE_ROOT / "official_rules_review_qa.json"
REQUIREMENT_IDS = (
    "SCORING_WEIGHTS",
    "SUBMISSION_FIELDS",
    "VIDEO_REQUIREMENTS",
    "EXTERNAL_API_POLICY",
    "OPEN_SOURCE_POLICY",
    "ON_SITE_RUNTIME_REQUIREMENT",
)
UNRESOLVED_REQUIREMENT_IDS = (
    "VIDEO_REQUIREMENTS",
    "EXTERNAL_API_POLICY",
    "ON_SITE_RUNTIME_REQUIREMENT",
)
ALLOWED_SOURCE_SUFFIXES = {
    ".pdf",
    ".html",
    ".htm",
    ".txt",
    ".md",
    ".png",
    ".jpg",
    ".jpeg",
    ".pptx",
}


class OfficialRulesReviewError(ValueError):
    """Raised when the private rule review or its source cannot be trusted."""


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


def _inside(path: Path, root: Path = DEFAULT_PRIVATE_ROOT) -> Path:
    resolved = path.expanduser().resolve()
    root = root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise OfficialRulesReviewError("规则审核记录和来源必须保存在私有提交目录") from exc
    return resolved


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OfficialRulesReviewError(f"{label}无法读取或不是合法JSON") from exc
    if not isinstance(value, dict):
        raise OfficialRulesReviewError(f"{label}必须是JSON对象")
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
        raise OfficialRulesReviewError("规则审核私有目录权限必须为0700")
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


def _safe_https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise OfficialRulesReviewError(
            "官方来源网址必须是无账号、查询参数和片段的HTTPS地址"
        )
    return value


def load_public_snapshot(
    path: Path = DEFAULT_PUBLIC_SNAPSHOT,
) -> tuple[dict[str, Any], str]:
    path = path.expanduser().resolve()
    try:
        document = validate_document(
            _read_json(path, "公开规则核验快照"),
            "official_rules_status.schema.json",
        )
    except (ContractValidationError, OfficialRulesReviewError) as exc:
        raise OfficialRulesReviewError("公开规则核验快照无效") from exc
    if (
        document["verification_status"] != "OFFICIAL_DETAIL_REQUIRED"
        or tuple(document["unresolved_requirements"]) != UNRESOLVED_REQUIREMENT_IDS
        or document["public_access_audit"]["official_detail_obtained"] is not False
    ):
        raise OfficialRulesReviewError("公开规则核验快照与当前六项待核要求不一致")
    return document, _sha256(path)


def initialize_official_rules_review(
    destination: Path = DEFAULT_INPUT,
    *,
    public_snapshot_path: Path = DEFAULT_PUBLIC_SNAPSHOT,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    destination = _inside(destination, private_root)
    if destination.exists():
        raise OfficialRulesReviewError("规则审核记录已存在；初始化不会覆盖已有内容")
    load_public_snapshot(public_snapshot_path)
    document = {
        "version": 1,
        "event_id": "nvidia-dgx-spark-hackathon-2",
        "updated_at": _now(),
        "status": "PENDING_INPUT",
        "reviewed_at": None,
        "source": None,
        "requirements": [
            {
                "requirement_id": requirement_id,
                "finding": "",
                "source_reference": "",
                "confirmed": False,
            }
            for requirement_id in REQUIREMENT_IDS
        ],
        "checks": {
            "official_source_confirmed": False,
            "full_material_read": False,
            "six_requirements_reviewed": False,
            "submission_plan_updated": False,
            "conflicts_resolved": False,
        },
        "notes": "",
        "data_policy": {
            "private_local_state": True,
            "contains_rule_details": True,
            "contains_credentials": False,
            "git_tracked": False,
        },
    }
    return _write_private_json(
        validate_document(document, "official_rules_review.schema.json"),
        destination,
        private_root=private_root,
    )


def _load_editable_review(
    review_path: Path,
    *,
    private_root: Path,
) -> dict[str, Any]:
    review_path = _inside(review_path, private_root)
    if not review_path.is_file():
        raise OfficialRulesReviewError("规则审核记录不存在；请先使用--init")
    if (
        stat.S_IMODE(review_path.parent.stat().st_mode) != 0o700
        or stat.S_IMODE(review_path.stat().st_mode) != 0o600
    ):
        raise OfficialRulesReviewError("规则审核记录权限必须为目录0700、文件0600")
    try:
        return validate_document(
            _read_json(review_path, "规则审核记录"),
            "official_rules_review.schema.json",
        )
    except ContractValidationError as exc:
        raise OfficialRulesReviewError("规则审核记录不符合严格Schema") from exc


def attach_local_source(
    source_path: Path,
    review_path: Path = DEFAULT_INPUT,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    review_path = _inside(review_path, private_root)
    document = _load_editable_review(review_path, private_root=private_root)
    if document["source"] is not None:
        raise OfficialRulesReviewError("规则审核记录已有来源；不会自动替换")
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file() or source_path.stat().st_size < 1:
        raise OfficialRulesReviewError("官方规则来源文件不存在或为空")
    suffix = source_path.suffix.lower()
    if suffix not in ALLOWED_SOURCE_SUFFIXES:
        raise OfficialRulesReviewError("官方规则来源文件类型不在允许范围")

    source_dir = _inside(private_root / "official_rules_sources", private_root)
    source_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(source_dir, 0o700)
    destination = source_dir / f"official_rules_source{suffix}"
    try:
        with source_path.open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        os.chmod(destination, 0o600)
        document["source"] = {
            "kind": "LOCAL_FILE",
            "relative_path": destination.relative_to(private_root.resolve()).as_posix(),
            "sha256": _sha256(destination),
            "bytes": destination.stat().st_size,
        }
        document["updated_at"] = _now()
        _write_private_json(
            validate_document(document, "official_rules_review.schema.json"),
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


def attach_source_url(
    source_url: str,
    review_path: Path = DEFAULT_INPUT,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    review_path = _inside(review_path, private_root)
    document = _load_editable_review(review_path, private_root=private_root)
    if document["source"] is not None:
        raise OfficialRulesReviewError("规则审核记录已有来源；不会自动替换")
    source_url = _safe_https_url(source_url)
    document["source"] = {
        "kind": "HTTPS_URL",
        "url": source_url,
        "url_sha256": _sha256_text(source_url),
    }
    document["updated_at"] = _now()
    _write_private_json(
        validate_document(document, "official_rules_review.schema.json"),
        review_path,
        private_root=private_root,
    )
    return review_path


def _source_summary(
    source: dict[str, Any],
    *,
    private_root: Path,
) -> dict[str, Any]:
    if source["kind"] == "HTTPS_URL":
        url = _safe_https_url(source["url"])
        if source["url_sha256"] != _sha256_text(url):
            raise OfficialRulesReviewError("官方规则网址绑定哈希不一致")
        return {
            "kind": "HTTPS_URL",
            "locator_sha256": _sha256_text(url),
            "content_sha256": None,
            "bytes": None,
        }

    path = _inside(private_root / source["relative_path"], private_root)
    expected_parent = private_root.resolve() / "official_rules_sources"
    if path.parent != expected_parent or not path.is_file():
        raise OfficialRulesReviewError("本地官方规则来源缺失或位置无效")
    if (
        stat.S_IMODE(path.parent.stat().st_mode) != 0o700
        or stat.S_IMODE(path.stat().st_mode) != 0o600
    ):
        raise OfficialRulesReviewError("本地官方规则来源权限必须为目录0700、文件0600")
    size = path.stat().st_size
    digest = _sha256(path)
    if size != source["bytes"] or digest != source["sha256"]:
        raise OfficialRulesReviewError("本地官方规则来源内容已变化")
    return {
        "kind": "LOCAL_FILE",
        "locator_sha256": _sha256_text(source["relative_path"]),
        "content_sha256": digest,
        "bytes": size,
    }


def verify_official_rules_review_document(
    document: dict[str, Any],
    *,
    review_sha256: str,
    review_bytes: int,
    public_snapshot_sha256: str,
    private_root: Path,
) -> dict[str, Any]:
    try:
        validate_document(document, "official_rules_review.schema.json")
    except ContractValidationError as exc:
        raise OfficialRulesReviewError("规则审核记录不符合严格Schema") from exc
    if document["status"] != "READY_FOR_CHECK":
        raise OfficialRulesReviewError("规则审核记录尚未填写完成")

    requirements = document["requirements"]
    requirement_ids = tuple(item["requirement_id"] for item in requirements)
    checks = {
        "public_snapshot_valid": True,
        "source_current": True,
        "requirement_set_exact": requirement_ids == REQUIREMENT_IDS,
        "all_findings_nonblank": all(item["finding"].strip() for item in requirements),
        "all_source_references_nonblank": all(
            item["source_reference"].strip() for item in requirements
        ),
        "all_requirements_confirmed": all(item["confirmed"] for item in requirements),
        **document["checks"],
    }
    if not all(checks.values()):
        failed = ",".join(key for key, value in checks.items() if not value)
        raise OfficialRulesReviewError(f"官方规则六项核对未通过：{failed}")
    source = document["source"]
    if source is None:
        raise OfficialRulesReviewError("规则审核记录未绑定官方来源")
    source_summary = _source_summary(source, private_root=private_root)

    report = {
        "version": 1,
        "event_id": "nvidia-dgx-spark-hackathon-2",
        "artifact_type": "OFFICIAL_RULES_REVIEW_QA",
        "checked_at": _now(),
        "status": "READY_FOR_HUMAN_CONFIRMATION",
        "review_sha256": review_sha256,
        "review_bytes": review_bytes,
        "public_snapshot_sha256": public_snapshot_sha256,
        "source": source_summary,
        "requirement_ids": list(requirement_ids),
        "requirement_count": len(requirements),
        "checks": checks,
        "human_gate_status": "PENDING",
        "data_policy": {
            "private_local_state": True,
            "contains_rule_details": False,
            "contains_source_locator": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "human_confirmation_generated": False,
        },
    }
    return validate_document(report, "official_rules_review_qa.schema.json")


def verify_official_rules_review(
    input_path: Path = DEFAULT_INPUT,
    *,
    public_snapshot_path: Path = DEFAULT_PUBLIC_SNAPSHOT,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> dict[str, Any]:
    input_path = _inside(input_path, private_root)
    document = _load_editable_review(input_path, private_root=private_root)
    _, public_snapshot_sha256 = load_public_snapshot(public_snapshot_path)
    return verify_official_rules_review_document(
        document,
        review_sha256=_sha256(input_path),
        review_bytes=input_path.stat().st_size,
        public_snapshot_sha256=public_snapshot_sha256,
        private_root=private_root.expanduser().resolve(),
    )


def official_rules_review_qa_issue(
    report_path: Path,
    evidence: dict[str, Any],
    *,
    public_snapshot_path: Path = DEFAULT_PUBLIC_SNAPSHOT,
) -> str | None:
    if evidence.get("kind") != "LOCAL_FILE":
        return "OFFICIAL_RULES_REVIEW_REQUIRES_LOCAL_FILE"
    locator = evidence.get("locator")
    if not isinstance(locator, str) or not locator:
        return "OFFICIAL_RULES_REVIEW_LOCATION_INVALID"
    report_path = report_path.expanduser().resolve()
    review_path = Path(locator).expanduser().resolve()
    if (
        review_path.name != "official_rules_review.json"
        or review_path.parent != report_path.parent
    ):
        return "OFFICIAL_RULES_REVIEW_LOCATION_INVALID"
    if (
        not review_path.is_file()
        or stat.S_IMODE(review_path.stat().st_mode) != 0o600
        or stat.S_IMODE(review_path.parent.stat().st_mode) != 0o700
    ):
        return "OFFICIAL_RULES_REVIEW_PERMISSIONS_UNSAFE"
    if not report_path.is_file():
        return "OFFICIAL_RULES_REVIEW_QA_MISSING"
    if (
        stat.S_IMODE(report_path.stat().st_mode) != 0o600
        or stat.S_IMODE(report_path.parent.stat().st_mode) != 0o700
    ):
        return "OFFICIAL_RULES_REVIEW_QA_PERMISSIONS_UNSAFE"
    try:
        saved = validate_document(
            _read_json(report_path, "规则审核QA报告"),
            "official_rules_review_qa.schema.json",
        )
    except (ContractValidationError, OfficialRulesReviewError):
        return "OFFICIAL_RULES_REVIEW_QA_INVALID"
    if (
        saved["review_sha256"] != evidence.get("sha256")
        or saved["review_bytes"] != evidence.get("size_bytes")
    ):
        return "OFFICIAL_RULES_REVIEW_RECORD_CHANGED"
    try:
        current = verify_official_rules_review(
            review_path,
            public_snapshot_path=public_snapshot_path,
            private_root=review_path.parent,
        )
    except (ContractValidationError, OfficialRulesReviewError, OSError):
        return "OFFICIAL_RULES_REVIEW_QA_INVALID"
    comparable_keys = (
        "review_sha256",
        "review_bytes",
        "public_snapshot_sha256",
        "source",
        "requirement_ids",
        "requirement_count",
        "checks",
        "human_gate_status",
        "data_policy",
    )
    if any(saved[key] != current[key] for key in comparable_keys):
        return "OFFICIAL_RULES_REVIEW_STATE_CHANGED"
    return None


_OFFICIAL_RULES_ERROR_HINTS = {
    "规则审核记录不存在；请先使用--init": [
        "── 官方规则审核待办（私有） ──",
        "  1. 初始化空白审核表：bash scripts/check_official_rules_review.sh --init",
        "  2. 绑定官方规则来源：bash scripts/check_official_rules_review.sh"
        " --attach-source /path/to/official_rules.pdf",
        "  3. 逐项填写六项核对结论并把 status 改为 READY_FOR_CHECK，"
        "再重新运行 bash scripts/check_official_rules_review.sh",
    ],
    "规则审核记录尚未填写完成": [
        "  提示：用编辑器打开私有审核表，补全六项核对结论，"
        "并把 status 改为 READY_FOR_CHECK，再重新运行本脚本。",
    ],
    "规则审核记录未绑定官方来源": [
        "  提示：bash scripts/check_official_rules_review.sh"
        " --attach-source /path/to/official_rules.pdf",
    ],
    "官方规则来源文件不存在或为空": [
        "  提示：确认 --attach-source 指向真实且非空的文件后重试。",
    ],
    "官方规则来源文件类型不在允许范围": [
        "  提示：来源仅支持 pdf/html/htm/txt/md/png/jpg/jpeg/pptx；"
        "转换格式后重试。",
    ],
    "规则审核记录不符合严格Schema": [
        "  提示：对照私有审核表模板的字段名与取值检查并修正，再重新运行本脚本。",
    ],
    "规则审核记录权限必须为目录0700、文件0600": [
        "  提示：私有目录应为 0700、审核表文件应为 0600；修正后重新运行本脚本。",
    ],
}

_OFFICIAL_RULES_PREFIX_HINTS = {
    "官方规则六项核对未通过": [
        "  提示：在私有审核表中补全上述未通过项的核对结论，"
        "并把 status 改为 READY_FOR_CHECK，再重新运行本脚本。",
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--public-snapshot", type=Path, default=DEFAULT_PUBLIC_SNAPSHOT)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--init", action="store_true")
    action.add_argument("--attach-source", type=Path)
    action.add_argument("--source-url")
    args = parser.parse_args()
    try:
        if args.init:
            initialize_official_rules_review(
                args.input,
                public_snapshot_path=args.public_snapshot,
            )
            result = {"status": "PENDING_INPUT", "requirement_count": 6}
        elif args.attach_source is not None:
            attach_local_source(args.attach_source, args.input)
            result = {"status": "SOURCE_ATTACHED", "source_kind": "LOCAL_FILE"}
        elif args.source_url is not None:
            attach_source_url(args.source_url, args.input)
            result = {"status": "SOURCE_ATTACHED", "source_kind": "HTTPS_URL"}
        else:
            report = verify_official_rules_review(
                args.input,
                public_snapshot_path=args.public_snapshot,
            )
            _write_private_json(report, args.output)
            result = {
                "status": report["status"],
                "requirement_count": report["requirement_count"],
                "source_kind": report["source"]["kind"],
                "human_gate_status": report["human_gate_status"],
            }
    except (ContractValidationError, OSError, OfficialRulesReviewError) as exc:
        if isinstance(exc, OfficialRulesReviewError):
            print_error_hints(
                str(exc),
                exact_hints=_OFFICIAL_RULES_ERROR_HINTS,
                prefix_hints=_OFFICIAL_RULES_PREFIX_HINTS,
            )
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": str(exc)
                    if isinstance(exc, OfficialRulesReviewError)
                    else "官方规则审核失败",
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
