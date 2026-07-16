#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
MODE="${1:-offline}"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目虚拟环境，请先运行 bash scripts/setup_native.sh" >&2
  exit 1
fi

cd "$ROOT"
case "$MODE" in
  live)
    "$PYTHON" -m skillforge.gold_rehearsal \
      --gold-sop cases/n31/gold/gold_sop.json \
      --constraints cases/n31/gold/constraints.json \
      --faults cases/n31/gold/fault_injection.json \
      --output cases/n31/output/demo_live >/dev/null
    export SKILLFORGE_N31_DIR="$ROOT/cases/n31/output/demo_live"
    ;;
  preprocessed)
    bash scripts/run_n31_local.sh >/dev/null
    export SKILLFORGE_N31_DIR="$ROOT/cases/n31/output/gold_rehearsal_v1"
    ;;
  offline)
    if [[ ! -f cases/n31/demo_bundle/summary.json ]]; then
      echo "缺少离线演示包，请先运行 .venv/bin/python scripts/build_n31_demo_bundle.py" >&2
      exit 1
    fi
    export SKILLFORGE_N31_DIR="$ROOT/cases/n31/demo_bundle"
    ;;
  *)
    echo "用法: bash scripts/run_demo_mode.sh [live|preprocessed|offline]" >&2
    exit 2
    ;;
esac

exec bash scripts/start_native.sh
