#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目虚拟环境，请先运行 bash scripts/setup_native.sh" >&2
  exit 1
fi

cd "$ROOT"
"$PYTHON" -m skillforge.selective_rebuild \
  --before cases/n31/demo_bundle/before_sop.json \
  --after cases/n31/demo_bundle/after_sop.json \
  --audit cases/n31/demo_bundle/revision_audit.json \
  --gold cases/n31/gold/gold_sop.json \
  --storyboard cases/n31/training_video_storyboard.json \
  --output cases/n31/evaluations/selective_rebuild_v1.json
