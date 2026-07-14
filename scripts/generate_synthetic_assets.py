#!/usr/bin/env python3
"""Generate tiny, copyright-safe video/PDF/audio fixtures outside Git."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import fitz

from skillforge.media import resolve_ffmpeg


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def make_pdf(path: Path) -> None:
    document = fitz.open()
    for title, lines in [
        (
            "Synthetic Filter Manual - Safety",
            [
                "Disconnect power before opening the service cover.",
                "Verify that the running indicator is off.",
                "Wear protective gloves and safety glasses.",
            ],
        ),
        (
            "Synthetic Filter Manual - Installation",
            [
                "Remove the old filter without shaking it.",
                "Install the replacement with the arrow facing inward.",
                "Close the cover and inspect the indicator after restart.",
            ],
        ),
    ]:
        page = document.new_page(width=595, height=842)
        page.insert_text((72, 90), title, fontsize=18)
        for index, line in enumerate(lines):
            page.insert_text((72, 140 + index * 36), f"{index + 1}. {line}", fontsize=12)
    document.save(path)
    document.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("outputs/synthetic_assets"),
    )
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    ffmpeg = str(resolve_ffmpeg())
    video = output / "synthetic_operation.mp4"
    audio = output / "synthetic_expert.wav"
    pdf = output / "synthetic_manual.pdf"
    run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=640x360:rate=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=16000",
            "-t",
            "8",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(video),
        ]
    )
    run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=16000:duration=5",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(audio),
        ]
    )
    make_pdf(pdf)
    print(f"SYNTHETIC_ASSETS_OK output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
