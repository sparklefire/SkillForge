import json
import subprocess
from pathlib import Path

import fitz

import skillforge.media as media
from skillforge.contracts import validate_document
from skillforge.ingest import IngestionPipeline


def make_assets(directory: Path) -> tuple[Path, Path, Path]:
    ffmpeg = str(media.resolve_ffmpeg())
    video = directory / "video.mp4"
    audio = directory / "audio.wav"
    pdf = directory / "manual.pdf"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=16000:duration=1",
            "-c:a",
            "pcm_s16le",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Disconnect power before opening the cover.")
    document.save(pdf)
    document.close()
    return video, pdf, audio


def test_native_video_pdf_audio_ingestion(tmp_path) -> None:
    video, pdf, audio = make_assets(tmp_path)
    output = tmp_path / "result"
    manifest = IngestionPipeline(output, frame_interval_seconds=1).run(
        video=video,
        pdf=pdf,
        audio=audio,
        synthetic=True,
    )
    assert manifest["status"] == "INGESTED"
    assert manifest["derived"]["pdf"]["page_count"] == 1
    assert len(manifest["derived"]["video"]["frames"]) == 2
    assert manifest["derived"]["audio"]["probe"]["audio_streams"]
    evidence = json.loads((output / "evidence_candidates.json").read_text(encoding="utf-8"))
    assert len(evidence) == 3
    for item in evidence:
        validate_document(item, "evidence.schema.json")


def test_ffmpeg_probe_fallback_without_ffprobe(tmp_path, monkeypatch) -> None:
    _, _, audio = make_assets(tmp_path)
    monkeypatch.setattr(media, "resolve_ffprobe", lambda required=False: None)
    result = media.probe_media(audio)
    assert result["probe_backend"] == "ffmpeg"
    assert result["duration_ms"] == 1000
    assert result["audio_streams"][0]["sample_rate"] == 16000
