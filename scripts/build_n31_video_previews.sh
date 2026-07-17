#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${SKILLFORGE_PYTHON_BIN:-$ROOT/.venv/bin/python}"
ARGS=(
  --project-root "$ROOT"
  --ingest-manifest cases/n31/ingest_manifest.json
  --output-profile cases/n31/output_profile.json
  --output-dir cases/n31/output/video_previews_v1
  --report cases/n31/evaluations/video_preview_manifest_v1.json
)

if [[ -n "${SKILLFORGE_N31_SAFE_VIDEO_DIR:-}" ]]; then
  ARGS+=(--source-dir "$SKILLFORGE_N31_SAFE_VIDEO_DIR")
fi

exec "$PYTHON_BIN" -m skillforge.video_preview "${ARGS[@]}"
