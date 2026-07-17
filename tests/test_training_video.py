import json
import stat
from pathlib import Path

from PIL import Image

from skillforge.training_video import (
    StepAudioTTSClient,
    build_training_video_evidence_pack,
    build_tts_payload,
    load_storyboard,
    render_scene_overlay,
    resolve_cjk_font,
    validate_storyboard_against_case,
)


ROOT = Path(__file__).resolve().parents[1]


def test_storyboard_covers_every_gold_step_and_stays_in_80_seconds() -> None:
    storyboard = load_storyboard()
    gold = json.loads((ROOT / "cases/n31/gold/gold_sop.json").read_text())
    ingest = json.loads((ROOT / "cases/n31/ingest_manifest.json").read_text())
    result = validate_storyboard_against_case(
        storyboard,
        gold,
        ingest,
        check_media_files=False,
    )
    assert result["duration_seconds"] == 80
    assert len(result["covered_step_ids"]) == 13
    assert len(result["required_step_ids"]) == 10
    assert result["evidence_boundary_passed"] is True
    assert len(result["narration"]) <= 1000


def test_training_video_evidence_pack_is_portable_and_complete() -> None:
    storyboard = load_storyboard()
    gold = json.loads((ROOT / "cases/n31/gold/gold_sop.json").read_text())
    pack = build_training_video_evidence_pack(
        storyboard,
        gold,
        video_sha256="a" * 64,
    )
    scene_ids = {
        evidence_id
        for scene in pack["scenes"]
        for evidence_id in scene["evidence_ids"]
    }
    assert scene_ids == {item["evidence_id"] for item in pack["evidence"]}
    assert pack["contains_raw_media"] is False
    assert pack["contains_credentials"] is False
    assert "/Users/" not in json.dumps(pack, ensure_ascii=False)


def test_tts_payload_uses_stepaudio_context_without_voice_label() -> None:
    payload = build_tts_payload(
        "测试旁白",
        model="stepaudio-2.5-tts",
        voice="zhixingjiejie",
        instruction="清晰专业",
        sample_rate=48000,
    )
    assert payload["model"] == "stepaudio-2.5-tts"
    assert payload["stream_format"] == "audio"
    assert "voice_label" not in payload


def test_tts_client_writes_cached_audio_with_private_permissions(tmp_path) -> None:
    destination = tmp_path / "narration.mp3"
    client = StepAudioTTSClient(transport=lambda _: b"ID3" + b"x" * 256)
    client.synthesize(
        "测试旁白",
        destination,
        model="stepaudio-2.5-tts",
        voice="zhixingjiejie",
        instruction="清晰专业",
        sample_rate=48000,
    )
    assert destination.read_bytes().startswith(b"ID3")
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_overlay_is_exact_1080p_and_contains_nontransparent_pixels(tmp_path) -> None:
    storyboard = load_storyboard()
    destination = tmp_path / "overlay.png"
    render_scene_overlay(
        storyboard["scenes"][1],
        destination,
        font_path=resolve_cjk_font(),
        width=1920,
        height=1080,
        scene_number=2,
        scene_count=len(storyboard["scenes"]),
    )
    image = Image.open(destination)
    assert image.size == (1920, 1080)
    assert image.mode == "RGBA"
    assert image.getchannel("A").getbbox() is not None
