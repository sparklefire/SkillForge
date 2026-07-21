#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "缺少 python3，请先安装 Python 3.10+ 再重跑本脚本。" >&2
  exit 1
fi

if [[ ! -f requirements.lock ]]; then
  echo "缺少 requirements.lock，无法安装依赖；请在项目根目录执行。" >&2
  exit 1
fi

echo "▶ [1/3] 创建虚拟环境 .venv…" >&2
python3 -m venv .venv
echo "▶ [2/3] 安装依赖（首次约需 1-2 分钟，请耐心等候）…" >&2
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.lock
echo "▶ [3/3] 安装 SkillForge 项目包…" >&2
.venv/bin/python -m pip install \
  --disable-pip-version-check \
  --no-deps \
  --no-build-isolation \
  -e .

echo "native_setup=ok"
echo "✅ 环境就绪。下一步可运行：bash scripts/run_demo_mode.sh offline（离线演示，最稳定）" >&2
