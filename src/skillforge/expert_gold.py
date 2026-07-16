"""Turn a timestamped expert interview into reviewed evidence and a Gold SOP."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .contracts import validate_document


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact(text: str) -> str:
    return re.sub(r"[\s，。；：、？！,.!?;:（）()“”\"'‘’·\-—]", "", text)


def _correct(text: str, corrections: list[dict[str, str]]) -> str:
    corrected = text
    for item in corrections:
        corrected = corrected.replace(item["from"], item["to"])
    return corrected.strip()


def _find_anchor(
    text: str,
    variants: list[str],
    *,
    start: int,
) -> int:
    matches = [position for item in variants if (position := text.find(item, start)) >= 0]
    if not matches:
        raise ValueError(f"专家转写中找不到问题锚点: {variants}")
    return min(matches)


def _time_for_offset(
    segments: list[dict[str, Any]],
    offset: int,
    *,
    use_end: bool,
) -> int:
    cursor = 0
    for segment in segments:
        text = str(segment.get("text", ""))
        next_cursor = cursor + len(text)
        contains_offset = offset <= next_cursor if use_end else offset < next_cursor
        if contains_offset:
            key = "end_ms" if use_end else "start_ms"
            value = segment.get(key)
            if value is None:
                value = segment.get("end_ms") or segment.get("start_ms") or 0
            return int(value)
        cursor = next_cursor
    if not segments:
        return 0
    value = segments[-1].get("end_ms") or segments[-1].get("start_ms") or 0
    return int(value)


def align_expert_answers(
    transcription: dict[str, Any],
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    """Align plan questions to canonical ASR deltas and verify expected terms."""

    segments = [
        item for item in transcription.get("segments", []) if str(item.get("text", ""))
    ]
    if not segments:
        raise ValueError("专家转写没有带时间戳的文本片段")
    stream_text = "".join(str(item["text"]) for item in segments)
    done_text = str(transcription.get("text", ""))
    stream_similarity = (
        SequenceMatcher(None, _compact(done_text), _compact(stream_text)).ratio()
        if done_text
        else 1.0
    )
    if stream_similarity < 0.95:
        raise ValueError(
            f"ASR 完成文本与时间戳增量差异过大: {stream_similarity:.3f}"
        )

    positions: list[int] = []
    cursor = 0
    for answer in plan["answers"]:
        position = _find_anchor(
            stream_text,
            [str(item) for item in answer["anchor_variants"]],
            start=cursor,
        )
        positions.append(position)
        cursor = position + 1

    corrections = plan.get("corrections", [])
    start_times = [
        _time_for_offset(segments, position, use_end=False) for position in positions
    ]
    final_end_ms = int(
        segments[-1].get("end_ms") or segments[-1].get("start_ms") or 0
    )
    aligned: list[dict[str, Any]] = []
    for index, answer in enumerate(plan["answers"]):
        start = positions[index]
        end = positions[index + 1] if index + 1 < len(positions) else len(stream_text)
        raw_text = stream_text[start:end].strip()
        corrected_text = _correct(raw_text, corrections)
        compacted = _compact(corrected_text)
        checks = {
            term: _compact(str(term)) in compacted
            for term in answer.get("required_terms", [])
        }
        missing = [term for term, present in checks.items() if not present]
        if missing:
            raise ValueError(
                f"{answer['question_id']} 转写缺少必要确认词: {'、'.join(missing)}"
            )
        start_ms = start_times[index]
        end_ms = (
            start_times[index + 1]
            if index + 1 < len(start_times)
            else final_end_ms
        )
        if end_ms <= start_ms:
            raise ValueError(f"{answer['question_id']} 时间范围无效")
        aligned.append(
            {
                "question_id": answer["question_id"],
                "topic": answer["topic"],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "raw_asr_text": raw_text,
                "corrected_asr_text": corrected_text,
                "verified_claim": answer["verified_claim"],
                "required_term_checks": checks,
                "bind_step_ids": answer.get("bind_step_ids", []),
            }
        )
    return aligned


def _next_evidence_number(evidence_catalog: list[dict[str, Any]]) -> int:
    numbers = [
        int(item["evidence_id"][1:])
        for item in evidence_catalog
        if re.fullmatch(r"E[0-9]{3}", str(item.get("evidence_id", "")))
    ]
    return max(numbers, default=0) + 1


def _build_audio_evidence(
    aligned: list[dict[str, Any]],
    plan: dict[str, Any],
    *,
    start_number: int,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    evidence: list[dict[str, Any]] = []
    by_question: dict[str, str] = {}
    for offset, answer in enumerate(aligned):
        number = start_number + offset
        if number > 999:
            raise ValueError("Evidence 数量超过 E001-E999 编号容量")
        evidence_id = f"E{number:03d}"
        item = {
            "evidence_id": evidence_id,
            "source_type": "audio",
            "source_ref": plan["source_ref"],
            "claim": answer["verified_claim"],
            "locator": {
                "start_ms": answer["start_ms"],
                "end_ms": answer["end_ms"],
            },
            "classification": "EXPERT_ADVICE",
            "relevance": 0.95,
            "confidence": 0.95,
            "review_status": "VERIFIED",
        }
        validate_document(item, "evidence.schema.json")
        evidence.append(item)
        by_question[answer["question_id"]] = evidence_id
    return evidence, by_question


def build_operator_gold(
    candidate_sop: dict[str, Any],
    transcription: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if candidate_sop["case_id"] != plan["case_id"]:
        raise ValueError("候选 SOP 与专家审核计划的 case_id 不一致")
    aligned = align_expert_answers(transcription, plan)
    gold = copy.deepcopy(candidate_sop)
    audio_evidence, evidence_by_question = _build_audio_evidence(
        aligned,
        plan,
        start_number=_next_evidence_number(gold["evidence_catalog"]),
    )
    gold["evidence_catalog"].extend(audio_evidence)

    answers_by_question = {item["question_id"]: item for item in plan["answers"]}
    for answer in plan["answers"]:
        evidence_id = evidence_by_question[answer["question_id"]]
        for step_id in answer.get("bind_step_ids", []):
            step = next(
                (item for item in gold["steps"] if item["step_id"] == step_id),
                None,
            )
            if step is None:
                raise ValueError(f"专家审核计划引用未知步骤: {step_id}")
            if evidence_id not in step["evidence"]:
                step["evidence"].append(evidence_id)

    for step in gold["steps"]:
        override = plan.get("step_overrides", {}).get(step["step_id"], {})
        if "required" in override:
            step["required"] = bool(override["required"])
        if "prerequisites" in override:
            step["prerequisites"] = list(override["prerequisites"])
        if "warnings" in override:
            step["warnings"] = list(override["warnings"])
        if "success_check" in override:
            step["success_check"] = str(override["success_check"])
        remove_parameters = set(override.get("remove_parameters", []))
        if remove_parameters:
            step["parameters"] = [
                item
                for item in step["parameters"]
                if item["name"] not in remove_parameters
            ]
        for parameter_name, question_id in override.get(
            "parameter_bindings", {}
        ).items():
            if question_id not in answers_by_question:
                raise ValueError(f"参数绑定引用未知问题: {question_id}")
            parameter = next(
                (
                    item
                    for item in step["parameters"]
                    if item["name"] == parameter_name
                ),
                None,
            )
            if parameter is None:
                raise ValueError(
                    f"{step['step_id']} 中找不到待绑定参数: {parameter_name}"
                )
            evidence_id = evidence_by_question[question_id]
            if evidence_id not in step["evidence"]:
                step["evidence"].append(evidence_id)
            if evidence_id not in parameter["evidence_ids"]:
                parameter["evidence_ids"].append(evidence_id)
        step["status"] = "VERIFIED"
        step["confidence"] = max(float(step["confidence"]), 0.85)

    gold["title"] = plan["gold_title"]
    gold["version"] = int(candidate_sop["version"]) + 1
    validate_document(gold, "sop.schema.json")

    transcript_record = {
        "case_id": plan["case_id"],
        "source_ref": plan["source_ref"],
        "source_file": plan["source_file"],
        "review_date": plan["review_date"],
        "reviewer_role": plan["reviewer_role"],
        "asr_model": plan["asr_model"],
        "asr_event_count": transcription.get("event_count"),
        "raw_text": transcription.get("text", ""),
        "timestamped_stream_text": "".join(
            str(item.get("text", "")) for item in transcription.get("segments", [])
        ),
        "raw_transcription_digest": _digest(transcription),
        "answer_count": len(aligned),
        "answers": aligned,
        "evidence_ids": evidence_by_question,
        "status": "OPERATOR_AUDIO_VERIFIED",
    }
    return gold, transcript_record


def _format_time(milliseconds: int) -> str:
    seconds = milliseconds // 1000
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def render_gold_review(
    gold: dict[str, Any],
    transcript_record: dict[str, Any],
    plan: dict[str, Any],
) -> str:
    answers = [
        (
            f"| {item['question_id']} | {item['topic']} | "
            f"{_format_time(item['start_ms'])}–{_format_time(item['end_ms'])} | "
            f"{item['verified_claim'].replace('|', '｜')} | "
            f"{'、'.join(item['bind_step_ids']) or '操作者资历'} |"
        )
        for item in transcript_record["answers"]
    ]
    steps = [
        (
            f"| {step['step_id']} | {step['title']} | "
            f"{'必需' if step['required'] else '条件执行'} | VERIFIED | "
            f"{'、'.join(item for item in step['evidence'] if item in transcript_record['evidence_ids'].values()) or '手册/视频'} |"
        )
        for step in gold["steps"]
    ]
    return "\n".join(
        [
            "# 汉印 N31 Gold SOP 审核记录",
            "",
            f"- 审核日期：{plan['review_date']}",
            f"- 审核角色：{plan['reviewer_role']}",
            f"- 审核来源：`{plan['source_ref']}`",
            f"- 录音时长：{_format_time(plan['audio_duration_ms'])}",
            f"- 审核状态：`{transcript_record['status']}`",
            "- 说明：实际操作者连续朗读并确认12项答案；原始录音保存在Git忽略目录，仓库只保存校正转写、时间点和审核结论。",
            "",
            "## 专家口述证据",
            "",
            "| 问题 | 主题 | 录音时间 | 已核对结论 | 绑定步骤 |",
            "|---|---|---|---|---|",
            *answers,
            "",
            "## 逐步结论",
            "",
            "| 步骤 | 名称 | 结论 | 状态 | 新增口述证据 |",
            "|---|---|---|---|---|",
            *steps,
            "",
            "## 关键审核决定",
            "",
            "- S05 打开顶盖检查、S06 调节内部导纸夹：首次配置、纸宽变化、卡纸或偏斜排查时执行；同规格纸日常补充可按现场规则省略。",
            "- S11 自检页：首次配置、介质规格变化或异常排查时执行，不作为每次同规格换纸的最低要求。",
            "- 换纸后的最低验证：S10 单张走纸检查和 S12 本地测试标签均需完成。",
            "- 删除候选 S10 中无明确来源的“短按0.5秒”精确参数，只保留手册和口述支持的“短按后立即释放”。",
            "- 核心不可交换顺序：介质确认 → 初始检查 → 开机 → 导轨/装纸 → 缝标学习 → 单张走纸 → 测试打印 → 结果验收。",
            "- 公开演示需隐藏姓名、电话、地址、条码、二维码和唯一编号；这是项目隐私规则，不伪装成厂商生产要求。",
            "",
            "## 审核边界",
            "",
            "- 本记录证明实际操作者已确认口述规则和成功标准。",
            "- 京东客服教程仍只作本地参考；厂商手册原件和真实面单原件不重新分发或发送外部模型。",
            "- 当前没有第二名领域复核者；后续复核属于增强项，不阻塞本次 Gold v1 评测。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sop", type=Path, required=True)
    parser.add_argument("--asr-manifest", type=Path, required=True)
    parser.add_argument("--review-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    candidate = _read_json(args.candidate_sop)
    asr_manifest = _read_json(args.asr_manifest)
    plan = _read_json(args.review_plan)
    transcription = asr_manifest.get("transcription", {})
    gold, transcript_record = build_operator_gold(candidate, transcription, plan)
    args.output.mkdir(parents=True, exist_ok=True)
    _write_json(args.output / "gold_sop.json", gold)
    _write_json(args.output / "expert_transcript.json", transcript_record)
    (args.output / "gold_review.md").write_text(
        render_gold_review(gold, transcript_record, plan),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": transcript_record["status"],
                "answer_count": transcript_record["answer_count"],
                "evidence_count": len(gold["evidence_catalog"]),
                "step_count": len(gold["steps"]),
                "required_step_count": sum(item["required"] for item in gold["steps"]),
                "gold_version": gold["version"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
