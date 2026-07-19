"""Build and verify the public, role-based release freeze manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import ContractValidationError, validate_document
from .demo import ROOT


DEFAULT_CONFIG = ROOT / "config/release_roles.json"
DEFAULT_RUNBOOK = ROOT / "cases/n31/pitch_runbook.json"
DEFAULT_MANIFEST = ROOT / "output/submission/release_manifest_v1.json"
EXPECTED_ROLES = {
    "TECHNICAL_OWNER",
    "EVIDENCE_OWNER",
    "CONTENT_OWNER",
    "DEMO_OPERATOR",
    "SUBMISSION_OWNER",
    "FINAL_REVIEWER",
}
EXPECTED_PUBLICATION_STATUS = {
    "PROJECT_PAGE": "PENDING_SUBMISSION",
    "CODE_REPOSITORY": "PENDING_SUBMISSION",
    "FINAL_RECORDING": "PENDING_HUMAN_REVIEW",
}


class ReleaseManifestError(ValueError):
    """Raised when a release freeze manifest cannot be trusted."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseManifestError("发布配置或清单无法读取") from exc
    if not isinstance(value, dict):
        raise ReleaseManifestError("发布配置或清单必须是JSON对象")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _inside(path: Path, root: Path) -> Path:
    resolved = path.expanduser().resolve()
    root = root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReleaseManifestError("发布成果路径越出项目根目录") from exc
    return resolved


