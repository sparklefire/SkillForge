"""Validate the public SkillForge contest article against frozen project evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from .contracts import ContractValidationError, validate_document
from .demo import ROOT


DEFAULT_POLICY = ROOT / "config/submission_article_policy.json"
DEFAULT_OUTPUT = ROOT / "output/submission/submission_article_qa_v1.json"
SOURCE_PATHS = {
    "AGENT_TOOL_TRACE": "cases/n31/evaluations/agent_tool_trace_v1.json",
    "CHECKLIST": "cases/n31/demo_bundle/checklist.json",
    "DGX_VISUAL": "cases/n31/evaluations/dgx_visual_compute_v1.json",
    "GOLD_SOP": "cases/n31/gold/gold_sop.json",
    "MULTISOURCE": "cases/n31/evaluations/multisource_comparison_v1.json",
    "PDF_STRUCTURE": "cases/n31/evaluations/pdf_structure_v1.json",
    "PITCH_RUNBOOK": "cases/n31/pitch_runbook.json",
    "POSTER": "output/pdf/n31_a4_training_poster.pdf",
    "QUIZ": "cases/n31/demo_bundle/quiz.json",
    "REFERENCE_AUDIT": "external/teacher_he_reference/audit_v1.json",
    "REVISION_AUDIT": "cases/n31/demo_bundle/revision_audit.json",
    "RUNTIME_BENCHMARK": "output/evaluation/runtime_benchmark_dgx.json",
    "SELECTIVE_REBUILD": "cases/n31/evaluations/selective_rebuild_v1.json",
    "SEMANTIC_REVIEW": "cases/n31/evaluations/semantic_review_v1.json",
    "SOURCE_CANDIDATES": "cases/n31/evaluations/source_candidate_synthesis_v1.json",
    "SUMMARY": "cases/n31/demo_bundle/summary.json",
    "VIDEO_MANIFEST": "output/video/n31_training_video_manifest_v1.json",
}
ABSOLUTE_PATH_MARKERS = ("/Users/", "/home/Developer/", "file://")
PRIVATE_STATE_MARKERS = (
    "outputs/submission",
    "submission_form_packet.json",
    "human_gate_confirmations.json",
)
SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{12,}", re.IGNORECASE),
    re.compile(r"Authorization\s*:", re.IGNORECASE),
)
CHINESE_CHARACTER_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
HTTP_URL_PATTERN = re.compile(r"https?://[^\s)<]+")


class SubmissionArticleError(ValueError):
    """Raised when the contest article is unsupported, unsafe or stale."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(payload)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubmissionArticleError(f"{label}无法读取或不是合法JSON") from exc
    if not isinstance(value, dict):
        raise SubmissionArticleError(f"{label}必须是JSON对象")
    return value


def _load_policy(path: Path = DEFAULT_POLICY) -> dict[str, Any]:
    try:
        return validate_document(
            _read_json(path, "赛事征文策略"),
            "submission_article_policy.schema.json",
        )
    except ContractValidationError as exc:
        raise SubmissionArticleError("赛事征文策略不符合严格Schema") from exc


