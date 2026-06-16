"""Pure decision core for opt-in finding verification (Gap B).

Before a surviving finding is posted, the poller can run *N independent
"is this real?" checks* and drop findings a majority refute. This module
holds only the **IO-free** half of that: build the per-lens prompt, parse a
verifier's stdout into a structured verdict, and decide survival from a set
of verdicts. The subprocess that actually runs a check lives behind a seam
in :mod:`bubo.poller` (``run_verification``) so this module — and the unit
tests for it — never spawn an agent.

Three deliberately **diverse** lenses ask the verifier to *try to refute*
the finding from different angles:

* ``correctness`` — is the described bug actually a bug in this code?
* ``in_diff`` — does the cited file/line really do what the finding claims?
* ``reproduce`` — can the failure be triggered, or is it speculative?

Each lens demands a strict JSON verdict ``{"real": bool, "confidence":
number in [0, 1], "reason": str}``. The contract is **conservative**: an
unparseable answer, a missing ``real`` flag, or a missing/garbled
``confidence`` resolves to *not real* / zero confidence, so uncertainty
never sneaks a finding past the floor.

Honesty caveat (see ``docs/configuration.md``): when the verifier command
is the *same* model as the reviewer (the default), this is the **weaker,
correlated** form of verification — it shares the original's blind spots.
The strong form is a genuinely different model or a hybrid static+LLM
check. The decision core treats a survived finding as "not refuted", never
as hard-proven "real".
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from bubo.types import JsonObject

# A finding's evidence/body fed to the verifier is truncated — a verdict
# does not need the whole transcript, and this bounds token cost.
_MAX_EXCERPT_CHARS = 4000

# The fixed, diverse lens set. Order is the order checks run in; each maps to
# a one-line angle injected into the prompt.
LENS_INSTRUCTIONS: dict[str, str] = {
    "correctness": (
        "Lens: CORRECTNESS. Decide whether the described problem is a real "
        "defect in the code as written. Read the actual code at the cited "
        "location. If the behavior is correct, intentional, or the finding "
        "misreads the code, it is NOT real."
    ),
    "in_diff": (
        "Lens: IN-DIFF GROUNDING. Decide whether the cited file and line "
        "actually contain what the finding claims. If the line does not "
        "exist, was not changed, or does something other than described, the "
        "finding is NOT real."
    ),
    "reproduce": (
        "Lens: REPRODUCIBILITY. Decide whether the failure the finding "
        "predicts can actually occur. Trace the inputs and control flow that "
        "would trigger it. If it cannot be reached, is guarded against, or is "
        "purely speculative, the finding is NOT real."
    ),
}

# Default lens set, in the order they run. Mirrors review_config's default.
DEFAULT_LENSES: tuple[str, ...] = ("correctness", "in_diff", "reproduce")


@dataclass(frozen=True, slots=True)
class Verdict:
    """One lens's structured answer.

    ``real`` is the verifier's claim that the finding is genuine;
    ``confidence`` is its self-rated certainty in ``[0.0, 1.0]``; ``reason``
    is free text for the audit trail. ``lens`` records which angle produced
    it. ``ok`` distinguishes a verdict that was actually produced from one
    synthesized for a check that *failed to run* (spawn error, timeout,
    non-zero exit) — the caller counts a failed check the way it counts a
    capped finding (post unverified + log), not as a refutation.
    """

    lens: str
    real: bool
    confidence: float
    reason: str
    ok: bool = True


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Outcome of :func:`decide` for one finding.

    ``survives`` is the post/drop decision. ``real_votes`` is the count of
    verdicts that voted real *and* cleared the confidence floor (the votes
    that mattered). ``votes`` is the per-lens tally persisted for audit.
    """

    survives: bool
    real_votes: int
    total: int
    votes: tuple[Verdict, ...]


def _finding_excerpt(finding: JsonObject) -> str:
    """Render the finding's own claim for the verifier prompt.

    Pulls the human-readable fields a verifier needs to judge the claim —
    title, the cited file/line, severity/category, and the model's evidence
    and proposed fix — without widening the call seam to carry the raw diff.
    The verifier inspects the real code via ``cwd=repo`` at the call site.
    """
    file_path = finding.get("file") or finding.get("path") or "?"
    line = finding.get("line") or finding.get("new_line")
    parts = [
        f"title: {finding.get('title') or '(none)'}",
        f"location: {file_path}:{line if line is not None else '?'}",
        f"type: {finding.get('type') or 'issue'}",
        f"severity: {finding.get('severity') or '?'}",
        f"category: {finding.get('category') or '?'}",
    ]
    impact = finding.get("impact")
    evidence = finding.get("evidence")
    body = finding.get("body") or finding.get("comment")
    if impact:
        parts.append(f"impact: {impact}")
    if evidence:
        parts.append(f"evidence: {evidence}")
    if body:
        parts.append(f"details: {str(body).strip()}")
    return "\n".join(str(part) for part in parts)[:_MAX_EXCERPT_CHARS]


