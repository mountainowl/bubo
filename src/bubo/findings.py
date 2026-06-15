"""Finding extraction, filtering, and diff-position mapping.

This module is the bridge between the raw LLM output (a string the reviewer
subprocess wrote to stdout) and structured per-line review threads that the
poster path can publish to GitLab.

Responsibilities:

* :func:`extract_findings` — robustly parse the reviewer's JSON output,
  including markdown-fenced and noisy-prose variants.
* :func:`filter_findings_by_policy` — apply the operator's confidence and
  kind whitelist policies from ``config/env.toml``.
* :func:`changed_lines_from_diffs` and :func:`build_position` — figure out
  whether a finding's ``file``/``line`` actually corresponds to an added
  line in the MR diff (only added lines can carry an inline GitLab comment).
* :func:`finding_body` — render a finding object into the
  Issue/Impact/Evidence/Fix/Confidence comment shape.
* :func:`finding_fingerprint` — stable hash for idempotent posting.

Everything in this module is pure (no IO, no globals) so it is easily
testable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from bubo.config_values import positive_int
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
            raise ValueError("review output is not JSON findings") from None
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
        raise ValueError("review JSON must be an array or an object with findings")
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
    suppressed_categories: Iterable[str] = (),
) -> tuple[list[JsonObject], list[tuple[JsonObject, str]]]:
    """Apply confidence, kind-whitelist, and dispute-suppression filters.

    Three filters are applied in order; the first one a finding fails wins
    the drop reason:

    1. **Confidence:** drop any finding whose ``confidence`` is missing,
       not numeric, or strictly less than ``min_confidence``. Confidence
       values from the agent are floats in ``[0.0, 1.0]``; the threshold is
       inclusive on the high side so ``min_confidence=0.85`` accepts a
       finding scored exactly ``0.85``.
    2. **Allowed kinds:** if ``allowed_kinds`` is non-empty, a finding must
       have **at least one** of its ``severity``, ``category``, or ``type``
       fields (case-insensitive) appear in the allowlist. An empty
       ``allowed_kinds`` skips this filter entirely — "no whitelist
       configured" means "allow all kinds that already passed
       confidence".
    3. **Suppressed categories:** if ``suppressed_categories`` is non-empty,
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
        ``"kind_not_allowed"``, or ``"disputed_class_suppressed"`` — useful
        for logging and metrics so an operator can see *why* a real finding
        got swallowed.
    """
    allowed = {kind.strip().lower() for kind in allowed_kinds if kind}
    suppressed = {kind.strip().lower() for kind in suppressed_categories if kind}
    kept: list[JsonObject] = []
    dropped: list[tuple[JsonObject, str]] = []
    for finding in findings:
        confidence = _finding_confidence(finding)
        if confidence is None or confidence < min_confidence:
            dropped.append((finding, "confidence_below_threshold"))
            continue
        if allowed and not _finding_matches_kinds(finding, allowed):
            dropped.append((finding, "kind_not_allowed"))
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


def finding_fingerprint(project: str, iid: int, sha: str, finding: JsonObject) -> str:
    payload = {
        "project": project,
        "iid": iid,
        "sha": sha,
        "file": finding.get("file") or finding.get("path"),
        "line": finding.get("line") or finding.get("new_line"),
        "body": " ".join(finding_body(finding).split()),
    }
    return stable_hash(payload)
