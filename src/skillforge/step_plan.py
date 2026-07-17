"""Step Plan client with central routing, JSON validation, retry and safe logging."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .contracts import ContractValidationError
from .json_tools import parse_and_validate
from .model_router import ModelRouter
from .observability import StructuredLogger, redact


ROOT = Path(__file__).resolve().parents[2]


class StepPlanError(RuntimeError):
    """A sanitized Step Plan request or response failure."""


def load_dotenv(path: Path | None = None) -> None:
    dotenv = path or ROOT / ".env"
    if not dotenv.exists():
        return
    for raw_line in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class StepPlanClient:
    def __init__(
        self,
        *,
        router: ModelRouter | None = None,
        logger: StructuredLogger | None = None,
        timeout_seconds: int = 90,
        transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        load_dotenv()
        self.api_key = os.getenv("STEP_API_KEY", "")
        self.url = os.getenv(
            "STEP_CHAT_COMPLETIONS_URL",
            "https://api.stepfun.com/step_plan/v1/chat/completions",
        )
        self.router = router or ModelRouter()
        self.logger = logger or StructuredLogger()
        self.timeout_seconds = timeout_seconds
        self.transport = transport or self._curl_transport
        self.call_count = 0

    def _curl_transport(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise StepPlanError("STEP_API_KEY 未配置")

        header_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False
            ) as header_file:
                header_path = header_file.name
                header_file.write("Content-Type: application/json\n")
                header_file.write(f"Authorization: Bearer {self.api_key}\n")
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
                    str(self.timeout_seconds),
                    "--header",
                    f"@{header_path}",
                    "--data-binary",
                    "@-",
                    self.url,
                ],
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stdout or completed.stderr or "unknown error")[:1200]
                raise StepPlanError(f"Step Plan HTTP 请求失败: {redact(detail)}")
            parsed = json.loads(completed.stdout)
            if not isinstance(parsed, dict):
                raise StepPlanError("Step Plan 响应不是 JSON 对象")
            return parsed
        except subprocess.TimeoutExpired as exc:
            raise StepPlanError("Step Plan 请求超时") from exc
        except json.JSONDecodeError as exc:
            raise StepPlanError("Step Plan HTTP 响应不是合法 JSON") from exc
        finally:
            if header_path:
                Path(header_path).unlink(missing_ok=True)

    @staticmethod
    def _content(response: dict[str, Any]) -> Any:
        choices = response.get("choices") or []
        if not choices:
            raise StepPlanError("Step Plan 响应缺少 choices")
        message = choices[0].get("message") or {}
        if "content" not in message:
            raise StepPlanError("Step Plan 响应缺少 message.content")
        return message["content"]

    def chat_json(
        self,
        *,
        messages: list[dict[str, Any]],
        route: str,
        schema_name: str,
        max_attempts: int = 3,
        max_tokens: int = 4096,
    ) -> Any:
        if not 1 <= max_attempts <= 3:
            raise ValueError("max_attempts 必须在 1 到 3 之间")
        selected = self.router.reasoning(route)
        working_messages = list(messages)
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            payload = {
                "model": selected["model"],
                "messages": working_messages,
                "reasoning_effort": selected["reasoning_effort"],
                "max_tokens": max_tokens,
            }
            self.logger.emit(
                "step_plan.request",
                model=selected["model"],
                reasoning_effort=selected["reasoning_effort"],
                schema=schema_name,
                attempt=attempt,
                message_count=len(working_messages),
            )
            self.call_count += 1
            response = self.transport(payload)
            try:
                document = parse_and_validate(self._content(response), schema_name)
                usage = response.get("usage") or {}
                self.logger.emit(
                    "step_plan.success",
                    model=response.get("model", selected["model"]),
                    schema=schema_name,
                    attempt=attempt,
                    total_tokens=usage.get("total_tokens"),
                )
                return document
            except (ValueError, TypeError, ContractValidationError) as exc:
                last_error = exc
                self.logger.emit(
                    "step_plan.invalid_json",
                    model=selected["model"],
                    schema=schema_name,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                    finish_reason=(
                        (response.get("choices") or [{}])[0].get("finish_reason")
                    ),
                    content_length=len(str(self._content(response))),
                )
                if attempt < max_attempts:
                    working_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "上一条输出未通过 JSON Schema 校验。"
                                f"错误：{str(exc)[:600]}。"
                                "请仅返回修正后的 JSON，不要使用 Markdown 代码块。"
                            ),
                        }
                    )

        assert last_error is not None
        raise StepPlanError(
            f"模型输出连续 {max_attempts} 次未通过 {schema_name} 校验: "
            f"{type(last_error).__name__}"
        ) from last_error
