#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SKILLFORGE_PYTHON_BIN:-$ROOT/.venv/bin/python}"

[[ -x "$PYTHON_BIN" ]] || {
  echo "缺少项目Python虚拟环境" >&2
  exit 1
}

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" -m skillforge.guided_human_review "$@"
