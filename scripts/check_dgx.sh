#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

: "${DGX_SSH_HOST:?DGX_SSH_HOST 未配置}"
: "${DGX_SSH_PORT:?DGX_SSH_PORT 未配置}"
: "${DGX_SSH_USER:?DGX_SSH_USER 未配置}"

ssh \
  -o BatchMode=yes \
  -o ConnectTimeout=12 \
  -o StrictHostKeyChecking=accept-new \
  -p "$DGX_SSH_PORT" \
  "$DGX_SSH_USER@$DGX_SSH_HOST" \
  'set -u
  echo "user=$(id -un)"
  echo "host=$(hostname)"
  echo "arch=$(uname -m)"
  nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
  python3 --version 2>/dev/null || echo "python=missing"
  docker --version 2>/dev/null || echo "docker_cli=missing"
  command -v ffmpeg >/dev/null 2>&1 && ffmpeg -version | head -n 1 || echo "ffmpeg=missing"
  command -v nvcc >/dev/null 2>&1 && nvcc --version | tail -n 1 || echo "nvcc=missing"
  if docker info >/dev/null 2>&1; then
    echo "docker_daemon=accessible"
  else
    echo "docker_daemon=inaccessible"
  fi
  free -h | sed -n "1,2p"
  df -h "$HOME" | tail -n 1'

