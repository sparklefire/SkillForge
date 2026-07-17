#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
SOURCE_ROOT="${1:-$HOME/skillforge/data/n31_safe/video}"
DATA_ROOT="${SKILLFORGE_DGX_DATA_ROOT:-$HOME/skillforge/data/n31_safe}"

if [[ ! -x "$PYTHON" ]]; then
  echo "native_env=missing (.venv 尚未建立)"
  exit 1
fi

umask 077
mkdir -p "$DATA_ROOT/frames" "$HOME/skillforge/bin"

"$PYTHON" -m skillforge.dgx_visual \
  --manifest "$ROOT/cases/n31/ingest_manifest.json" \
  --source-root "$SOURCE_ROOT" \
  --frame-root "$DATA_ROOT/frames" \
  --binary "$HOME/skillforge/bin/dgx_frame_features" \
  --output "$ROOT/cases/n31/evaluations/dgx_visual_compute_v1.json" \
  --allow-dgx-safe-derivatives
