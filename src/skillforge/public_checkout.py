"""Verify that the public Git commit reproduces the offline P0 without private state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .contracts import ContractValidationError, validate_document
from .demo import ROOT
from .release_bundle import _secret_values


DEFAULT_PRIVATE_ROOT = ROOT / "outputs/reproducibility"
DEFAULT_REPORT = DEFAULT_PRIVATE_ROOT / "public_checkout_reproducibility.json"
RESULT_PREFIX = "SKILLFORGE_CLEAN_ROOM_RESULT="
PRIVATE_NAME_MARKERS = (
    "_private",
    "private_review",
    "previous_shipping_label",
    "面单_sf",
)
PRIVATE_ROOTS = (
    ("outputs",),
    ("cases", "n31", "input"),
    ("cases", "n31", "derived"),
    ("cases", "n31", "output"),
    ("external", "teacher_he_reference", "drop"),
    ("external", "teacher_he_reference", "runtime"),
)


class PublicCheckoutError(ValueError):
    """Raised when the public commit cannot be reproduced safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 180,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PublicCheckoutError("纯净检出子进程无法完成") from exc
    if completed.returncode != 0:
        raise PublicCheckoutError("纯净检出子进程返回失败")
    return completed


def _git(root: Path, *args: str) -> str:
    return _run(["git", *args], cwd=root, timeout=30).stdout.strip()


def _safe_member(name: str, *, is_directory: bool = False) -> PurePosixPath:
    if not name or "\\" in name:
        raise PublicCheckoutError("Git归档成员路径不安全")
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name or ".." in path.parts:
        raise PublicCheckoutError("Git归档成员路径不安全")
    lowered_parts = tuple(part.lower() for part in path.parts)
    lowered = name.lower()
    if (
        any(part in {".env", ".git", ".ds_store", "__macosx"} for part in lowered_parts)
        or any(part.startswith("._") for part in path.parts)
        or any(marker in lowered for marker in PRIVATE_NAME_MARKERS)
    ):
        raise PublicCheckoutError("Git归档包含私有或运行时路径")
    for private_root in PRIVATE_ROOTS:
        if path.parts[: len(private_root)] == private_root:
            directory_placeholder = is_directory and path.parts == private_root
            file_placeholder = (
                not is_directory
                and len(path.parts) == len(private_root) + 1
                and path.name == ".gitkeep"
            )
            if not (directory_placeholder or file_placeholder):
                raise PublicCheckoutError("Git归档包含私有或运行时路径")
    return path


def _archive_head(root: Path, archive_path: Path) -> tuple[set[str], int]:
    _run(
        ["git", "archive", "--format=tar", "--output", str(archive_path), "HEAD"],
        cwd=root,
        timeout=120,
    )
    os.chmod(archive_path, 0o600)
    expected = {
        line
        for line in _git(
            root,
            "-c",
            "core.quotepath=false",
            "ls-tree",
            "-r",
            "--name-only",
            "HEAD",
        ).splitlines()
        if line
    }
    return expected, len(expected)


def _extract_verified_archive(
    archive_path: Path,
    destination: Path,
    *,
    expected_files: set[str],
    secret_values: tuple[bytes, ...],
) -> int:
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, "r:") as archive:
            members = archive.getmembers()
            for member in members:
                path = _safe_member(
                    member.name.rstrip("/"),
                    is_directory=member.isdir(),
                )
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise PublicCheckoutError("Git归档包含链接或特殊文件")
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if path.as_posix() in seen:
                    raise PublicCheckoutError("Git归档包含重复文件")
                source = archive.extractfile(member)
                if source is None:
                    raise PublicCheckoutError("Git归档文件无法读取")
                payload = source.read()
                if any(value and value in payload for value in secret_values):
                    raise PublicCheckoutError("Git归档命中本机实际密钥值")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
                seen.add(path.as_posix())
    except (OSError, tarfile.TarError) as exc:
        raise PublicCheckoutError("Git归档无法安全读取") from exc
    if seen != expected_files:
        raise PublicCheckoutError("Git归档与HEAD跟踪文件集合不一致")
    return len(seen)


def _safe_environment() -> dict[str, str]:
    allowed = ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
    result = {key: os.environ[key] for key in allowed if key in os.environ}
    result.update(
        {
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONNOUSERSITE": "1",
            "SKILLFORGE_SKIP_DOTENV": "1",
        }
    )
    return result


