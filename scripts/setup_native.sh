#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.lock
.venv/bin/python -m pip install \
  --disable-pip-version-check \
  --no-deps \
  --no-build-isolation \
  -e .

echo "native_setup=ok"
