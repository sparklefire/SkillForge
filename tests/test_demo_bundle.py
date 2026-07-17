import json
from pathlib import Path

from scripts.build_n31_demo_bundle import build_bundle


ROOT = Path(__file__).resolve().parents[1]


def test_builds_asset_free_gold_bundle(tmp_path) -> None:
    source = ROOT / "cases/n31/demo_bundle"
    output = tmp_path / "bundle"
    manifest = build_bundle(source, output)
    assert manifest["gold_status"] == "GOLD"
    assert manifest["metrics_status"] == "FINAL"
    assert manifest["contains_raw_media"] is False
    assert manifest["contains_credentials"] is False
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["after"]["severe_error_count"] == 0
    before = json.loads((output / "before_sop.json").read_text(encoding="utf-8"))
    assert "evidence_catalog" not in before
    views = json.loads((output / "sop_views.json").read_text(encoding="utf-8"))
    assert views["artifact_type"] == "SOP_VIEWS"
    checklist = json.loads((output / "checklist.json").read_text(encoding="utf-8"))
    assert checklist["interaction_mode"] == "ONE_STEP_PER_SCREEN"
