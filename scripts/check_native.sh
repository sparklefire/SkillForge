#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "native_env=missing (.venv 尚未建立)"
  exit 1
fi

"$PYTHON" - <<'PY'
from skillforge.media import resolve_ffmpeg, resolve_ffprobe
import fastapi
import fitz
import jsonschema
import uvicorn

print("native_python=ok")
print(f"ffmpeg={resolve_ffmpeg()}")
print(f"ffprobe={resolve_ffprobe() or 'fallback_to_ffmpeg'}")
print("native_dependencies=ok")
PY
