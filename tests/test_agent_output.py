from __future__ import annotations

import json

from bubo.telemetry import parse_agent_output, structured_output_command
from bubo.telemetry.cost import TokenUsage

# Real `codex exec --json` output (Codex 0.163.0), stderr interleaved as in run_bounded.
CODEX_JSONL = "\n".join(
    [
        "Reading additional input from stdin...",
        '{"type":"thread.started","thread_id":"01a14d62"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
        '"text":"I will check."}}',
        '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"[]"}}',
        '{"type":"turn.completed","usage":{"input_tokens":71154,"cached_input_tokens":49280,'
        '"cache_write_input_tokens":0,"output_tokens":220,"reasoning_output_tokens":0}}',
    ]
)

# Shape of real `claude -p --output-format json` output.
CLAUDE_JSON = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "[]",
        "total_cost_usd": 0.0492628,
        "usage": {
            "input_tokens": 2,
            "cache_creation_input_tokens": 5876,
            "cache_read_input_tokens": 10834,
            "output_tokens": 4,
        },
    }
)


def test_codex_exec_gets_json_flag_after_exec() -> None:
    cmd = ["codex", "--ask-for-approval", "never", "exec", "--profile", "bubo"]
    assert structured_output_command(cmd) == [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--json",
        "--profile",
        "bubo",
    ]


def test_claude_print_gets_json_output_format() -> None:
    assert structured_output_command(["/usr/bin/claude", "-p"]) == [
        "/usr/bin/claude",
        "-p",
        "--output-format",
        "json",
    ]


def test_commands_already_structured_or_unknown_are_left_alone() -> None:
    for cmd in (
        ["codex", "exec", "--json"],
        ["claude", "-p", "--output-format", "stream-json"],
        ["claude", "-p", "--output-format=text"],
        ["my-wrapper", "exec", "-p"],
        [],
    ):
        assert structured_output_command(cmd) == cmd


def test_codex_jsonl_gives_last_message_and_split_usage() -> None:
    out = parse_agent_output(CODEX_JSONL)
    assert out.review == "[]"
    assert out.usage == TokenUsage(input=71154, output=220, cached=49280, total=71374)
    assert out.cost_usd is None


def test_claude_json_gives_result_usage_and_reported_cost() -> None:
    out = parse_agent_output("some stderr warning\n" + CLAUDE_JSON)
    assert out.review == "[]"
    assert out.usage == TokenUsage(input=16712, output=4, cached=10834, total=16716)
    assert out.cost_usd == 0.0492628


def test_plain_text_output_falls_back_to_text_counters() -> None:
    text = 'Findings: [{"a": 1}]\ntokens used\n22,094\n'
    out = parse_agent_output(text)
    assert out.review == text
    assert out.usage.total == 22094
    assert out.cost_usd is None


def test_codex_json_quota_error_still_trips_the_subscription_circuit() -> None:
    from bubo.subscription import is_subscription_failure

    # Real `codex exec --json` output (0.163.0) when the API answers 429 insufficient_quota.
    out = "\n".join(
        [
            '{"type":"thread.started","thread_id":"01a14d70"}',
            '{"type":"turn.started"}',
            '{"type":"error","message":"Quota exceeded. Check your plan and billing details."}',
            '{"type":"turn.failed","error":{"message":"Quota exceeded. Check your plan and '
            'billing details."}}',
        ]
    )
    assert is_subscription_failure(out, terminal_only=True)
