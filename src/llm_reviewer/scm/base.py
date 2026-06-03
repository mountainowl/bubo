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

from pathlib import Path
from typing import Protocol

from llm_reviewer.review_config import ReviewConfig
from llm_reviewer.types import JsonObject

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


class ScmProvider(Protocol):
    """Structural interface every source-control backend implements.

    Implementations are stateless — they read everything from the
    arguments and the environment. ``token`` and ``bot_username`` read the
    process environment (populated by ``bin/env`` from ``config/env.toml``).
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

    def review_prompt(self, project: str, change: JsonObject, cfg: ReviewConfig) -> str:
        """Build the per-change review task prompt."""
        ...
