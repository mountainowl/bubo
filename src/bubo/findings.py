"""Finding extraction, filtering, and diff-position mapping.

This module is the bridge between the raw LLM output (a string the reviewer
subprocess wrote to stdout) and structured per-line review threads that the
poster path can publish to GitLab.

Responsibilities:

* :func:`extract_findings` — robustly parse the reviewer's JSON output,
  including markdown-fenced and noisy-prose variants.
* :func:`normalize_category` / :func:`normalize_finding_categories` — map the
  reviewer's free-form ``category`` string onto a fixed canonical taxonomy
  (defect vs non-defect), stored in a *separate* ``category_canonical`` field
  so the operator's ``mode``/kind policies can match deterministically while
  the original label is preserved verbatim in the body, fingerprint, and audit.
* :func:`filter_findings_by_policy` — apply the operator's confidence,
  kind whitelist, surface-mode, and dispute-suppression policies from
  ``config/env.toml``.
* :func:`changed_lines_from_diffs` and :func:`build_position` — figure out
  whether a finding's ``file``/``line`` actually corresponds to an added
  line in the MR diff (only added lines can carry an inline GitLab comment).
* :func:`finding_body` — render a finding into the canonical, mood-neutral
  Issue/Impact/Evidence/Fix/Confidence comment shape. This is what feeds the
  fingerprint and the recorded body, regardless of review tone.
* :func:`finding_comment_body` — the body actually POSTED, which honors the
  operator's ``[review].tone``: for a non-default tone it prefers the reviewer's
  in-voice ``comment`` field, otherwise it falls back to :func:`finding_body`.
  Kept separate from :func:`finding_body` so the *voice* never leaks into the
  fingerprint or the audit dataset.
* :func:`finding_fingerprint` — stable hash for idempotent posting; computed
  from :func:`finding_body` (the structured render), so it is **tone-invariant**
  — switching tone never re-posts a finding or splits its outcome history.

Everything in this module is pure (no IO, no globals) so it is easily
testable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping

from bubo.config_values import positive_int
from bubo.errors import describe
from bubo.hash_utils import stable_hash
from bubo.types import JsonObject

# Regex constants — compiled at module load so ``extract_findings`` and
# ``changed_lines_from_diffs`` do not pay the compile cost on every call.
_FENCE_START = re.compile(r"^```(?:json)?\s*")
_FENCE_END = re.compile(r"\s*```$")
_JSON_ARRAY_START = re.compile(r"\[")
_CODEX_ASSISTANT_MARKER = re.compile(r"(?m)^codex\s*$")
_HUNK_HEADER = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# Fields of a finding that participate in the kind whitelist match. The order
# does not matter — any one of these fields matching the allowlist keeps the
# finding. Defined as a module constant so the documentation in env.toml and
# the runtime behavior stay in sync.
_KIND_FIELDS = ("severity", "category", "type")

# ---------------------------------------------------------------------------
# Canonical category taxonomy
# ---------------------------------------------------------------------------
# The reviewer emits a *free-form* ``category`` string, and different models
# (and the same model across runs) use wildly inconsistent labels for the same
# thing — ``logic`` vs ``code-logic``, ``test`` vs ``testing`` vs
# ``test-coverage``, ``documentation`` vs ``docs``. A raw whitelist cannot match
# that reliably, so the surface-mode policy below first maps each label onto a
# fixed, orthogonal taxonomy (the classic Orthogonal-Defect-Classification
# idea: a small set of mutually-independent buckets, applied consistently).
#
# The split is defect vs non-defect. The defect set is what a high-precision
# merge ``gate`` surfaces; the non-defect set (style/docs/test nits) is the
# bulk of the observed low-value noise and is dropped by the gate but kept by
# the default ``collaborate`` mode.
DEFECT_CATEGORIES = frozenset(
    {"correctness", "security", "concurrency", "resource", "error_handling", "performance"}
)
NON_DEFECT_CATEGORIES = frozenset({"style", "docs", "test", "design", "naming", "other"})
CANONICAL_CATEGORIES = DEFECT_CATEGORIES | NON_DEFECT_CATEGORIES

# The bucket an unrecognized label falls into. Deliberately a *non-defect* bucket
# so an unknown category is never silently promoted into a merge gate.
UNKNOWN_CATEGORY = "other"

# Synonym table: ``stripped/lowercased/separator-normalized label -> canonical``.
# Keys use hyphen as the separator; :func:`normalize_category` folds spaces and
# underscores to hyphens before the lookup, so ``"Error Handling"``,
# ``"error_handling"`` and ``"error-handling"`` all resolve identically. Covers
# the labels observed empirically plus the contract's own enum
# (``prompts/00-meta.md``: ``failure``, ``compatibility``, ``maintainability``,
# ``documentation`` …). Unlisted labels fall through to ``other``.
_CATEGORY_SYNONYMS: dict[str, str] = {
    # --- correctness (incl. the contract's ``compatibility``: a broken API or
    #     data contract is a correctness defect, so it belongs in the gate) ---
    "correctness": "correctness",
    "correct": "correctness",
    "logic": "correctness",
    "code-logic": "correctness",
    "logic-error": "correctness",
    "functional": "correctness",
    "functionality": "correctness",
    "bug": "correctness",
    "defect": "correctness",
    "behavior": "correctness",
    "behaviour": "correctness",
    "data-integrity": "correctness",
    "data": "correctness",
    "compatibility": "correctness",
    "compat": "correctness",
    "regression": "correctness",
    # --- security ---
    "security": "security",
    "vulnerability": "security",
    "vuln": "security",
    "auth": "security",
    "authentication": "security",
    "authorization": "security",
    "authz": "security",
    "injection": "security",
    "crypto": "security",
    # --- concurrency ---
    "concurrency": "concurrency",
    "concurrent": "concurrency",
    "race": "concurrency",
    "race-condition": "concurrency",
    "data-race": "concurrency",
    "threading": "concurrency",
    "thread-safety": "concurrency",
    "async": "concurrency",
    "deadlock": "concurrency",
    "synchronization": "concurrency",
    # --- resource (memory / handles / lifecycle) ---
    "resource": "resource",
    "resources": "resource",
    "resource-management": "resource",
    "resource-leak": "resource",
    "memory": "resource",
    "memory-safety": "resource",
    "memory-leak": "resource",
    "leak": "resource",
    "lifecycle": "resource",
    # --- error handling (incl. the contract's ``failure``) ---
    "error-handling": "error_handling",
    "error": "error_handling",
    "errors": "error_handling",
    "failure": "error_handling",
    "exception": "error_handling",
    "exception-handling": "error_handling",
    "robustness": "error_handling",
    "reliability": "error_handling",
    "missing-check": "error_handling",
    "validation": "error_handling",
    "edge-case": "error_handling",
    "null-safety": "error_handling",
    # --- performance ---
    "performance": "performance",
    "perf": "performance",
    "efficiency": "performance",
    "optimization": "performance",
    "scalability": "performance",
    # --- style / formatting / readability (non-defect) ---
    "style": "style",
    "code-style": "style",
    "formatting": "style",
    "format": "style",
    "lint": "style",
    "linting": "style",
    "clarity": "style",
    "readability": "style",
    "consistency": "style",
    "convention": "style",
    "conventions": "style",
    # --- docs (incl. the contract's ``documentation``) ---
    "docs": "docs",
    "doc": "docs",
    "documentation": "docs",
    "comment": "docs",
    "comments": "docs",
    "javadoc": "docs",
    "docstring": "docs",
    # --- test ---
    "test": "test",
    "tests": "test",
    "testing": "test",
    "test-coverage": "test",
    "coverage": "test",
    "testability": "test",
    # --- design / maintainability (incl. the contract's ``maintainability``) ---
    "design": "design",
    "maintainability": "design",
    "architecture": "design",
    "refactor": "design",
    "refactoring": "design",
    "code-quality": "design",
    "quality": "design",
    "complexity": "design",
    "cleanup": "design",
    "structure": "design",
    "abstraction": "design",
    # --- naming ---
    "naming": "naming",
    "name": "naming",
    "names": "naming",
    "nomenclature": "naming",
    # --- explicit ``other`` (non-defect catch-alls seen in the wild) ---
    "other": "other",
    "misc": "other",
    "miscellaneous": "other",
    "general": "other",
    "unknown": "other",
    "usability": "other",
    "ux": "other",
    "ui": "other",
    "ci": "other",
    "ci-cd": "other",
    "build": "other",
    "config": "other",
    "configuration": "other",
    "observability": "other",
    "logging": "other",
    "monitoring": "other",
    "process": "other",
    "dependency": "other",
    "dependencies": "other",
    "i18n": "other",
}

# Severities the ``gate`` preset treats as merge-blocking. The contract asks for
# ``blocking``/``non-blocking`` (``prompts/00-meta.md``), but real models also
# emit ``high``/``critical``; an inclusion set (rather than a literal
# ``== "blocking"`` test) keeps those severe defects in the gate instead of
# silently dropping them. A finding with no/other severity is NOT gated through
# — a merge gate should only block on an explicit high-severity signal.
_GATE_SEVERITIES = frozenset({"blocking", "blocker", "critical", "high"})

# Finding types the ``gate`` preset excludes: the deliberately collaborative
# output modes. ``gate`` is the merge-blocking lane, where a question or
# suggestion cannot be a blocker; ``collaborate`` (the default) keeps them.
# Matched as an *exclusion* set so any assertion-style type (``issue``,
# ``finding``, ``bug`` …) — and a finding with no type — still surfaces.
_NON_ASSERTION_TYPES = frozenset({"suggestion", "question"})


def normalize_category(value: object) -> str:
    """Map a free-form ``category`` label onto the canonical taxonomy.

    Pure and total: every input returns exactly one member of
    :data:`CANONICAL_CATEGORIES`. The label is stripped, lowercased, and has
    its spaces/underscores folded to hyphens before lookup, so ``"Error
    Handling"``, ``"error_handling"`` and ``"error-handling"`` all map to
    ``"error_handling"``. Anything not in :data:`_CATEGORY_SYNONYMS` — including
    ``None``, a non-string, or an empty string — maps to
    :data:`UNKNOWN_CATEGORY` (``"other"``), never raising.
    """
    if not isinstance(value, str):
        return UNKNOWN_CATEGORY
    key = value.strip().lower().replace(" ", "-").replace("_", "-")
    if not key:
        return UNKNOWN_CATEGORY
    return _CATEGORY_SYNONYMS.get(key, UNKNOWN_CATEGORY)


def normalize_finding_categories(findings: Iterable[JsonObject]) -> list[JsonObject]:
    """Annotate each finding with a canonical ``category_canonical`` field.

    Mutates each finding in place (adds one key) and returns the list so the
    caller can chain. The reviewer's original free-form ``category`` is left
    untouched: :func:`finding_body`, :func:`finding_fingerprint`, and the
    recorded audit row all read ``category`` (not the canonical field), so
    normalization never re-renders a comment, shifts a fingerprint, or rewrites
    the operator's audit history. The canonical field exists purely so the
    surface-mode/kind policy can match deterministically.
    """
    annotated = list(findings)
    for finding in annotated:
        finding["category_canonical"] = normalize_category(finding.get("category"))
    return annotated


def finding_canonical_category(finding: JsonObject) -> str:
    """Return a finding's canonical category, preferring a pre-annotated value.

    Falls back to normalizing ``category`` on the fly when
    :func:`normalize_finding_categories` has not run, so policy checks are
    correct whether or not the finding was annotated first.
    """
    annotated = finding.get("category_canonical")
    if isinstance(annotated, str) and annotated:
        return annotated
    return normalize_category(finding.get("category"))


def gate_surfaces(finding: JsonObject) -> bool:
    """Surface predicate for the ``gate`` preset: keep only merge-blocking defects.

    Conjunctive — a finding survives the gate only when **all** hold:

    1. its ``type`` is not a collaborative mode (not ``suggestion``/``question``;
       a missing type counts as an assertion and passes);
    2. its ``severity`` is an explicit merge-blocking tier
       (:data:`_GATE_SEVERITIES`); and
    3. its canonical category is a defect (:data:`DEFECT_CATEGORIES`).

    This is the high-precision, model-agnostic cut: on the empirical gpt-4o run
    it drops the suggestion/question modes and the docs/style/CI-nit categories
    while keeping blocking correctness/security/etc. defects. Pure; safe to call
    on a finding that has not been through :func:`normalize_finding_categories`.
    """
    finding_type = finding.get("type")
    if isinstance(finding_type, str) and finding_type.strip().lower() in _NON_ASSERTION_TYPES:
        return False
    severity = finding.get("severity")
    if not isinstance(severity, str) or severity.strip().lower() not in _GATE_SEVERITIES:
        return False
    return finding_canonical_category(finding) in DEFECT_CATEGORIES


def surface_predicate_for_mode(mode: str) -> Callable[[JsonObject], bool] | None:
    """Resolve a ``[review].mode`` preset to a surface predicate for the filter.

    Returns :func:`gate_surfaces` for ``"gate"`` (the high-precision merge lane)
    and ``None`` for ``"collaborate"`` (the default) or any other value —
    ``None`` means "no surface filter", which is byte-for-byte the pre-existing
    behavior. Kept tiny and string-keyed so :mod:`bubo.review_config` owns no
    review behavior.
    """
    if mode.strip().lower() == "gate":
        return gate_surfaces
    return None


def dispute_stats_by_canonical(raw_stats: Iterable[JsonObject]) -> list[JsonObject]:
    """Fold raw per-category dispute stats into the canonical taxonomy.

    The dispute reader (:func:`bubo.db.disputed_class_stats`) keys on the
    reviewer's **raw** category — deliberately, so dispute-driven *suppression*
    learns the exact labels a team rejects. But *calibration* must aggregate on
    the **canonical** category: otherwise the same 33-variant fragmentation this
    module exists to fix (``test`` vs ``testing`` vs ``test-coverage``) splits
    one category's dispute signal across several thin buckets and no floor ever
    accrues enough samples. This pure fold sums ``total``/``rejected`` across all
    raw labels that normalize to the same canonical category.

    Each input row is a mapping with ``category`` (raw), ``total``, and
    ``rejected``. Returns ``{category, total, rejected, dispute_rate}`` dicts
    keyed by canonical category, sorted by category for deterministic output.
    """
    agg: dict[str, list[int]] = {}
    for row in raw_stats:
        canon = normalize_category(row.get("category"))
        total = int(row.get("total", 0) or 0)
        rejected = int(row.get("rejected", 0) or 0)
        bucket = agg.setdefault(canon, [0, 0])
        bucket[0] += total
        bucket[1] += rejected
    return [
        {
            "category": canon,
            "total": total,
            "rejected": rejected,
            "dispute_rate": (rejected / total) if total else 0.0,
        }
        for canon, (total, rejected) in sorted(agg.items())
    ]


def calibrated_category_floors(
    canonical_stats: Iterable[JsonObject],
    *,
    base: float,
    max_floor: float,
    min_samples: int,
) -> dict[str, float]:
    """Derive per-canonical-category confidence floors from dispute history.

    The data-driven populator for the ``category_floors`` mechanism: a category
    the team disputes more earns a higher confidence bar, so noisy classes must
    be *more* certain to surface. Linear in the dispute rate::

        floor = base + dispute_rate * (max_floor - base)

    so ``dispute_rate == 0`` leaves the floor at ``base`` (no change) and
    ``dispute_rate == 1`` reaches ``max_floor``. Only categories with at least
    ``min_samples`` resolved outcomes are calibrated (a thin signal is not
    trusted), and only floors strictly above ``base`` are returned — a category
    at the global floor needs no entry.

    Deliberately **gentler than suppression**: calibration only *raises the
    confidence bar*, it never zeroes a category out, so a genuinely
    high-confidence finding in a disputed class still surfaces. Like
    suppression it is self-reinforcing (raising a floor reduces that class's
    new outcomes, freezing its rate); the escape hatch is operator-side —
    lower ``max_floor`` / raise ``min_samples`` / disable calibration. Pure.
    """
    floors: dict[str, float] = {}
    for row in canonical_stats:
        total = int(row.get("total", 0) or 0)
        if total < min_samples:
            continue
        rejected = int(row.get("rejected", 0) or 0)
        rate = (rejected / total) if total else 0.0
        floor = base + rate * (max_floor - base)
        floor = min(max(floor, base), max_floor)
        if floor > base:
            category = row.get("category")
            if isinstance(category, str) and category:
                floors[category] = round(floor, 4)
    return floors


def extract_findings(raw: str, max_findings: int | None = None) -> list[JsonObject]:
    """Parse the reviewer subprocess's stdout into a list of finding objects.

    Handles three shapes the agent CLIs are known to emit:

    1. A bare JSON array of finding objects.
    2. A JSON object with a ``"findings"`` key holding the array.
    3. The above wrapped in a ```` ```json ... ``` ```` markdown fence.

    If ``json.loads`` of the cleaned text fails, the function falls back to
    scanning for every ``[`` in the string and trying ``raw_decode`` from
    each position. Among the parses that succeed, **the first non-empty
    candidate wins** — picking the first non-empty array prefers the
    primary findings list over trailing example arrays the model sometimes
    appends.

    Parameters
    ----------
    raw:
        Raw subprocess stdout.
    max_findings:
        Optional hard cap; the returned list is truncated to this length.

    Raises
    ------
    ValueError
        If the text does not contain a recognizable JSON array of
        finding objects.
    """
    text = raw.strip()
    if not text or text == "No actionable findings.":
        return []
    if text.startswith("```"):
        text = _FENCE_START.sub("", text)
        text = _FENCE_END.sub("", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        candidates = []
        for match in _JSON_ARRAY_START.finditer(text):
            try:
                candidate, _ = decoder.raw_decode(text[match.start() :])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, list) and all(isinstance(item, dict) for item in candidate):
                candidates.append((match.start(), candidate))
        if not candidates:
            raise ValueError(
                describe(
                    "review output is not JSON findings",
                    reason=(
                        "the reviewer did not return the structured findings JSON the "
                        "contract requires"
                    ),
                    fix=(
                        "check the review prompt/model and the run transcript; the agent may "
                        "have errored or returned prose instead of the findings array."
                    ),
                )
            ) from None
        marker = _last_codex_assistant_marker_end(text)
        if marker is not None:
            final_candidates = [candidate for start, candidate in candidates if start >= marker]
            if final_candidates:
                data = next(
                    (candidate for candidate in final_candidates if candidate),
                    final_candidates[0],
                )
            else:
                data = next(
                    (candidate for _, candidate in candidates if candidate),
                    candidates[0][1],
                )
        else:
            data = next(
                (candidate for _, candidate in candidates if candidate),
                candidates[0][1],
            )
    if isinstance(data, dict):
        data = data.get("findings", [])
    if not isinstance(data, list):
        raise ValueError(
            describe(
                "review JSON must be an array or an object with findings",
                reason=(
                    "the reviewer did not return the structured findings JSON the contract "
                    "requires"
                ),
                fix=(
                    "check the review prompt/model and the run transcript; the agent may "
                    "have errored or returned prose instead of the findings array."
                ),
            )
        )
    findings = [item for item in data if isinstance(item, dict)]
    if max_findings is not None:
        return findings[: positive_int(max_findings, "max_findings")]
    return findings


def _last_codex_assistant_marker_end(text: str) -> int | None:
    """Return the character offset just after the last Codex assistant marker."""
    marker = None
    for match in _CODEX_ASSISTANT_MARKER.finditer(text):
        marker = match.end()
    return marker


def filter_findings_by_policy(
    findings: Iterable[JsonObject],
    *,
    min_confidence: float,
    allowed_kinds: Iterable[str] = (),
    category_floors: Mapping[str, float] | None = None,
    surface_predicate: Callable[[JsonObject], bool] | None = None,
    suppressed_categories: Iterable[str] = (),
) -> tuple[list[JsonObject], list[tuple[JsonObject, str]]]:
    """Apply confidence, kind-whitelist, surface-mode, and dispute filters.

    Four filters are applied in order; the first one a finding fails wins
    the drop reason:

    1. **Confidence:** drop any finding whose ``confidence`` is missing,
       not numeric, or strictly less than the effective floor. Confidence
       values from the agent are floats in ``[0.0, 1.0]``; the threshold is
       inclusive on the high side so ``min_confidence=0.85`` accepts a
       finding scored exactly ``0.85``. When ``category_floors`` supplies a
       floor for the finding's canonical category that is **higher** than
       ``min_confidence``, that per-category floor is the bar instead and a
       finding failing it is dropped as ``confidence_below_category_floor``
       (the ``[review]`` calibrated-confidence lever). A per-category floor
       only ever *raises* the bar — it never lowers it below ``min_confidence``.
    2. **Allowed kinds:** if ``allowed_kinds`` is non-empty, a finding must
       have **at least one** of its ``severity``, ``category``, or ``type``
       fields (case-insensitive) appear in the allowlist. An empty
       ``allowed_kinds`` skips this filter entirely — "no whitelist
       configured" means "allow all kinds that already passed
       confidence".
    3. **Surface mode:** if ``surface_predicate`` is provided, drop any
       finding it returns falsey for. This is the ``[review].mode`` preset
       hook — ``gate`` passes :func:`gate_surfaces` (keep only merge-blocking
       defects), ``collaborate`` (the default) passes ``None`` and skips this
       filter entirely. Unlike :data:`allowed_kinds` (an OR across raw
       severity/category/type fields), a predicate can express the gate's
       *conjunction* across type + severity + canonical category, which a
       flat whitelist cannot.
    4. **Suppressed categories:** if ``suppressed_categories`` is non-empty,
       drop any finding whose ``category`` (case-insensitive) is in the set.
       This is the opt-in dispute-driven noise filter — the caller passes
       the categories a team has repeatedly rejected on this repo (see
       :func:`bubo.db.disputed_finding_classes`). Empty means "no
       suppression", which is the default, so this module stays IO-free:
       the dispute aggregation lives in the DB layer, not here.

    Parameters
    ----------
    findings:
        Iterable of finding dicts, typically the output of
        :func:`extract_findings`.
    min_confidence:
        Minimum confidence to keep a finding (``0.0`` keeps everything).
    allowed_kinds:
        Lowercase set of allowed severity/category/type values. Empty
        means "no kind filter".
    category_floors:
        Optional mapping of canonical category -> minimum confidence. A
        finding in that category must clear ``max(min_confidence, floor)``.
        ``None`` (the default) means "global ``min_confidence`` for every
        category" — the pre-calibration behavior. Build it from the
        operator's manual ``[review.category_min_confidence]`` table and/or
        :func:`calibrated_category_floors`.
    surface_predicate:
        Optional callable run per finding; a falsey return drops it with
        reason ``"surface_mode_excluded"``. ``None`` (the default) skips this
        filter, preserving the pre-mode behavior exactly. Resolve it from the
        operator's ``[review].mode`` via :func:`surface_predicate_for_mode`.
    suppressed_categories:
        Categories to drop wholesale. Matched against the finding's
        ``category`` field only (not severity/type), normalized with
        ``.strip().lower()`` on both sides to mirror the DB-side
        ``lower(trim(...))``. Empty means "no suppression".

    Returns
    -------
    tuple
        ``(kept, dropped)``. ``kept`` is the filtered list in original
        order. ``dropped`` is a list of ``(finding, reason)`` tuples where
        ``reason`` is one of ``"confidence_below_threshold"``,
        ``"confidence_below_category_floor"``, ``"kind_not_allowed"``,
        ``"surface_mode_excluded"``, or ``"disputed_class_suppressed"`` —
        useful for logging and metrics so an operator can see *why* a real
        finding got swallowed.
    """
    allowed = {kind.strip().lower() for kind in allowed_kinds if kind}
    suppressed = {kind.strip().lower() for kind in suppressed_categories if kind}
    kept: list[JsonObject] = []
    dropped: list[tuple[JsonObject, str]] = []
    for finding in findings:
        confidence = _finding_confidence(finding)
        # Global confidence gate first: a missing/non-numeric or below-floor
        # score is dropped as ``confidence_below_threshold`` regardless of any
        # per-category floor — the global bar is what kills it.
        if confidence is None or confidence < min_confidence:
            dropped.append((finding, "confidence_below_threshold"))
            continue
        # Per-category floor (the calibrated-confidence lever): a finding that
        # cleared the global bar but falls under its canonical category's
        # (higher) floor is dropped with a distinct reason, so the per-class
        # gate is visible to an operator. A floor only ever *raises* the bar —
        # one set at or below ``min_confidence`` is a no-op here because the
        # finding already cleared it above.
        if category_floors:
            cat_floor = category_floors.get(finding_canonical_category(finding))
            if cat_floor is not None and confidence < cat_floor:
                dropped.append((finding, "confidence_below_category_floor"))
                continue
        if allowed and not _finding_matches_kinds(finding, allowed):
            dropped.append((finding, "kind_not_allowed"))
            continue
        if surface_predicate is not None and not surface_predicate(finding):
            dropped.append((finding, "surface_mode_excluded"))
            continue
        if suppressed and _finding_category_in(finding, suppressed):
            dropped.append((finding, "disputed_class_suppressed"))
            continue
        kept.append(finding)
    return kept, dropped


def _finding_confidence(finding: JsonObject) -> float | None:
    """Return the finding's confidence as a float, or ``None`` if absent or
    malformed.

    The agent is asked to emit a number in [0.0, 1.0]; defensive coercion
    here treats anything else as "missing" so a single garbled finding does
    not break the whole batch.
    """
    value = finding.get("confidence")
    if value is None:
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _finding_matches_kinds(finding: JsonObject, allowed: set[str]) -> bool:
    """Return ``True`` if any of the finding's kind-style fields appears in
    ``allowed`` (lowercase comparison).

    Looks at ``severity``, ``category``, and ``type`` per :data:`_KIND_FIELDS`.
    Missing fields are skipped silently — a finding with no ``category``
    still passes if its ``severity`` matches the allowlist.
    """
    for field_name in _KIND_FIELDS:
        value = finding.get(field_name)
        if not isinstance(value, str):
            continue
        if value.strip().lower() in allowed:
            return True
    return False


def _finding_category_in(finding: JsonObject, suppressed: set[str]) -> bool:
    """Return ``True`` if the finding's ``category`` is in ``suppressed``.

    Matches the ``category`` field only — unlike the kind whitelist, which
    looks at severity/category/type — because the dispute signal is
    aggregated per category in :func:`bubo.db.disputed_finding_classes`.
    Normalized with ``.strip().lower()`` to mirror the DB-side
    ``lower(trim(...))``. A finding with no string ``category`` is never
    suppressed.
    """
    value = finding.get("category")
    return isinstance(value, str) and value.strip().lower() in suppressed


def changed_lines_from_files(
    files: Iterable[tuple[str | None, str | None, str]],
) -> dict[str, JsonObject]:
    """Provider-neutral changed-line map from unified-diff hunks.

    Each item in ``files`` is ``(new_path, old_path, diff_text)``. Both the
    GitLab and GitHub providers normalize their per-file diff payloads into
    this tuple shape and call here, so the hunk-walking logic lives in one
    place.

    Returns ``{new_path: {"new_path", "old_path", "new_lines": set[int]}}``
    where ``new_lines`` are the 1-based line numbers of added lines (the only
    lines an inline comment can attach to).
    """
    changed: dict[str, JsonObject] = {}
    for new_path, old_path, diff_text in files:
        if not new_path:
            continue
        entry = changed.setdefault(
            new_path,
            {"new_path": new_path, "old_path": old_path or new_path, "new_lines": set()},
        )
        _accumulate_added_lines(entry, diff_text or "")
    return changed


def _accumulate_added_lines(entry: JsonObject, diff_text: str) -> None:
    """Walk one file's unified diff, recording added-line numbers into ``entry``."""
    new_line: int | None = None
    for line in diff_text.splitlines():
        match = _HUNK_HEADER.match(line)
        if match:
            new_line = int(match.group(1))
            continue
        if new_line is None or line.startswith("\\"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            entry["new_lines"].add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_line += 1


def changed_lines_from_diffs(diffs: list[JsonObject]) -> dict[str, JsonObject]:
    """GitLab-shaped adapter for :func:`changed_lines_from_files`.

    GitLab's MR diffs endpoint returns ``{new_path, old_path, diff}`` per
    file (some API versions use camelCase ``newPath``/``oldPath``).
    """
    return changed_lines_from_files(
        (
            item.get("new_path") or item.get("newPath"),
            item.get("old_path") or item.get("oldPath"),
            item.get("diff") or "",
        )
        for item in diffs
    )


def resolve_finding_line(
    changed: dict[str, JsonObject], finding: JsonObject
) -> tuple[JsonObject, int] | None:
    """Validate a finding's target against the changed-line map.

    Returns ``(changed_entry, line)`` when the finding names a file and a
    1-based line that is an *added* line in the diff (the only lines an
    inline comment can attach to). Returns ``None`` otherwise — missing
    file/line, non-integer line, file not in the diff, or line not added.

    Provider-neutral: both :func:`build_position` (GitLab) and the GitHub
    provider's position builder use this to decide whether a finding is
    placeable.
    """
    file_path = finding.get("file") or finding.get("path")
    line = finding.get("line") or finding.get("new_line")
    if not file_path or line is None:
        return None
    try:
        line = int(line)
    except TypeError, ValueError:
        return None
    entry = changed.get(file_path)
    if not entry or line not in entry["new_lines"]:
        return None
    return entry, line


def build_position(
    mr: JsonObject, changed: dict[str, JsonObject], finding: JsonObject
) -> JsonObject | None:
    """Build a GitLab inline-comment ``position`` dict for a finding.

    GitLab requires base/start/head SHAs from the MR's ``diff_refs`` plus
    the old/new path and the new line. Returns ``None`` if the finding is
    not placeable or the MR lacks diff refs.
    """
    resolved = resolve_finding_line(changed, finding)
    if resolved is None:
        return None
    entry, line = resolved
    refs = mr.get("diff_refs") or {}
    if not refs.get("base_sha") or not refs.get("start_sha") or not refs.get("head_sha"):
        return None
    return {
        "position_type": "text",
        "base_sha": refs["base_sha"],
        "start_sha": refs["start_sha"],
        "head_sha": refs["head_sha"],
        "old_path": entry["old_path"],
        "new_path": entry["new_path"],
        "new_line": line,
    }


def finding_body(finding: JsonObject) -> str:
    """Render the canonical, mood-neutral comment body for a finding.

    This is the structured ``**Issue (…):** … **Impact:** … **Evidence:** …
    **Fix:** … **Confidence:** …`` shape. It is what the fingerprint and the
    recorded DB body use, *independent of review tone* — see
    :func:`finding_comment_body` for the tone-aware body that is actually
    posted.

    The ``body``/``comment`` fallback only fires for a degenerate finding that
    has *no* structured fields (just a freeform body): the in-voice ``comment``
    a non-default tone adds is ignored here because the structured fields are
    present, which is exactly what keeps the fingerprint tone-invariant.
    """
    body = finding.get("body") or finding.get("comment")
    title = finding.get("title") or "review finding"
    kind = finding.get("type") or "issue"
    severity = finding.get("severity") or "blocking"
    category = finding.get("category") or "correctness"
    impact = finding.get("impact")
    evidence = finding.get("evidence")
    fix = finding.get("fix")
    confidence = finding.get("confidence")
    parts = [f"**{kind.title()} ({severity}, {category}):** {str(title).strip()}"]
    if impact:
        parts.append(f"**Impact:** {impact}")
    if evidence:
        parts.append(f"**Evidence:** {evidence}")
    if fix:
        parts.append(f"**Fix:** {fix}")
    if confidence is not None:
        parts.append(f"**Confidence:** {confidence}")
    if body and len(parts) == 1:
        parts.append(str(body).strip())
    return "\n\n".join(parts).strip()


def finding_comment_body(finding: JsonObject, tone: str = "terse") -> str:
    """Return the body to POST for ``finding``, honoring the review ``tone``.

    For a non-default tone, prefer the reviewer's in-voice ``comment`` field
    (produced by the tone directive in the review prompt); fall back to the
    structured :func:`finding_body` when it is missing or blank, or whenever
    the tone is the default ``terse``.

    Deliberately separate from :func:`finding_body`: the fingerprint and the
    recorded canonical body always use the structured render, so the posted
    voice never affects dedup, outcome identity, or the audit dataset.
    """
    if tone != "terse":
        comment = finding.get("comment")
        if isinstance(comment, str) and comment.strip():
            return comment.strip()
    return finding_body(finding)


def finding_fingerprint(project: str, iid: int, sha: str, finding: JsonObject) -> str:
    """Stable hash identifying a finding for idempotent posting and dedup.

    Uses :func:`finding_body` (the structured render) for the ``body`` part —
    NOT :func:`finding_comment_body` — so the fingerprint is **tone-invariant**:
    the same finding produces the same hash whether it is posted terse or in a
    mood, which is what lets operators change ``[review].tone`` without
    re-posting findings or fragmenting their accept/dispute history.
    """
    payload = {
        "project": project,
        "iid": iid,
        "sha": sha,
        "file": finding.get("file") or finding.get("path"),
        "line": finding.get("line") or finding.get("new_line"),
        "body": " ".join(finding_body(finding).split()),
    }
    return stable_hash(payload)
