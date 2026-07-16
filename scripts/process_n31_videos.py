#!/usr/bin/env python3
"""Generate privacy-safe N31 videos using local FFmpeg only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skillforge.media_privacy import process_config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("cases/n31/video_processing.json"),
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("cases/n31/derived/video_processing_report.json"),
    )
    parser.add_argument(
        "--job",
        action="append",
        dest="job_ids",
        help="只重新处理指定 job_id；可重复提供，报告会合并既有任务结果",
    )
    args = parser.parse_args()
    project_root = args.project_root.expanduser().resolve()
    report_path = args.report
    if not report_path.is_absolute():
        report_path = project_root / report_path
    selected_report = process_config(
        args.config,
        project_root,
        job_ids=set(args.job_ids) if args.job_ids else None,
    )
    report = selected_report
    if args.job_ids and report_path.is_file():
        existing = json.loads(report_path.read_text(encoding="utf-8"))
        selected_by_id = {
            item["job_id"]: item for item in selected_report.get("jobs", [])
        }
        merged_jobs = [
            selected_by_id.get(item["job_id"], item)
            for item in existing.get("jobs", [])
        ]
        existing_ids = {item["job_id"] for item in merged_jobs}
        merged_jobs.extend(
            item
            for item in selected_report.get("jobs", [])
            if item["job_id"] not in existing_ids
        )
        report = {
            **existing,
            "status": (
                "PASSED"
                if merged_jobs and all(item["passed"] for item in merged_jobs)
                else "FAILED"
            ),
            "jobs": merged_jobs,
        }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {
        "status": report["status"],
        "report": report_path.relative_to(project_root).as_posix(),
        "outputs": [
            {
                "job_id": item["job_id"],
                "destination": item["destination"],
                "duration_ms": item["output_probe"]["duration_ms"],
                "mask_checks_passed": all(
                    check["passed"] for check in item["mask_checks"]
                ),
                "loudness": item["loudness"],
            }
            for item in selected_report["jobs"]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
