#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${SKILLFORGE_SKIP_DOTENV:-0}" != "1" && -f "$ROOT/.env" ]]; then
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

PORT="${SKILLFORGE_PORT:-7860}"
echo "▶ 正在启动 SkillForge Web 演示…" >&2
echo "  浏览器打开：http://127.0.0.1:${PORT}（按 Ctrl+C 停止）" >&2
exec "$ROOT/.venv/bin/python" -m skillforge.web
