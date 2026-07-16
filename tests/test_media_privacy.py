import json
import subprocess
from pathlib import Path

import pytest

from skillforge.media import probe_media, resolve_ffmpeg
from skillforge.media_privacy import (
    OpaqueMask,
    PrivacyJob,
    Segment,
    build_filter_graph,
    load_jobs,
    process_config,
    process_job,
)


def test_build_filter_graph_for_segments_masks_and_audio(tmp_path) -> None:
    job = PrivacyJob(
        job_id="test",
        source=tmp_path / "source.mp4",
        destination=tmp_path / "safe.mp4",
        segments=(Segment(0, 1), Segment(2, 3)),
        masks=(OpaqueMask(10, 20, 30, 40),),
        normalize_audio=True,
        target_lufs=-16,
        max_true_peak_dbtp=-1.5,
    )
    graph, video_map, audio_map = build_filter_graph(job, has_audio=True)
    assert "concat=n=2:v=1:a=1" in graph
    assert "drawbox=x=10:y=20:w=30:h=40:color=black@1:t=fill" in graph
    assert "loudnorm=I=-16:LRA=11:TP=-1.5" in graph
    assert "alimiter=limit=0.75" in graph
    assert video_map == "[vout]"
    assert audio_map == "[aout]"


def test_load_jobs_rejects_source_overwrite(tmp_path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "job_id": "bad",
                        "source": "same.mp4",
                        "destination": "same.mp4",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="不允许覆盖原始视频"):
        load_jobs(config, tmp_path)


def test_process_config_rejects_unknown_selected_job(tmp_path) -> None:
    source = tmp_path / "source.mp4"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "job_id": "known",
                        "source": source.name,
                        "destination": "safe.mp4",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="未知视频处理任务"):
        process_config(config, tmp_path, job_ids={"missing"})


def test_process_job_applies_opaque_mask_and_safe_segments(tmp_path) -> None:
    source = tmp_path / "source.mp4"
    destination = tmp_path / "safe.mp4"
    subprocess.run(
        [
            str(resolve_ffmpeg()),
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=white:size=320x180:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    result = process_job(
        PrivacyJob(
            job_id="integration",
            source=source,
            destination=destination,
            segments=(Segment(0, 1), Segment(2, 3)),
            masks=(OpaqueMask(40, 30, 100, 80),),
            normalize_audio=True,
            target_lufs=-16,
            max_true_peak_dbtp=-1.5,
        )
    )
    assert result["passed"] is True
    assert result["mask_checks"][0]["y_average"] <= 24
    assert abs(probe_media(destination)["duration_ms"] - 2000) <= 250
    assert probe_media(destination)["audio_streams"][0]["sample_rate"] == 48000
