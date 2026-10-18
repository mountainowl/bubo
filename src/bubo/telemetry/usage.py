"""Tally the tokens and cost of every agent CLI call one review makes.

A review is more than its main agent run: finding verification, fixed-finding
reconciliation and developer-reply classification each start the agent CLI
again. :func:`record_agent_usage` prices one call and adds it to the meter
that the caller opened with :func:`start_metering`, so a review's recorded cost
covers all of them.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field

from bubo.events import log
from bubo.telemetry.agent_output import AgentOutput
from bubo.telemetry.config import TelemetryConfig
from bubo.telemetry.cost import TokenUsage, add_usage, estimate_cost_usd
from bubo.telemetry.pricing import model_pricing


@dataclass(slots=True)
class UsageMeter:
    tokens: TokenUsage = field(default_factory=TokenUsage)
    cost_usd: float = 0.0

    def add(self, tokens: TokenUsage, cost_usd: float) -> None:
        self.tokens = add_usage(self.tokens, tokens)
        self.cost_usd = round(self.cost_usd + cost_usd, 8)


_METER: ContextVar[UsageMeter | None] = ContextVar("bubo_usage_meter", default=None)


def start_metering() -> tuple[UsageMeter, Token[UsageMeter | None]]:
    meter = UsageMeter()
    return meter, _METER.set(meter)


def stop_metering(token: Token[UsageMeter | None]) -> None:
    _METER.reset(token)


def record_agent_usage(
    operation: str, output: AgentOutput, *, model: str | None, cfg: TelemetryConfig
) -> tuple[TokenUsage, float]:
    """Price one agent call, add it to the open meter, and return its usage and cost."""
    if output.cost_usd is not None:
        cost = output.cost_usd
    else:
        cost = estimate_cost_usd(output.usage, model_pricing(model, cfg))
    log(
        "agent_usage",
        operation=operation,
        model=model,
        tokens_input=output.usage.input,
        tokens_output=output.usage.output,
        tokens_cached=output.usage.cached,
        cost_usd=cost,
    )
    meter = _METER.get()
    if meter is not None:
        meter.add(output.usage, cost)
    return output.usage, cost
