#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="python3"
fi

cd "${ROOT}"
PYTHONPATH="${ROOT}/src" "${PYTHON}" -m skillforge.step_discovery \
  --evidence cases/demo_case/synthetic/discovery_evidence.json \
  --response-fixture cases/demo_case/synthetic/discovery_response_fixture.json \
  --output outputs/step_discovery/report.json
PYTHONPATH="${ROOT}/src" "${PYTHON}" -m skillforge.step_discovery_eval \
  --discovery outputs/step_discovery/report.json \
  --spec cases/demo_case/synthetic/discovery_evaluation_spec.json \
  --output outputs/step_discovery/evaluation.json
