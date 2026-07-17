#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="tunnel"
LOCAL_PORT="17860"
REMOTE_PORT="7860"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --smoke)
      MODE="smoke"
      shift
      ;;
    --local-port)
      LOCAL_PORT="${2:?--local-port 需要端口号}"
      shift 2
      ;;
    *)
      echo "用法: bash scripts/dgx_demo_tunnel.sh [--smoke] [--local-port PORT]" >&2
      exit 2
      ;;
  esac
done

[[ "$LOCAL_PORT" =~ ^[1-9][0-9]{0,4}$ ]] && (( LOCAL_PORT <= 65535 )) || {
  echo "无效本地端口: $LOCAL_PORT" >&2
  exit 2
}

if [[ -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.env"
fi

# 隧道只需要SSH连接字段，显式清除模型和媒体环境，避免子进程继承无关凭证。
unset STEP_API_KEY STEP_BASE_URL STEP_CHAT_COMPLETIONS_URL STEP_MESSAGES_URL
unset STEP_ASR_URL STEP_MODEL SKILLFORGE_FFMPEG_BIN SKILLFORGE_FFPROBE_BIN

: "${DGX_SSH_HOST:?DGX_SSH_HOST 未配置}"
: "${DGX_SSH_PORT:?DGX_SSH_PORT 未配置}"
: "${DGX_SSH_USER:?DGX_SSH_USER 未配置}"

SSH_TARGET="$DGX_SSH_USER@$DGX_SSH_HOST"
SSH_BASE=(
  ssh
  -o BatchMode=yes
  -o ConnectTimeout=12
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -p "$DGX_SSH_PORT"
)

"${SSH_BASE[@]}" "$SSH_TARGET" \
  'systemctl --user start skillforge-demo.service && systemctl --user is-active --quiet skillforge-demo.service'

if [[ "$MODE" == "tunnel" ]]; then
  echo "SkillForge DGX 演示地址: http://127.0.0.1:$LOCAL_PORT"
  echo "按 Ctrl+C 关闭隧道；DGX 服务不会暴露到公网。"
  exec "${SSH_BASE[@]}" \
    -N -T \
    -L "127.0.0.1:$LOCAL_PORT:127.0.0.1:$REMOTE_PORT" \
    "$SSH_TARGET"
fi

"${SSH_BASE[@]}" \
  -N -T \
  -L "127.0.0.1:$LOCAL_PORT:127.0.0.1:$REMOTE_PORT" \
  "$SSH_TARGET" &
tunnel_pid=$!
trap 'kill "$tunnel_pid" 2>/dev/null || true; wait "$tunnel_pid" 2>/dev/null || true' EXIT

health_file="$(mktemp)"
case_file="$(mktemp)"
trap 'rm -f "$health_file" "$case_file"; kill "$tunnel_pid" 2>/dev/null || true; wait "$tunnel_pid" 2>/dev/null || true' EXIT

for _ in $(seq 1 20); do
  if curl -fsS --max-time 3 "http://127.0.0.1:$LOCAL_PORT/health" >"$health_file" 2>/dev/null; then
    break
  fi
  sleep 1
done

curl -fsS --max-time 5 "http://127.0.0.1:$LOCAL_PORT/health" >"$health_file"
curl -fsS --max-time 5 "http://127.0.0.1:$LOCAL_PORT/api/n31" >"$case_file"
python3 - "$health_file" "$case_file" <<'PY'
import json
import sys

health = json.load(open(sys.argv[1], encoding="utf-8"))
payload = json.load(open(sys.argv[2], encoding="utf-8"))
summary = payload.get("summary", {})
result = {
    "health": health.get("status"),
    "runtime": health.get("runtime"),
    "docker_required": health.get("docker_required"),
    "gold_status": summary.get("gold_status"),
    "metrics_status": summary.get("metrics_status"),
    "workflow_state": summary.get("workflow_state"),
    "severe_before": summary.get("before", {}).get("severe_error_count"),
    "severe_after": summary.get("after", {}).get("severe_error_count"),
    "revision_count": summary.get("revision_count"),
}
assert result == {
    "health": "ok",
    "runtime": "native-python",
    "docker_required": False,
    "gold_status": "GOLD",
    "metrics_status": "FINAL",
    "workflow_state": "COMPLETED",
    "severe_before": 5,
    "severe_after": 0,
    "revision_count": 4,
}, result
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
PY

echo "tunnel_smoke=passed local=http://127.0.0.1:$LOCAL_PORT remote=127.0.0.1:$REMOTE_PORT"
