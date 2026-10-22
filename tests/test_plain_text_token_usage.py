"""Token counters read from a custom reviewer command's plain-text output.

A custom command prints the review first and its usage summary last, and
cached tokens are part of input tokens. Drives the real ``parse_agent_output``
fallback that review runs use for any non-Codex, non-Claude command.
"""

from bubo.telemetry.agent_output import parse_agent_output
from bubo.telemetry.cost import TokenUsage


def test_review_prose_before_the_summary_does_not_win() -> None:
    stdout = (
        "The docs say input tokens: 5 are allowed per call, output tokens: 1.\n"
        "[]\n"
        "input tokens: 10,000\n"
        "output tokens: 2,000\n"
    )
    assert parse_agent_output(stdout).usage == TokenUsage(input=10_000, output=2_000, total=12_000)


def test_cached_input_is_not_counted_twice_in_the_total() -> None:
    stdout = "input tokens: 10,000\ncached input tokens: 8,000\noutput tokens: 500\n"
    assert parse_agent_output(stdout).usage == TokenUsage(
        input=10_000, output=500, cached=8_000, total=10_500
    )


def test_a_cached_input_line_is_not_read_as_input() -> None:
    stdout = "input tokens: 10,000\noutput tokens: 500\ncached input tokens: 8,000\n"
    assert parse_agent_output(stdout).usage.input == 10_000


def test_a_reported_total_is_kept() -> None:
    stdout = (
        "input tokens: 12,000\ncached tokens: 2,000\noutput tokens: 1,500\ntotal tokens: 13,500\n"
    )
    assert parse_agent_output(stdout).usage.total == 13_500