def _load_inputs(
    root: Path,
    config_path: Path,
    runbook_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        config = validate_document(
            _read_json(config_path),
            "release_roles.schema.json",
        )
        runbook = validate_document(
            _read_json(runbook_path),
            "pitch_runbook.schema.json",
        )
    except ContractValidationError as exc:
        raise ReleaseManifestError("发布角色配置或路演运行单不符合严格Schema") from exc

    role_ids = [item["role_id"] for item in config["roles"]]
    if len(role_ids) != len(set(role_ids)) or set(role_ids) != EXPECTED_ROLES:
        raise ReleaseManifestError("发布角色必须完整且不能重复")
    artifacts = {item["artifact_id"]: item for item in runbook["required_artifacts"]}
    assignments = {
        item["artifact_id"]: item for item in config["artifact_assignments"]
    }
    if (
        len(assignments) != len(config["artifact_assignments"])
        or set(assignments) != set(artifacts)
    ):
        raise ReleaseManifestError("18项成果必须逐项且唯一分配发布角色")
    for assignment in assignments.values():
        if assignment["responsible_role"] == assignment["final_checker_role"]:
            raise ReleaseManifestError("成果负责人和最终检查角色必须分离")
        if not {
            assignment["responsible_role"],
            assignment["final_checker_role"],
        } <= EXPECTED_ROLES:
            raise ReleaseManifestError("成果分配引用了未知角色")

    target_items = config["publication_targets"]
    targets = {item["target_id"]: item for item in target_items}
    if len(targets) != len(target_items) or {
        key: item["status"] for key, item in targets.items()
    } != EXPECTED_PUBLICATION_STATUS:
        raise ReleaseManifestError("三个公开入口状态必须完整、唯一且保持待提交")
    for target in targets.values():
        if target["owner_role"] == target["final_checker_role"]:
            raise ReleaseManifestError("公开入口负责人和最终检查角色必须分离")

    if len(runbook["human_gates"]) != 5 or any(
        item["status"] != "PENDING" or not item["blocking_for_submission"]
        for item in runbook["human_gates"]
    ):
        raise ReleaseManifestError("公开冻结清单必须保留5项人工门禁")
    return config, runbook


def build_release_manifest(
    *,
    root: Path = ROOT,
    config_path: Path = DEFAULT_CONFIG,
    runbook_path: Path = DEFAULT_RUNBOOK,
    generated_at: str | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    config_path = config_path.expanduser().resolve()
    runbook_path = runbook_path.expanduser().resolve()
    config, runbook = _load_inputs(root, config_path, runbook_path)
    assignments = {
        item["artifact_id"]: item for item in config["artifact_assignments"]
    }
    artifacts = []
    for item in runbook["required_artifacts"]:
        path = _inside(root / item["path"], root)
        if not path.is_file() or path.stat().st_size < 1:
            raise ReleaseManifestError("发布成果缺失或为空")
        assignment = assignments[item["artifact_id"]]
        artifacts.append(
            {
                "artifact_id": item["artifact_id"],
                "path": item["path"],
                "kind": item["kind"],
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
                "responsible_role": assignment["responsible_role"],
                "final_checker_role": assignment["final_checker_role"],
                "release_channels": assignment["release_channels"],
                "status": "FROZEN_MACHINE_VERIFIED",
            }
        )
    runbook_sha256 = _sha256(runbook_path)
    config_sha256 = _sha256(config_path)
    freeze_payload = {
        "runbook_sha256": runbook_sha256,
        "role_config_sha256": config_sha256,
        "artifacts": artifacts,
        "branch_policy": config["branch_policy"],
    }
    manifest = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "RELEASE_FREEZE_MANIFEST",
        "generated_at": generated_at or _now(),
        "status": "TECHNICAL_ARTIFACTS_FROZEN_HUMAN_GATES_PENDING",
        "runbook_sha256": runbook_sha256,
        "role_config_sha256": config_sha256,
        "technical_freeze_digest": _canonical_sha256(freeze_payload),
        "artifact_count": len(artifacts),
        "roles": config["roles"],
        "artifacts": artifacts,
        "publication_targets": [
            {**item, "public_url": None} for item in config["publication_targets"]
        ],
        "human_gates": [
            {
                "gate_id": item["gate_id"],
                "status": item["status"],
                "blocking_for_submission": item["blocking_for_submission"],
            }
            for item in runbook["human_gates"]
        ],
        "branch_policy": config["branch_policy"],
        "identity_assignment": config["identity_assignment"],
        "data_policy": {
            "contains_credentials": False,
            "contains_personal_data": False,
            "contains_absolute_paths": False,
            "contains_public_urls": False,
            "contains_artifact_content": False,
        },
    }
    return validate_document(manifest, "release_manifest.schema.json")


def validate_release_manifest_document(
    document: dict[str, Any],
    *,
    root: Path = ROOT,
    config_path: Path = DEFAULT_CONFIG,
    runbook_path: Path = DEFAULT_RUNBOOK,
) -> dict[str, Any]:
    try:
        validate_document(document, "release_manifest.schema.json")
    except ContractValidationError as exc:
        raise ReleaseManifestError("发布冻结清单不符合严格Schema") from exc
    expected = build_release_manifest(
        root=root,
        config_path=config_path,
        runbook_path=runbook_path,
        generated_at=document["generated_at"],
    )
    if document != expected:
        raise ReleaseManifestError("发布冻结清单与当前成果、角色或策略不一致")
    return document


def verify_release_manifest(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    root: Path = ROOT,
    config_path: Path = DEFAULT_CONFIG,
    runbook_path: Path = DEFAULT_RUNBOOK,
) -> dict[str, Any]:
    return validate_release_manifest_document(
        _read_json(manifest_path.expanduser().resolve()),
        root=root,
        config_path=config_path,
        runbook_path=runbook_path,
    )


def write_manifest(document: dict[str, Any], destination: Path) -> Path:
    validate_document(document, "release_manifest.schema.json")
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
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
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--runbook", type=Path, default=DEFAULT_RUNBOOK)
    parser.add_argument("--output", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.verify_only:
            document = verify_release_manifest(
                args.output,
                config_path=args.config,
                runbook_path=args.runbook,
            )
        else:
            document = build_release_manifest(
                config_path=args.config,
                runbook_path=args.runbook,
            )
            write_manifest(document, _inside(args.output, ROOT))
            verify_release_manifest(
                args.output,
                config_path=args.config,
                runbook_path=args.runbook,
            )
    except (ReleaseManifestError, ContractValidationError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": "发布冻结清单生成或验证失败",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": document["status"],
                "artifact_count": document["artifact_count"],
                "role_count": len(document["roles"]),
                "publication_target_count": len(document["publication_targets"]),
                "pending_human_gate_count": len(document["human_gates"]),
                "technical_freeze_digest": document["technical_freeze_digest"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
