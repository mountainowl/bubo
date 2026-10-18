"""Read review text, token usage and reported cost from an agent CLI's output.

Bubo asks the agent CLIs for machine-readable output so usage is exact:

* ``codex exec --json`` prints JSONL events. The review is the last
  ``agent_message`` item; usage is on ``turn.completed`` (no cost, no model).
* ``claude -p --output-format json`` prints one result object with the review
  in ``result``, token ``usage`` and the run's own ``total_cost_usd``.

Any other output (a custom reviewer command) falls back to the plain-text
token counters in :func:`bubo.telemetry.cost.parse_codex_token_usage`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bubo.telemetry.cost import TokenUsage, parse_codex_token_usage


@dataclass(frozen=True, slots=True)
class AgentOutput:
    review: str
    usage: TokenUsage
    # What the agent CLI itself says the run cost; None when it does not say.
    cost_usd: float | None = None


def structured_output_command(command: list[str]) -> list[str]:
    """Add the flag that makes a known agent CLI print machine-readable output."""
    if not command:
        return list(command)
    agent = Path(command[0]).stem.lower()
    if agent == "codex" and "exec" in command and "--json" not in command:
        at = command.index("exec") + 1
        return [*command[:at], "--json", *command[at:]]
    if (
        agent == "claude"
        and ("-p" in command or "--print" in command)
        and not any(arg.startswith("--output-format") for arg in command)
    ):
        return [*command, "--output-format", "json"]
    return list(command)


def parse_agent_output(stdout: str) -> AgentOutput:
    events = _json_objects(stdout)
    claude = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if claude is not None:
        return _claude_output(claude)
    if any(e.get("type") == "turn.completed" for e in events):
        return _codex_output(events)
    return AgentOutput(review=stdout, usage=parse_codex_token_usage(stdout))


def _claude_output(result: dict[str, Any]) -> AgentOutput:
    usage = result.get("usage") or {}
    fresh = _int(usage.get("input_tokens"))
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens"))
    output = _int(usage.get("output_tokens"))
    input_tokens = fresh + cache_read + cache_write
    cost = result.get("total_cost_usd")
    return AgentOutput(
        review=str(result.get("result") or ""),
        usage=TokenUsage(
            input=input_tokens,
            output=output,
            cached=cache_read,
            total=input_tokens + output,
        ),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
    )


def _codex_output(events: list[dict[str, Any]]) -> AgentOutput:
    review = ""
    input_tokens = cached = output = 0
    for event in events:
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
        ):
            review = str(item.get("text") or "")
        if event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            # input_tokens already includes the cached part, as in the OpenAI API.
            input_tokens += _int(usage.get("input_tokens"))
            cached += _int(usage.get("cached_input_tokens"))
            output += _int(usage.get("output_tokens"))
    return AgentOutput(
        review=review,
        usage=TokenUsage(
            input=input_tokens, output=output, cached=cached, total=input_tokens + output
        ),
    )


def _json_objects(text: str) -> list[dict[str, Any]]:
    """Every line that is a JSON object; the CLIs' stderr lands in between."""
    objects = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            objects.append(value)
    return objects


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
