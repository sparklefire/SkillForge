#!/usr/bin/env python3
"""Extract public checklist previews from the already-approved training video."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from skillforge.contracts import validate_document


ROOT = Path(__file__).resolve().parents[1]


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_thumbnails(
    storyboard_path: Path,
    video_path: Path,
    output_dir: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> dict[str, Any]:
    storyboard = validate_document(
        _read(storyboard_path), "training_video_storyboard.schema.json"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    cursor_ms = 0
    items = []
    for scene in storyboard["scenes"]:
        duration_ms = scene["end_ms"] - scene["start_ms"]
        if scene["step_ids"]:
            timestamp_ms = cursor_ms + duration_ms // 2
            for step_id in scene["step_ids"]:
                output = output_dir / f"{step_id}.jpg"
                subprocess.run(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-ss",
                        f"{timestamp_ms / 1000:.3f}",
                        "-i",
                        str(video_path),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=640:-2",
                        "-q:v",
                        "3",
                        "-y",
                        str(output),
                    ],
                    check=True,
                )
                items.append(
                    {
                        "step_id": step_id,
                        "scene_id": scene["scene_id"],
                        "timestamp_ms": timestamp_ms,
                        "preview_path": output.relative_to(ROOT).as_posix(),
                        "bytes": output.stat().st_size,
                        "sha256": _sha256(output),
                    }
                )
        cursor_ms += duration_ms
    document = {
        "artifact_type": "CHECKLIST_THUMBNAIL_MANIFEST",
        "version": 1,
        "case_id": storyboard["case_id"],
        "source_video": {
            "path": video_path.relative_to(ROOT).as_posix(),
            "bytes": video_path.stat().st_size,
            "sha256": _sha256(video_path),
        },
        "items": items,
        "data_policy": {
            "derived_from_approved_training_video": True,
            "contains_credentials": False,
            "contains_raw_media": False,
            "contains_absolute_paths": False,
        },
    }
    validate_document(document, "checklist_thumbnail_manifest.schema.json")
    (output_dir / "manifest.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--storyboard",
        type=Path,
        default=ROOT / "cases/n31/training_video_storyboard.json",
    )
    parser.add_argument(
        "--video",
        type=Path,
        default=ROOT / "output/video/n31_training_video_v1.mp4",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output/checklist_thumbnails",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    manifest = build_thumbnails(
        args.storyboard,
        args.video,
        args.output,
        ffmpeg=args.ffmpeg,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
