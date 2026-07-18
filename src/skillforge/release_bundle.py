"""Build and verify a deterministic, public-safe technical release bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .contracts import ContractValidationError, validate_document
from .demo import ROOT
from .release_manifest import (
    DEFAULT_MANIFEST as DEFAULT_RELEASE_MANIFEST,
    ReleaseManifestError,
    verify_release_manifest,
)


BUNDLE_NAME = "skillforge_n31_public_release_v1.zip"
BUNDLE_ROOT = "skillforge_n31_public_release_v1"
BUNDLE_MANIFEST_MEMBER = f"{BUNDLE_ROOT}/BUNDLE_MANIFEST.json"
RELEASE_MANIFEST_RELATIVE = "output/submission/release_manifest_v1.json"
RELEASE_MANIFEST_MEMBER = f"{BUNDLE_ROOT}/{RELEASE_MANIFEST_RELATIVE}"
DEFAULT_ARCHIVE = ROOT / "outputs/release" / BUNDLE_NAME
DEFAULT_REPORT = ROOT / "outputs/release/skillforge_n31_public_release_v1.qa.json"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ABSOLUTE_PATH_MARKERS = (b"/Users/", b"/home/Developer/", b"file://")
SECRET_KEY_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION")
PRIVATE_NAME_MARKERS = (
    "_private",
    "private_review",
    "previous_shipping_label",
    "面单_sf",
)


class ReleaseBundleError(ValueError):
    """Raised when the public technical bundle cannot be trusted."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(document: Any) -> bytes:
    return (
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _secret_values(env_path: Path) -> tuple[bytes, ...]:
    if not env_path.is_file():
        return ()
    values: set[bytes] = set()
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if not any(marker in key.upper() for marker in SECRET_KEY_MARKERS):
            continue
        normalized = value.strip().strip("\"'")
        if len(normalized) >= 8:
            values.add(normalized.encode("utf-8"))
    return tuple(sorted(values))


def _contains_needles(path: Path, needles: Iterable[bytes]) -> bool:
    targets = tuple(item for item in needles if item)
    if not targets:
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


def _artifact_contains_markers(path: Path, markers: Iterable[bytes]) -> bool:
    targets = tuple(markers)
    if path.suffix.lower() == ".pptx":
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    payload = archive.read(info)
                    if any(marker in payload for marker in targets):
                        return True
        except (OSError, zipfile.BadZipFile):
            return True
        return False
    return _contains_needles(path, targets)


def _safe_relative(value: str) -> PurePosixPath:
    if not value or "\\" in value:
        raise ReleaseBundleError("交付包路径格式不安全")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or ".." in path.parts:
        raise ReleaseBundleError("交付包路径格式不安全")
    lowered_parts = tuple(part.lower() for part in path.parts)
    lowered = value.lower()
    if (
        not path.parts
        or any(part in {".env", ".git", ".ds_store", "__macosx"} for part in lowered_parts)
        or any(part.startswith("._") for part in path.parts)
        or path.parts[0] == "outputs"
        or (
            len(path.parts) >= 3
            and path.parts[0] == "cases"
            and path.parts[2] in {"input", "derived", "output"}
        )
        or any(marker in lowered for marker in PRIVATE_NAME_MARKERS)
    ):
        raise ReleaseBundleError("交付包包含私有或运行时路径")
    return path


def _safe_member(value: str) -> PurePosixPath:
    path = _safe_relative(value)
    if len(path.parts) < 2 or path.parts[0] != BUNDLE_ROOT:
        raise ReleaseBundleError("交付包成员不在固定根目录")
    _safe_relative(PurePosixPath(*path.parts[1:]).as_posix())
    return path


def _source(root: Path, relative: str) -> Path:
    relative_path = _safe_relative(relative)
    root = root.expanduser().resolve()
    lexical = root.joinpath(*relative_path.parts)
    cursor = root
    for part in relative_path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ReleaseBundleError("交付包拒绝符号链接来源")
    resolved = lexical.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ReleaseBundleError("交付包来源越出项目目录") from exc
    if not resolved.is_file() or resolved.stat().st_size < 1:
        raise ReleaseBundleError("交付包来源缺失或为空")
    return resolved


def _verified_release(root: Path, manifest_path: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    manifest_path = manifest_path.expanduser().resolve()
    expected_path = _source(root, RELEASE_MANIFEST_RELATIVE)
    if manifest_path != expected_path:
        raise ReleaseBundleError("交付包只接受固定发布冻结清单")
    try:
        return verify_release_manifest(
            manifest_path,
            root=root,
            config_path=root / "config/release_roles.json",
            runbook_path=root / "cases/n31/pitch_runbook.json",
        )
    except (ReleaseManifestError, ContractValidationError, OSError) as exc:
        raise ReleaseBundleError("发布冻结清单不是当前有效版本") from exc


def build_bundle_manifest(
    *,
    root: Path = ROOT,
    release_manifest_path: Path = DEFAULT_RELEASE_MANIFEST,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    release = _verified_release(root, release_manifest_path)
    markers = (*ABSOLUTE_PATH_MARKERS, *_secret_values(root / ".env"))
    artifacts: list[dict[str, Any]] = []
    artifact_ids: set[str] = set()
    source_paths: set[str] = set()
    archive_paths: set[str] = set()
    for item in release["artifacts"]:
        source = _source(root, item["path"])
        archive_path = f"{BUNDLE_ROOT}/{item['path']}"
        _safe_member(archive_path)
        if (
            item["artifact_id"] in artifact_ids
            or item["path"] in source_paths
            or archive_path in archive_paths
        ):
            raise ReleaseBundleError("交付包成果ID或路径重复")
        if source.stat().st_size != item["bytes"] or _sha256(source) != item["sha256"]:
            raise ReleaseBundleError("交付包成果与冻结清单不一致")
        if _artifact_contains_markers(source, markers):
            raise ReleaseBundleError("交付包成果触发绝对路径或密钥边界")
        artifact_ids.add(item["artifact_id"])
        source_paths.add(item["path"])
        archive_paths.add(archive_path)
        artifacts.append(
            {
                "artifact_id": item["artifact_id"],
                "kind": item["kind"],
                "source_path": item["path"],
                "archive_path": archive_path,
                "sha256": item["sha256"],
                "bytes": item["bytes"],
            }
        )
    if len(artifacts) != 18:
        raise ReleaseBundleError("交付包必须精确包含18项冻结成果")
    release_path = _source(root, RELEASE_MANIFEST_RELATIVE)
    if _artifact_contains_markers(release_path, markers):
        raise ReleaseBundleError("发布冻结清单触发绝对路径或密钥边界")
    document = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "PUBLIC_TECHNICAL_RELEASE_BUNDLE_MANIFEST",
        "status": "TECHNICAL_BUNDLE_VERIFIED_HUMAN_GATES_PENDING",
        "bundle_name": BUNDLE_NAME,
        "bundle_root": BUNDLE_ROOT,
        "technical_freeze_digest": release["technical_freeze_digest"],
        "release_manifest": {
            "archive_path": RELEASE_MANIFEST_MEMBER,
            "sha256": _sha256(release_path),
            "bytes": release_path.stat().st_size,
        },
        "artifact_count": len(artifacts),
        "archive_member_count": len(artifacts) + 2,
        "artifacts": artifacts,
        "data_policy": {
            "contains_credentials": False,
            "contains_personal_data": False,
            "contains_absolute_paths": False,
            "contains_private_submission_urls": False,
            "public_reference_urls_may_be_present": True,
            "contains_raw_media": False,
            "contains_public_safe_derived_media": True,
            "requires_human_gates_before_submission": True,
            "official_upload_format_claimed": False,
        },
    }
    return validate_document(document, "release_bundle_manifest.schema.json")


def _zip_info(name: str) -> zipfile.ZipInfo:
    _safe_member(name)
    info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.extra = b""
    info.comment = b""
    return info


def _write_member_from_path(
    archive: zipfile.ZipFile,
    name: str,
    source: Path,
) -> None:
    with source.open("rb") as source_handle, archive.open(_zip_info(name), "w") as target:
        shutil.copyfileobj(source_handle, target, length=1024 * 1024)


def _secure_parent(path: Path, root: Path) -> None:
    parent = path.parent
    existed = parent.exists()
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    outputs = (root / "outputs").resolve()
    try:
        parent.resolve().relative_to(outputs)
    except ValueError:
        return
    if not existed or parent.resolve() == outputs or outputs in parent.resolve().parents:
        os.chmod(parent, 0o700)


def write_public_release_bundle(
    *,
    root: Path = ROOT,
    release_manifest_path: Path = DEFAULT_RELEASE_MANIFEST,
    destination: Path = DEFAULT_ARCHIVE,
) -> Path:
    root = root.expanduser().resolve()
    destination = destination.expanduser().resolve()
    document = build_bundle_manifest(
        root=root,
        release_manifest_path=release_manifest_path,
    )
    source_files = {
        item["archive_path"]: _source(root, item["source_path"])
        for item in document["artifacts"]
    }
    source_files[RELEASE_MANIFEST_MEMBER] = _source(
        root, RELEASE_MANIFEST_RELATIVE
    )
    if destination in source_files.values():
        raise ReleaseBundleError("交付包输出不能覆盖来源成果")
    _secure_parent(destination, root)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.comment = b""
            for name in sorted(source_files):
                _write_member_from_path(archive, name, source_files[name])
            archive.writestr(
                _zip_info(BUNDLE_MANIFEST_MEMBER),
                _canonical_json_bytes(document),
            )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _read_bundle_manifest(archive: zipfile.ZipFile) -> tuple[dict[str, Any], bytes]:
    try:
        payload = archive.read(BUNDLE_MANIFEST_MEMBER)
        value = json.loads(payload.decode("utf-8"))
        document = validate_document(value, "release_bundle_manifest.schema.json")
    except (
        KeyError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ContractValidationError,
    ) as exc:
        raise ReleaseBundleError("包内清单缺失或不符合严格Schema") from exc
    if payload != _canonical_json_bytes(document):
        raise ReleaseBundleError("包内清单不是固定规范JSON")
    return document, payload


def _verify_zip_metadata(info: zipfile.ZipInfo) -> None:
    if (
        info.is_dir()
        or info.date_time != ZIP_TIMESTAMP
        or info.compress_type != zipfile.ZIP_STORED
        or stat.S_IMODE(info.external_attr >> 16) != 0o644
        or info.extra
        or info.comment
        or info.flag_bits & 0x1
    ):
        raise ReleaseBundleError("交付包成员元数据不确定或不安全")


def verify_public_release_bundle(
    archive_path: Path = DEFAULT_ARCHIVE,
    *,
    root: Path = ROOT,
    release_manifest_path: Path = DEFAULT_RELEASE_MANIFEST,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    archive_path = archive_path.expanduser().resolve()
    if not archive_path.is_file() or archive_path.stat().st_size < 1:
        raise ReleaseBundleError("技术交付包不存在或为空")
    expected_manifest = build_bundle_manifest(
        root=root,
        release_manifest_path=release_manifest_path,
    )
    try:
        with zipfile.ZipFile(archive_path) as archive:
            if archive.comment:
                raise ReleaseBundleError("交付包注释必须为空")
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ReleaseBundleError("交付包存在重复成员")
            for info in infos:
                _safe_member(info.filename)
                _verify_zip_metadata(info)
            document, manifest_payload = _read_bundle_manifest(archive)
            if document != expected_manifest:
                raise ReleaseBundleError("包内清单与当前冻结成果不一致")
            expected_names = {
                BUNDLE_MANIFEST_MEMBER,
                RELEASE_MANIFEST_MEMBER,
                *(item["archive_path"] for item in document["artifacts"]),
            }
            if set(names) != expected_names or len(names) != 20:
                raise ReleaseBundleError("交付包成员集合不是冻结的20项")
            release_payload = archive.read(RELEASE_MANIFEST_MEMBER)
            if (
                len(release_payload) != document["release_manifest"]["bytes"]
                or _sha256_bytes(release_payload)
                != document["release_manifest"]["sha256"]
            ):
                raise ReleaseBundleError("包内发布冻结清单哈希不一致")
            for item in document["artifacts"]:
                payload = archive.read(item["archive_path"])
                if len(payload) != item["bytes"] or _sha256_bytes(payload) != item["sha256"]:
                    raise ReleaseBundleError("包内成果哈希或大小不一致")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ReleaseBundleError("技术交付包无法读取") from exc
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "PUBLIC_TECHNICAL_RELEASE_BUNDLE_QA",
        "status": "PASSED",
        "bundle_name": BUNDLE_NAME,
        "archive_sha256": _sha256(archive_path),
        "archive_bytes": archive_path.stat().st_size,
        "bundle_manifest_sha256": _sha256_bytes(manifest_payload),
        "release_manifest_sha256": expected_manifest["release_manifest"]["sha256"],
        "technical_freeze_digest": expected_manifest["technical_freeze_digest"],
        "artifact_count": expected_manifest["artifact_count"],
        "archive_member_count": expected_manifest["archive_member_count"],
        "checks": {
            "release_manifest_current": True,
            "exact_artifact_set": True,
            "source_hashes_match": True,
            "member_hashes_match": True,
            "deterministic_zip_metadata": True,
            "safe_member_paths": True,
            "no_duplicate_or_extra_members": True,
            "public_artifact_boundary_clean": True,
            "human_gate_boundary_preserved": True,
        },
        "data_policy": {
            "contains_credentials": False,
            "contains_personal_data": False,
            "contains_absolute_paths": False,
            "contains_private_submission_urls": False,
            "public_reference_urls_may_be_present": True,
            "contains_raw_media": False,
            "contains_public_safe_derived_media": True,
            "contains_private_submission_state": False,
            "official_upload_format_claimed": False,
        },
    }
    return validate_document(report, "release_bundle_qa.schema.json")


def write_report(document: dict[str, Any], destination: Path, *, root: Path = ROOT) -> Path:
    validate_document(document, "release_bundle_qa.schema.json")
    destination = destination.expanduser().resolve()
    _secure_parent(destination, root.expanduser().resolve())
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json_bytes(document))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def verify_saved_release_bundle_qa(
    report_path: Path = DEFAULT_REPORT,
    *,
    archive_path: Path = DEFAULT_ARCHIVE,
    root: Path = ROOT,
    release_manifest_path: Path = DEFAULT_RELEASE_MANIFEST,
) -> dict[str, Any]:
    report_path = report_path.expanduser().resolve()
    try:
        saved = validate_document(
            json.loads(report_path.read_text(encoding="utf-8")),
            "release_bundle_qa.schema.json",
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ContractValidationError,
    ) as exc:
        raise ReleaseBundleError("技术交付包QA缺失或无效") from exc
    current = verify_public_release_bundle(
        archive_path,
        root=root,
        release_manifest_path=release_manifest_path,
    )
    if saved != current:
        raise ReleaseBundleError("技术交付包QA与当前归档不一致")
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--release-manifest", type=Path, default=DEFAULT_RELEASE_MANIFEST
    )
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.verify_only:
            report = verify_saved_release_bundle_qa(
                args.report,
                archive_path=args.archive,
                release_manifest_path=args.release_manifest,
            )
        else:
            write_public_release_bundle(
                release_manifest_path=args.release_manifest,
                destination=args.archive,
            )
            report = verify_public_release_bundle(
                args.archive,
                release_manifest_path=args.release_manifest,
            )
            write_report(report, args.report)
            verify_saved_release_bundle_qa(
                args.report,
                archive_path=args.archive,
                release_manifest_path=args.release_manifest,
            )
    except (
        ReleaseBundleError,
        ReleaseManifestError,
        ContractValidationError,
        OSError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": "公开技术交付包生成或验证失败",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "artifact_count": report["artifact_count"],
                "archive_member_count": report["archive_member_count"],
                "archive_bytes": report["archive_bytes"],
                "archive_sha256": report["archive_sha256"],
                "technical_freeze_digest": report["technical_freeze_digest"],
                "human_gates_pending": True,
                "official_upload_format_claimed": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
