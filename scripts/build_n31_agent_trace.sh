#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SKILLFORGE_PYTHON_BIN:-$ROOT/.venv/bin/python}"

exec "$PYTHON_BIN" -m skillforge.agent_trace \
  --project-root "$ROOT" \
  --output cases/n31/evaluations/agent_tool_trace_v1.json
