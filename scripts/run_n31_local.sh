#!/usr/bin/env bash
set -euo pipefail

export LANG="${SKILLFORGE_LOCALE:-zh_CN.UTF-8}"
export LC_ALL="$LANG"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$ROOT/.venv/bin/python"
MODE="preprocessed"

if [[ "${1:-}" == "--with-video-processing" ]]; then
  MODE="full-local"
elif [[ -n "${1:-}" ]]; then
  echo "用法: bash scripts/run_n31_local.sh [--with-video-processing]" >&2
  exit 2
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "缺少项目虚拟环境，请先运行 bash scripts/setup_native.sh" >&2
  exit 1
fi

cd "$ROOT"

if [[ "$MODE" == "full-local" ]]; then
  "$PYTHON" scripts/process_n31_videos.py >/dev/null
fi

if [[ "${SKILLFORGE_OFFLINE_OCR:-0}" == "1" ]]; then
  bash scripts/setup_ocr_languages.sh --offline >/dev/null
else
  bash scripts/setup_ocr_languages.sh >/dev/null
fi

"$PYTHON" -m skillforge.case_ingest \
  --manifest cases/n31/ingest_manifest.json \
  --output cases/n31/output/ingest_local_v1 >/dev/null

"$PYTHON" -m skillforge.candidate_sop \
  --plan cases/n31/candidate_sop_plan.json \
  --catalog cases/n31/output/ingest_local_v1/evidence_catalog.json \
  --output cases/n31/output/candidate_v1 >/dev/null

if [[ -f cases/n31/gold/gold_sop.json ]]; then
  bash scripts/build_n31_source_candidates.sh >/dev/null
  "$PYTHON" -m skillforge.gold_rehearsal \
    --gold-sop cases/n31/gold/gold_sop.json \
    --constraints cases/n31/gold/constraints.json \
    --faults cases/n31/gold/fault_injection.json \
    --output cases/n31/output/gold_rehearsal_v1 >/dev/null
  REHEARSAL_DIR="cases/n31/output/gold_rehearsal_v1"
else
  "$PYTHON" -m skillforge.provisional_rehearsal \
    --candidate-sop cases/n31/output/candidate_v1/candidate_sop.json \
    --constraints cases/n31/provisional_constraints.json \
    --faults cases/n31/provisional_fault_injection.json \
    --output cases/n31/output/rehearsal_v1 >/dev/null
  REHEARSAL_DIR="cases/n31/output/rehearsal_v1"
fi

"$PYTHON" - "$REHEARSAL_DIR" <<'PY'
import json
import sys
from pathlib import Path

root = Path.cwd()
rehearsal_dir = root / sys.argv[1]
ingest = json.loads(
    (root / "cases/n31/output/ingest_local_v1/manifest.json").read_text(encoding="utf-8")
)
candidate = json.loads(
    (root / "cases/n31/output/candidate_v1/human_review_queue.json").read_text(
        encoding="utf-8"
    )
)
source_candidates_path = (
    root / "cases/n31/output/source_candidates_v1/source_candidate_synthesis.json"
)
source_candidates = (
    json.loads(source_candidates_path.read_text(encoding="utf-8"))
    if source_candidates_path.is_file()
    else None
)
rehearsal = json.loads(
    (rehearsal_dir / "summary.json").read_text(encoding="utf-8")
)
print(
    json.dumps(
        {
            "status": "N31_LOCAL_PIPELINE_READY",
            "external_model_calls": 0,
            "ingest": {
                "source_count": ingest["source_count"],
                "evidence_count": ingest["evidence_count"],
            },
            "candidate": {
                "step_count": candidate["step_count"],
                "gold_status": candidate["gold_status"],
            },
            "source_candidates": (
                {
                    "candidate_count": source_candidates["summary"]["source_candidate_count"],
                    "source_candidate_counts": source_candidates["summary"]["source_candidate_counts"],
                    "merged_step_count": source_candidates["summary"]["ordered_step_count"],
                    "multi_source_step_count": source_candidates["summary"]["multi_source_step_count"],
                    "coarse_candidate_count": source_candidates["summary"]["coarse_candidate_count"],
                    "fine_candidate_count": source_candidates["summary"]["fine_candidate_count"],
                    "confidence_band_counts": source_candidates["summary"]["confidence_band_counts"],
                    "review_route_counts": source_candidates["summary"]["review_route_counts"],
                    "low_confidence_step_ids": source_candidates["summary"]["low_confidence_step_ids"],
                }
                if source_candidates is not None
                else None
            ),
            "rehearsal": {
                "before_severe_errors": rehearsal["before"]["severe_error_count"],
                "after_severe_errors": rehearsal["after"]["severe_error_count"],
                "revision_count": rehearsal["revision_count"],
                "gold_status": rehearsal["gold_status"],
                "metrics_status": rehearsal["metrics_status"],
            },
            "web_command": "bash scripts/start_native.sh",
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
)
PY