def build_verification_prompt(finding: JsonObject, *, lens: str, diff_excerpt: str = "") -> str:
    """Render the verification prompt for one finding under one lens.

    The prompt frames the verifier as an adversary: its job is to *try to
    refute* the finding, defaulting to NOT real when uncertain, and to
    answer with a single strict-JSON verdict. ``diff_excerpt`` is optional
    extra context; when empty the finding's own excerpt (file/line, evidence)
    plus the checked-out code the verifier can read via ``cwd`` carry the
    judgment.

    An unknown ``lens`` falls back to the correctness instruction rather than
    raising — the config layer validates the lens list, so this is only a
    defensive default.
    """
    instruction = LENS_INSTRUCTIONS.get(lens, LENS_INSTRUCTIONS["correctness"])
    excerpt = _finding_excerpt(finding)
    extra = diff_excerpt.strip()[:_MAX_EXCERPT_CHARS]
    context = f"\n\nRELEVANT DIFF:\n{extra}" if extra else ""
    return (
        "You are independently verifying an automated code-review finding. "
        "Your goal is to TRY TO REFUTE it: assume it may be a false positive "
        "and look for the reason it is wrong. Inspect the real code in the "
        "current working directory to check the claim.\n\n"
        f"{instruction}\n\n"
        "FINDING UNDER REVIEW:\n"
        f"{excerpt}"
        f"{context}\n\n"
        "Be conservative: if you cannot confirm the problem is real, treat it "
        "as NOT real. Respond with ONLY a single JSON object, no prose and no "
        "code fence:\n"
        '{"real": true | false, "confidence": <number 0..1>, '
        '"reason": "<one sentence>"}\n\n'
        '- "real": true only when you confirmed the finding describes a '
        "genuine problem in this code.\n"
        '- "confidence": your certainty in the verdict, 0.0 (none) to 1.0 '
        "(certain).\n"
        '- "reason": one short sentence justifying the verdict.'
    )


def parse_verdict(stdout: str, *, lens: str = "") -> Verdict | None:
    """Salvage the last JSON verdict object from a verifier's stdout.

    Agent CLIs interleave reasoning/tool transcript with the answer and the
    real answer comes last, so — mirroring
    :func:`bubo.outcome_classifier.parse_verdict` — every ``{`` is scanned
    and the final object carrying a ``real`` key wins. Returns ``None`` when
    no such object is found, so the caller can treat unparseable output as a
    *failed* check rather than a refutation.

    Conservative coercion: a non-bool ``real`` resolves to ``False``; a
    missing or non-numeric/out-of-range ``confidence`` resolves to ``0.0``
    (so a "real, no confidence" answer cannot clear the floor).
    """
    decoder = json.JSONDecoder()
    found: JsonObject | None = None
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "real" in candidate:
            found = candidate
    if found is None:
        return None
    return Verdict(
        lens=lens,
        real=_coerce_real(found.get("real")),
        confidence=_coerce_confidence(found.get("confidence")),
        reason=str(found.get("reason", "")).strip(),
    )


def _coerce_real(value: object) -> bool:
    """Treat only a literal JSON ``true`` as real — everything else NOT real."""
    return value is True


def _coerce_confidence(value: object) -> float:
    """Parse confidence to ``[0.0, 1.0]``; anything malformed → ``0.0``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    parsed = float(value)
    if parsed < 0.0 or parsed > 1.0:
        return 0.0
    return parsed


def decide(
    verdicts: Sequence[Verdict], *, min_votes: int, confidence_floor: float
) -> VerificationResult:
    """Majority rule over a finding's verdicts.

    A finding **survives** iff at least ``min_votes`` verdicts both voted
    ``real`` and met ``confidence >= confidence_floor`` (inclusive, matching
    ``min_confidence`` semantics). Verdicts whose check *failed to run*
    (``ok`` is ``False``) never count as a real vote — the caller already
    decides those the way it decides a capped finding. Pure and total: an
    empty ``verdicts`` with ``min_votes >= 1`` does not survive.

    Returns the survival decision plus the real-vote count and the full
    per-lens tally for the audit trail.
    """
    real_votes = sum(
        1
        for verdict in verdicts
        if verdict.ok and verdict.real and verdict.confidence >= confidence_floor
    )
    return VerificationResult(
        survives=real_votes >= min_votes,
        real_votes=real_votes,
        total=len(verdicts),
        votes=tuple(verdicts),
    )


def votes_summary(verdicts: Sequence[Verdict]) -> str:
    """Compact JSON tally of the verdicts, for the ``verify_votes`` column."""
    return json.dumps(
        [
            {
                "lens": verdict.lens,
                "real": verdict.real,
                "confidence": round(verdict.confidence, 4),
                "ok": verdict.ok,
                "reason": verdict.reason,
            }
            for verdict in verdicts
        ],
        separators=(",", ":"),
    )


__all__ = [
    "DEFAULT_LENSES",
    "LENS_INSTRUCTIONS",
    "Verdict",
    "VerificationResult",
    "build_verification_prompt",
    "decide",
    "parse_verdict",
    "votes_summary",
]
