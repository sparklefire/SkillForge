#!/usr/bin/env python3
"""用最小请求验证 Step Plan 密钥和 step-3.7-flash，且不打印密钥。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_dotenv(ROOT / ".env")

    api_key = os.getenv("STEP_API_KEY")
    url = os.getenv(
        "STEP_CHAT_COMPLETIONS_URL",
        "https://api.stepfun.com/step_plan/v1/chat/completions",
    )
    model = os.getenv("STEP_MODEL", "step-3.7-flash")

    if not api_key:
        print("STEP_PLAN_ERROR: STEP_API_KEY 未配置", file=sys.stderr)
        return 2

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "这是接入健康检查。请只回复 STEP_PLAN_OK。",
            }
        ],
        "reasoning_effort": "low",
        "max_tokens": 256,
    }

    # 当前开发机的 Python 系统证书链可能含自签名代理证书。使用系统 curl
    # 继续执行完整 TLS 校验；Authorization 放入 600 权限临时文件，避免出现在参数列表。
    header_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as headers:
            header_path = headers.name
            headers.write("Content-Type: application/json\n")
            headers.write(f"Authorization: Bearer {api_key}\n")
        os.chmod(header_path, 0o600)
        completed = subprocess.run(
            [
                "curl",
                "--silent",
                "--show-error",
                "--fail-with-body",
                "--connect-timeout",
                "15",
                "--max-time",
                "60",
                "--header",
                f"@{header_path}",
                "--data-binary",
                "@-",
                url,
            ],
            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            body = completed.stdout.decode("utf-8", errors="replace")
            error = completed.stderr.decode("utf-8", errors="replace")
            print(f"STEP_PLAN_HTTP_ERROR: {(body or error)[:1000]}", file=sys.stderr)
            return 1
        result = json.loads(completed.stdout.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 健康检查需要给出可操作错误。
        print(f"STEP_PLAN_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if header_path:
            Path(header_path).unlink(missing_ok=True)

    choices = result.get("choices") or []
    content = ""
    if choices:
        content = choices[0].get("message", {}).get("content", "")
    finish_reason = choices[0].get("finish_reason", "unknown") if choices else "missing"
    usage = result.get("usage") or {}
    print(
        "STEP_PLAN_OK "
        f"model={result.get('model', model)} "
        f"total_tokens={usage.get('total_tokens', 'unknown')} "
        f"finish_reason={finish_reason} "
        f"reply={content.strip()[:200]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
