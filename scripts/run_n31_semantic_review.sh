#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
PRIOR_MODEL_CALLS="${SEMANTIC_REVIEW_PRIOR_MODEL_CALLS:-0}"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目虚拟环境，请先运行 bash scripts/setup_native.sh" >&2
  exit 1
fi
if [[ ! -f "$ROOT/cases/n31/gold/gold_sop.json" ]]; then
  echo "缺少N31 Gold SOP，请先运行 bash scripts/run_n31_expert.sh" >&2
  exit 1
fi

cd "$ROOT"
exec "$PYTHON" -m skillforge.semantic_review \
  --gold-sop cases/n31/gold/gold_sop.json \
  --constraints cases/n31/gold/constraints.json \
  --output cases/n31/evaluations/semantic_review_v1.json \
  --prior-model-calls "$PRIOR_MODEL_CALLS" \
  --external-processing-authorized
