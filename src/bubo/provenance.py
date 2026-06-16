"""Pure AI-code-provenance logic (governance Rec ②a, Phase 1).

IO-free, mirroring :mod:`bubo.findings`: the poller fetches commit messages
and the changed-path set, the DB layer persists the result, and everything
here is a deterministic function — so it is unit-testable in isolation.

Design (see ``docs/configuration.md`` → "Governance & provenance"):

* **Band, never a verdict.** :attr:`ProvenanceSignal.band` is one of
  ``unknown`` / ``likely_ai`` / ``collaborative``. ``human`` is intentionally
  *not* emitted from the absence of a signal — "no AI declaration" is not
  proof of human authorship — so the default is ``unknown``. (``human`` is
  reserved for a future positive signal.)
* **Declared ≠ detected.** :attr:`ProvenanceSignal.source` (``trailer`` /
  ``detection`` / ``both`` / ``none``) is kept distinct from the band. This
  phase only ever produces ``trailer`` or ``none``: a commit trailer is a
  *declaration* of AI assistance, not proof. Post-hoc LLM detection is
  deliberately deferred to a later phase, and would set ``source=detection``.
* The band is read off the *kind* of declaration, honestly: a
  ``Co-authored-by: <agent>`` trailer declares collaboration; an explicit
  ``Generated-by`` / ``AI-generated`` trailer declares generation.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch

# Bands (see module docstring).
BAND_UNKNOWN = "unknown"
BAND_LIKELY_AI = "likely_ai"
BAND_COLLABORATIVE = "collaborative"

# Sources — where the signal came from (declared vs detected).
SOURCE_NONE = "none"
SOURCE_TRAILER = "trailer"

# Coarse confidence labels (a band, not a number — see the brief).
CONFIDENCE_NONE = "none"
CONFIDENCE_DECLARED = "declared"

# A ``Co-authored-by:`` trailer declares co-authorship; when the co-author is
# an AI agent that is *collaboration*, vs an explicit generation trailer.
_COAUTHOR_TRAILER = re.compile(r"^\s*co-authored-by\s*:", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ProvenanceSignal:
    """A change's provenance as a banded, audit-friendly signal."""

    band: str = BAND_UNKNOWN
    source: str = SOURCE_NONE
    confidence: str = CONFIDENCE_NONE
    ai_signals: list[str] = field(default_factory=list)
    sensitive_paths: list[str] = field(default_factory=list)


def compile_patterns(patterns: Iterable[str]) -> list[re.Pattern[str]]:
    """Compile trailer patterns case-insensitively, skipping malformed ones.

    A bad operator-supplied regex must not crash provenance capture, so an
    un-compilable pattern is silently dropped rather than raised.
    """
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error:
            continue
    return compiled


def parse_ai_trailers(
    commit_messages: Iterable[str], patterns: list[re.Pattern[str]]
) -> list[str]:
    """Return the commit-message lines that DECLARE AI assistance.

    Each message is scanned line-by-line (a trailer may sit anywhere in the
    body); a line matching any pattern is returned verbatim (stripped) for the
    audit trail, de-duplicated with original order preserved.
    """
    hits: list[str] = []
    seen: set[str] = set()
    for message in commit_messages:
        for raw_line in message.splitlines():
            line = raw_line.strip()
            if not line or line in seen:
                continue
            if any(pattern.search(line) for pattern in patterns):
                seen.add(line)
                hits.append(line)
    return hits


def match_sensitive_paths(paths: Iterable[str], globs: Iterable[str]) -> list[str]:
    """Return changed paths matching any sensitive glob (``fnmatch`` semantics).

    ``fnmatch``'s ``*`` spans ``/``, so ``payments/*`` and ``*.pem`` both match
    nested files; a basename fallback also lets a bare ``*.pem`` match deep
    paths. Result is de-duplicated and sorted for a stable audit record.
    """
    patterns = [glob for glob in globs if glob]
    if not patterns:
        return []
    matched = {
        path
        for path in paths
        if path and any(_glob_match(path, glob) for glob in patterns)
    }
    return sorted(matched)


def _glob_match(path: str, glob: str) -> bool:
    # fnmatch has no `**` semantics (``*`` already spans ``/``), so a leading
    # ``**/`` would otherwise fail to match at depth 0 — ``**/auth/**`` would
    # miss a top-level ``auth/x``. Strip a leading ``**/`` and retry so the
    # idiomatic "anywhere" glob also matches the zero-prefix case.
    if fnmatch(path, glob) or fnmatch(path.rsplit("/", 1)[-1], glob):
        return True
    return glob.startswith("**/") and fnmatch(path, glob[3:])


def compute_provenance(
    commit_messages: Iterable[str],
    changed_paths: Iterable[str],
    *,
    trailer_patterns: list[re.Pattern[str]],
    sensitive_globs: Iterable[str],
) -> ProvenanceSignal:
    """Combine trailer declarations + sensitive-path matches into one signal.

    No AI declaration → ``unknown`` / ``source=none`` (never ``human``).
    Otherwise ``source=trailer`` and the band follows the declaration kind:
    only co-authored-by trailers → ``collaborative``; any explicit
    generation/assistance trailer → ``likely_ai``. Sensitive-path matches are
    recorded regardless of band.
    """
    messages = list(commit_messages)
    ai_lines = parse_ai_trailers(messages, trailer_patterns)
    sensitive = match_sensitive_paths(changed_paths, sensitive_globs)
    if not ai_lines:
        return ProvenanceSignal(
            band=BAND_UNKNOWN,
            source=SOURCE_NONE,
            confidence=CONFIDENCE_NONE,
            sensitive_paths=sensitive,
        )
    only_coauthors = all(_COAUTHOR_TRAILER.match(line) for line in ai_lines)
    return ProvenanceSignal(
        band=BAND_COLLABORATIVE if only_coauthors else BAND_LIKELY_AI,
        source=SOURCE_TRAILER,
        confidence=CONFIDENCE_DECLARED,
        ai_signals=ai_lines,
        sensitive_paths=sensitive,
    )


__all__ = [
    "BAND_COLLABORATIVE",
    "BAND_LIKELY_AI",
    "BAND_UNKNOWN",
    "CONFIDENCE_DECLARED",
    "CONFIDENCE_NONE",
    "SOURCE_NONE",
    "SOURCE_TRAILER",
    "ProvenanceSignal",
    "compile_patterns",
    "compute_provenance",
    "match_sensitive_paths",
    "parse_ai_trailers",
]
