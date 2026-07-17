"""Run and atomically publish the N31 Gold workflow by real artifact stage."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .creator import create_checklist, create_quiz, create_sop_views
from .gold_rehearsal import GOLD_BASIS
from .observability import StructuredLogger, redact
from .revision import revise_sop
from .synthetic_case import inject_faults
from .verifier import metrics, verify_sop
from .workflow import WorkflowState, WorkflowStateMachine


class GoldStage(StrEnum):
    INGESTING = "INGESTING"
    EXTRACTING = "EXTRACTING"
    PLANNING = "PLANNING"
    CREATING = "CREATING"
    VERIFYING_INITIAL = "VERIFYING_INITIAL"
    REVISING = "REVISING"
    VERIFYING_FINAL = "VERIFYING_FINAL"
    RENDERING = "RENDERING"


STAGES = tuple(GoldStage)
WORKFLOW_STATE = {
    GoldStage.INGESTING: WorkflowState.INGESTING,
    GoldStage.EXTRACTING: WorkflowState.EXTRACTING,
    GoldStage.PLANNING: WorkflowState.PLANNING,
    GoldStage.CREATING: WorkflowState.CREATING,
    GoldStage.VERIFYING_INITIAL: WorkflowState.VERIFYING,
    GoldStage.REVISING: WorkflowState.REVISING,
    GoldStage.VERIFYING_FINAL: WorkflowState.VERIFYING,
    GoldStage.RENDERING: WorkflowState.RENDERING,
}
STAGE_OUTPUTS = {
    GoldStage.INGESTING: ("reference_sop.json", "constraints.json", "fault_spec.json"),
    GoldStage.EXTRACTING: ("evidence_catalog.json",),
    GoldStage.PLANNING: ("planned_sop.json",),
    GoldStage.CREATING: ("before_sop.json",),
    GoldStage.VERIFYING_INITIAL: ("initial_conflicts.json", "before_metrics.json"),
    GoldStage.REVISING: ("after_sop.json", "revision_audit.json"),
    GoldStage.VERIFYING_FINAL: ("final_conflicts.json", "after_metrics.json"),
    GoldStage.RENDERING: (
        "sop_views.json",
        "checklist.json",
        "quiz.json",
        "summary.json",
    ),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"阶段产物必须是JSON对象: {path.name}")
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _output_record(path: Path) -> dict[str, Any]:
    return {
        "name": path.name,
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


class GoldStageRunStore:
    """Private, hash-bound run store with an atomic current-release pointer."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        for directory in (self.root, self.root / "runs", self.root / "failed", self.root / ".staging"):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)

    def _manifest(self, directory: Path) -> dict[str, Any]:
        document = validate_document(_read(directory / "stage_run.json"), "gold_stage_run.schema.json")
        stage_ids = [item["stage_id"] for item in document["stages"]]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("阶段运行清单包含重复阶段")
        expected_prefix = [item.value for item in STAGES[: len(stage_ids)]]
        if stage_ids != expected_prefix:
            raise ValueError("阶段运行清单顺序不连续")
        if document["status"] == "COMPLETED" and stage_ids != [item.value for item in STAGES]:
            raise ValueError("完成运行必须包含全部八个阶段")
        seen: set[str] = set()
        for stage in document["stages"]:
            expected_names = set(STAGE_OUTPUTS[GoldStage(stage["stage_id"])])
            actual_names = {item["name"] for item in stage["outputs"]}
            if stage["status"] == "COMPLETED" and actual_names != expected_names:
                raise ValueError(f"{stage['stage_id']} 阶段产物集合不完整")
            for output in stage["outputs"]:
                if output["name"] in seen:
                    raise ValueError("阶段运行清单包含重复产物")
                seen.add(output["name"])
                path = directory / output["name"]
                if (
                    not path.is_file()
                    or path.stat().st_size != output["size_bytes"]
                    or _sha256(path) != output["sha256"]
                ):
                    raise ValueError(f"阶段产物哈希校验失败: {output['name']}")
        workflow_path = directory / "workflow.json"
        if not workflow_path.is_file() or _sha256(workflow_path) != document["workflow_sha256"]:
            raise ValueError("工作流检查点与阶段运行清单不一致")
        workflow = WorkflowStateMachine.load_checkpoint(workflow_path)
        expected_state = "COMPLETED" if document["status"] == "COMPLETED" else document["status"]
        if workflow.state.value != expected_state:
            raise ValueError("阶段运行状态与工作流检查点不一致")
        expected_digest = _canonical_sha256(
            {
                "stages": document["stages"],
                "workflow_sha256": document["workflow_sha256"],
            }
        )
        if document["release_digest"] != expected_digest:
            raise ValueError("发布摘要与阶段运行清单不一致")
        return document

    def current(self) -> tuple[Path, dict[str, Any]]:
        pointer = _read(self.root / "current.json")
        if set(pointer) != {"run_id", "relative_path", "release_digest"}:
            raise ValueError("当前发布指针格式无效")
        relative = Path(pointer["relative_path"])
        if relative.is_absolute() or relative.parts[:1] != ("runs",) or ".." in relative.parts:
            raise ValueError("当前发布指针越界")
        directory = (self.root / relative).resolve()
        if self.root not in directory.parents:
            raise ValueError("当前发布指针越界")
        document = self._manifest(directory)
        if (
            document["run_id"] != pointer["run_id"]
            or document["release_digest"] != pointer["release_digest"]
            or not document["published"]
        ):
            raise ValueError("当前发布指针与运行清单不一致")
        return directory, document

    def publish(self, staging: Path, document: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        document["published"] = True
        _write_json(staging / "stage_run.json", validate_document(document, "gold_stage_run.schema.json"))
        target = self.root / "runs" / document["run_id"]
        if target.exists():
            raise FileExistsError("运行标识已存在")
        os.replace(staging, target)
        _write_json(
            self.root / "current.json",
            {
                "run_id": document["run_id"],
                "relative_path": f"runs/{document['run_id']}",
                "release_digest": document["release_digest"],
            },
        )
        return target, self._manifest(target)

    def retain_failed(self, staging: Path, document: dict[str, Any]) -> Path:
        _write_json(staging / "stage_run.json", validate_document(document, "gold_stage_run.schema.json"))
        target = self.root / "failed" / document["run_id"]
        os.replace(staging, target)
        self._manifest(target)
        return target


class GoldStageExecutor:
    """Execute all stages or rebuild one stage and every real downstream artifact."""

    def __init__(
        self,
        store: GoldStageRunStore,
        gold_dir: Path,
        *,
        visual_review_path: Path | None = None,
        stage_hook: Callable[[GoldStage, Path], None] | None = None,
    ) -> None:
        self.store = store
        self.gold_dir = gold_dir.resolve()
        self.visual_review_path = visual_review_path
        self.stage_hook = stage_hook

    def run_full(self) -> dict[str, Any]:
        return self._run(GoldStage.INGESTING, None, None)

    def rerun(self, start_stage: GoldStage | str) -> dict[str, Any]:
        selected = GoldStage(start_stage)
        source_dir, source = self.store.current()
        return self._run(selected, source_dir, source)

    def _run(
        self,
        start_stage: GoldStage,
        source_dir: Path | None,
        source: dict[str, Any] | None,
    ) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        staging = self.store.root / ".staging" / run_id
        staging.mkdir(mode=0o700)
        logger = StructuredLogger(staging / "run.jsonl")
        created_at = _now()
        if source_dir is None:
            workflow = WorkflowStateMachine(logger)
        else:
            workflow = WorkflowStateMachine.load_checkpoint(source_dir / "workflow.json", logger)
            workflow.rerun_stage(WORKFLOW_STATE[start_stage], f"从 {start_stage.value} 实际重建阶段产物")

        records: list[dict[str, Any]] = []
        start_index = STAGES.index(start_stage)
        current_stage = start_stage
        try:
            for index, stage in enumerate(STAGES):
                current_stage = stage
                started_at = _now()
                if index < start_index:
                    if source_dir is None or source is None:
                        raise ValueError("完整运行不能跳过上游阶段")
                    outputs = self._reuse_stage(stage, source_dir, source, staging)
                    records.append(self._record(stage, "REUSED", started_at, outputs))
                    continue
                if not (source_dir is not None and index == start_index):
                    workflow.transition(WORKFLOW_STATE[stage], f"执行 {stage.value} 产物阶段")
                if self.stage_hook is not None:
                    self.stage_hook(stage, staging)
                outputs = self._build_stage(stage, staging, workflow)
                records.append(self._record(stage, "REBUILT", started_at, outputs))

            workflow.transition(WorkflowState.COMPLETED, "Gold阶段产物已原子发布")
            workflow.write_checkpoint(staging / "workflow.json")
            manifest = self._manifest(
                run_id,
                start_stage,
                source,
                records,
                workflow,
                created_at,
                status="COMPLETED",
            )
            directory, published = self.store.publish(staging, manifest)
            return {
                "run_id": run_id,
                "status": published["status"],
                "start_stage": start_stage.value,
                "source_run_id": published["source_run_id"],
                "release_digest": published["release_digest"],
                "reused_stages": [item["stage_id"] for item in records if item["execution"] == "REUSED"],
                "rebuilt_stages": [item["stage_id"] for item in records if item["execution"] == "REBUILT"],
                "summary": _read(directory / "summary.json"),
            }
        except Exception as exc:
            if workflow.state not in {WorkflowState.FAILED, WorkflowState.COMPLETED, WorkflowState.NEEDS_REVIEW}:
                workflow.fail(exc, retryable=True, reason=f"{current_stage.value} 阶段执行失败")
            error = {
                "error_type": type(exc).__name__,
                "message": str(redact(str(exc)))[:500] or type(exc).__name__,
                "retryable": True,
            }
            failed_record = {
                "stage_id": current_stage.value,
                "workflow_state": WORKFLOW_STATE[current_stage].value,
                "execution": "REBUILT",
                "status": "FAILED",
                "started_at": _now(),
                "completed_at": _now(),
                "outputs": [],
                "error": error,
            }
            if records and records[-1]["stage_id"] == current_stage.value:
                failed_record["started_at"] = records[-1]["started_at"]
                failed_record["outputs"] = records[-1]["outputs"]
                records[-1] = failed_record
            else:
                records.append(failed_record)
            workflow.write_checkpoint(staging / "workflow.json")
            run_status = (
                WorkflowState.NEEDS_REVIEW.value
                if workflow.state == WorkflowState.NEEDS_REVIEW
                else WorkflowState.FAILED.value
            )
            manifest = self._manifest(
                run_id,
                start_stage,
                source,
                records,
                workflow,
                created_at,
                status=run_status,
                failure={"stage_id": current_stage.value, **error},
            )
            self.store.retain_failed(staging, manifest)
            raise

    def _record(
        self,
        stage: GoldStage,
        execution: str,
        started_at: str,
        paths: list[Path],
    ) -> dict[str, Any]:
        return {
            "stage_id": stage.value,
            "workflow_state": WORKFLOW_STATE[stage].value,
            "execution": execution,
            "status": "COMPLETED",
            "started_at": started_at,
            "completed_at": _now(),
            "outputs": [_output_record(path) for path in paths],
            "error": None,
        }

    def _reuse_stage(
        self,
        stage: GoldStage,
        source_dir: Path,
        source: dict[str, Any],
        staging: Path,
    ) -> list[Path]:
        record = next(item for item in source["stages"] if item["stage_id"] == stage.value)
        paths = []
        for output in record["outputs"]:
            source_path = source_dir / output["name"]
            if _sha256(source_path) != output["sha256"]:
                raise ValueError(f"拒绝复用已变化的上游产物: {output['name']}")
            target = staging / output["name"]
            shutil.copyfile(source_path, target)
            os.chmod(target, 0o600)
            paths.append(target)
        return paths

    def _build_stage(
        self,
        stage: GoldStage,
        directory: Path,
        workflow: WorkflowStateMachine,
    ) -> list[Path]:
        if stage == GoldStage.INGESTING:
            reference = _read(self.gold_dir / "gold_sop.json")
            constraints = _read(self.gold_dir / "constraints.json")
            faults = _read(self.gold_dir / "fault_injection.json")
            validate_document(reference, "sop.schema.json")
            if constraints.get("evaluation_basis") != GOLD_BASIS or faults.get("evaluation_basis") != GOLD_BASIS:
                raise ValueError("Gold输入的评测基准不一致")
            if faults.get("controlled_rehearsal") is not True:
                raise ValueError("错误注入必须标记 controlled_rehearsal=true")
            if any(step["status"] != "VERIFIED" for step in reference["steps"]):
                raise ValueError("Gold SOP 中仍有未验证步骤")
            if not any(item["source_type"] == "audio" and item["review_status"] == "VERIFIED" for item in reference["evidence_catalog"]):
                raise ValueError("Gold SOP 缺少已验证专家口述证据")
            _write_json(directory / "reference_sop.json", reference)
            _write_json(directory / "constraints.json", constraints)
            _write_json(directory / "fault_spec.json", faults)
        elif stage == GoldStage.EXTRACTING:
            reference = _read(directory / "reference_sop.json")
            _write_json(
                directory / "evidence_catalog.json",
                {"case_id": reference["case_id"], "evidence_catalog": reference["evidence_catalog"]},
            )
        elif stage == GoldStage.PLANNING:
            reference = _read(directory / "reference_sop.json")
            catalog = _read(directory / "evidence_catalog.json")
            if catalog["evidence_catalog"] != reference["evidence_catalog"]:
                raise ValueError("证据抽取结果与已审核Gold不一致")
            _write_json(directory / "planned_sop.json", reference)
        elif stage == GoldStage.CREATING:
            draft = inject_faults(_read(directory / "planned_sop.json"), _read(directory / "fault_spec.json"))
            validate_document(draft, "sop.schema.json")
            _write_json(directory / "before_sop.json", draft)
        elif stage == GoldStage.VERIFYING_INITIAL:
            draft = _read(directory / "before_sop.json")
            reference = _read(directory / "reference_sop.json")
            constraints = _read(directory / "constraints.json")
            report = verify_sop(draft, reference, constraints, iteration=1)
            _write_json(directory / "initial_conflicts.json", report)
            _write_json(directory / "before_metrics.json", metrics(draft, report, constraints))
        elif stage == GoldStage.REVISING:
            revised, audit = revise_sop(
                _read(directory / "before_sop.json"),
                _read(directory / "initial_conflicts.json"),
                _read(directory / "reference_sop.json"),
                _read(directory / "constraints.json"),
                iteration=1,
            )
            _write_json(directory / "after_sop.json", revised)
            _write_json(directory / "revision_audit.json", audit)
        elif stage == GoldStage.VERIFYING_FINAL:
            revised = _read(directory / "after_sop.json")
            constraints = _read(directory / "constraints.json")
            report = verify_sop(revised, _read(directory / "reference_sop.json"), constraints, iteration=2)
            after = metrics(revised, report, constraints)
            _write_json(directory / "final_conflicts.json", report)
            _write_json(directory / "after_metrics.json", after)
            if after["severe_error_count"]:
                workflow.transition(WorkflowState.NEEDS_REVIEW, "复检仍有严重问题")
                raise ValueError("Gold复检仍有严重问题，不能进入发布")
        elif stage == GoldStage.RENDERING:
            revised = _read(directory / "after_sop.json")
            visual_review = None
            if self.visual_review_path is not None and self.visual_review_path.is_file():
                visual_review = _read(self.visual_review_path)
            _write_json(directory / "sop_views.json", create_sop_views(revised))
            _write_json(directory / "checklist.json", create_checklist(revised, visual_review=visual_review))
            _write_json(directory / "quiz.json", create_quiz(revised))
            audit = _read(directory / "revision_audit.json")
            before = _read(directory / "before_metrics.json")
            after = _read(directory / "after_metrics.json")
            _write_json(
                directory / "summary.json",
                {
                    "case_id": revised["case_id"],
                    "synthetic": False,
                    "evaluation_basis": GOLD_BASIS,
                    "gold_status": "GOLD",
                    "metrics_status": "FINAL",
                    "external_model_calls": 0,
                    "workflow_state": "COMPLETED",
                    "before": before,
                    "after": after,
                    "revision_count": len(audit["changes"]),
                    "conflict_kinds_before": [item["kind"] for item in _read(directory / "initial_conflicts.json")["conflicts"]],
                    "human_review_required": False,
                },
            )
        return [directory / name for name in STAGE_OUTPUTS[stage]]

    def _manifest(
        self,
        run_id: str,
        start_stage: GoldStage,
        source: dict[str, Any] | None,
        records: list[dict[str, Any]],
        workflow: WorkflowStateMachine,
        created_at: str,
        *,
        status: str,
        failure: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workflow_path = self.store.root / ".staging" / run_id / "workflow.json"
        workflow_sha = _sha256(workflow_path)
        release_digest = _canonical_sha256({"stages": records, "workflow_sha256": workflow_sha})
        return validate_document(
            {
                "version": 1,
                "run_id": run_id,
                "case_id": "n31_media_change",
                "status": status,
                "start_stage": start_stage.value,
                "source_run_id": source["run_id"] if source else None,
                "created_at": created_at,
                "completed_at": _now(),
                "published": False,
                "stages": records,
                "workflow_sha256": workflow_sha,
                "release_digest": release_digest,
                "failure": failure,
                "data_policy": {
                    "external_model_calls": 0,
                    "contains_raw_media": False,
                    "contains_credentials": False,
                    "contains_absolute_paths": False,
                },
            },
            "gold_stage_run.schema.json",
        )
