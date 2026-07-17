#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目虚拟环境，请先运行 bash scripts/setup_native.sh" >&2
  exit 1
fi
if [[ ! -f "$ROOT/cases/n31/gold/gold_sop.json" ]]; then
  echo "缺少N31 Gold SOP，请先运行 bash scripts/run_n31_expert.sh" >&2
  exit 1
fi

cd "$ROOT"
"$PYTHON" -m skillforge.training_video "$@"
"$PYTHON" scripts/build_checklist_thumbnails.py >/dev/null
