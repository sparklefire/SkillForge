"""Step Plan client with central routing, JSON validation, retry and safe logging."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
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


class StepPlanTransportError(StepPlanError):
    """A classified transport failure that is safe to log and retry."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code


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
        retry_sleep: Callable[[float], None] = time.sleep,
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
        self.retry_sleep = retry_sleep
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
                    "--write-out",
                    "\n%{http_code}",
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
            body = completed.stdout
            status_code: int | None = None
            if "\n" in body:
                possible_body, possible_status = body.rsplit("\n", 1)
                if possible_status.isdigit() and len(possible_status) == 3:
                    body = possible_body
                    status_code = int(possible_status)
            if completed.returncode != 0 or (status_code is not None and status_code >= 400):
                if completed.returncode == 28 or status_code == 408:
                    category = "TIMEOUT"
                    retryable = True
                elif status_code == 429:
                    category = "RATE_LIMIT"
                    retryable = True
                elif status_code is not None and 500 <= status_code <= 599:
                    category = "SERVICE_UNAVAILABLE"
                    retryable = True
                elif status_code is None or status_code == 0:
                    category = "CONNECTION"
                    retryable = completed.returncode in {5, 6, 7, 18, 28, 52, 55, 56}
                else:
                    category = "HTTP_CLIENT_ERROR"
                    retryable = False
                detail = redact((completed.stderr or "").strip())
                suffix = f"；curl={completed.returncode}"
                if detail:
                    suffix += f"；detail={str(detail)[:300]}"
                raise StepPlanTransportError(
                    f"Step Plan传输失败：{category}{suffix}",
                    category=category,
                    retryable=retryable,
                    status_code=status_code,
                )
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError as exc:
                raise StepPlanTransportError(
                    "Step Plan HTTP响应不是合法JSON",
                    category="INVALID_HTTP_JSON",
                    retryable=True,
                    status_code=status_code,
                ) from exc
            if not isinstance(parsed, dict):
                raise StepPlanTransportError(
                    "Step Plan HTTP响应不是JSON对象",
                    category="INVALID_HTTP_JSON",
                    retryable=True,
                    status_code=status_code,
                )
            return parsed
        except subprocess.TimeoutExpired as exc:
            raise StepPlanTransportError(
                "Step Plan子进程请求超时",
                category="TIMEOUT",
                retryable=True,
            ) from exc
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
        attempts_used = 0

        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
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
            try:
                response = self.transport(payload)
                if not isinstance(response, dict):
                    raise StepPlanTransportError(
                        "Step Plan传输返回值不是JSON对象",
                        category="INVALID_HTTP_JSON",
                        retryable=True,
                    )
            except (TimeoutError, ConnectionError) as exc:
                transport_error = StepPlanTransportError(
                    "Step Plan连接或请求超时",
                    category=("TIMEOUT" if isinstance(exc, TimeoutError) else "CONNECTION"),
                    retryable=True,
                )
                last_error = transport_error
                self.logger.emit(
                    "step_plan.transport_error",
                    model=selected["model"],
                    schema=schema_name,
                    attempt=attempt,
                    category=transport_error.category,
                    retryable=True,
                    status_code=None,
                )
                if attempt < max_attempts:
                    delay = min(2.0, 0.5 * (2 ** (attempt - 1)))
                    self.logger.emit(
                        "step_plan.retry",
                        model=selected["model"],
                        schema=schema_name,
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        reason=transport_error.category,
                        delay_seconds=delay,
                    )
                    self.retry_sleep(delay)
                    continue
                break
            except StepPlanTransportError as exc:
                last_error = exc
                self.logger.emit(
                    "step_plan.transport_error",
                    model=selected["model"],
                    schema=schema_name,
                    attempt=attempt,
                    category=exc.category,
                    retryable=exc.retryable,
                    status_code=exc.status_code,
                )
                if exc.retryable and attempt < max_attempts:
                    delay = min(2.0, 0.5 * (2 ** (attempt - 1)))
                    self.logger.emit(
                        "step_plan.retry",
                        model=selected["model"],
                        schema=schema_name,
                        attempt=attempt,
                        next_attempt=attempt + 1,
                        reason=exc.category,
                        delay_seconds=delay,
                    )
                    self.retry_sleep(delay)
                    continue
                break
            content: Any = None
            try:
                content = self._content(response)
                document = parse_and_validate(content, schema_name)
                usage = response.get("usage") or {}
                self.logger.emit(
                    "step_plan.success",
                    model=response.get("model", selected["model"]),
                    schema=schema_name,
                    attempt=attempt,
                    total_tokens=usage.get("total_tokens"),
                )
                return document
            except (ValueError, TypeError, ContractValidationError, StepPlanError) as exc:
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
                    content_length=len(str(content)) if content is not None else 0,
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
        if isinstance(last_error, StepPlanTransportError):
            raise StepPlanError(
                f"Step Plan传输在{attempts_used}次尝试内未成功：{last_error.category}"
            ) from last_error
        raise StepPlanError(
            f"模型输出连续 {max_attempts} 次未通过 {schema_name} 校验: "
            f"{type(last_error).__name__}"
        ) from last_error
