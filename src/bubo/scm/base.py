"""The ``ScmProvider`` protocol and shared provider helpers.

A provider encapsulates every operation that differs between GitLab and
GitHub: listing changes, checking one out, fetching diffs, mapping a
finding to an inline-comment anchor, posting the comment, and classifying
its outcome. The poller calls only these methods, so it never branches on
the provider name.

The terminology is normalized to "change" to cover both GitLab merge
requests and GitHub pull requests.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Protocol

from bubo.errors import describe
from bubo.review_config import ReviewConfig
from bubo.secrets import redact_secrets
from bubo.subproc import run_bounded
from bubo.types import JsonObject

# The finding-output contract shared by every provider's review prompt. Only
# the change-specific header differs per provider; the JSON shape the agent
# must return is identical. ``{max_findings}`` is filled in per review.
REVIEW_CONTRACT = """Use the `code-reviewer` skill through Superpowers for the review contract.
Review the diff only. Do not post comments yourself.
Return a JSON array only. Do not wrap it in markdown.
Return at most {max_findings} findings.
Each finding object must have:
- type
- severity
- category
- title
- file
- line
- impact
- evidence
- fix
- confidence
type must be one of: issue, suggestion, question.
severity must be one of: blocking, non-blocking.
confidence must be a number from 0 to 1.
Use line for the changed new-line where the inline comment should be placed.
Return [] when there are no actionable findings."""

# Per-tone voice directives appended to the review contract when the operator
# selects a non-default ``[review].tone``. Each asks the reviewer for ONE extra
# ``comment`` field — the finding rewritten in that voice — which bubo posts in
# place of the structured render. Two invariants every block enforces:
#   * the terse house style still governs title/impact/evidence/fix (those feed
#     the audit dataset + the mood-neutral dedup fingerprint), so only the
#     ``comment`` field changes voice;
#   * the example teaches register only (cross-domain, "do not reuse"), which is
#     the most reliable lever for tone and resists topic/phrasing overfit.
_VOICE_HEADER = """In addition to the structured fields, add a "comment" field to each finding:
the same finding rewritten as one short inline comment in the voice below. The
terse house style still governs title/impact/evidence/fix — the voice applies to
"comment" ONLY. Do not put a confidence number in "comment". The example shows
voice only; do not reuse its wording or topic."""

COMMENT_VOICES: dict[str, str] = {
    "collaborative": _VOICE_HEADER
    + """

Voice: a thoughtful senior engineer leaving an inline note. Acknowledge intent
when natural, show the bug with one concrete example, phrase the fix as a
suggestion. Hedges ("I think") and contractions are fine.
Example (style only):
  fix:     "Guard the divide; return 0 when count is 0."
  comment: "Heads up — if count comes in as 0 this throws, since we divide
           total/count with no guard. Probably worth returning 0 (or skipping)
           when the list is empty."
""",
    "socratic": _VOICE_HEADER
    + """

Voice: lead with a question that surfaces the gap, point at the mechanism, and
invite the author to confirm rather than asserting a verdict.
Example (style only):
  fix:     "Guard the divide; return 0 when count is 0."
  comment: "What happens here when count is 0? We divide total/count with no
           guard, so this looks like it'll throw on an empty list — should we
           return 0 (or skip) in that case?"
""",
    "formal": _VOICE_HEADER
    + """

Voice: measured and professional, complete sentences, no contractions or slang.
State the condition, the consequence, and the recommendation.
Example (style only):
  fix:     "Guard the divide; return 0 when count is 0."
  comment: "When count is 0, this computes total/count without a guard and will
           raise a ZeroDivisionError. Recommend returning 0, or skipping, for
           the empty case."
""",
    "casual": _VOICE_HEADER
    + """

Voice: relaxed, friendly, and brief. Contractions and light informality are
fine; stay precise about the actual bug.
Example (style only):
  fix:     "Guard the divide; return 0 when count is 0."
  comment: "Quick one — count being 0 will blow up here since we divide
           total/count with no guard. Just return 0 (or bail) when it's empty."
