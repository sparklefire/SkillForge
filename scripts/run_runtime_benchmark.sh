#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-local}"
WARMUP="${SKILLFORGE_BENCHMARK_WARMUP:-2}"
ITERATIONS="${SKILLFORGE_BENCHMARK_ITERATIONS:-20}"

[[ -x "$ROOT/.venv/bin/python" ]] || {
  echo "缺少项目虚拟环境" >&2
  exit 1
}

case "$MODE" in
  local)
    LOCATION="LOCAL_DEVELOPMENT"
    ACCELERATOR="NONE"
    OUTPUT="$ROOT/output/evaluation/runtime_benchmark_local.json"
    ;;
  dgx)
    LOCATION="DGX_SPARK"
    ACCELERATOR="NVIDIA GB10"
    OUTPUT="$ROOT/output/evaluation/runtime_benchmark_dgx.json"
    ;;
  *)
    echo "用法: bash scripts/run_runtime_benchmark.sh [local|dgx]" >&2
    exit 2
    ;;
esac

cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m skillforge.runtime_benchmark \
  --output "$OUTPUT" \
  --location "$LOCATION" \
  --accelerator "$ACCELERATOR" \
  --warmup "$WARMUP" \
  --iterations "$ITERATIONS"
