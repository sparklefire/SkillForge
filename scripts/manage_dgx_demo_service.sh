#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="skillforge-demo.service"
SOURCE_UNIT="$ROOT/deploy/systemd/$UNIT_NAME"
USER_UNIT_DIR="$HOME/.config/systemd/user"
INSTALLED_UNIT="$USER_UNIT_DIR/$UNIT_NAME"
HEALTH_URL="http://127.0.0.1:7860/health"
CASE_URL="http://127.0.0.1:7860/api/n31"
ACTION="${1:-verify}"

require_runtime() {
  [[ -x "$ROOT/.venv/bin/python" ]] || {
    echo "缺少 $ROOT/.venv/bin/python" >&2
    exit 1
  }
  [[ -f "$ROOT/cases/n31/demo_bundle/bundle.json" ]] || {
    echo "缺少离线演示包" >&2
    exit 1
  }
  [[ -f "$SOURCE_UNIT" ]] || {
    echo "缺少用户服务模板" >&2
    exit 1
  }
}

wait_for_health() {
  local attempt
  for attempt in $(seq 1 20); do
    if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "SkillForge 健康检查超时" >&2
  systemctl --user status "$UNIT_NAME" --no-pager >&2 || true
  exit 1
}

verify_payload() {
  local health_file case_file
  health_file="$(mktemp)"
  case_file="$(mktemp)"
  trap 'rm -f "$health_file" "$case_file"' RETURN
  curl -fsS --max-time 5 "$HEALTH_URL" >"$health_file"
  curl -fsS --max-time 5 "$CASE_URL" >"$case_file"
  "$ROOT/.venv/bin/python" - "$health_file" "$case_file" <<'PY'
import json
import sys

health = json.load(open(sys.argv[1], encoding="utf-8"))
payload = json.load(open(sys.argv[2], encoding="utf-8"))
summary = payload.get("summary", {})
expected = {
    "status": health.get("status"),
    "runtime": health.get("runtime"),
    "docker_required": health.get("docker_required"),
    "gold_status": summary.get("gold_status"),
    "metrics_status": summary.get("metrics_status"),
    "workflow_state": summary.get("workflow_state"),
    "severe_before": summary.get("before", {}).get("severe_error_count"),
    "severe_after": summary.get("after", {}).get("severe_error_count"),
    "revision_count": summary.get("revision_count"),
}
assert expected == {
    "status": "ok",
    "runtime": "native-python",
    "docker_required": False,
    "gold_status": "GOLD",
    "metrics_status": "FINAL",
    "workflow_state": "COMPLETED",
    "severe_before": 5,
    "severe_after": 0,
    "revision_count": 4,
}, expected
print(json.dumps(expected, ensure_ascii=False, sort_keys=True))
PY
  rm -f "$health_file" "$case_file"
  trap - RETURN
}

verify_service() {
  require_runtime
  [[ "$(systemctl --user is-active "$UNIT_NAME")" == "active" ]]
  [[ "$(systemctl --user is-enabled "$UNIT_NAME")" == "enabled" ]]
  wait_for_health
  verify_payload
  echo "service=active enabled=enabled bind=127.0.0.1:7860"
}

case "$ACTION" in
  install)
    require_runtime
    install -d -m 700 "$USER_UNIT_DIR"
    install -m 600 "$SOURCE_UNIT" "$INSTALLED_UNIT"
    systemd-analyze --user verify "$INSTALLED_UNIT"
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT_NAME"
    verify_service
    ;;
  verify)
    verify_service
    ;;
  restart-test)
    verify_service >/dev/null
    before_pid="$(systemctl --user show "$UNIT_NAME" -p MainPID --value)"
    [[ "$before_pid" =~ ^[1-9][0-9]*$ ]]
    systemctl --user kill --kill-whom=main --signal=KILL "$UNIT_NAME"
    after_pid=""
    for _ in $(seq 1 20); do
      after_pid="$(systemctl --user show "$UNIT_NAME" -p MainPID --value)"
      if [[ "$after_pid" =~ ^[1-9][0-9]*$ ]] && [[ "$after_pid" != "$before_pid" ]]; then
        break
      fi
      sleep 1
    done
    [[ "$after_pid" =~ ^[1-9][0-9]*$ ]]
    [[ "$after_pid" != "$before_pid" ]]
    wait_for_health
    verify_payload
    echo "restart=passed old_pid=$before_pid new_pid=$after_pid"
    ;;
  uninstall)
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null || true
    rm -f "$INSTALLED_UNIT"
    systemctl --user daemon-reload
    systemctl --user reset-failed "$UNIT_NAME" 2>/dev/null || true
    echo "service=uninstalled"
    ;;
  *)
    echo "用法: bash scripts/manage_dgx_demo_service.sh [install|verify|restart-test|uninstall]" >&2
    exit 2
    ;;
esac