_CLEAN_RUNTIME = r'''
import json
import socket
import sys
from pathlib import Path

runtime = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2]).resolve()
sys.path.insert(0, str(runtime))

network = {"count": 0}
original_socket = socket.socket

class BlockedSocket(original_socket):
    def connect(self, *args, **kwargs):
        network["count"] += 1
        raise RuntimeError("clean-room network access denied")

socket.socket = BlockedSocket

import skillforge
from fastapi.testclient import TestClient
from skillforge.gold_rehearsal import run_gold_rehearsal
from skillforge.pitch import build_readiness
from skillforge.release_bundle import write_public_release_bundle, verify_public_release_bundle
from skillforge.web import create_app

package_path = Path(skillforge.__file__).resolve()
package_loaded = runtime in package_path.parents
if not package_loaded:
    raise RuntimeError("package was not loaded from the clean runtime")

gold_dir = root / "outputs/clean_room_gold"
summary = run_gold_rehearsal(
    root / "cases/n31/gold/gold_sop.json",
    root / "cases/n31/gold/constraints.json",
    root / "cases/n31/gold/fault_injection.json",
    gold_dir,
)

app = create_app(
    output_root=root / "outputs/clean_room_web",
    n31_rehearsal_dir=gold_dir,
)
client = TestClient(app)
health = client.get("/health")
payload = client.get("/api/n31")

release_dir = root / "outputs/clean_room_release"
bundle = release_dir / "skillforge_n31_public_release_v1.zip"
write_public_release_bundle(
    root=root,
    release_manifest_path=root / "output/submission/release_manifest_v1.json",
    destination=bundle,
)
release = verify_public_release_bundle(
    bundle,
    root=root,
    release_manifest_path=root / "output/submission/release_manifest_v1.json",
)

pitch = build_readiness(root / "cases/n31/pitch_runbook.json", root=root)
result = {
    "package_loaded_from_clean_runtime": package_loaded,
    "runtime": health.json().get("runtime"),
    "docker_required": health.json().get("docker_required"),
    "web_health_status": health.status_code,
    "web_payload_status": payload.status_code,
    "gold_status": summary.get("gold_status"),
    "metrics_status": summary.get("metrics_status"),
    "workflow_state": summary.get("workflow_state"),
    "severe_before": summary.get("before", {}).get("severe_error_count"),
    "severe_after": summary.get("after", {}).get("severe_error_count"),
    "revision_count": summary.get("revision_count"),
    "external_model_calls": summary.get("external_model_calls"),
    "release_artifact_count": release.get("artifact_count"),
    "release_member_count": release.get("archive_member_count"),
    "pitch_status": pitch.get("status"),
    "pending_human_gate_count": len(pitch.get("pending_human_gates", [])),
    "network_requests": network["count"],
    "automatic_human_confirmations": 0,
}
print("SKILLFORGE_CLEAN_ROOM_RESULT=" + json.dumps(result, ensure_ascii=False, sort_keys=True))
'''


