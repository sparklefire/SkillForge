"""Privately verify final public submission links using anonymous HTTPS requests."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .cli_hints import print_error_hints
from .contracts import ContractValidationError, validate_document
from .demo import ROOT


DEFAULT_PRIVATE_ROOT = ROOT / "outputs/submission"
DEFAULT_INPUT = DEFAULT_PRIVATE_ROOT / "publication_links.json"
DEFAULT_REPORT = DEFAULT_PRIVATE_ROOT / "publication_links_qa.json"
EXPECTED_TARGETS = {
    "PROJECT_PAGE": "HTML",
    "CODE_REPOSITORY": "HTML",
    "FINAL_RECORDING": "HTML_OR_VIDEO",
}
SENSITIVE_QUERY_MARKERS = {
    "access_token",
    "api_key",
    "auth",
    "authorization",
    "expires",
    "key",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
}
PRIVATE_HOST_SUFFIXES = (".internal", ".lan", ".local", ".localhost", ".home")


class PublicationLinksError(ValueError):
    """Raised when private public-link input or verification is unsafe."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path = DEFAULT_PRIVATE_ROOT) -> Path:
    resolved = path.expanduser().resolve()
    root = root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PublicationLinksError("公开链接输入和报告必须保存在私有提交目录") from exc
    return resolved


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationLinksError("公开链接输入无法读取") from exc
    if not isinstance(value, dict):
        raise PublicationLinksError("公开链接输入必须是JSON对象")
    return value


def _query_key_is_sensitive(value: str) -> bool:
    normalized = value.strip().lower().replace("-", "_")
    if normalized in SENSITIVE_QUERY_MARKERS:
        return True
    return normalized.endswith(
        (
            "_access_token",
            "_api_key",
            "_auth",
            "_authorization",
            "_credential",
            "_expires",
            "_key",
            "_password",
            "_secret",
            "_sig",
            "_signature",
            "_token",
        )
    )


def _public_host_safe(hostname: str) -> bool:
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(PRIVATE_HOST_SUFFIXES):
        return False
    try:
        return ipaddress.ip_address(normalized).is_global
    except ValueError:
        return "." in normalized


def _safe_public_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not _public_host_safe(parsed.hostname)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise PublicationLinksError("公开链接必须是无账号、片段或私有主机的HTTPS地址")
    query_keys = {key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if any(_query_key_is_sensitive(key) for key in query_keys):
        raise PublicationLinksError("公开链接不能包含疑似凭证或签名参数")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def initialize_private_input(
    destination: Path = DEFAULT_INPUT,
    *,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> Path:
    destination = _inside(destination, private_root)
    if destination.exists():
        raise PublicationLinksError("公开链接私有输入已存在；初始化不会覆盖已有内容")
    document = {
        "version": 1,
        "case_id": "n31_media_change",
        "updated_at": _now(),
        "status": "PENDING_INPUT",
        "targets": [
            {
                "target_id": target_id,
                "expected_surface": surface,
                "public_url": None,
            }
            for target_id, surface in EXPECTED_TARGETS.items()
        ],
        "data_policy": {
            "private_local_state": True,
            "contains_credentials": False,
            "contains_personal_data": False,
        },
    }
    return _write_private_json(
        validate_document(document, "publication_links_input.schema.json"),
        destination,
        private_root=private_root,
    )


def _curl_head(url: str) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--head",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--connect-timeout",
            "10",
            "--max-time",
            "30",
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}\t%{content_type}\t%{url_effective}\t%{num_redirects}\t%{remote_ip}",
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=40,
    )
    if completed.returncode != 0:
        return {
            "http_status": 0,
            "content_type": None,
            "final_url": None,
            "redirect_count": 0,
            "remote_ip": None,
        }
    parts = completed.stdout.split("\t")
    if len(parts) != 5:
        return {
            "http_status": 0,
            "content_type": None,
            "final_url": None,
            "redirect_count": 0,
            "remote_ip": None,
        }
    try:
        http_status = int(parts[0])
        redirect_count = int(parts[3])
    except ValueError:
        http_status = 0
        redirect_count = 0
    return {
        "http_status": http_status,
        "content_type": parts[1] or None,
        "final_url": parts[2] or None,
        "redirect_count": max(0, redirect_count),
        "remote_ip": parts[4] or None,
    }


def _content_type_matches(surface: str, content_type: str | None) -> bool:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if surface == "HTML":
        return normalized in {"text/html", "application/xhtml+xml"}
    return normalized in {"text/html", "application/xhtml+xml"} or normalized.startswith(
        "video/"
    )


