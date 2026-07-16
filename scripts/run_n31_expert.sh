#!/usr/bin/env bash
set -euo pipefail

export LANG="${SKILLFORGE_LOCALE:-zh_CN.UTF-8}"
export LC_ALL="$LANG"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
AUDIO="$ROOT/cases/n31/input/audio/n31_expert_interview.m4a"

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目虚拟环境，请先运行 bash scripts/setup_native.sh" >&2
  exit 1
fi
if [[ ! -f "$AUDIO" ]]; then
  echo "缺少专家录音：cases/n31/input/audio/n31_expert_interview.m4a" >&2
  exit 1
fi

cd "$ROOT"

"$PYTHON" -m skillforge.case_ingest \
  --manifest cases/n31/ingest_manifest.json \
  --output cases/n31/output/ingest_local_v1 >/dev/null

"$PYTHON" -m skillforge.candidate_sop \
  --plan cases/n31/candidate_sop_plan.json \
  --catalog cases/n31/output/ingest_local_v1/evidence_catalog.json \
  --output cases/n31/output/candidate_v1 >/dev/null

"$PYTHON" -m skillforge.ingest \
  --audio "$AUDIO" \
  --output cases/n31/output/expert_audio_v1 \
  --asr \
  --external-processing-authorized \
  --case-id n31_media_change \
  --title "汉印 N31 更换折叠标签纸、缝标学习与试印验收" >/dev/null

"$PYTHON" -m skillforge.expert_gold \
  --candidate-sop cases/n31/output/candidate_v1/candidate_sop.json \
  --asr-manifest cases/n31/output/expert_audio_v1/manifest.json \
  --review-plan cases/n31/expert_review_plan.json \
  --output cases/n31/gold >/dev/null

"$PYTHON" -m skillforge.gold_rehearsal \
  --gold-sop cases/n31/gold/gold_sop.json \
  --constraints cases/n31/gold/constraints.json \
  --faults cases/n31/gold/fault_injection.json \
  --output cases/n31/output/gold_rehearsal_v1
