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
  if command -v ffmpeg >/dev/null 2>&1; then
    ffmpeg -version | head -n 1
  elif [[ -x "$HOME/skillforge/bin/ffmpeg" ]]; then
    "$HOME/skillforge/bin/ffmpeg" -version | head -n 1 | sed "s/^/ffmpeg_user=/"
  else
    echo "ffmpeg=missing"
  fi
  command -v nvcc >/dev/null 2>&1 && nvcc --version | tail -n 1 || echo "nvcc=missing"
  echo "docker_service=$(systemctl is-active docker 2>/dev/null || true)"
  stat -c "docker_socket=%A:%U:%G" /var/run/docker.sock 2>/dev/null || echo "docker_socket=missing"
  if docker info >/dev/null 2>&1; then
    echo "docker_daemon=accessible"
  else
    echo "docker_daemon=inaccessible"
  fi
  command -v nvidia-ctk >/dev/null 2>&1 && echo "nvidia_ctk=present" || echo "nvidia_ctk=missing"
  if nvidia-container-cli -k -d /dev/null info >/dev/null 2>&1; then
    echo "nvidia_container_cli=gpu_accessible"
  else
    echo "nvidia_container_cli=gpu_inaccessible"
  fi
  test -d "$HOME/skillforge/data/input" && echo "skillforge_dirs=present" || echo "skillforge_dirs=missing"
  free -h | sed -n "1,2p"
  df -h "$HOME" | tail -n 1'
