#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目虚拟环境，请先运行 bash scripts/setup_native.sh" >&2
  exit 1
fi

cd "$ROOT"
"$PYTHON" -m skillforge.source_candidates \
  --source-plan cases/n31/source_candidate_plan.json \
  --candidate-plan cases/n31/candidate_sop_plan.json \
  --catalog cases/n31/gold/gold_sop.json \
  --output cases/n31/output/source_candidates_v1 \
  --public-report cases/n31/evaluations/source_candidate_synthesis_v1.json
