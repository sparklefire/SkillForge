"""StepAudio SSE ASR client using the official Step Plan request format."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .observability import StructuredLogger, redact
from .step_plan import StepPlanError, load_dotenv


def parse_asr_sse(raw: str) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    done_text = ""
    usage: dict[str, Any] = {}
    event_count = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise StepPlanError("ASR SSE 包含非法 JSON 事件") from exc
        event_count += 1
        event_type = event.get("type")
        if event_type == "error":
            raise StepPlanError(f"ASR 返回错误: {redact(str(event.get('message', 'unknown')))}")
        if event_type == "transcript.text.delta":
            segments.append(
                {
                    "text": str(event.get("delta", "")),
                    "start_ms": event.get("start_time"),
                    "end_ms": event.get("end_time"),
                }
            )
        elif event_type == "transcript.text.done":
            done_text = str(event.get("text", ""))
            usage = event.get("usage") or {}
    if not event_count:
        raise StepPlanError("ASR SSE 响应没有 data 事件")
    return {
        "text": done_text or "".join(segment["text"] for segment in segments),
        "segments": segments,
        "usage": usage,
        "event_count": event_count,
    }


class StepAudioASRClient:
    def __init__(
        self,
        *,
        logger: StructuredLogger | None = None,
        timeout_seconds: int = 180,
    ) -> None:
        load_dotenv()
        self.api_key = os.getenv("STEP_API_KEY", "")
        self.url = os.getenv(
            "STEP_ASR_URL",
            "https://api.stepfun.com/step_plan/v1/audio/asr/sse",
        )
        self.logger = logger or StructuredLogger()
        self.timeout_seconds = timeout_seconds

    def transcribe_wav(
        self,
        path: Path,
        *,
        language: str = "zh",
        hotwords: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise StepPlanError("STEP_API_KEY 未配置")
        audio = path.read_bytes()
        payload = {
            "audio": {
                "data": base64.b64encode(audio).decode("ascii"),
                "input": {
                    "transcription": {
                        "language": language,
                        "hotwords": hotwords or [],
                        "model": "stepaudio-2.5-asr",
                        "enable_itn": True,
                        "enable_timestamp": True,
                    },
                    "format": {"type": "wav"},
                },
            }
        }
        self.logger.emit(
            "step_audio.asr.request",
            model="stepaudio-2.5-asr",
            language=language,
            bytes=len(audio),
            hotword_count=len(hotwords or []),
        )
        header_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", delete=False
            ) as header_file:
                header_path = header_file.name
                header_file.write("Content-Type: application/json\n")
                header_file.write("Accept: text/event-stream\n")
                header_file.write(f"Authorization: Bearer {self.api_key}\n")
            os.chmod(header_path, 0o600)
            completed = subprocess.run(
                [
                    "curl",
                    "--silent",
                    "--show-error",
                    "--fail-with-body",
                    "--no-buffer",
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
                detail = (completed.stdout or completed.stderr or "unknown error")[-1200:]
                raise StepPlanError(f"StepAudio ASR 请求失败: {redact(detail)}")
            result = parse_asr_sse(completed.stdout)
            self.logger.emit(
                "step_audio.asr.success",
                model="stepaudio-2.5-asr",
                event_count=result["event_count"],
                segment_count=len(result["segments"]),
                text_length=len(result["text"]),
                total_tokens=result["usage"].get("total_tokens"),
            )
            return result
        finally:
            if header_path:
                Path(header_path).unlink(missing_ok=True)
