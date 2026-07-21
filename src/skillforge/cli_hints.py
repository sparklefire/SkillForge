"""Shared helper for printing actionable stderr hints on CLI error paths.

Every interactive checker keeps stdout as pure machine-readable JSON; human
guidance (what to run next, what to fill in) goes to stderr so it never
contaminates piped JSON output.
"""

from __future__ import annotations

import sys


def print_error_hints(
    message: str,
    exact_hints: dict[str, list[str]] | None = None,
    prefix_hints: dict[str, list[str]] | None = None,
) -> None:
    """Print actionable hints for ``message`` to stderr.

    Exact matches win; otherwise the first matching prefix is used. If nothing
    matches, print nothing. Hints must never contain private values, paths to
    sensitive material, or confirmation content — only reusable next-step text.
    """
    lines: list[str] | None = None
    if exact_hints:
        lines = exact_hints.get(message)
    if lines is None and prefix_hints:
        for prefix, candidate in prefix_hints.items():
            if message.startswith(prefix):
                lines = candidate
                break
    if lines:
        for line in lines:
            print(line, file=sys.stderr)
