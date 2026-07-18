from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from skillforge.contracts import validate_document
from skillforge.release_bundle import (
    BUNDLE_MANIFEST_MEMBER,
    BUNDLE_ROOT,
    DEFAULT_ARCHIVE,
    DEFAULT_REPORT,
    RELEASE_MANIFEST_MEMBER,
    ZIP_TIMESTAMP,
    ReleaseBundleError,
    _source,
    verify_public_release_bundle,
    verify_saved_release_bundle_qa,
    write_public_release_bundle,
    write_report,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def built_bundle(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, dict]:
    output = tmp_path_factory.mktemp("release-bundle")
    archive = output / "skillforge_n31_public_release_v1.zip"
    report_path = output / "skillforge_n31_public_release_v1.qa.json"
    write_public_release_bundle(destination=archive)
    report = verify_public_release_bundle(archive)
    write_report(report, report_path)
    return archive, report_path, report


def _info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _rewrite(
    source: Path,
    destination: Path,
    *,
    mutate_name: str | None = None,
    mutate_payload: bytes | None = None,
) -> None:
    with zipfile.ZipFile(source) as current, zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_STORED
    ) as target:
        for info in current.infolist():
            payload = current.read(info)
            if info.filename == mutate_name:
                payload = mutate_payload if mutate_payload is not None else payload
            target.writestr(info, payload)


def test_builds_exact_public_bundle_and_strict_qa(built_bundle: tuple[Path, Path, dict]) -> None:
    archive, report_path, report = built_bundle
    validate_document(report, "release_bundle_qa.schema.json")
    assert report["status"] == "PASSED"
    assert report["artifact_count"] == 18
    assert report["archive_member_count"] == 20
    assert report["data_policy"] == {
        "contains_credentials": False,
        "contains_personal_data": False,
        "contains_absolute_paths": False,
        "contains_private_submission_urls": False,
        "public_reference_urls_may_be_present": True,
        "contains_raw_media": False,
        "contains_public_safe_derived_media": True,
        "contains_private_submission_state": False,
        "official_upload_format_claimed": False,
    }
    assert verify_saved_release_bundle_qa(
        report_path, archive_path=archive
    ) == report

    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert len(names) == len(set(names)) == 20
        assert BUNDLE_MANIFEST_MEMBER in names
        assert RELEASE_MANIFEST_MEMBER in names
        assert all(name.startswith(f"{BUNDLE_ROOT}/") for name in names)
        manifest = json.loads(bundle.read(BUNDLE_MANIFEST_MEMBER))
    validate_document(manifest, "release_bundle_manifest.schema.json")
    assert manifest["status"] == "TECHNICAL_BUNDLE_VERIFIED_HUMAN_GATES_PENDING"
    assert manifest["data_policy"]["requires_human_gates_before_submission"] is True
    assert manifest["data_policy"]["official_upload_format_claimed"] is False


def test_bundle_is_byte_deterministic(built_bundle: tuple[Path, Path, dict], tmp_path: Path) -> None:
    first, _, report = built_bundle
    second = tmp_path / "second.zip"
    write_public_release_bundle(destination=second)
    second_report = verify_public_release_bundle(second)
    assert second.read_bytes() == first.read_bytes()
    assert second_report["archive_sha256"] == report["archive_sha256"]


def test_bundle_and_report_use_restrictive_local_permissions(tmp_path: Path) -> None:
    output = tmp_path / "private-output"
    archive = output / "bundle.zip"
    report_path = output / "bundle.qa.json"
    write_public_release_bundle(destination=archive)
    report = verify_public_release_bundle(archive)
    write_report(report, report_path)
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600


def test_rejects_tampered_artifact_member(
    built_bundle: tuple[Path, Path, dict], tmp_path: Path
) -> None:
    source, _, _ = built_bundle
    with zipfile.ZipFile(source) as bundle:
        manifest = json.loads(bundle.read(BUNDLE_MANIFEST_MEMBER))
        member = manifest["artifacts"][0]["archive_path"]
    tampered = tmp_path / "tampered.zip"
    _rewrite(source, tampered, mutate_name=member, mutate_payload=b"tampered")
    with pytest.raises(ReleaseBundleError, match="哈希|大小"):
        verify_public_release_bundle(tampered)


