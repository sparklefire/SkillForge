#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "缺少 .venv。请先建立环境并安装 requirements.lock，再以 --no-deps 安装当前项目。" >&2
  exit 1
fi

mkdir -p "$ROOT/outputs"
cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m skillforge.web
