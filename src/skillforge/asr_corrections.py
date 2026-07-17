"""Private, source-bound operator corrections for timestamped ASR answers."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from .contracts import validate_document
from .observability import redact
from .revision import digest


SESSION_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
QUESTION_ID_PATTERN = re.compile(r"^Q[0-9]{2}$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _compact(text: str) -> str:
    return re.sub(r"[\s，。；：、？！,.!?;:（）()“”\"'‘’·\-—]", "", text)


def _binding_digest(
    *,
    evidence_id: str,
    source_ref: str,
    claim: str,
    locator: dict[str, int],
    corrected_text: str,
) -> str:
    return digest(
        {
            "evidence_id": evidence_id,
            "source_ref": source_ref,
            "claim": claim,
            "locator": locator,
            "corrected_asr_text": corrected_text,
        }
    )


def source_bindings(
    transcript: dict[str, Any],
    gold_sop: dict[str, Any],
) -> dict[str, str]:
    raw_digest = str(transcript.get("raw_transcription_digest", ""))
    if not SHA256_PATTERN.fullmatch(raw_digest):
        raise ValueError("专家转写缺少有效的原始ASR摘要")
    return {
        "expert_transcript_sha256": digest(transcript),
        "gold_sop_sha256": digest(gold_sop),
        "raw_transcription_sha256": raw_digest,
    }


def _source_answers(
    transcript: dict[str, Any],
    gold_sop: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    validate_document(gold_sop, "sop.schema.json")
    if transcript.get("case_id") != gold_sop["case_id"]:
        raise ValueError("专家转写与Gold SOP案例编号不一致")
    source_ref = transcript.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref:
        raise ValueError("专家转写缺少来源编号")
    answers = transcript.get("answers")
    evidence_ids = transcript.get("evidence_ids")
    if not isinstance(answers, list) or not answers or not isinstance(evidence_ids, dict):
        raise ValueError("专家转写缺少问答或Evidence映射")
    evidence_map = {
        item["evidence_id"]: item for item in gold_sop["evidence_catalog"]
    }
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_questions: set[str] = set()
    seen_evidence: set[str] = set()
    for answer in answers:
        if not isinstance(answer, dict):
            raise ValueError("专家转写问答必须是JSON对象")
        question_id = answer.get("question_id")
        if not isinstance(question_id, str) or not QUESTION_ID_PATTERN.fullmatch(question_id):
            raise ValueError("专家转写包含无效问题编号")
        if question_id in seen_questions:
            raise ValueError("专家转写包含重复问题编号")
        evidence_id = evidence_ids.get(question_id)
        evidence = evidence_map.get(evidence_id)
        if evidence is None:
            raise ValueError(f"{question_id} 未绑定有效Gold Evidence")
        if evidence_id in seen_evidence:
            raise ValueError("专家转写多个问题绑定同一Evidence")
        if evidence["source_type"] != "audio" or evidence["source_ref"] != source_ref:
            raise ValueError(f"{question_id} Evidence不是当前专家录音")
        start_ms = answer.get("start_ms")
        end_ms = answer.get("end_ms")
        if (
            not isinstance(start_ms, int)
            or isinstance(start_ms, bool)
            or not isinstance(end_ms, int)
            or isinstance(end_ms, bool)
            or end_ms <= start_ms
        ):
            raise ValueError(f"{question_id} ASR时间范围无效")
        if evidence["locator"] != {"start_ms": start_ms, "end_ms": end_ms}:
            raise ValueError(f"{question_id} ASR时间范围与Evidence不一致")
        for field in ("topic", "raw_asr_text", "corrected_asr_text"):
            if not isinstance(answer.get(field), str) or not answer[field].strip():
                raise ValueError(f"{question_id} 缺少{field}")
        checks = answer.get("required_term_checks")
        if not isinstance(checks, dict) or not checks or not all(
            isinstance(key, str) and key and value is True
            for key, value in checks.items()
        ):
            raise ValueError(f"{question_id} 必要术语检查未通过")
        seen_questions.add(question_id)
        seen_evidence.add(evidence_id)
        pairs.append((answer, evidence))
    return pairs


def _recalculate(document: dict[str, Any]) -> None:
    answers = document["answers"]
    correction_events = sum(item["correction_count"] for item in answers)
    document["status"] = "CORRECTED" if correction_events else "OPEN"
    document["summary"] = {
        "answer_count": len(answers),
        "corrected_answer_count": sum(
            item["correction_state"] == "CORRECTED" for item in answers
        ),
        "correction_event_count": correction_events,
        "evidence_binding_count": len(answers),
    }


def _validate_session(document: dict[str, Any]) -> dict[str, Any]:
    validate_document(document, "asr_correction_session.schema.json")
    answers = document["answers"]
    question_ids = [item["question_id"] for item in answers]
    evidence_ids = [item["evidence_id"] for item in answers]
    if len(question_ids) != len(set(question_ids)):
        raise ValueError("ASR修正会话包含重复问题编号")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ValueError("ASR修正会话包含重复Evidence编号")
    correction_ids: list[str] = []
    for answer in answers:
        if answer["end_ms"] <= answer["start_ms"]:
            raise ValueError("ASR修正会话包含无效时间范围")
        binding = answer["evidence_binding"]
        locator = {"start_ms": answer["start_ms"], "end_ms": answer["end_ms"]}
        if binding["locator"] != locator:
            raise ValueError("ASR修正会话的Evidence时间范围不一致")
        baseline_digest = _binding_digest(
            evidence_id=answer["evidence_id"],
            source_ref=binding["source_ref"],
            claim=binding["claim"],
            locator=locator,
            corrected_text=answer["baseline_corrected_text"],
        )
        if binding["baseline_sha256"] != baseline_digest:
            raise ValueError("ASR修正会话的基线Evidence摘要无效")
        current_digest = _binding_digest(
            evidence_id=answer["evidence_id"],
            source_ref=binding["source_ref"],
            claim=binding["claim"],
            locator=locator,
            corrected_text=answer["effective_corrected_text"],
        )
        if binding["current_sha256"] != current_digest:
            raise ValueError("ASR修正会话的当前Evidence摘要无效")
        current_text = answer["baseline_corrected_text"]
        chained_digest = baseline_digest
        for correction in answer["corrections"]:
            correction_ids.append(correction["correction_id"])
            if correction["before_text"] != current_text:
                raise ValueError("ASR修正历史文本链断裂")
            if correction["previous_evidence_binding_sha256"] != chained_digest:
                raise ValueError("ASR修正历史Evidence摘要链断裂")
            current_text = correction["after_text"]
            chained_digest = _binding_digest(
                evidence_id=answer["evidence_id"],
                source_ref=binding["source_ref"],
                claim=binding["claim"],
                locator=locator,
                corrected_text=current_text,
            )
            if correction["evidence_binding_sha256"] != chained_digest:
                raise ValueError("ASR修正历史Evidence摘要无效")
        if current_text != answer["effective_corrected_text"] or chained_digest != current_digest:
            raise ValueError("ASR修正历史与当前值不一致")
        if answer["correction_count"] != len(answer["corrections"]):
            raise ValueError("ASR修正次数汇总不一致")
        expected_state = (
            "UNCHANGED"
            if answer["effective_corrected_text"] == answer["baseline_corrected_text"]
            else "CORRECTED"
        )
        if answer["correction_state"] != expected_state:
            raise ValueError("ASR修正状态与当前文本不一致")
        compacted = _compact(answer["effective_corrected_text"])
        if any(_compact(term) not in compacted for term in answer["required_terms"]):
            raise ValueError("ASR修正删除了已审核必要术语")
        if answer["corrections"]:
            latest = answer["corrections"][-1]
            if (
                answer["latest_operator"] != latest["operator"]
                or answer["latest_reason"] != latest["reason"]
                or answer["latest_corrected_at"] != latest["recorded_at"]
            ):
                raise ValueError("ASR修正最新操作者信息与历史不一致")
        elif any(
            answer[field] is not None
            for field in ("latest_operator", "latest_reason", "latest_corrected_at")
        ):
            raise ValueError("未修正问答不能包含最新操作者信息")
    if len(correction_ids) != len(set(correction_ids)):
        raise ValueError("ASR修正历史包含重复事件编号")
    event_ids = [item["event_id"] for item in document["events"]]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("ASR修正会话包含重复事件编号")
    if document["events"][0]["event_type"] != "SESSION_CREATED":
        raise ValueError("ASR修正会话缺少创建事件")
    if sum(item["event_type"] == "ANSWER_CORRECTED" for item in document["events"]) != len(
        correction_ids
    ):
        raise ValueError("ASR修正事件数与修正历史不一致")
    copy = json.loads(json.dumps(document))
    _recalculate(copy)
    if copy["status"] != document["status"] or copy["summary"] != document["summary"]:
        raise ValueError("ASR修正会话汇总无效")
    return document


class AsrCorrectionStore:
    """Persist immutable-source ASR corrections in a private mode-600 store."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self._lock = Lock()

    def _path(self, session_id: str) -> Path:
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise FileNotFoundError(session_id)
        return self.root / f"{session_id}.json"

    def _write(self, document: dict[str, Any]) -> None:
        _validate_session(document)
        path = self._path(document["session_id"])
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{document['session_id']}.", dir=self.root
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _assert_sources(
        document: dict[str, Any],
        transcript: dict[str, Any],
        gold_sop: dict[str, Any],
    ) -> None:
        if document["source_bindings"] != source_bindings(transcript, gold_sop):
            raise ValueError("ASR修正会话绑定的转写或Gold SOP已经变化，请新建会话")

    def create(
        self,
        transcript: dict[str, Any],
        gold_sop: dict[str, Any],
    ) -> dict[str, Any]:
        pairs = _source_answers(transcript, gold_sop)
        now = _now()
        answers: list[dict[str, Any]] = []
        for answer, evidence in pairs:
            corrected = answer["corrected_asr_text"].strip()
            locator = {
                "start_ms": answer["start_ms"],
                "end_ms": answer["end_ms"],
            }
            binding_digest = _binding_digest(
                evidence_id=evidence["evidence_id"],
                source_ref=evidence["source_ref"],
                claim=evidence["claim"],
                locator=locator,
                corrected_text=corrected,
            )
            answers.append(
                {
                    "question_id": answer["question_id"],
                    "topic": answer["topic"],
                    "start_ms": answer["start_ms"],
                    "end_ms": answer["end_ms"],
                    "evidence_id": evidence["evidence_id"],
                    "raw_asr_text": answer["raw_asr_text"],
                    "baseline_corrected_text": corrected,
                    "effective_corrected_text": corrected,
                    "required_terms": sorted(answer["required_term_checks"]),
                    "correction_state": "UNCHANGED",
                    "correction_count": 0,
                    "evidence_binding": {
                        "source_ref": evidence["source_ref"],
                        "claim": evidence["claim"],
                        "locator": locator,
                        "baseline_sha256": binding_digest,
                        "current_sha256": binding_digest,
                    },
                    "latest_operator": None,
                    "latest_reason": None,
                    "latest_corrected_at": None,
                    "corrections": [],
                }
            )
        document = {
            "artifact_type": "ASR_CORRECTION_SESSION",
            "version": 1,
            "session_id": uuid.uuid4().hex,
            "case_id": transcript["case_id"],
            "source_ref": transcript["source_ref"],
            "created_at": now,
            "updated_at": now,
            "status": "OPEN",
            "source_bindings": source_bindings(transcript, gold_sop),
            "answers": answers,
            "summary": {
                "answer_count": len(answers),
                "corrected_answer_count": 0,
                "correction_event_count": 0,
                "evidence_binding_count": len(answers),
            },
            "events": [
                {
                    "event_id": uuid.uuid4().hex,
                    "event_type": "SESSION_CREATED",
                    "question_id": None,
                    "actor": "SYSTEM",
                    "detail": "绑定专家转写、原始ASR摘要和Gold Evidence",
                    "recorded_at": now,
                }
            ],
            "data_policy": {
                "storage_scope": "LOCAL_PRIVATE_ONLY",
                "external_model_calls": 0,
                "contains_raw_transcript_snippets": True,
                "contains_raw_media": False,
                "contains_credentials": False,
                "contains_absolute_paths": False,
            },
        }
        with self._lock:
            self._write(document)
        return document

    def get(self, session_id: str) -> dict[str, Any]:
        path = self._path(session_id)
        if not path.is_file():
            raise FileNotFoundError(session_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("ASR修正会话必须是JSON对象")
        return _validate_session(payload)

    def get_bound(
        self,
        session_id: str,
        transcript: dict[str, Any],
        gold_sop: dict[str, Any],
    ) -> dict[str, Any]:
        document = self.get(session_id)
        _source_answers(transcript, gold_sop)
        self._assert_sources(document, transcript, gold_sop)
        return document

    def correct(
        self,
        session_id: str,
        question_id: str,
        *,
        corrected_text: str,
        operator: str,
        reason: str,
        transcript: dict[str, Any],
        gold_sop: dict[str, Any],
    ) -> dict[str, Any]:
        if not QUESTION_ID_PATTERN.fullmatch(question_id):
            raise ValueError("ASR修正问题编号无效")
        for name, value, maximum in (
            ("修正文本", corrected_text, 5000),
            ("操作者", operator, 80),
            ("修正原因", reason, 500),
        ):
            if not isinstance(value, str):
                raise ValueError(f"{name}必须为文本")
            if not value.strip():
                raise ValueError(f"{name}不能为空")
            if len(value.strip()) > maximum:
                raise ValueError(f"{name}不能超过{maximum}字")
        safe_text = str(redact(corrected_text.strip()))
        safe_operator = str(redact(operator.strip()))
        safe_reason = str(redact(reason.strip()))
        with self._lock:
            document = self.get_bound(session_id, transcript, gold_sop)
            answer = next(
                (item for item in document["answers"] if item["question_id"] == question_id),
                None,
            )
            if answer is None:
                raise ValueError("ASR修正会话不存在该问题")
            before = answer["effective_corrected_text"]
            if safe_text == before:
                raise ValueError("修正文本与当前文本相同")
            compacted = _compact(safe_text)
            missing = [
                term for term in answer["required_terms"] if _compact(term) not in compacted
            ]
            if missing:
                raise ValueError(f"修正文本缺少已审核必要术语: {'、'.join(missing)}")
            binding = answer["evidence_binding"]
            previous_digest = binding["current_sha256"]
            current_digest = _binding_digest(
                evidence_id=answer["evidence_id"],
                source_ref=binding["source_ref"],
                claim=binding["claim"],
                locator=binding["locator"],
                corrected_text=safe_text,
            )
            now = _now()
            correction = {
                "correction_id": uuid.uuid4().hex,
                "before_text": before,
                "after_text": safe_text,
                "operator": safe_operator,
                "reason": safe_reason,
                "recorded_at": now,
                "previous_evidence_binding_sha256": previous_digest,
                "evidence_binding_sha256": current_digest,
            }
            answer["effective_corrected_text"] = safe_text
            answer["correction_state"] = (
                "UNCHANGED" if safe_text == answer["baseline_corrected_text"] else "CORRECTED"
            )
            answer["corrections"].append(correction)
            answer["correction_count"] = len(answer["corrections"])
            answer["evidence_binding"]["current_sha256"] = current_digest
            answer["latest_operator"] = safe_operator
            answer["latest_reason"] = safe_reason
            answer["latest_corrected_at"] = now
            document["updated_at"] = now
            document["events"].append(
                {
                    "event_id": uuid.uuid4().hex,
                    "event_type": "ANSWER_CORRECTED",
                    "question_id": question_id,
                    "actor": "OPERATOR",
                    "detail": safe_reason,
                    "recorded_at": now,
                }
            )
            _recalculate(document)
            self._write(document)
        return document