def verify_publication_links_document(
    document: dict[str, Any],
    *,
    input_sha256: str,
    transport: Callable[[str], dict[str, Any]] = _curl_head,
) -> dict[str, Any]:
    try:
        validate_document(document, "publication_links_input.schema.json")
    except ContractValidationError as exc:
        raise PublicationLinksError("公开链接输入不符合严格Schema") from exc
    items = document["targets"]
    targets = {item["target_id"]: item for item in items}
    if len(targets) != len(items) or {
        key: item["expected_surface"] for key, item in targets.items()
    } != EXPECTED_TARGETS:
        raise PublicationLinksError("三个公开入口必须完整、唯一且类型正确")
    if document["status"] != "READY_FOR_CHECK" or any(
        item["public_url"] is None for item in items
    ):
        raise PublicationLinksError("公开链接尚未填写完成")

    results = []
    for item in items:
        raw_url = str(item["public_url"])
        safe_url = _safe_public_url(raw_url)
        response = transport(safe_url)
        final_url_raw = response.get("final_url")
        final_url = None
        final_url_safe = False
        if final_url_raw:
            try:
                final_url = _safe_public_url(str(final_url_raw))
                final_url_safe = True
            except PublicationLinksError:
                final_url_safe = False
        http_status = int(response.get("http_status") or 0)
        content_type = response.get("content_type")
        remote_ip_raw = response.get("remote_ip")
        remote_ip_public = False
        if remote_ip_raw:
            try:
                remote_ip_public = ipaddress.ip_address(str(remote_ip_raw)).is_global
            except ValueError:
                remote_ip_public = False
        checks = {
            "input_url_safe": True,
            "anonymous_reachable": 200 <= http_status <= 299,
            "content_type_matches": _content_type_matches(
                item["expected_surface"], content_type
            ),
            "final_url_safe": final_url_safe,
            "remote_ip_public": remote_ip_public,
        }
        results.append(
            {
                "target_id": item["target_id"],
                "expected_surface": item["expected_surface"],
                "url_sha256": _sha256_text(safe_url),
                "final_url_sha256": _sha256_text(final_url) if final_url else None,
                "http_status": http_status,
                "content_type": str(content_type)[:200] if content_type else None,
                "redirect_count": max(0, int(response.get("redirect_count") or 0)),
                "status": "PASSED" if all(checks.values()) else "FAILED",
                "checks": checks,
            }
        )
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "PUBLICATION_LINKS_QA",
        "checked_at": _now(),
        "status": "PASSED" if all(item["status"] == "PASSED" for item in results) else "FAILED",
        "input_sha256": input_sha256,
        "target_count": len(results),
        "targets": results,
        "data_policy": {
            "private_local_state": True,
            "contains_credentials": False,
            "contains_urls": False,
            "contains_response_body": False,
            "anonymous_requests_only": True,
            "authorization_headers_sent": False,
            "cookies_sent": False,
        },
    }
    return validate_document(report, "publication_links_qa.schema.json")


def verify_publication_links(
    input_path: Path = DEFAULT_INPUT,
    *,
    transport: Callable[[str], dict[str, Any]] = _curl_head,
    private_root: Path = DEFAULT_PRIVATE_ROOT,
) -> dict[str, Any]:
    input_path = _inside(input_path, private_root)
    if not input_path.is_file():
        raise PublicationLinksError("公开链接私有输入不存在；请先使用--init")
    if (
        stat.S_IMODE(input_path.stat().st_mode) != 0o600
        or stat.S_IMODE(input_path.parent.stat().st_mode) != 0o700
    ):
        raise PublicationLinksError("公开链接私有输入权限必须为目录0700、文件0600")
    return verify_publication_links_document(
        _read_json(input_path),
        input_sha256=_sha256_file(input_path),
        transport=transport,
    )


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
        raise PublicationLinksError("公开链接私有目录权限必须为0700")
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


_PUBLICATION_LINKS_ERROR_HINTS = {
    "公开链接私有输入不存在；请先使用--init": [
        "── 公开链接待办（私有） ──",
        "  1. 初始化空白链接表：bash scripts/check_publication_links.sh --init",
        "  2. 用编辑器填入作品页、代码仓库、最终录屏三个公开网址，"
        "并把 status 改为 READY_FOR_CHECK",
        "  3. 重新运行 bash scripts/check_publication_links.sh 复核三个公开入口",
    ],
    "公开链接尚未填写完成": [
        "  提示：用编辑器填入三个公开网址并把 status 改为 READY_FOR_CHECK，"
        "再重新运行本脚本。",
    ],
    "三个公开入口必须完整、唯一且类型正确": [
        "  提示：作品页、代码仓库、最终录屏三个网址必须都填写、互不重复且类型正确，"
        "修正后重新运行本脚本。",
    ],
    "公开链接必须是无账号、片段或私有主机的HTTPS地址": [
        "  提示：三个入口必须是可匿名访问的公开 HTTPS 网址，"
        "不能含登录账号、#片段或私有/内网主机。",
    ],
    "公开链接不能包含疑似凭证或签名参数": [
        "  提示：公开网址不能携带 token、签名或密钥类查询参数；改用干净链接后重试。",
    ],
    "公开链接输入不符合严格Schema": [
        "  提示：对照私有链接表模板的字段名与取值检查并修正，再重新运行本脚本。",
    ],
    "公开链接私有输入权限必须为目录0700、文件0600": [
        "  提示：私有目录应为 0700、链接表文件应为 0600；修正后重新运行本脚本。",
    ],
    "公开链接私有目录权限必须为0700": [
        "  提示：私有提交目录权限应为 0700；修正后重新运行本脚本。",
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
            initialize_private_input(args.input)
            print(json.dumps({"status": "PENDING_INPUT", "target_count": 3}, ensure_ascii=False))
            return 0
        report = verify_publication_links(args.input)
        _write_private_json(report, args.output)
    except (ContractValidationError, OSError, PublicationLinksError) as exc:
        if isinstance(exc, PublicationLinksError):
            print_error_hints(str(exc), exact_hints=_PUBLICATION_LINKS_ERROR_HINTS)
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": str(exc)
                    if isinstance(exc, PublicationLinksError)
                    else "公开链接验证失败",
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
                "target_count": report["target_count"],
                "passed": sum(item["status"] == "PASSED" for item in report["targets"]),
                "failed_target_ids": [item["target_id"] for item in report["targets"] if item["status"] == "FAILED"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
