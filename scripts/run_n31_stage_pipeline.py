#!/usr/bin/env python3
"""Run the N31 Gold artifact pipeline or rebuild one stage and downstream."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skillforge.demo import ROOT
from skillforge.gold_stage_runner import GoldStage, GoldStageExecutor, GoldStageRunStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=ROOT / "outputs/n31_stage_runs")
    parser.add_argument("--rerun", choices=[item.value for item in GoldStage])
    args = parser.parse_args()
    executor = GoldStageExecutor(
        GoldStageRunStore(args.store),
        ROOT / "cases/n31/gold",
        visual_review_path=ROOT / "cases/n31/evaluations/visual_sequence_review_v1.json",
    )
    result = executor.rerun(args.rerun) if args.rerun else executor.run_full()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
