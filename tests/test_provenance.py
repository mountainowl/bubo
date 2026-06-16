"""Tests for the pure provenance logic (governance Rec ②a, Phase 1).

Covers trailer parsing, sensitive-path matching, and the banding rules in
:mod:`bubo.provenance`. The DB/provider/poller wiring lives in
``test_governance.py``.
"""

from __future__ import annotations

from bubo.governance_config import DEFAULT_AI_TRAILER_PATTERNS
from bubo.provenance import (
    BAND_COLLABORATIVE,
    BAND_LIKELY_AI,
    BAND_UNKNOWN,
    CONFIDENCE_DECLARED,
    CONFIDENCE_NONE,
    SOURCE_NONE,
    SOURCE_TRAILER,
    compile_patterns,
    compute_provenance,
    match_sensitive_paths,
    parse_ai_trailers,
)

_PATTERNS = compile_patterns(DEFAULT_AI_TRAILER_PATTERNS)


def _provenance(messages, paths=(), globs=()):
    return compute_provenance(
        messages, paths, trailer_patterns=_PATTERNS, sensitive_globs=globs
    )


def test_generated_by_trailer_is_likely_ai() -> None:
    sig = _provenance(["feat: thing\n\nGenerated-by: GPT-4"])
    assert sig.band == BAND_LIKELY_AI
    assert sig.source == SOURCE_TRAILER
    assert sig.confidence == CONFIDENCE_DECLARED
    assert sig.ai_signals == ["Generated-by: GPT-4"]


def test_ai_assisted_marker_is_likely_ai() -> None:
    sig = _provenance(["fix: bug\n\nAI-assisted"])
    assert sig.band == BAND_LIKELY_AI
    assert sig.source == SOURCE_TRAILER


def test_coauthored_by_agent_is_collaborative() -> None:
    # A co-author trailer naming an agent declares human+AI collaboration.
    sig = _provenance(["feat: x\n\nCo-authored-by: Claude <noreply@anthropic.com>"])
    assert sig.band == BAND_COLLABORATIVE
    assert sig.source == SOURCE_TRAILER


def test_coauthored_by_human_is_not_flagged() -> None:
    # A human co-author must not trip the AI signal.
    sig = _provenance(["feat: x\n\nCo-authored-by: Jane Dev <jane@example.com>"])
    assert sig.band == BAND_UNKNOWN
    assert sig.source == SOURCE_NONE
    assert sig.ai_signals == []


def test_generation_plus_coauthor_is_likely_ai() -> None:
    # Any explicit generation trailer dominates the softer co-author band.
    sig = _provenance(
        ["feat: x\n\nGenerated-by: Codex\nCo-authored-by: Claude <a@b.c>"]
    )
    assert sig.band == BAND_LIKELY_AI


def test_no_trailers_is_unknown_never_human() -> None:
    sig = _provenance(["just a normal commit message\n\nwith a body"])
    assert sig.band == BAND_UNKNOWN
    assert sig.source == SOURCE_NONE
    assert sig.confidence == CONFIDENCE_NONE
    assert sig.ai_signals == []
    # The load-bearing honesty rule: absence of a declaration is NOT "human".
    assert sig.band != "human"


def test_prose_mention_is_not_a_declaration() -> None:
    # A body line that merely mentions AI is not a trailer/marker declaration.
    sig = _provenance(["refactor: thing\n\nAI-generated code was reviewed here by a human"])
    assert sig.band == BAND_UNKNOWN
    assert sig.ai_signals == []


def test_standalone_marker_and_marker_with_value_both_flag() -> None:
    assert _provenance(["x\n\nAI-generated"]).band == BAND_LIKELY_AI
    assert _provenance(["x\n\nAI-generated: true"]).band == BAND_LIKELY_AI


def test_parse_ai_trailers_dedups_across_commits() -> None:
    lines = parse_ai_trailers(
        ["a\n\nGenerated-by: GPT-4", "b\n\nGenerated-by: GPT-4"], _PATTERNS
    )
    assert lines == ["Generated-by: GPT-4"]


def test_sensitive_paths_match_nested_and_dedup_sorted() -> None:
    matched = match_sensitive_paths(
        ["payments/charge.py", "src/util.py", "deploy/key.pem", "a/b/key.pem"],
        ["payments/**", "*.pem"],
    )
    assert matched == ["a/b/key.pem", "deploy/key.pem", "payments/charge.py"]


def test_sensitive_paths_no_globs_is_empty() -> None:
    assert match_sensitive_paths(["payments/charge.py"], []) == []


def test_sensitive_paths_recorded_even_when_band_unknown() -> None:
    sig = _provenance(["plain commit"], paths=["payments/charge.py"], globs=["payments/*"])
    assert sig.band == BAND_UNKNOWN
    assert sig.sensitive_paths == ["payments/charge.py"]


def test_compile_patterns_skips_malformed_regex() -> None:
    # A bad operator regex must not crash capture — it is dropped.
    compiled = compile_patterns(["(unterminated", r"^\s*ai-generated\b"])
    assert len(compiled) == 1
    lines = parse_ai_trailers(["x\n\nAI-generated"], compiled)
    assert lines == ["AI-generated"]
