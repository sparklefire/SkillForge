import json
import stat
from pathlib import Path

import pytest

from skillforge.demo import ROOT
from skillforge.contracts import validate_document
from skillforge.gold_stage_runner import (
    STAGES,
    GoldStage,
    GoldStageExecutor,
    GoldStageRunStore,
)


def _executor(store_root: Path, *, hook=None) -> GoldStageExecutor:
    return GoldStageExecutor(
        GoldStageRunStore(store_root),
        ROOT / "cases/n31/gold",
        visual_review_path=ROOT / "cases/n31/evaluations/visual_sequence_review_v1.json",
        stage_hook=hook,
    )


def _stage_hashes(manifest: dict, stage: GoldStage) -> dict[str, str]:
    record = next(item for item in manifest["stages"] if item["stage_id"] == stage.value)
    return {item["name"]: item["sha256"] for item in record["outputs"]}


def test_full_gold_stage_run_is_hash_bound_and_atomically_published(tmp_path) -> None:
    store_root = tmp_path / "private-stage-runs"
    result = _executor(store_root).run_full()
    directory, manifest = GoldStageRunStore(store_root).current()

    assert result["status"] == "COMPLETED"
    assert result["rebuilt_stages"] == [item.value for item in STAGES]
    assert result["reused_stages"] == []
    assert manifest["published"] is True
    assert manifest["source_run_id"] is None
    assert manifest["data_policy"] == {
        "external_model_calls": 0,
        "contains_raw_media": False,
        "contains_credentials": False,
        "contains_absolute_paths": False,
    }
    assert all(
        stage["metrics"]["elapsed_ms"] >= 0
        and stage["metrics"]["cpu_user_seconds"] >= 0
        and stage["metrics"]["cpu_system_seconds"] >= 0
        and stage["metrics"]["process_peak_rss_bytes"] > 0
        and stage["metrics"]["output_bytes"]
        == sum(item["size_bytes"] for item in stage["outputs"])
        and stage["metrics"]["resource_scope"]
        == "PROCESS_CUMULATIVE_PEAK_AND_STAGE_DELTAS"
        and stage["metrics"]["external_model_calls"] == 0
        for stage in manifest["stages"]
    )
    assert json.loads((directory / "summary.json").read_text())["after"]["severe_error_count"] == 0
    assert stat.S_IMODE(store_root.stat().st_mode) == 0o700
    assert stat.S_IMODE((store_root / "current.json").stat().st_mode) == 0o600
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o600
        for path in directory.iterdir()
        if path.is_file()
    )
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert str(ROOT) not in serialized
    assert "Authorization" not in serialized

    legacy = json.loads(json.dumps(manifest))
    for stage in legacy["stages"]:
        stage.pop("metrics")
    validate_document(legacy, "gold_stage_run.schema.json")


def test_render_rerun_reuses_verified_upstream_and_rebuilds_only_rendering(tmp_path) -> None:
    executor = _executor(tmp_path / "runs")
    first = executor.run_full()
    _, first_manifest = executor.store.current()
    second = executor.rerun(GoldStage.RENDERING)
    _, second_manifest = executor.store.current()

    assert second["source_run_id"] == first["run_id"]
    assert second["reused_stages"] == [item.value for item in STAGES[:-1]]
    assert second["rebuilt_stages"] == ["RENDERING"]
    for stage in STAGES[:-1]:
        assert _stage_hashes(second_manifest, stage) == _stage_hashes(first_manifest, stage)
    assert next(
        item for item in second_manifest["stages"] if item["stage_id"] == "RENDERING"
    )["execution"] == "REBUILT"
    assert second_manifest["workflow_sha256"] != first_manifest["workflow_sha256"]


def test_mid_pipeline_rerun_rebuilds_selected_stage_and_all_downstream(tmp_path) -> None:
    executor = _executor(tmp_path / "runs")
    executor.run_full()
    result = executor.rerun(GoldStage.VERIFYING_INITIAL)
    assert result["reused_stages"] == [item.value for item in STAGES[:4]]
    assert result["rebuilt_stages"] == [item.value for item in STAGES[4:]]
    directory, manifest = executor.store.current()
    workflow = json.loads((directory / "workflow.json").read_text())
    assert workflow["state"] == "COMPLETED"
    assert workflow["stage_attempts"]["VERIFYING"] == 4
    assert manifest["status"] == "COMPLETED"


def test_render_failure_keeps_current_release_and_can_be_retried(tmp_path) -> None:
    store_root = tmp_path / "runs"
    good_executor = _executor(store_root)
    first = good_executor.run_full()

    def fail_render(stage: GoldStage, _directory: Path) -> None:
        if stage == GoldStage.RENDERING:
            raise TimeoutError("render worker temporarily unavailable")

    with pytest.raises(TimeoutError, match="temporarily unavailable"):
        _executor(store_root, hook=fail_render).rerun(GoldStage.RENDERING)

    _, still_current = GoldStageRunStore(store_root).current()
    assert still_current["run_id"] == first["run_id"]
    failed_dirs = list((store_root / "failed").iterdir())
    assert len(failed_dirs) == 1
    failed = json.loads((failed_dirs[0] / "stage_run.json").read_text())
    assert failed["status"] == "FAILED"
    assert failed["failure"]["stage_id"] == "RENDERING"
    failed_stage = failed["stages"][-1]
    assert failed_stage["metrics"]["elapsed_ms"] >= 0
    assert failed_stage["metrics"]["process_peak_rss_bytes"] > 0
    assert failed_stage["metrics"]["external_model_calls"] == 0
    assert json.loads((failed_dirs[0] / "workflow.json").read_text())["last_failure"]["retryable"] is True

    recovered = good_executor.rerun(GoldStage.RENDERING)
    assert recovered["status"] == "COMPLETED"
    assert recovered["source_run_id"] == first["run_id"]


def test_tampered_current_artifact_is_rejected_before_reuse(tmp_path) -> None:
    executor = _executor(tmp_path / "runs")
    executor.run_full()
    directory, _ = executor.store.current()
    (directory / "constraints.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="阶段产物哈希校验失败"):
        executor.rerun(GoldStage.RENDERING)