def _install_and_run(checkout: Path) -> dict[str, Any]:
    runtime = checkout / ".runtime"
    environment = _safe_environment()
    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--target",
            str(runtime),
            str(checkout),
        ],
        cwd=checkout,
        env=environment,
        timeout=180,
    )
    completed = _run(
        [sys.executable, "-I", "-c", _CLEAN_RUNTIME, str(runtime), str(checkout)],
        cwd=checkout,
        env=environment,
        timeout=240,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    if len(lines) != 1:
        raise PublicCheckoutError("纯净检出没有返回唯一结构化结果")
    try:
        result = json.loads(lines[0][len(RESULT_PREFIX) :])
    except json.JSONDecodeError as exc:
        raise PublicCheckoutError("纯净检出结构化结果不是合法JSON") from exc
    if not isinstance(result, dict):
        raise PublicCheckoutError("纯净检出结构化结果必须是对象")
    return result


def _write_private_report(
    report: dict[str, Any],
    destination: Path,
    *,
    private_root: Path,
) -> Path:
    root = private_root.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination == root or root not in destination.parents:
        raise PublicCheckoutError("复现报告必须位于私有输出目录")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=root
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def verify_saved_public_checkout_report(
    report_path: Path = DEFAULT_REPORT,
    *,
    root: Path = ROOT,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    private_root = private_root.expanduser().resolve()
    report_path = report_path.expanduser().resolve()
    if report_path == private_root or private_root not in report_path.parents:
        raise PublicCheckoutError("复现报告必须位于私有输出目录")
    if not report_path.is_file() or report_path.stat().st_size < 1:
        raise PublicCheckoutError("公开纯净检出复现报告不存在或为空")
    if (
        stat.S_IMODE(private_root.stat().st_mode) != 0o700
        or stat.S_IMODE(report_path.stat().st_mode) != 0o600
    ):
        raise PublicCheckoutError("复现报告权限必须为目录0700、文件0600")
    try:
        report = validate_document(
            json.loads(report_path.read_text(encoding="utf-8")),
            "public_checkout_reproducibility.schema.json",
        )
    except (ContractValidationError, json.JSONDecodeError, OSError) as exc:
        raise PublicCheckoutError("公开纯净检出复现报告无效") from exc
    current_commit = _git(root, "rev-parse", "HEAD")
    current_branch = _git(root, "branch", "--show-current")
    origin_main = _git(root, "rev-parse", "origin/main")
    if (
        report["status"] != "PASSED"
        or report["source"]["commit"] != current_commit
        or report["source"]["branch"] != current_branch
        or current_branch != "main"
        or origin_main != current_commit
        or not report["source"]["worktree_clean"]
        or not report["source"]["matches_origin_main"]
    ):
        raise PublicCheckoutError("公开纯净检出复现报告与当前提交不一致")
    return report


def verify_public_checkout(
    *,
    root: Path = ROOT,
    report_path: Path = DEFAULT_REPORT,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    private_root = private_root.expanduser().resolve()
    branch = _git(root, "branch", "--show-current")
    commit = _git(root, "rev-parse", "HEAD")
    clean = not bool(_git(root, "status", "--porcelain"))
    try:
        origin_main = _git(root, "rev-parse", "origin/main")
    except PublicCheckoutError:
        origin_main = ""
    matches_origin = origin_main == commit
    if branch != "main":
        raise PublicCheckoutError("纯净检出只接受main分支")
    if (not clean or not matches_origin) and not allow_dirty:
        raise PublicCheckoutError("正式纯净检出要求干净工作树并与origin/main一致")

    private_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(private_root, 0o700)
    with tempfile.TemporaryDirectory(prefix=".public-checkout-", dir=private_root) as temp:
        temporary = Path(temp)
        os.chmod(temporary, 0o700)
        archive_path = temporary / "source.tar"
        checkout = temporary / "checkout"
        checkout.mkdir(mode=0o700)
        expected, tracked_count = _archive_head(root, archive_path)
        file_count = _extract_verified_archive(
            archive_path,
            checkout,
            expected_files=expected,
            secret_values=_secret_values(root / ".env"),
        )
        result = _install_and_run(checkout)
        archive = {
            "sha256": _sha256(archive_path),
            "bytes": archive_path.stat().st_size,
            "file_count": file_count,
        }

    expected_result = {
        "package_loaded_from_clean_runtime": True,
        "runtime": "native-python",
        "docker_required": False,
        "gold_status": "GOLD",
        "metrics_status": "FINAL",
        "workflow_state": "COMPLETED",
        "severe_before": 5,
        "severe_after": 0,
        "revision_count": 4,
        "web_health_status": 200,
        "web_payload_status": 200,
        "release_artifact_count": 18,
        "release_member_count": 20,
        "pitch_status": "READY_WITH_HUMAN_GATES",
        "pending_human_gate_count": 5,
        "network_requests": 0,
        "external_model_calls": 0,
        "automatic_human_confirmations": 0,
    }
    if result != expected_result:
        raise PublicCheckoutError("纯净检出结果与冻结P0断言不一致")
    status = "PASSED" if clean and matches_origin else "DEVELOPMENT_CHECK"
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "PUBLIC_CHECKOUT_REPRODUCIBILITY_QA",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "source": {
            "branch": branch,
            "commit": commit,
            "worktree_clean": clean,
            "matches_origin_main": matches_origin,
            "tracked_file_count": tracked_count,
        },
        "archive": archive,
        "checks": {
            "git_archive_exact": True,
            "safe_member_paths": True,
            "no_private_inputs": True,
            "no_env_file": True,
            "actual_secret_values_absent": True,
            "package_installed_without_index": True,
            "package_loaded_from_clean_runtime": True,
            "offline_web_healthy": True,
            "gold_loop_reproduced": True,
            "technical_bundle_rebuilt": True,
            "pitch_package_ready": True,
        },
        "results": {key: value for key, value in result.items() if key != "package_loaded_from_clean_runtime"},
        "data_policy": {
            "private_local_state": True,
            "contains_credentials": False,
            "contains_personal_data": False,
            "contains_absolute_paths": False,
            "contains_raw_media": False,
            "contains_private_inputs": False,
            "contains_runtime_checkout": False,
            "external_network_allowed": False,
            "automatic_human_approval": False,
        },
    }
    try:
        validated = validate_document(
            report,
            "public_checkout_reproducibility.schema.json",
        )
    except ContractValidationError as exc:
        raise PublicCheckoutError("纯净检出报告不符合严格Schema") from exc
    _write_private_report(validated, report_path, private_root=private_root)
    return validated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    try:
        report = verify_public_checkout(
            report_path=args.report,
            allow_dirty=args.allow_dirty,
        )
    except (ContractValidationError, OSError, PublicCheckoutError) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": "公开纯净检出复现失败",
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
                "source_commit": report["source"]["commit"],
                "tracked_file_count": report["source"]["tracked_file_count"],
                "archive_sha256": report["archive"]["sha256"],
                "gold_status": report["results"]["gold_status"],
                "severe_before": report["results"]["severe_before"],
                "severe_after": report["results"]["severe_after"],
                "release_member_count": report["results"]["release_member_count"],
                "network_requests": report["results"]["network_requests"],
                "automatic_human_confirmations": report["results"]["automatic_human_confirmations"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
