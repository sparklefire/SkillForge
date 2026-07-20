from __future__ import annotations

import json
import stat
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image

from skillforge.contracts import validate_document
from skillforge.final_recording_build import (
    FinalRecordingBuildError,
    PUBLIC_RECORDING_FOOTER,
    PUBLIC_RECORDING_KICKER,
    _atempo_chain,
    _sequence_distance,
    load_storyboard,
    validate_scene_sequence,
    validate_storyboard,
)


ROOT = Path(__file__).resolve().parents[1]
STORYBOARD = ROOT / "config/final_recording_storyboard.json"


def test_frozen_storyboard_is_strict_public_and_exact() -> None:
    storyboard = validate_storyboard(load_storyboard(STORYBOARD), root=ROOT)

    assert len(storyboard["scenes"]) == 9
    assert sum(scene["duration_ms"] for scene in storyboard["scenes"]) == 178000
    assert [scene["scene_id"] for scene in storyboard["scenes"]] == [
        f"R{number:02d}" for number in range(1, 10)
    ]
    assert storyboard["tts"]["text_only"] is True
    assert storyboard["data_policy"]["automatic_human_approval"] is False
    assert storyboard["scenes"][0]["title"] == "星星之火 · 匠传 SkillForge"
    assert "人工确认" not in storyboard["scenes"][7]["narration"]


def test_public_recording_overlay_uses_team_identity_without_draft_label() -> None:
    assert PUBLIC_RECORDING_KICKER == "星星之火 · SKILLFORGE"
    assert "候选" not in PUBLIC_RECORDING_KICKER
    assert "候选" not in PUBLIC_RECORDING_FOOTER
    assert "机器QA" not in PUBLIC_RECORDING_FOOTER


def test_storyboard_rejects_private_text_and_visual_substitution() -> None:
    storyboard = load_storyboard(STORYBOARD)
    unsafe = deepcopy(storyboard)
    unsafe["scenes"][0]["narration"] += " /Users/example/private"
    with pytest.raises(FinalRecordingBuildError, match="私有定位"):
        validate_storyboard(unsafe, root=ROOT)

    substituted = deepcopy(storyboard)
    substituted["scenes"][7]["visual"]["source"] = "private/raw.mp4"
    with pytest.raises(FinalRecordingBuildError, match="已发布的80秒培训视频"):
        validate_storyboard(substituted, root=ROOT)


def test_storyboard_rejects_missing_or_reordered_scene() -> None:
    storyboard = load_storyboard(STORYBOARD)
    reordered = deepcopy(storyboard)
    reordered["scenes"][0], reordered["scenes"][1] = (
        reordered["scenes"][1],
        reordered["scenes"][0],
    )

    with pytest.raises(FinalRecordingBuildError, match="场景顺序"):
        validate_storyboard(reordered, root=ROOT)


def test_atempo_chain_stays_within_ffmpeg_bounds() -> None:
    for ratio in (0.2, 0.4, 0.75, 1.0, 1.8, 2.8, 4.0):
        filters = _atempo_chain(ratio)
        factors = [float(item.split("=", 1)[1]) for item in filters]
        assert all(0.5 <= factor <= 2.0 for factor in factors)
        product = 1.0
        for factor in factors:
            product *= factor
        assert product == pytest.approx(ratio, rel=1e-5)

    with pytest.raises(FinalRecordingBuildError, match="差异过大"):
        _atempo_chain(4.1)


def test_sequence_distance_detects_changed_frame(tmp_path: Path) -> None:
    first = tmp_path / "first.png"
    same = tmp_path / "same.png"
    changed = tmp_path / "changed.png"
    gradient = Image.new("L", (320, 180))
    gradient.putdata([(x * 7 + y * 3) % 256 for y in range(180) for x in range(320)])
    gradient.save(first)
    gradient.save(same)
    flipped = gradient.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    flipped.save(changed)

    assert _sequence_distance(first, same) == 0
    assert _sequence_distance(first, changed) > 24


def test_sequence_validator_rejects_count_mismatch(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir(mode=0o700)

    with pytest.raises(FinalRecordingBuildError, match="数量不一致"):
        validate_scene_sequence([], tmp_path / "final.mp4", [{}], work_dir=work)


def test_build_report_schema_keeps_human_review_pending() -> None:
    digest = "a" * 64
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "FINAL_RECORDING_BUILD",
        "generated_at": "2026-07-19T00:00:00+00:00",
        "status": "READY_FOR_HUMAN_REVIEW",
        "storyboard_sha256": digest,
        "scene_count": 9,
        "target_duration_ms": 178000,
        "media": {
            "filename": "skillforge_final_recording.mp4",
            "sha256": digest,
            "bytes": 1,
            "duration_ms": 178000,
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "video_codec": "h264",
            "audio_codec": "aac",
        },
        "scenes": [
            {
                "scene_id": f"R{order:02d}",
                "order": order,
                "duration_ms": 15000,
                "visual_kind": "SCREENSHOT",
                "visual_source_sha256": digest,
                "narration_sha256": digest,
                "tts_audio_sha256": digest,
                "rendered_sha256": digest,
                "output_probe_ms": order * 1000,
                "difference_hash_distance": 0,
                "sequence_match": True,
            }
            for order in range(1, 10)
        ],
        "tts": {
            "model": "stepaudio-2.5-tts",
            "voice": "zhixingjiejie",
            "scene_count": 9,
            "generated_count": 0,
            "reused_count": 9,
            "external_model_calls": 0,
            "text_only": True,
        },
        "machine_qa": {
            "status": "READY_FOR_HUMAN_REVIEW",
            "report_sha256": digest,
            "all_checks_passed": True,
            "scene_sequence_all_matched": True,
        },
        "human_review": {
            "required": True,
            "status": "PENDING",
            "automatic_approval": False,
        },
        "data_policy": {
            "private_local_state": True,
            "screenshot_assets_private": True,
            "raw_media_sent_to_tts": False,
            "tts_text_only": True,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "automatic_human_approval": False,
        },
    }

    validate_document(report, "final_recording_build.schema.json")
    assert json.dumps(report).count("PENDING") == 1


def test_candidate_build_script_is_executable() -> None:
    script = ROOT / "scripts/build_final_recording_candidate.sh"
    assert script.is_file()
    assert stat.S_IMODE(script.stat().st_mode) & 0o111