""",
}


def comment_voice_directive(tone: str) -> str:
    """Return the voice directive for ``tone``, or ``""`` for the default.

    ``terse`` (and any unrecognized value) returns the empty string, so the
    review prompt — and therefore the whole review — is byte-identical to its
    pre-tone behavior. Config validation (``review.tone`` via ``one_of``) keeps
    unrecognized values from reaching here in practice.
    """
    return COMMENT_VOICES.get(tone, "")


def build_review_contract(cfg: ReviewConfig) -> str:
    """Build the finding-output contract for a review.

    Fills the per-review ``max_findings`` cap and, when a non-default
    ``[review].tone`` is set, appends that tone's voice directive. Shared by
    both providers so their prompts cannot drift on the contract.
    """
    contract = REVIEW_CONTRACT.format(max_findings=cfg.max_findings_per_merge_request)
    voice = comment_voice_directive(cfg.tone)
    return f"{contract}\n\n{voice}" if voice else contract


def _git_auth_args(token: str, username: str) -> list[str]:
    """Per-invocation HTTP Basic auth, passed as a ``-c http.extraHeader`` so the
    token is sent on the wire but **never written to ``.git/config``** — the review
    agent later runs with read access to the worktree, so a persisted credential
    would be exfiltratable. Basic auth works for both GitLab (``oauth2:<token>``)
    and GitHub (``x-access-token:<token>``) git-over-HTTPS.
    """
    cred = base64.b64encode(f"{username}:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {cred}"]


def _run_git(args: list[str], *, cwd: Path | None, what: str) -> None:
    result = run_bounded(args, cwd=cwd, timeout=900)
    if result.returncode:
        raise RuntimeError(
            describe(
                what,
                reason=redact_secrets(result.stdout[-3000:]),
                fix=(
                    "verify the repo URL, the token's scope/permissions, and "
                    "network access to the host."
                ),
            )
        )


def git_checkout_change(
    *,
    clone_url: str,
    ref_fetch: str,
    sha: str,
    dest: Path,
    token: str,
    username: str,
) -> None:
    """Clone (if needed), fetch the change ref, and detach at ``sha`` over HTTPS.

    Auth is supplied per-invocation via :func:`_git_auth_args` on every
    remote-touching call (clone, ``fetch --prune``, the ref fetch), so the
    ``origin`` remote URL stays plain and **no credential is persisted to
    ``.git/config``**. ``checkout`` is local and needs no auth. Raises
    ``RuntimeError`` with a secret-redacted reason on any git failure.
    """
    auth = _git_auth_args(token, username)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        _run_git(
            ["git", *auth, "clone", clone_url, str(dest)],
            cwd=None,
            what="git clone failed for the change",
        )
    for remote_args in (
        ["git", *auth, "fetch", "origin", "--prune"],
        ["git", *auth, "fetch", "origin", ref_fetch],
    ):
        _run_git(remote_args, cwd=dest, what="git fetch failed for the change")
    _run_git(
        ["git", "checkout", "--detach", sha],
        cwd=dest,
        what="git checkout failed for the change",
    )


class ScmProvider(Protocol):
    """Structural interface every source-control backend implements.

    Implementations are stateless — they read everything from the
    arguments and the environment. ``token`` and ``bot_username`` read the
    process environment (populated by ``bin/bubo`` from ``config/env.toml``).
    """

    name: str

    def token(self) -> str:
        """Return the API token from the environment, or raise ``ConfigError``."""
        ...

    def bot_username(self) -> str:
        """Return the bot account username (for outcome-sync attribution)."""
        ...

    def list_open_changes(self, cfg: ReviewConfig, project: str, token: str) -> list[JsonObject]:
        """List open MRs / PRs for ``project``."""
        ...

    def change_number(self, change: JsonObject) -> int:
        """Return the MR IID / PR number from a change payload."""
        ...

    def head_sha(self, change: JsonObject) -> str:
        """Return the head commit SHA from a change payload, or ``""``."""
        ...

    def get_change(self, cfg: ReviewConfig, token: str, project: str, number: int) -> JsonObject:
        """Re-fetch a single change for fresh diff refs."""
        ...

    def changed_lines(
        self, cfg: ReviewConfig, token: str, project: str, number: int
    ) -> dict[str, JsonObject]:
        """Return the added-line map for the change's diff."""
        ...

    def list_commits(
        self, cfg: ReviewConfig, token: str, project: str, number: int
    ) -> list[JsonObject]:
        """Return the change's commits, normalized to ``{sha, message, author}``.

        Used by the opt-in governance provenance capture to read declared-AI
        commit trailers. ``message`` is the full commit message; ``author`` is
        a display name. Returned dicts are intentionally small (not the raw
        provider payload) so the audit trail stays stable across providers.
        """
        ...

    def build_position(
        self, change: JsonObject, changed: dict[str, JsonObject], finding: JsonObject
    ) -> JsonObject | None:
        """Build the provider-specific inline-comment anchor, or ``None``."""
        ...

    def checkout(self, cfg: ReviewConfig, project: str, change: JsonObject, dest: Path) -> None:
        """Clone + fetch the change's head into ``dest`` (detached)."""
        ...

    def post_inline_comment(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        body: str,
        position: JsonObject,
    ) -> str:
        """Post one inline comment; return its thread/comment ID (or ``""``)."""
        ...

    def post_change_comment(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        body: str,
    ) -> str:
        """Post one change-level (non-inline) comment; return its ID (or ``""``).

        Used for narrative posts that are not anchored to a specific diff
        line — currently only the no-findings acknowledgement comment.
        Implementations are expected to be idempotent on exact-body match
        AND scoped to the bot's own authored comments: if a comment with
        the same body already exists *and was authored by the bot*,
        return its ID instead of posting a duplicate. A foreign author
        reproducing the body must NOT satisfy the match, or the bot
        would stop posting its own acknowledgement.
        """
        ...

    def fetch_outcome(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        bot_username: str,
    ) -> JsonObject:
        """Fetch a posted comment's current state and classify its outcome."""
        ...

    def review_prompt(
        self, project: str, change: JsonObject, cfg: ReviewConfig, *, extra_directive: str = ""
    ) -> str:
        """Build the per-change review task prompt.

        ``extra_directive`` is optional governance context (e.g. the
        heightened-scrutiny notice for an escalated change) appended after the
        review contract. Empty (the default) leaves the prompt unchanged, so
        existing callers and the provenance-off path are byte-identical.
        """
        ...
