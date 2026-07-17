#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目虚拟环境，请先运行 bash scripts/setup_native.sh" >&2
  exit 1
fi

cd "$ROOT"
"$PYTHON" -m skillforge.grounding_gate \
  --gold-sop cases/n31/gold/gold_sop.json \
  --constraints cases/n31/gold/constraints.json \
  --output cases/n31/evaluations/deterministic_grounding_gate_v1.json
