#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
FRAMES="$ROOT/cases/n31/output/ingest_local_v1"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目虚拟环境，请先运行 bash scripts/setup_native.sh" >&2
  exit 1
fi
if [[ ! -f "$ROOT/cases/n31/gold/gold_sop.json" ]]; then
  echo "缺少N31 Gold SOP，请先运行 bash scripts/run_n31_expert.sh" >&2
  exit 1
fi
if [[ ! -d "$FRAMES/derived/video" ]]; then
  echo "缺少安全关键帧，请先运行 bash scripts/run_n31_local.sh" >&2
  exit 1
fi

cd "$ROOT"
exec "$PYTHON" -m skillforge.visual_review \
  --gold-sop cases/n31/gold/gold_sop.json \
  --frame-root cases/n31/output/ingest_local_v1 \
  --output cases/n31/output/visual_review_v1/visual_sequence_review.json \
  --max-frames-per-step 6 \
  --external-processing-authorized
