"""Governance / provenance configuration (governance Recs ②/③).

Parses the ``[governance]`` block of ``config/env.toml`` into a typed,
immutable view. Everything is **off by default** — governance capture is
opt-in and, in this first phase, changes no review behavior: it only records
per-change provenance signals for audit.

Kept in its own module (mirroring :mod:`bubo.telemetry.config`) because the
governance surface grows across phases (rigor modulation, policy gates,
reporting). Imports only :mod:`bubo.config_values` to stay free of cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bubo.config_values import bool_value, section, string_list

# Default commit-trailer patterns that DECLARE AI assistance. Case-insensitive
# regexes matched against individual commit-message lines (see
# :func:`bubo.provenance.parse_ai_trailers`). ``Co-authored-by`` counts only
# when the value names a known agent — humans pair-program and co-author too —
# while explicit ``Generated-by`` / ``AI-assisted`` style trailers count on
# their own. Operators override the whole list via ``ai_trailer_patterns``.
DEFAULT_AI_TRAILER_PATTERNS: tuple[str, ...] = (
    r"^\s*co-authored-by\s*:.*\b("
    r"claude|copilot|chatgpt|gpt|openai|codex|gemini|cursor|devin|anthropic|llm"
    r")\b",
    r"^\s*(?:assisted|generated|written)-by\s*:.*\b("
    r"claude|copilot|chatgpt|gpt|openai|codex|gemini|cursor|devin|anthropic|llm|ai"
    r")\b",
    # Standalone marker line — anchored to end-of-line (optionally ``: value``)
    # so a prose body like "AI-generated code was reviewed" is NOT a declaration.
    r"^\s*(?:ai-assisted|ai-generated|generated-by-ai)\b(?:\s*[:=].*)?$",
)


@dataclass(frozen=True, slots=True)
class GovernanceConfig:
    """Immutable view of ``[governance]``. All-off defaults.

    Attributes
    ----------
    capture_provenance:
        Master switch for Phase 1. When ``False`` (default) no commit/diff
        metadata is fetched and no provenance is recorded — zero behavior
        change and zero extra API calls.
    ai_trailer_patterns:
        Regexes (case-insensitive) matched against commit-message lines to
        detect *declared* AI assistance. Defaults to
        :data:`DEFAULT_AI_TRAILER_PATTERNS`.
    sensitive_path_globs:
        ``fnmatch`` globs (case-preserving) flagged on a change's changed
        paths — e.g. ``payments/**``, ``*.pem``. Empty (default) = off; each
        org sets its own. Recorded for audit; no behavior change in Phase 1.
    """

    capture_provenance: bool = False
    ai_trailer_patterns: list[str] = field(
        default_factory=lambda: list(DEFAULT_AI_TRAILER_PATTERNS)
    )
    sensitive_path_globs: list[str] = field(default_factory=list)


def governance_config_from_dict(raw: dict[str, Any]) -> GovernanceConfig:
    """Parse a loaded TOML mapping's ``[governance]`` block into a config."""
    governance = section(raw, "governance")
    patterns = governance.get("ai_trailer_patterns")
    return GovernanceConfig(
        capture_provenance=bool_value(
            governance.get("capture_provenance"),
            "capture_provenance",
            default=False,
        ),
        ai_trailer_patterns=(
            list(DEFAULT_AI_TRAILER_PATTERNS)
            if patterns is None
            else string_list(patterns, "ai_trailer_patterns")
        ),
        sensitive_path_globs=string_list(
            governance.get("sensitive_path_globs", []),
            "sensitive_path_globs",
        ),
    )


__all__ = [
    "DEFAULT_AI_TRAILER_PATTERNS",
    "GovernanceConfig",
    "governance_config_from_dict",
]