def _load_sources(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    values: dict[str, Any] = {}
    checks: list[dict[str, Any]] = []
    for source_id, relative in SOURCE_PATHS.items():
        path = (root / relative).resolve()
        if root != path and root not in path.parents:
            raise SubmissionArticleError(f"来源路径越出项目目录: {source_id}")
        if not path.is_file() or path.stat().st_size < 1:
            raise SubmissionArticleError(f"赛事征文事实来源缺失: {source_id}")
        if path.suffix.lower() == ".json":
            values[source_id] = _read_json(path, source_id)
        else:
            values[source_id] = path.read_bytes()
        checks.append(
            {
                "source_id": source_id,
                "path": relative,
                "sha256": _sha256(path),
                "status": "PASSED",
            }
        )
    return values, checks


def _claim(
    article: str,
    *,
    claim_id: str,
    source_ids: list[str],
    facts: Any,
    valid: bool,
    fragments: list[str],
) -> dict[str, Any]:
    missing = [fragment for fragment in fragments if fragment not in article]
    if not valid or missing:
        reason = "事实来源不满足" if not valid else f"正文缺少片段={missing}"
        raise SubmissionArticleError(f"赛事征文主张未通过: {claim_id}; {reason}")
    return {
        "claim_id": claim_id,
        "source_ids": source_ids,
        "fact_sha256": _canonical_sha256(facts),
        "status": "PASSED",
    }


def _claim_checks(article: str, sources: dict[str, Any]) -> list[dict[str, Any]]:
    summary = sources["SUMMARY"]
    gold = sources["GOLD_SOP"]
    audit = sources["REVISION_AUDIT"]
    checklist = sources["CHECKLIST"]
    quiz = sources["QUIZ"]
    multisource = sources["MULTISOURCE"]
    dgx = sources["DGX_VISUAL"]
    pdf = sources["PDF_STRUCTURE"]
    candidates = sources["SOURCE_CANDIDATES"]
    trace = sources["AGENT_TOOL_TRACE"]
    selective = sources["SELECTIVE_REBUILD"]
    video = sources["VIDEO_MANIFEST"]
    runtime = sources["RUNTIME_BENCHMARK"]
    semantic = sources["SEMANTIC_REVIEW"]
    reference = sources["REFERENCE_AUDIT"]
    runbook = sources["PITCH_RUNBOOK"]

    steps = gold.get("steps", [])
    required_steps = sum(item.get("required") is True for item in steps)
    conditional_steps = sum(item.get("required") is False for item in steps)
    before = summary.get("before", {})
    after = summary.get("after", {})
    candidate_summary = candidates.get("summary", {})
    trace_summary = trace.get("summary", {})
    selective_summary = selective.get("summary", {})
    pdf_summary = pdf.get("summary", {})
    dgx_summary = dgx.get("summary", {})
    video_coverage = video.get("coverage", {})
    runtime_items = {
        item.get("benchmark_id"): item for item in runtime.get("benchmarks", [])
    }
    gold_runtime = runtime_items.get("GOLD_WORKFLOW", {})
    web_runtime = runtime_items.get("WEB_LIVE_RERUN", {})
    ablation = multisource.get("source_ablation", {})
    modes = runbook.get("demo_modes", [])

    claims = [
        _claim(
            article,
            claim_id="TRACEABLE_REVISION_LOOP",
            source_ids=["SUMMARY", "REVISION_AUDIT"],
            facts={
                "workflow_state": summary.get("workflow_state"),
                "revision_changes": len(audit.get("changes", [])),
            },
            valid=summary.get("workflow_state") == "COMPLETED"
            and len(audit.get("changes", [])) == 4,
            fragments=["发现问题 → 展示证据 → 局部修订 → 再次验证"],
        ),
        _claim(
            article,
            claim_id="GOLD_13_STEPS",
            source_ids=["GOLD_SOP", "SUMMARY"],
            facts={
                "step_count": len(steps),
                "required_steps": required_steps,
                "conditional_steps": conditional_steps,
                "evaluation_basis": summary.get("evaluation_basis"),
            },
            valid=len(steps) == 13
            and required_steps == 10
            and conditional_steps == 3
            and summary.get("evaluation_basis") == "OPERATOR_REVIEWED_GOLD",
            fragments=[
                "13步Gold SOP",
                "10步必需、3步按条件执行",
                "Gold由实际操作者审核",
            ],
        ),
        _claim(
            article,
            claim_id="ERRORS_5_TO_0",
            source_ids=["SUMMARY"],
            facts={
                "before": before.get("severe_error_count"),
                "after": after.get("severe_error_count"),
            },
            valid=before.get("severe_error_count") == 5
            and after.get("severe_error_count") == 0,
            fragments=["严重问题从5项降至0项"],
        ),
        _claim(
            article,
            claim_id="COVERAGE_90_TO_100",
            source_ids=["SUMMARY"],
            facts={
                "before_required": before.get("required_step_coverage"),
                "after_required": after.get("required_step_coverage"),
                "before_evidence": before.get("evidence_supported_required_steps"),
                "after_evidence": after.get("evidence_supported_required_steps"),
            },
            valid=before.get("required_step_coverage") == 0.9
            and after.get("required_step_coverage") == 1.0
            and before.get("evidence_supported_required_steps") == 0.9
            and after.get("evidence_supported_required_steps") == 1.0,
            fragments=["必要步骤覆盖和证据覆盖都从90%提升到100%"],
        ),
        _claim(
            article,
            claim_id="FOUR_LOCAL_REVISIONS",
            source_ids=["SUMMARY", "REVISION_AUDIT"],
            facts={
                "summary_revision_count": summary.get("revision_count"),
                "audit_change_count": len(audit.get("changes", [])),
            },
            valid=summary.get("revision_count") == 4
            and len(audit.get("changes", [])) == 4,
            fragments=["4项局部修订"],
        ),
        _claim(
            article,
            claim_id="TRAINING_OUTPUTS",
            source_ids=["CHECKLIST", "QUIZ", "POSTER", "VIDEO_MANIFEST"],
            facts={
                "checklist_items": len(checklist.get("items", [])),
                "quiz_questions": len(quiz.get("questions", [])),
                "poster_pdf": sources["POSTER"][:5].decode("ascii", errors="ignore"),
                "video_duration_ms": video.get("output", {}).get("duration_ms"),
                "video_scenes": video_coverage.get("scene_count"),
                "covered_gold_steps": video_coverage.get("covered_gold_step_count"),
                "evidence_count": video.get("evidence_pack", {}).get("evidence_count"),
            },
            valid=len(checklist.get("items", [])) == 13
            and len(quiz.get("questions", [])) == 5
            and sources["POSTER"].startswith(b"%PDF-")
            and video.get("output", {}).get("duration_ms") == 80_000
            and video_coverage.get("scene_count") == 15
            and video_coverage.get("covered_gold_step_count") == 13
            and video.get("evidence_pack", {}).get("evidence_count") == 25
            and video.get("final_human_review_required") is True,
            fragments=[
                "13项手机检查清单、5题培训测验、一页A4海报和80秒培训视频",
                "15个镜头覆盖13/13个Gold步骤，绑定25条去重Evidence",
            ],
        ),
        _claim(
            article,
            claim_id="DGX_NATIVE_CUDA",
            source_ids=["DGX_VISUAL"],
            facts={
                "backend": dgx.get("backend"),
                "actual_gpu_compute": dgx.get("actual_gpu_compute"),
                "processed_video_count": dgx_summary.get("processed_video_count"),
                "sampled_frame_count": dgx_summary.get("sampled_frame_count"),
                "selected_frame_count": dgx_summary.get("selected_frame_count"),
                "claim_scope": dgx.get("semantic_claim_scope"),
            },
            valid=dgx.get("backend") == "CUDA_NATIVE"
            and dgx.get("actual_gpu_compute") is True
            and dgx_summary.get("processed_video_count") == 6
            and dgx_summary.get("sampled_frame_count") == 420
            and dgx_summary.get("selected_frame_count") == 50
            and dgx.get("semantic_claim_scope") == "CANDIDATE_SELECTION_ONLY",
            fragments=["6段安全派生视频、420帧、50个候选时间点"],
        ),
        _claim(
            article,
            claim_id="PDF_STRUCTURE",
            source_ids=["PDF_STRUCTURE"],
            facts=pdf_summary,
            valid=pdf_summary.get("source_count") == 2
            and pdf_summary.get("page_count") == 58
            and pdf_summary.get("block_count") == 607
            and pdf_summary.get("ocr_applied_page_count") == 9
            and pdf_summary.get("needs_ocr_page_count") == 0,
            fragments=[
                "两份手册共58页，形成607个页码绑定结构块；9页在本地完成中文OCR，待处理页为0"
            ],
        ),
        _claim(
            article,
            claim_id="MULTISOURCE_ABLATION",
            source_ids=["MULTISOURCE"],
            facts={
                "manual": ablation.get("manual_only", {}).get("coverage"),
                "expert": ablation.get("expert_audio_only", {}).get("coverage"),
                "two_or_more": ablation.get("two_or_more_source_types", {}).get(
                    "coverage"
                ),
            },
            valid=ablation.get("manual_only", {}).get("coverage") == 0.8
            and ablation.get("expert_audio_only", {}).get("coverage") == 0.9
            and ablation.get("two_or_more_source_types", {}).get("coverage") == 1.0,
            fragments=[
                "手册单源覆盖80%，专家口述单源覆盖90%，至少两类来源联合覆盖100%"
            ],
        ),
        _claim(
            article,
            claim_id="SOURCE_CANDIDATE_SYNTHESIS",
            source_ids=["SOURCE_CANDIDATES"],
            facts=candidate_summary,
            valid=candidate_summary.get("source_candidate_counts")
            == {"audio": 8, "pdf": 7, "video": 18}
            and candidate_summary.get("ordered_step_count") == 13
            and candidate_summary.get("multi_source_step_count") == 12
            and candidate_summary.get("three_source_step_count") == 10
            and candidate_summary.get("graph_acyclic") is True,
            fragments=[
                "视频、手册和口述先分别产生18条、7条和8条候选",
                "12步至少有两类来源，10步覆盖全部三类来源",
            ],
        ),
        _claim(
            article,
            claim_id="AGENT_TOOL_TRACE",
            source_ids=["AGENT_TOOL_TRACE"],
            facts=trace_summary,
            valid=trace_summary.get("agent_count") == 5
            and trace_summary.get("tool_count") == 13
            and trace_summary.get("tool_call_count") == 14,
            fragments=["五类Agent、13种工具和14次工具调用"],
        ),
        _claim(
            article,
            claim_id="SELECTIVE_REBUILD",
            source_ids=["SELECTIVE_REBUILD"],
            facts=selective_summary,
            valid=selective_summary.get("affected_step_count") == 7
            and selective_summary.get("quiz_question_count") == 1
            and selective_summary.get("video_scene_count") == 7,
            fragments=["只影响7个步骤、1道题和7个视频镜头"],
        ),
        _claim(
            article,
            claim_id="RUNTIME_SCOPE",
            source_ids=["RUNTIME_BENCHMARK"],
            facts={
                "measured_iterations": runtime.get("configuration", {}).get(
                    "measured_iterations"
                ),
                "warmup_iterations": runtime.get("configuration", {}).get(
                    "warmup_iterations"
                ),
                "total_measured_iterations": runtime.get("stability", {}).get(
                    "total_measured_iterations"
                ),
                "unique_semantic_fingerprint_count": runtime.get(
                    "stability", {}
                ).get("unique_semantic_fingerprint_count"),
                "gold_failure_count": gold_runtime.get("failure_count"),
                "web_failure_count": web_runtime.get("failure_count"),
                "raw_media_processed": runtime.get("data_policy", {}).get(
                    "raw_media_processed"
                ),
            },
            valid=runtime.get("configuration", {}).get("measured_iterations") == 20
            and runtime.get("configuration", {}).get("warmup_iterations") == 2
            and runtime.get("stability", {}).get("total_measured_iterations") == 40
            and runtime.get("stability", {}).get(
                "unique_semantic_fingerprint_count"
            )
            == 1
            and runtime.get("stability", {}).get(
                "gold_and_web_semantics_equal"
            )
            is True
            and gold_runtime.get("successful_iterations") == 20
            and web_runtime.get("successful_iterations") == 20
            and gold_runtime.get("failure_count") == 0
            and web_runtime.get("failure_count") == 0
            and runtime.get("data_policy", {}).get("raw_media_processed") is False,
            fragments=[
                "直接Gold与Web现场重算各20次，共40轮全部成功且唯一P0语义指纹为1个",
                "这个数值不包含原始视频、PDF或录音的预处理",
            ],
        ),
        _claim(
            article,
            claim_id="MODEL_AND_REFERENCE_BOUNDARY",
            source_ids=["SEMANTIC_REVIEW", "REFERENCE_AUDIT"],
            facts={
                "semantic_model": semantic.get("model"),
                "semantic_status": semantic.get("status"),
                "safe_derivative_only": semantic.get("data_policy", {}).get(
                    "safe_structured_derivative_only"
                ),
                "reference_status": reference.get("status"),
                "reference_execution_claim": reference.get("execution_claim"),
            },
            valid=semantic.get("model") == "step-3.7-flash"
            and semantic.get("status") == "COMPLETED"
            and semantic.get("data_policy", {}).get("safe_structured_derivative_only")
            is True
            and reference.get("status") == "WAITING_ON_RUNTIME_BUNDLE"
            and reference.get("execution_claim", {}).get(
                "current_skillforge_dgx_execution_completed"
            )
            is False,
            fragments=[
                "Step 3.7语义规划和视觉复核使用Step Plan API",
                "不把外部API包装成DGX本地推理",
                "何老师参考代码只完成静态分析",
            ],
        ),
        _claim(
            article,
            claim_id="DEMO_MODES",
            source_ids=["PITCH_RUNBOOK"],
            facts={
                "modes": [item.get("mode") for item in modes],
                "expected": [item.get("expected") for item in modes],
            },
            valid=[item.get("mode") for item in modes]
            == ["LIVE", "PREPROCESSED", "OFFLINE"]
            and all(
                item.get("expected", {}).get("before_errors") == 5
                and item.get("expected", {}).get("after_errors") == 0
                and item.get("expected", {}).get("revision_count") == 4
                for item in modes
            ),
            fragments=["现场重算、预处理结果和无素材离线包三种演示模式"],
        ),
    ]
    return claims


def _check_links(article: str, *, root: Path, policy: dict[str, Any]) -> int:
    links = MARKDOWN_LINK_PATTERN.findall(article)
    raw_http_urls = HTTP_URL_PATTERN.findall(article)
    if sorted(raw_http_urls) != sorted(link for link in links if link.startswith("http")):
        raise SubmissionArticleError("正文中的HTTP网址必须全部使用Markdown链接")
    prefix = policy["allowed_repository_url_prefix"]
    repository_count = 0
    for link in links:
        if link.startswith("http"):
            if link == prefix:
                repository_count += 1
                continue
            blob_prefix = f"{prefix}/blob/main/"
            if not link.startswith(blob_prefix):
                raise SubmissionArticleError("赛事征文只能链接当前公开代码仓库")
            relative = unquote(link[len(blob_prefix) :])
            target = (root / relative).resolve()
            if root != target and root not in target.parents:
                raise SubmissionArticleError("赛事征文仓库链接越出项目目录")
            if not target.is_file():
                raise SubmissionArticleError(f"赛事征文仓库链接不存在: {relative}")
            repository_count += 1
            continue
        target = (root / "docs" / unquote(link)).resolve()
        if root != target and root not in target.parents:
            raise SubmissionArticleError("赛事征文相对链接越出项目目录")
        if not target.exists():
            raise SubmissionArticleError(f"赛事征文相对链接不存在: {link}")
    return repository_count


def verify_submission_article_text(
    article: str,
    *,
    root: Path = ROOT,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    policy = policy or _load_policy()
    headings = [
        match.group(1).strip()
        for match in re.finditer(r"^##\s+(.+?)\s*$", article, re.MULTILINE)
    ]
    chinese_count = len(CHINESE_CHARACTER_PATTERN.findall(article))
    if not article.startswith("# ") or not article.splitlines()[0][2:].strip():
        raise SubmissionArticleError("赛事征文缺少一级标题")
    if headings != policy["required_headings"]:
        raise SubmissionArticleError("赛事征文二级标题集合或顺序不符合冻结策略")
    if chinese_count < policy["minimum_chinese_characters"]:
        raise SubmissionArticleError("赛事征文中文字符不足600")
    present_forbidden = [
        phrase for phrase in policy["forbidden_phrases"] if phrase in article
    ]
    if present_forbidden:
        raise SubmissionArticleError(f"赛事征文包含禁止夸大措辞: {present_forbidden}")
    if any(marker in article for marker in ABSOLUTE_PATH_MARKERS):
        raise SubmissionArticleError("赛事征文包含绝对路径")
    if any(marker in article for marker in PRIVATE_STATE_MARKERS):
        raise SubmissionArticleError("赛事征文包含私有提交状态定位")
    if any(pattern.search(article) for pattern in SECRET_PATTERNS):
        raise SubmissionArticleError("赛事征文包含疑似凭证或Authorization")
    repository_link_count = _check_links(article, root=root, policy=policy)
    sources, source_checks = _load_sources(root)
    claims = _claim_checks(article, sources)
    claim_ids = [item["claim_id"] for item in claims]
    if claim_ids != policy["required_claim_ids"] or len(claim_ids) != len(set(claim_ids)):
        raise SubmissionArticleError("赛事征文主张集合与冻结策略不一致")
    return {
        "chinese_character_count": chinese_count,
        "heading_count": len(headings),
        "repository_link_count": repository_link_count,
        "source_checks": source_checks,
        "claim_checks": claims,
    }


def build_submission_article_qa(
    *,
    root: Path = ROOT,
    policy_path: Path = DEFAULT_POLICY,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    policy = _load_policy(policy_path)
    article_path = (root / policy["article_path"]).resolve()
    if root != article_path and root not in article_path.parents:
        raise SubmissionArticleError("赛事征文路径越出项目目录")
    try:
        article_bytes = article_path.read_bytes()
        article = article_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SubmissionArticleError("赛事征文缺失或不是UTF-8") from exc
    verified = verify_submission_article_text(article, root=root, policy=policy)
    report = {
        "version": 1,
        "case_id": "n31_media_change",
        "artifact_type": "SUBMISSION_ARTICLE_QA",
        "status": "READY_FOR_MANUAL_PUBLICATION",
        "article_path": policy["article_path"],
        "article_sha256": _sha256_bytes(article_bytes),
        "article_bytes": len(article_bytes),
        **verified,
        "checks": {
            "title_present": True,
            "minimum_length_met": True,
            "required_headings_exact": True,
            "required_headings_ordered": True,
            "required_claims_source_backed": True,
            "repository_links_safe": True,
            "repository_links_resolve": True,
            "forbidden_phrases_absent": True,
            "absolute_paths_absent": True,
            "credentials_absent": True,
            "private_submission_state_absent": True,
            "manual_publication_only": True,
        },
        "publication_state": {
            "article_content_ready": True,
            "public_url_available": False,
            "manual_publication_required": True,
            "automatic_publication_performed": False,
        },
        "data_policy": {
            "contains_credentials": False,
            "contains_personal_data": False,
            "contains_form_url": False,
            "contains_absolute_paths": False,
            "contains_private_submission_state": False,
            "contains_raw_media": False,
            "network_requests": 0,
        },
    }
    try:
        return validate_document(report, "submission_article_qa.schema.json")
    except ContractValidationError as exc:
        raise SubmissionArticleError("赛事征文QA不符合严格Schema") from exc


def _write_json(document: dict[str, Any], destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def verify_saved_submission_article_qa(
    report_path: Path = DEFAULT_OUTPUT,
    *,
    root: Path = ROOT,
    policy_path: Path = DEFAULT_POLICY,
) -> dict[str, Any]:
    try:
        saved = validate_document(
            _read_json(report_path, "赛事征文QA"),
            "submission_article_qa.schema.json",
        )
    except ContractValidationError as exc:
        raise SubmissionArticleError("赛事征文保存QA不符合严格Schema") from exc
    current = build_submission_article_qa(root=root, policy_path=policy_path)
    if saved != current:
        raise SubmissionArticleError("赛事征文、事实来源、策略或保存QA已漂移")
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.verify_only:
            report = verify_saved_submission_article_qa(
                args.output,
                policy_path=args.policy,
            )
        else:
            report = build_submission_article_qa(policy_path=args.policy)
            _write_json(report, args.output)
    except (ContractValidationError, OSError, SubmissionArticleError) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "message": (
                        str(exc)
                        if isinstance(exc, SubmissionArticleError)
                        else "赛事征文检查失败"
                    ),
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": report["status"],
                "chinese_character_count": report["chinese_character_count"],
                "claim_count": len(report["claim_checks"]),
                "source_count": len(report["source_checks"]),
                "repository_link_count": report["repository_link_count"],
                "manual_publication_required": report["publication_state"][
                    "manual_publication_required"
                ],
                "network_requests": report["data_policy"]["network_requests"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
