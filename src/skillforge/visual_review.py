"""Review Gold SOP steps against privacy-safe keyframe sequences."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import validate_document
from .observability import StructuredLogger
from .perception import image_data_url
from .step_plan import StepPlanClient, StepPlanError


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _video_evidence(gold_sop: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["evidence_id"]: item
        for item in gold_sop["evidence_catalog"]
        if item["source_type"] == "video" and "keyframe" in item["locator"]
    }


def select_visual_windows(
    gold_sop: dict[str, Any],
    frame_root: Path,
    *,
    max_frames_per_step: int = 6,
) -> list[dict[str, Any]]:
    if not 1 <= max_frames_per_step <= 6:
        raise ValueError("max_frames_per_step 必须在1到6之间")
    video = _video_evidence(gold_sop)
    by_source: dict[str, list[dict[str, Any]]] = {}
    for item in video.values():
        by_source.setdefault(item["source_ref"], []).append(item)
    for items in by_source.values():
        items.sort(key=lambda value: int(value["locator"]["start_ms"]))

    windows: list[dict[str, Any]] = []
    for step in gold_sop["steps"]:
        direct = [video[item] for item in step["evidence"] if item in video]
        if not direct:
            raise ValueError(f"{step['step_id']} 没有可用于视觉复核的关键帧")
        selected: dict[str, tuple[int, dict[str, Any]]] = {}
        source_order = {item["source_ref"]: index for index, item in enumerate(direct)}
        for item in direct:
            selected[item["evidence_id"]] = (0, item)
            source_items = by_source[item["source_ref"]]
            position = next(
                index
                for index, candidate in enumerate(source_items)
                if candidate["evidence_id"] == item["evidence_id"]
            )
            for distance, neighbor_index in ((1, position - 1), (1, position + 1)):
                if 0 <= neighbor_index < len(source_items):
                    neighbor = source_items[neighbor_index]
                    selected.setdefault(neighbor["evidence_id"], (distance, neighbor))

        direct_ids = {item["evidence_id"] for item in direct}
        ordered = sorted(
            selected.values(),
            key=lambda pair: (
                0 if pair[1]["evidence_id"] in direct_ids else 1,
                source_order.get(pair[1]["source_ref"], len(source_order)),
                int(pair[1]["locator"]["start_ms"]),
            ),
        )[:max_frames_per_step]
        chosen = [item for _, item in ordered]
        chosen.sort(
            key=lambda item: (
                source_order.get(item["source_ref"], len(source_order)),
                int(item["locator"]["start_ms"]),
            )
        )
        frames = []
        for item in chosen:
            locator = item["locator"]
            path = (frame_root / locator["keyframe"]).resolve()
            if not path.is_file():
                raise FileNotFoundError(path)
            frames.append(
                {
                    "evidence_id": item["evidence_id"],
                    "source_ref": item["source_ref"],
                    "start_ms": int(locator["start_ms"]),
                    "end_ms": int(locator["end_ms"]),
                    "keyframe": locator["keyframe"],
                    "_path": path,
                }
            )
        windows.append(
            {
                "step_id": step["step_id"],
                "title": step["title"],
                "action": step["action"],
                "success_check": step["success_check"],
                "required": step["required"],
                "frames": frames,
            }
        )
    return windows


class VisualSequenceAgent:
    def __init__(self, client: StepPlanClient | None = None) -> None:
        self.client = client or StepPlanClient()

    def review(self, window: dict[str, Any]) -> dict[str, Any]:
        allowed = [item["evidence_id"] for item in window["frames"]]
        frame_index = [
            {
                "evidence_id": item["evidence_id"],
                "source_ref": item["source_ref"],
                "start_ms": item["start_ms"],
                "end_ms": item["end_ms"],
            }
            for item in window["frames"]
        ]
        example = {
            "step_id": window["step_id"],
            "verdict": "PARTIAL",
            "observed_claim": "画面可见标签纸和打印机，部分动作可确认。",
            "visible_actions": ["标签纸位于打印机进纸区域"],
            "missing_or_uncertain": ["单帧无法确认完整动作结果"],
            "cited_evidence_ids": [allowed[0]],
            "confidence": 0.6,
            "privacy_observation": "NO_SENSITIVE_CONTENT_VISIBLE",
        }
        prompt = (
            "你是SkillForge视觉序列复核Agent。输入是同一个Gold步骤附近的隐私安全关键帧，"
            "可能来自连续主视频和补充特写。只判断画面直接可见的动作、部件和状态，"
            "不得用手册、常识、口述或步骤文字补齐画面未显示的内容。"
            "SUPPORTED表示关键动作和完成状态都有清楚视觉支持；"
            "PARTIAL表示只看到部分动作或结果；NOT_VISIBLE表示无法从这些帧判断；"
            "CONTRADICTED仅用于画面明确显示与步骤相反的动作。"
            "时间先后必须按同一source_ref内的start_ms判断，不得比较不同视频来源的绝对时间。"
            "cited_evidence_ids只能使用给定ID。若出现不透明遮挡属于MASKED_CONTENT_PRESENT；"
            "若可能看到姓名、电话、地址、条码、二维码或唯一编号，必须标记POTENTIAL_SENSITIVE_CONTENT。"
            "只返回符合Visual Sequence Assessment Schema的严格JSON，不使用Markdown。"
            "字符串内容中不要使用未转义的英文双引号；需要强调术语时使用中文书名号。"
            f"输出形状示例={json.dumps(example, ensure_ascii=False)}\n"
            f"step_id={window['step_id']}\n"
            f"title={window['title']}\n"
            f"expected_action={window['action']}\n"
            f"success_check={window['success_check']}\n"
            f"allowed_frames={json.dumps(frame_index, ensure_ascii=False)}"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for item in window["frames"]:
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"{item['evidence_id']} | {item['source_ref']} | "
                        f"{item['start_ms']}-{item['end_ms']}ms"
                    ),
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": image_data_url(item["_path"]),
                        "detail": "high",
                    },
                }
            )

        last_unknown: list[str] = []
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        for attempt in range(2):
            result = self.client.chat_json(
                messages=messages,
                route="planner",
                schema_name="visual_assessment.schema.json",
                max_attempts=3,
                max_tokens=4096,
            )
            unknown = sorted(set(result["cited_evidence_ids"]) - set(allowed))
            if not unknown:
                result["step_id"] = window["step_id"]
                return validate_document(
                    result,
                    "visual_assessment.schema.json",
                )
            last_unknown = unknown
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"上一条引用了未知Evidence ID：{unknown}。"
                        f"只能引用：{allowed}。请返回完整修正JSON。"
                    ),
                }
            )
        raise StepPlanError(f"视觉复核连续引用未知Evidence ID: {last_unknown}")


def run_visual_review(
    gold_sop_path: Path,
    frame_root: Path,
    output_path: Path,
    *,
    max_frames_per_step: int = 6,
    client: StepPlanClient | None = None,
    logger: StructuredLogger | None = None,
) -> dict[str, Any]:
    gold = _read_json(gold_sop_path)
    validate_document(gold, "sop.schema.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_logger = logger or StructuredLogger(output_path.parent / "visual_review.jsonl")
    model_client = client or StepPlanClient(logger=run_logger, timeout_seconds=120)
    agent = VisualSequenceAgent(model_client)
    windows = select_visual_windows(
        gold,
        frame_root,
        max_frames_per_step=max_frames_per_step,
    )
    checkpoint_path = output_path.parent / "visual_sequence_review.partial.json"
    cached: dict[str, dict[str, Any]] = {}
    if checkpoint_path.is_file():
        partial = _read_json(checkpoint_path)
        for item in partial.get("assessments", []):
            validate_document(
                item["model_result"],
                "visual_assessment.schema.json",
            )
            cached[item["step_id"]] = item
    assessments = []
    for index, window in enumerate(windows, start=1):
        expected_frame_ids = [
            item["evidence_id"] for item in window["frames"]
        ]
        cached_item = cached.get(window["step_id"])
        if cached_item and [
            item["evidence_id"] for item in cached_item["frames"]
        ] == expected_frame_ids:
            assessments.append(cached_item)
            run_logger.emit(
                "visual_review.step.cached",
                step_id=window["step_id"],
                index=index,
                total=len(windows),
            )
            continue
        run_logger.emit(
            "visual_review.step.started",
            step_id=window["step_id"],
            index=index,
            total=len(windows),
            frame_count=len(window["frames"]),
        )
        result = agent.review(window)
        public_frames = [
            {key: value for key, value in item.items() if key != "_path"}
            for item in window["frames"]
        ]
        assessments.append(
            {
                "step_id": window["step_id"],
                "title": window["title"],
                "required": window["required"],
                "model_result": result,
                "frames": public_frames,
            }
        )
        run_logger.emit(
            "visual_review.step.completed",
            step_id=window["step_id"],
            verdict=result["verdict"],
            confidence=result["confidence"],
            privacy_observation=result["privacy_observation"],
        )
        _write_json(
            checkpoint_path,
            {
                "run_id": "N31_VISUAL_SEQUENCE_REVIEW_V1",
                "status": "PARTIAL",
                "assessments": assessments,
            },
        )

    counts = Counter(item["model_result"]["verdict"] for item in assessments)
    required = [item for item in assessments if item["required"]]
    required_supported = sum(
        item["model_result"]["verdict"] == "SUPPORTED" for item in required
    )
    privacy_flags = sum(
        item["model_result"]["privacy_observation"]
        == "POTENTIAL_SENSITIVE_CONTENT"
        for item in assessments
    )
    needs_review = [
        item["step_id"]
        for item in assessments
        if item["model_result"]["verdict"] != "SUPPORTED"
        or item["model_result"]["privacy_observation"]
        == "POTENTIAL_SENSITIVE_CONTENT"
    ]
    summary = {
        "step_count": len(assessments),
        "required_step_count": len(required),
        "supported_count": counts["SUPPORTED"],
        "partial_count": counts["PARTIAL"],
        "not_visible_count": counts["NOT_VISIBLE"],
        "contradicted_count": counts["CONTRADICTED"],
        "required_supported_count": required_supported,
        "strict_visual_support_rate": counts["SUPPORTED"] / len(assessments),
        "observable_rate": (
            counts["SUPPORTED"] + counts["PARTIAL"]
        )
        / len(assessments),
        "required_strict_visual_support_rate": required_supported / len(required),
        "privacy_flag_count": privacy_flags,
        "needs_review_step_ids": needs_review,
    }
    selected = model_client.router.reasoning("planner")
    report = {
        "case_id": gold["case_id"],
        "run_id": "N31_VISUAL_SEQUENCE_REVIEW_V1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "NEEDS_REVIEW" if needs_review else "COMPLETED",
        "model": selected["model"],
        "external_processing_authorized": True,
        "model_calls": len(assessments),
        "assessments": assessments,
        "summary": summary,
    }
    validate_document(report, "visual_review_report.schema.json")
    _write_json(output_path, report)
    checkpoint_path.unlink(missing_ok=True)
    run_logger.emit("visual_review.completed", **summary)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold-sop", type=Path, required=True)
    parser.add_argument("--frame-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-frames-per-step", type=int, default=6)
    parser.add_argument("--external-processing-authorized", action="store_true")
    args = parser.parse_args()
    if not args.external_processing_authorized:
        raise ValueError("视觉复核前必须明确确认外部处理授权")
    report = run_visual_review(
        args.gold_sop,
        args.frame_root,
        args.output,
        max_frames_per_step=args.max_frames_per_step,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "model": report["model"],
                "model_calls": report["model_calls"],
                "summary": report["summary"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
