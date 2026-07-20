from __future__ import annotations

import json
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from skillforge.contracts import ContractValidationError, validate_document
from skillforge.submission_article import (
    DEFAULT_OUTPUT,
    SubmissionArticleError,
    _load_policy,
    build_submission_article_qa,
    verify_saved_submission_article_qa,
    verify_submission_article_text,
)


ROOT = Path(__file__).resolve().parents[1]
ARTICLE = ROOT / "docs/赛事征文.md"
POLICY = ROOT / "config/submission_article_policy.json"


def _article() -> str:
    return ARTICLE.read_text(encoding="utf-8")


def test_real_article_is_source_backed_publication_ready_and_deterministic() -> None:
    first = build_submission_article_qa(root=ROOT, policy_path=POLICY)
    second = build_submission_article_qa(root=ROOT, policy_path=POLICY)
    validate_document(first, "submission_article_qa.schema.json")

    assert first == second
    assert first["status"] == "READY_FOR_MANUAL_PUBLICATION"
    assert first["chinese_character_count"] >= 600
    assert first["heading_count"] == 9
    assert first["repository_link_count"] >= 8
    assert len(first["source_checks"]) == 17
    assert len(first["claim_checks"]) == 15
    assert all(item["status"] == "PASSED" for item in first["claim_checks"])
    assert first["publication_state"] == {
        "article_content_ready": True,
        "public_url_available": False,
        "manual_publication_required": True,
        "automatic_publication_performed": False,
    }
    assert first["data_policy"]["network_requests"] == 0


def test_article_qa_has_no_private_or_credential_content() -> None:
    report = build_submission_article_qa(root=ROOT, policy_path=POLICY)
    serialized = json.dumps(report, ensure_ascii=False)
    for marker in (
        "/Users/",
        "/home/Developer/",
        "file://",
        "Authorization",
        "Bearer ",
        "outputs/submission",
        "human_gate_confirmations",
        "jsjform",
    ):
        assert marker not in serialized


def test_missing_heading_or_metric_claim_is_rejected() -> None:
    policy = _load_policy(POLICY)
    missing_heading = _article().replace("## 真实案例", "## 案例")
    with pytest.raises(SubmissionArticleError, match="标题集合或顺序"):
        verify_submission_article_text(missing_heading, root=ROOT, policy=policy)

    changed_metric = _article().replace(
        "严重问题从5项降至0项", "严重问题得到明显改善"
    )
    with pytest.raises(SubmissionArticleError, match="ERRORS_5_TO_0"):
        verify_submission_article_text(changed_metric, root=ROOT, policy=policy)


@pytest.mark.parametrize(
    "phrase",
    [
        "全部模型均在DGX本地运行",
        "原始多模态处理仅需179.826毫秒",
        "无需人工审核",
        "完全自动生成Gold",
        "100%安全",
        "何老师参考代码已在本项目跑通",
        "视频完整证明全部步骤",
    ],
)
def test_overclaim_phrases_are_rejected(phrase: str) -> None:
    with pytest.raises(SubmissionArticleError, match="禁止夸大措辞"):
        verify_submission_article_text(
            _article() + f"\n{phrase}\n",
            root=ROOT,
            policy=_load_policy(POLICY),
        )


def test_external_or_broken_repository_link_is_rejected() -> None:
    external = _article().replace(
        "https://github.com/sparklefire/SkillForge)",
        "https://example.com/skillforge)",
    )
    with pytest.raises(SubmissionArticleError, match="当前公开代码仓库"):
        verify_submission_article_text(
            external,
            root=ROOT,
            policy=_load_policy(POLICY),
        )

    broken = _article().replace(
        "cases/n31/demo_bundle/summary.json",
        "cases/n31/demo_bundle/does_not_exist.json",
    )
    with pytest.raises(SubmissionArticleError, match="链接不存在"):
        verify_submission_article_text(
            broken,
            root=ROOT,
            policy=_load_policy(POLICY),
        )


def test_absolute_path_private_state_and_credentials_are_rejected() -> None:
    policy = _load_policy(POLICY)
    for injected, match in (
        ("/Users/example/private", "绝对路径"),
        ("outputs/submission/private.json", "私有提交状态"),
        ("Authorization: Bearer abcdefghijklmnop", "凭证"),
    ):
        with pytest.raises(SubmissionArticleError, match=match):
            verify_submission_article_text(
                _article() + f"\n{injected}\n",
                root=ROOT,
                policy=policy,
            )


def test_saved_qa_verification_detects_drift(tmp_path: Path) -> None:
    report = build_submission_article_qa(root=ROOT, policy_path=POLICY)
    valid_path = tmp_path / "valid.json"
    valid_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert verify_saved_submission_article_qa(
        valid_path,
        root=ROOT,
        policy_path=POLICY,
    ) == report

    stale = deepcopy(report)
    stale["article_sha256"] = "0" * 64
    stale_path = tmp_path / "stale.json"
    stale_path.write_text(
        json.dumps(stale, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SubmissionArticleError, match="已漂移"):
        verify_saved_submission_article_qa(
            stale_path,
            root=ROOT,
            policy_path=POLICY,
        )


def test_policy_and_report_schemas_reject_false_readiness() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    invalid_policy = deepcopy(policy)
    invalid_policy["minimum_chinese_characters"] = 100
    with pytest.raises(ContractValidationError):
        validate_document(invalid_policy, "submission_article_policy.schema.json")

    changed_guardrail = deepcopy(policy)
    changed_guardrail["forbidden_phrases"][0] = "替换后的规则"
    with pytest.raises(ContractValidationError):
        validate_document(changed_guardrail, "submission_article_policy.schema.json")

    report = build_submission_article_qa(root=ROOT, policy_path=POLICY)
    invalid_report = deepcopy(report)
    invalid_report["publication_state"]["automatic_publication_performed"] = True
    with pytest.raises(ContractValidationError):
        validate_document(invalid_report, "submission_article_qa.schema.json")


def test_generated_report_matches_tracked_qa_and_script_is_executable() -> None:
    assert DEFAULT_OUTPUT.is_file()
    assert verify_saved_submission_article_qa(
        DEFAULT_OUTPUT,
        root=ROOT,
        policy_path=POLICY,
    ) == build_submission_article_qa(root=ROOT, policy_path=POLICY)
    script = ROOT / "scripts/check_submission_article.sh"
    assert script.is_file()
    assert stat.S_IMODE(script.stat().st_mode) & 0o111
