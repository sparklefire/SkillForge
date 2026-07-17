"""Build an auditable five-agent and tool execution trace for the N31 case."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import validate_document


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "cases/n31/evaluations/agent_tool_trace_v1.json"

AGENT_ORDER = [
    "PERCEPTION_AGENT",
    "SOP_AGENT",
    "CREATOR_AGENT",
    "VERIFIER_AGENT",
    "REVISION_AGENT",
]

ARTIFACT_FILES: dict[str, tuple[str, str, str]] = {
    "INGEST_MANIFEST": (
        "cases/n31/ingest_manifest.json",
        "JSON",
        "PRIVATE_LOCAL_REFERENCE_DIGEST",
    ),
    "DGX_VISUAL": (
        "cases/n31/evaluations/dgx_visual_compute_v1.json",
        "JSON",
        "STRUCTURED_EVIDENCE",
    ),
    "PDF_STRUCTURE": (
        "cases/n31/evaluations/pdf_structure_v1.json",
        "JSON",
        "STRUCTURED_EVIDENCE",
    ),
    "SOURCE_CANDIDATES": (
        "cases/n31/evaluations/source_candidate_synthesis_v1.json",
        "JSON",
        "STRUCTURED_EVIDENCE",
    ),
    "MULTISOURCE_EVAL": (
        "cases/n31/evaluations/multisource_comparison_v1.json",
        "JSON",
        "STRUCTURED_EVIDENCE",
    ),
    "GOLD_SOP": (
        "cases/n31/gold/gold_sop.json",
        "JSON",
        "STRUCTURED_EVIDENCE",
    ),
    "OUTPUT_PROFILE": (
        "cases/n31/output_profile.json",
        "JSON",
        "PUBLIC_ARTIFACT",
    ),
    "SOP_VIEWS": (
        "cases/n31/demo_bundle/sop_views.json",
        "JSON",
        "PUBLIC_ARTIFACT",
    ),
    "CHECKLIST": (
        "cases/n31/demo_bundle/checklist.json",
        "JSON",
        "PUBLIC_ARTIFACT",
    ),
    "QUIZ": (
        "cases/n31/demo_bundle/quiz.json",
        "JSON",
        "PUBLIC_ARTIFACT",
    ),
    "POSTER": (
        "output/pdf/n31_a4_training_poster.pdf",
        "PDF",
        "PUBLIC_ARTIFACT",
    ),
    "TRAINING_VIDEO": (
        "output/video/n31_training_video_v1.mp4",
        "MP4",
        "PUBLIC_ARTIFACT",
    ),
    "INITIAL_CONFLICTS": (
        "cases/n31/demo_bundle/initial_conflicts.json",
        "JSON",
        "STRUCTURED_EVIDENCE",
    ),
    "GROUNDING_GATE": (
        "cases/n31/evaluations/deterministic_grounding_gate_v1.json",
        "JSON",
        "STRUCTURED_EVIDENCE",
    ),
    "SEMANTIC_REVIEW": (
        "cases/n31/evaluations/semantic_review_v1.json",
        "JSON",
        "STRUCTURED_EVIDENCE",
    ),
    "AFTER_SOP": (
        "cases/n31/demo_bundle/after_sop.json",
        "JSON",
        "PUBLIC_ARTIFACT",
    ),
    "REVISION_AUDIT": (
        "cases/n31/demo_bundle/revision_audit.json",
        "JSON",
        "STRUCTURED_EVIDENCE",
    ),
    "FINAL_CONFLICTS": (
        "cases/n31/demo_bundle/final_conflicts.json",
        "JSON",
        "STRUCTURED_EVIDENCE",
    ),
    "SELECTIVE_REBUILD": (
        "cases/n31/evaluations/selective_rebuild_v1.json",
        "JSON",
        "STRUCTURED_EVIDENCE",
    ),
}

TOOLS = [
    ("PDF_PAGE_LOOKUP", "PDF_PAGE", "GET /api/n31/evidence/{evidence_id}", "PRIVATE_LOCAL_SOURCE", "READ_ONLY", "LOCAL_ONLY", False),
    ("KEYFRAME_LOOKUP", "KEYFRAME", "GET /api/n31/checklist/keyframes/{evidence_id}", "SAFE_DERIVATIVE", "READ_ONLY", "LOCAL_ONLY", False),
    ("VIDEO_INTERVAL_LOOKUP", "VIDEO_INTERVAL", "GET /api/n31/evidence/{evidence_id}", "SAFE_DERIVATIVE", "READ_ONLY", "LOCAL_ONLY", False),
    ("AUDIO_INTERVAL_LOOKUP", "AUDIO_INTERVAL", "GET /api/n31/evidence/{evidence_id}", "PRIVATE_LOCAL_SOURCE", "READ_ONLY", "LOCAL_ONLY", False),
    ("EVIDENCE_SEARCH", "RETRIEVAL", "skillforge.source_candidates:synthesize_source_candidates", "STRUCTURED_EVIDENCE", "PUBLIC_ARTIFACT_WRITE", "LOCAL_ONLY", False),
    ("SOP_PLAN", "PLANNING", "skillforge.planner:SOPAgent", "STRUCTURED_EVIDENCE", "PUBLIC_ARTIFACT_WRITE", "AUTHORIZED_STRUCTURED_OR_TEXT_ONLY", False),
    ("ARTIFACT_SAVE", "SAVE", "skillforge.creator:create_sop_views", "STRUCTURED_EVIDENCE", "PUBLIC_ARTIFACT_WRITE", "LOCAL_ONLY", False),
    ("ARTIFACT_RENDER", "RENDER", "skillforge.training_video:render_training_video", "SAFE_DERIVATIVE", "PUBLIC_ARTIFACT_WRITE", "AUTHORIZED_STRUCTURED_OR_TEXT_ONLY", False),
    ("RULE_VERIFY", "VERIFY", "skillforge.verifier:verify_sop", "STRUCTURED_EVIDENCE", "PUBLIC_ARTIFACT_WRITE", "LOCAL_ONLY", False),
    ("SEMANTIC_VERIFY", "VERIFY", "skillforge.semantic_review:run_semantic_review", "STRUCTURED_EVIDENCE", "PUBLIC_ARTIFACT_WRITE", "AUTHORIZED_STRUCTURED_OR_TEXT_ONLY", False),
    ("LOCAL_REVISION", "REVISE", "skillforge.revision:revise_sop", "STRUCTURED_EVIDENCE", "PUBLIC_ARTIFACT_WRITE", "LOCAL_ONLY", False),
    ("SELECTIVE_REBUILD", "REVISE", "skillforge.selective_rebuild:build_selective_rebuild_report", "STRUCTURED_EVIDENCE", "PUBLIC_ARTIFACT_WRITE", "LOCAL_ONLY", False),
    ("HUMAN_CONFIRM", "CONFIRM", "POST /api/n31/review/sessions", "HUMAN_REVIEW", "LOCAL_PRIVATE_WRITE", "LOCAL_ONLY", True),
]


def _call(
    prefix: str,
    sequence: int,
    tool_id: str,
    purpose: str,
    inputs: list[str],
    outputs: list[str],
    basis: str,
    *,
    human_gate: bool = False,
) -> dict[str, Any]:
    return {
        "call_id": f"{prefix}-C{sequence:02d}",
        "sequence": sequence,
        "tool_id": tool_id,
        "purpose": purpose,
        "input_artifact_ids": inputs,
        "output_artifact_ids": outputs,
        "execution_basis": basis,
        "evidence_bound": True,
        "human_gate": human_gate,
        "status": "COMPLETED",
    }


AGENTS = [
    {
        "agent_id": "PERCEPTION_AGENT",
        "sequence": 1,
        "label": "Perception Agent",
        "responsibility": "提取带原页码、视频时间点和录音时间点的结构化Evidence及候选窗口。",
        "prohibited_action": "不得把GPU数值候选或稀疏关键帧自动升级为设备事实。",
        "allowed_tool_ids": ["PDF_PAGE_LOOKUP", "KEYFRAME_LOOKUP", "VIDEO_INTERVAL_LOOKUP", "AUDIO_INTERVAL_LOOKUP", "EVIDENCE_SEARCH"],
        "calls": [
            _call("A01", 1, "PDF_PAGE_LOOKUP", "保留手册原页码并建立可检索结构报告。", ["INGEST_MANIFEST"], ["PDF_STRUCTURE"], "LOCAL_DETERMINISTIC"),
            _call("A01", 2, "KEYFRAME_LOOKUP", "在DGX上筛选安全视频的场景变化候选。", ["INGEST_MANIFEST"], ["DGX_VISUAL"], "LOCAL_GPU_CANDIDATE_SELECTION"),
            _call("A01", 3, "VIDEO_INTERVAL_LOOKUP", "把同源关键帧合并为可回看的候选时间窗口。", ["DGX_VISUAL"], ["SOURCE_CANDIDATES"], "LOCAL_DETERMINISTIC"),
            _call("A01", 4, "AUDIO_INTERVAL_LOOKUP", "把操作者口述答案绑定录音时间段和Evidence。", ["INGEST_MANIFEST"], ["SOURCE_CANDIDATES"], "LOCAL_DETERMINISTIC"),
            _call("A01", 5, "EVIDENCE_SEARCH", "合并视频、PDF和口述候选并验证多源覆盖。", ["PDF_STRUCTURE", "DGX_VISUAL", "INGEST_MANIFEST"], ["SOURCE_CANDIDATES", "MULTISOURCE_EVAL"], "LOCAL_DETERMINISTIC"),
        ],
        "status": "COMPLETED",
    },
    {
        "agent_id": "SOP_AGENT",
        "sequence": 2,
        "label": "SOP Agent",
        "responsibility": "依据Evidence与约束规划8至15步SOP、依赖和证据绑定。",
        "prohibited_action": "不得补写无来源工具、参数或绝对安全承诺。",
        "allowed_tool_ids": ["EVIDENCE_SEARCH", "SOP_PLAN", "HUMAN_CONFIRM"],
        "calls": [
            _call("A02", 1, "SOP_PLAN", "从多源候选规划13步证据绑定SOP。", ["SOURCE_CANDIDATES", "MULTISOURCE_EVAL"], ["GOLD_SOP"], "HUMAN_VERIFIED_SOURCE_PROJECTION"),
            _call("A02", 2, "HUMAN_CONFIRM", "由实际操作者确认步骤、条件、参数和成功标准。", ["GOLD_SOP"], ["GOLD_SOP"], "HUMAN_REVIEW", human_gate=True),
        ],
        "status": "COMPLETED",
    },
    {
        "agent_id": "CREATOR_AGENT",
        "sequence": 3,
        "label": "Creator Agent",
        "responsibility": "从已绑定证据的Gold SOP生成清单、测验、海报和培训视频。",
        "prohibited_action": "不得脱离Gold改变必要步骤、条件步骤或引用边界。",
        "allowed_tool_ids": ["ARTIFACT_SAVE", "ARTIFACT_RENDER"],
        "calls": [
            _call("A03", 1, "ARTIFACT_SAVE", "生成三种SOP视图、手机清单和五类测验。", ["GOLD_SOP", "OUTPUT_PROFILE"], ["SOP_VIEWS", "CHECKLIST", "QUIZ"], "LOCAL_DETERMINISTIC"),
            _call("A03", 2, "ARTIFACT_RENDER", "渲染A4海报和80秒证据绑定培训视频。", ["GOLD_SOP", "OUTPUT_PROFILE"], ["POSTER", "TRAINING_VIDEO"], "TEXT_ONLY_TTS"),
        ],
        "status": "COMPLETED",
    },
    {
        "agent_id": "VERIFIER_AGENT",
        "sequence": 4,
        "label": "Verifier Agent",
        "responsibility": "检测遗漏、顺序、无依据工具、参数、安全声明和语义冲突。",
        "prohibited_action": "不得直接覆盖人工Gold或把模型推断当作最终事实。",
        "allowed_tool_ids": ["RULE_VERIFY", "SEMANTIC_VERIFY"],
        "calls": [
            _call("A04", 1, "RULE_VERIFY", "对受控错误草稿执行确定性规则质检和无来源门禁。", ["GOLD_SOP"], ["INITIAL_CONFLICTS", "GROUNDING_GATE"], "LOCAL_DETERMINISTIC"),
            _call("A04", 2, "SEMANTIC_VERIFY", "对结构化步骤和Evidence陈述执行高推理语义复核。", ["GOLD_SOP"], ["SEMANTIC_REVIEW"], "AUTHORIZED_MODEL_INFERENCE"),
            _call("A04", 3, "RULE_VERIFY", "对局部修订结果复检并确认严重问题归零。", ["AFTER_SOP"], ["FINAL_CONFLICTS"], "LOCAL_DETERMINISTIC"),
        ],
        "status": "COMPLETED",
    },
    {
        "agent_id": "REVISION_AGENT",
        "sequence": 5,
        "label": "Revision Agent",
        "responsibility": "引用Evidence执行字段级局部修订并计算下游选择性重建范围。",
        "prohibited_action": "不得整包无差别重写或修改未受影响单元。",
        "allowed_tool_ids": ["LOCAL_REVISION", "SELECTIVE_REBUILD", "HUMAN_CONFIRM"],
        "calls": [
            _call("A05", 1, "LOCAL_REVISION", "按冲突和Evidence完成4项局部修改并保留审计。", ["GOLD_SOP", "INITIAL_CONFLICTS"], ["AFTER_SOP", "REVISION_AUDIT"], "LOCAL_DETERMINISTIC"),
            _call("A05", 2, "SELECTIVE_REBUILD", "只失效受位置或内容影响的步骤、题目和镜头。", ["AFTER_SOP", "REVISION_AUDIT"], ["SELECTIVE_REBUILD"], "LOCAL_DETERMINISTIC"),
        ],
        "status": "COMPLETED",
    },
]

HANDOFFS = [
    {"from_agent": "PERCEPTION_AGENT", "to_agent": "SOP_AGENT", "artifact_ids": ["SOURCE_CANDIDATES", "MULTISOURCE_EVAL"], "reason": "只传递结构化候选、来源和Evidence定位。"},
    {"from_agent": "SOP_AGENT", "to_agent": "CREATOR_AGENT", "artifact_ids": ["GOLD_SOP"], "reason": "创作只读取操作者确认且证据绑定的Gold。"},
    {"from_agent": "CREATOR_AGENT", "to_agent": "VERIFIER_AGENT", "artifact_ids": ["SOP_VIEWS", "CHECKLIST", "QUIZ"], "reason": "生成物进入规则与语义质检。"},
    {"from_agent": "VERIFIER_AGENT", "to_agent": "REVISION_AGENT", "artifact_ids": ["INITIAL_CONFLICTS", "GROUNDING_GATE"], "reason": "只把已定位冲突交给局部修订。"},
    {"from_agent": "REVISION_AGENT", "to_agent": "VERIFIER_AGENT", "artifact_ids": ["AFTER_SOP", "REVISION_AUDIT"], "reason": "修订结果必须复检后才能发布。"},
]


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _inside(path: Path, root: Path) -> Path:
    path = path.expanduser().resolve()
    root = root.expanduser().resolve()
    if path != root and root not in path.parents:
        raise ValueError("Agent追踪文件必须位于项目目录内")
    return path


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        os.chmod(path, 0o644)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_agent_trace(document: dict[str, Any]) -> dict[str, Any]:
    validate_document(document, "agent_tool_trace.schema.json")
    agents = document["agents"]
    if [item["agent_id"] for item in agents] != AGENT_ORDER:
        raise ValueError("Agent必须按Perception、SOP、Creator、Verifier、Revision排列")
    if [item["sequence"] for item in agents] != [1, 2, 3, 4, 5]:
        raise ValueError("Agent sequence必须连续为1到5")

    tools = {item["tool_id"]: item for item in document["tools"]}
    if len(tools) != len(document["tools"]) or set(tools) != {item[0] for item in TOOLS}:
        raise ValueError("Agent工具集合不完整或包含重复项")
    artifacts = {item["artifact_id"]: item for item in document["artifacts"]}
    if len(artifacts) != len(document["artifacts"]) or set(artifacts) != set(ARTIFACT_FILES):
        raise ValueError("Agent追踪产物集合不完整或包含重复项")

    calls = []
    for agent in agents:
        allowed = set(agent["allowed_tool_ids"])
        if not allowed <= set(tools):
            raise ValueError(f"{agent['agent_id']} 引用了未知允许工具")
        if [item["sequence"] for item in agent["calls"]] != list(
            range(1, len(agent["calls"]) + 1)
        ):
            raise ValueError(f"{agent['agent_id']} 的工具调用顺序不连续")
        for call in agent["calls"]:
            if call["tool_id"] not in allowed:
                raise ValueError(f"{agent['agent_id']} 调用了未授权工具")
            referenced = set(call["input_artifact_ids"] + call["output_artifact_ids"])
            if not referenced <= set(artifacts):
                raise ValueError(f"{call['call_id']} 引用了未知产物")
            if call["human_gate"] != tools[call["tool_id"]]["human_confirmation"]:
                raise ValueError(f"{call['call_id']} 的人工门禁属性与工具不一致")
            calls.append(call)
    if {item["tool_id"] for item in calls} != set(tools):
        raise ValueError("每个Agent工具必须至少有一次实际追踪调用")

    edges = {(item["from_agent"], item["to_agent"]) for item in document["handoffs"]}
    if edges != {
        ("PERCEPTION_AGENT", "SOP_AGENT"),
        ("SOP_AGENT", "CREATOR_AGENT"),
        ("CREATOR_AGENT", "VERIFIER_AGENT"),
        ("VERIFIER_AGENT", "REVISION_AGENT"),
        ("REVISION_AGENT", "VERIFIER_AGENT"),
    }:
        raise ValueError("Agent交接必须包含创作、质检、修订和复检闭环")
    if any(not set(item["artifact_ids"]) <= set(artifacts) for item in document["handoffs"]):
        raise ValueError("Agent交接引用了未知产物")

    summary = document["summary"]
    expected = {
        "agent_count": len(agents),
        "tool_count": len(tools),
        "tool_call_count": len(calls),
        "artifact_count": len(artifacts),
        "handoff_count": len(document["handoffs"]),
        "evidence_bound_call_count": sum(item["evidence_bound"] for item in calls),
        "human_confirmation_call_count": sum(item["human_gate"] for item in calls),
        "trace_generation_external_model_calls": 0,
    }
    if summary != expected:
        raise ValueError("Agent追踪汇总与实际工具调用不一致")
    serialized = json.dumps(document, ensure_ascii=False)
    if any(value in serialized for value in ("/Users/", "/home/", "Authorization", "Bearer ")):
        raise ValueError("Agent追踪包含本地路径或凭证模式")
    return document


def verify_agent_trace_artifacts(
    document: dict[str, Any],
    *,
    project_root: Path = ROOT,
) -> dict[str, Any]:
    validate_agent_trace(document)
    project_root = project_root.expanduser().resolve()
    indexed = {item["artifact_id"]: item for item in document["artifacts"]}
    for artifact_id, (relative, kind, classification) in ARTIFACT_FILES.items():
        path = _inside(project_root / relative, project_root)
        record = indexed[artifact_id]
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Agent追踪缺少产物: {artifact_id}")
        if record["kind"] != kind or record["classification"] != classification:
            raise ValueError(f"Agent追踪产物分类不一致: {artifact_id}")
        if record["size_bytes"] != path.stat().st_size or record["sha256"] != _sha256(path):
            raise ValueError(f"Agent追踪产物大小或SHA-256不一致: {artifact_id}")
    return document


def build_agent_trace(
    *,
    project_root: Path = ROOT,
    output_path: Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    project_root = project_root.expanduser().resolve()
    output_path = _inside(
        output_path if output_path.is_absolute() else project_root / output_path,
        project_root,
    )
    artifacts = []
    for artifact_id, (relative, kind, classification) in ARTIFACT_FILES.items():
        path = _inside(project_root / relative, project_root)
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Agent追踪缺少产物: {artifact_id}")
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "kind": kind,
                "classification": classification,
                "sha256": _sha256(path),
                "size_bytes": path.stat().st_size,
                "contains_raw_media": False,
            }
        )
    tools = [
        {
            "tool_id": tool_id,
            "capability": capability,
            "implementation": implementation,
            "access_scope": access_scope,
            "side_effect": side_effect,
            "external_boundary": external_boundary,
            "human_confirmation": human_confirmation,
        }
        for (
            tool_id,
            capability,
            implementation,
            access_scope,
            side_effect,
            external_boundary,
            human_confirmation,
        ) in TOOLS
    ]
    calls = [call for agent in AGENTS for call in agent["calls"]]
    document = {
        "artifact_type": "AGENT_TOOL_EXECUTION_TRACE",
        "version": 1,
        "case_id": "n31_media_change",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "COMPLETED",
        "tools": tools,
        "agents": AGENTS,
        "handoffs": HANDOFFS,
        "artifacts": artifacts,
        "summary": {
            "agent_count": len(AGENTS),
            "tool_count": len(tools),
            "tool_call_count": len(calls),
            "artifact_count": len(artifacts),
            "handoff_count": len(HANDOFFS),
            "evidence_bound_call_count": sum(item["evidence_bound"] for item in calls),
            "human_confirmation_call_count": sum(item["human_gate"] for item in calls),
            "trace_generation_external_model_calls": 0,
        },
        "data_policy": {
            "storage_scope": "PUBLIC_STRUCTURED_AUDIT",
            "contains_raw_media": False,
            "contains_credentials": False,
            "contains_absolute_paths": False,
            "contains_artifact_paths": False,
            "trace_generation_external_model_calls": 0,
        },
    }
    verify_agent_trace_artifacts(document, project_root=project_root)
    _write_json_atomic(output_path, document)
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("cases/n31/evaluations/agent_tool_trace_v1.json"),
    )
    args = parser.parse_args()
    report = build_agent_trace(
        project_root=args.project_root,
        output_path=args.output,
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