def test_rejects_extra_and_duplicate_members(
    built_bundle: tuple[Path, Path, dict], tmp_path: Path
) -> None:
    source, _, _ = built_bundle
    extra = tmp_path / "extra.zip"
    _rewrite(source, extra)
    with zipfile.ZipFile(extra, "a") as archive:
        archive.writestr(_info(f"{BUNDLE_ROOT}/EXTRA.txt"), b"extra")
    with pytest.raises(ReleaseBundleError, match="20项"):
        verify_public_release_bundle(extra)

    duplicate = tmp_path / "duplicate.zip"
    _rewrite(source, duplicate)
    with zipfile.ZipFile(duplicate, "a") as archive, pytest.warns(UserWarning):
        archive.writestr(_info(BUNDLE_MANIFEST_MEMBER), b"{}")
    with pytest.raises(ReleaseBundleError, match="重复"):
        verify_public_release_bundle(duplicate)


def test_rejects_path_traversal_and_nondeterministic_metadata(
    built_bundle: tuple[Path, Path, dict], tmp_path: Path
) -> None:
    source, _, _ = built_bundle
    traversal = tmp_path / "traversal.zip"
    _rewrite(source, traversal)
    with zipfile.ZipFile(traversal, "a") as archive:
        archive.writestr(_info(f"{BUNDLE_ROOT}/../escape.txt"), b"escape")
    with pytest.raises(ReleaseBundleError, match="路径"):
        verify_public_release_bundle(traversal)

    metadata = tmp_path / "metadata.zip"
    with zipfile.ZipFile(source) as current, zipfile.ZipFile(metadata, "w") as target:
        for info in current.infolist():
            payload = current.read(info)
            if info.filename == BUNDLE_MANIFEST_MEMBER:
                changed = _info(info.filename)
                changed.date_time = (2026, 7, 19, 0, 0, 0)
                info = changed
            target.writestr(info, payload)
    with pytest.raises(ReleaseBundleError, match="元数据"):
        verify_public_release_bundle(metadata)


def test_rejects_stale_saved_qa(
    built_bundle: tuple[Path, Path, dict], tmp_path: Path
) -> None:
    archive, _, report = built_bundle
    stale = dict(report)
    stale["archive_sha256"] = "0" * 64
    report_path = tmp_path / "stale.qa.json"
    write_report(stale, report_path)
    with pytest.raises(ReleaseBundleError, match="不一致"):
        verify_saved_release_bundle_qa(report_path, archive_path=archive)


def test_rejects_symbolic_link_sources(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = root / "public.json"
    link.symlink_to(target)
    with pytest.raises(ReleaseBundleError, match="符号链接"):
        _source(root, "public.json")


def test_verify_only_missing_bundle_is_safe(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "skillforge.release_bundle",
            "--verify-only",
            "--archive",
            str(tmp_path / "missing.zip"),
            "--report",
            str(tmp_path / "missing.json"),
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert result.returncode == 1
    assert '"status": "ERROR"' in result.stdout
    assert str(tmp_path) not in result.stdout
    assert "/Users/" not in result.stdout


def test_default_runtime_outputs_are_git_ignored_and_script_is_executable() -> None:
    assert DEFAULT_ARCHIVE.is_file()
    assert DEFAULT_REPORT.is_file()
    script = ROOT / "scripts/build_public_release_bundle.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111
    git_state = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    if git_state.returncode == 0:
        ignored = subprocess.run(
            [
                "git",
                "check-ignore",
                "-q",
                str(DEFAULT_ARCHIVE.relative_to(ROOT)),
            ],
            cwd=ROOT,
            check=False,
        )
        assert ignored.returncode == 0
    else:
        assert "outputs/*" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert stat.S_IMODE(DEFAULT_ARCHIVE.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(DEFAULT_ARCHIVE.stat().st_mode) == 0o600
    assert stat.S_IMODE(DEFAULT_REPORT.stat().st_mode) == 0o600
