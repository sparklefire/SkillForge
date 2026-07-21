from __future__ import annotations

import pytest

from skillforge.cli_hints import print_error_hints


def test_exact_match_wins_over_prefix(capsys: pytest.CaptureFixture[str]) -> None:
    print_error_hints(
        "完全匹配消息",
        exact_hints={"完全匹配消息": ["exact-line"]},
        prefix_hints={"完全": ["prefix-line"]},
    )
    captured = capsys.readouterr()
    assert "exact-line" in captured.err
    assert "prefix-line" not in captured.err
    assert captured.out == ""


def test_prefix_fallback_when_no_exact(capsys: pytest.CaptureFixture[str]) -> None:
    print_error_hints(
        "官方规则六项核对未通过：R1, R2",
        exact_hints={"别处": ["nope"]},
        prefix_hints={"官方规则六项核对未通过": ["prefix-line"]},
    )
    captured = capsys.readouterr()
    assert "prefix-line" in captured.err


def test_no_match_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    print_error_hints(
        "未知错误",
        exact_hints={"别处": ["nope"]},
        prefix_hints={"也不匹配": ["nope"]},
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_no_hints_is_safe(capsys: pytest.CaptureFixture[str]) -> None:
    print_error_hints("任意消息")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
