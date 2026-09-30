"""GitHub provider — GitHub REST client.

Composes :mod:`bubo.github` (REST) with GitHub-specific checkout and position
logic.

Key differences from GitLab, all encapsulated here:

* **Checkout** uses plain ``git`` over HTTPS (credential-safe, see
  :func:`bubo.scm.base.git_checkout_change`) and the ``refs/pull/<n>/head`` ref.
* **Position** is GitHub's ``{commit_id, path, line, side}`` anchor, not
  GitLab's base/start/head ``position`` dict.
* **Posting** goes through the GitHub REST API (an inline PR review comment).
* **Outcome** is classified from REST data; thread *resolution* state is
  GitHub-GraphQL-only and is reported as unresolved (documented).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from bubo import github
from bubo.config_values import ConfigError
from bubo.errors import describe
from bubo.events import log
from bubo.findings import changed_lines_from_files, resolve_finding_line
from bubo.review_config import ReviewConfig
from bubo.scm.base import (
    FindingThread,
    FindingThreadReply,
    FindingThreadResolution,
    FindingThreadState,
    build_review_contract,
    git_checkout_change,
)
from bubo.types import JsonObject


class GitHubProvider:
    """:class:`~bubo.scm.base.ScmProvider` for GitHub pull requests."""

    name = "github"

    def token(self) -> str:
        for key in ("GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN", "GH_TOKEN"):
            if os.environ.get(key):
                return os.environ[key]
        raise ConfigError(
            describe(
                "missing GitHub token",
                reason="no GitHub token found in the environment",
                fix=(
                    "set [github].token in config/env.toml or export GITHUB_TOKEN "
                    "(needs repo scope)."
                ),
            )
        )

    def bot_username(self) -> str:
        return os.environ.get("BUBO_GITHUB_USERNAME", "bubo")

    def list_open_changes(self, cfg: ReviewConfig, project: str, token: str) -> list[JsonObject]:
        return github.open_prs(cfg, project, token)

    def change_number(self, change: JsonObject) -> int:
        return int(change["number"])

    def head_sha(self, change: JsonObject) -> str:
        head = change.get("head") or {}
        return str(head.get("sha") or "")

    def get_change(self, cfg: ReviewConfig, token: str, project: str, number: int) -> JsonObject:
        return github.get_pr(cfg, token, project, number)

    def changed_lines(
        self, cfg: ReviewConfig, token: str, project: str, number: int
    ) -> dict[str, JsonObject]:
        files = github.get_pr_files(cfg, token, project, number)
        return changed_lines_from_files(
            (
                f.get("filename"),
                f.get("previous_filename") or f.get("filename"),
                f.get("patch") or "",
            )
            for f in files
        )

    def list_commits(
        self, cfg: ReviewConfig, token: str, project: str, number: int
    ) -> list[JsonObject]:
        out: list[JsonObject] = []
        for entry in github.get_pr_commits(cfg, token, project, number):
            commit = entry.get("commit") or {}
            author = commit.get("author") or {}
            out.append(
                {
                    "sha": str(entry.get("sha") or ""),
                    "message": str(commit.get("message") or ""),
                    "author": str(author.get("name") or ""),
                }
            )
        return out

    def build_position(
        self, change: JsonObject, changed: dict[str, JsonObject], finding: JsonObject
    ) -> JsonObject | None:
        resolved = resolve_finding_line(changed, finding)
        if resolved is None:
            return None
        entry, line = resolved
        commit_id = self.head_sha(change)
        if not commit_id:
            return None
        # GitHub's inline-comment anchor: comment on the RIGHT (new) side of
        # the diff at the given line of the head commit.
        return {
            "commit_id": commit_id,
            "path": entry["new_path"],
            "line": line,
            "side": "RIGHT",
        }

    def checkout(self, cfg: ReviewConfig, project: str, change: JsonObject, dest: Path) -> None:
        number = self.change_number(change)
        # Derive the web host from the API URL: api.github.com → github.com;
        # GitHub Enterprise uses https://<host>/api/v3, whose web host is the netloc.
        host = urlparse(cfg.github_api_url).netloc or "github.com"
        if host == "api.github.com":
            host = "github.com"
        clone_url = f"https://{host}/{project}.git"
        git_checkout_change(
            clone_url=clone_url,
            ref_fetch=f"refs/pull/{number}/head:refs/remotes/origin/pr-{number}",
            sha=self.head_sha(change),
            dest=dest,
            token=self.token(),
            username="x-access-token",
        )

    def post_inline_comment(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        body: str,
        position: JsonObject,
    ) -> str:
        existing = github.find_review_comment_by_body(cfg, token, project, number, body)
        if existing:
            return existing
        created = github.create_pr_review_comment(cfg, token, project, number, body, position)
        return str(created.get("id") or "")

    def post_change_comment(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        body: str,
    ) -> str:
        existing = github.find_issue_comment_by_body(
            cfg, token, project, number, body, bot_username=self.bot_username()
        )
        if existing:
            return existing
        created = github.create_issue_comment(cfg, token, project, number, body)
        comment_id = created.get("id")
        return "" if comment_id is None else str(comment_id)

    def fetch_outcome(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        bot_username: str,
    ) -> JsonObject:
        pr = self.get_change(cfg, token, project, number)
        # GitHub PR `state` is open/closed; a merged PR is closed + merged.
        # Normalize to "merged" so merged_unresolved is computed correctly.
        pr_state = (
            "merged" if (pr.get("merged") or pr.get("merged_at")) else str(pr.get("state") or "")
        )
        # Resolution state is GraphQL-only. Try it first; fall back to the
        # resolution-blind REST classifier on any GraphQL failure so a
        # GraphQL outage never blocks outcome sync entirely.
        try:
            threads = github.get_pr_review_threads(cfg, token, project, number)
            thread = github.find_thread_for_comment(threads, thread_id)
            if thread is not None:
                return github.classify_graphql_thread_outcome(thread, bot_username, pr_state)
            log(
                "github_graphql_thread_not_found",
                project=project,
                number=number,
                thread=str(thread_id),
            )
        except (RuntimeError, TimeoutError, OSError) as exc:
            log("github_graphql_outcome_failed", project=project, number=number, error=str(exc))
        comment = github.get_pr_review_comment(cfg, token, project, thread_id)
        # Replies are review comments whose in_reply_to_id chains to ours.
        all_comments = github.get_pr_review_comments(cfg, token, project, number)
        replies = [c for c in all_comments if str(c.get("in_reply_to_id") or "") == str(thread_id)]
        return github.classify_review_thread_outcome(
            comment, replies, bot_username=bot_username, pr_state=pr_state
        )

    def finding_thread(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        bot_username: str,
        reply_marker: str,
    ) -> FindingThread:
        """Return a fail-closed, Bubo-owned GitHub review-thread state."""
        threads = github.get_pr_review_threads(cfg, token, project, number)
        thread = github.find_thread_for_comment(threads, thread_id)
        if thread is None:
            # GraphQL does not expose deleted review comments.  A missing
            # thread is only known-deleted when its persisted REST id returns
            # 404.  Either result remains non-writable.
            if thread_id.isdecimal() and github.pr_review_comment_deleted(
                cfg, token, project, thread_id
            ):
                return FindingThread(FindingThreadState.DELETED)
            return FindingThread(FindingThreadState.FOREIGN)
        root = github.bubo_owned_root(thread, thread_id, bot_username)
        if root is None:
            return FindingThread(FindingThreadState.FOREIGN)
        marker = github.bubo_reply_with_marker(thread, bot_username, reply_marker) is not None
        state = (
            FindingThreadState.RESOLVED if thread.get("is_resolved") else FindingThreadState.OPEN
        )
        return FindingThread(state, reply_marker_present=marker)

    def reply_to_finding_thread(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        body: str,
        reply_marker: str,
    ) -> FindingThreadReply:
        """Post/reuse a marked reply and prove its marker survived GitHub."""
        if reply_marker not in body:
            raise ValueError("Bubo reconciliation reply is missing its idempotency marker")
        threads = github.get_pr_review_threads(cfg, token, project, number)
        thread = github.find_thread_for_comment(threads, thread_id)
        if thread is None:
            snapshot = self.finding_thread(
                cfg, token, project, number, thread_id, self.bot_username(), reply_marker
            )
            return FindingThreadReply(
                None,
                marker_confirmed=snapshot.reply_marker_present,
                applied_by_bubo=False,
                state=snapshot.state,
            )
        if thread.get("is_resolved"):
            return FindingThreadReply(
                None,
                marker_confirmed=False,
                applied_by_bubo=False,
                state=FindingThreadState.RESOLVED,
            )
        root = github.bubo_owned_root(thread, thread_id, self.bot_username())
        if root is None:
            return FindingThreadReply(
                None,
                marker_confirmed=False,
                applied_by_bubo=False,
                state=FindingThreadState.FOREIGN,
            )
        prior = github.bubo_reply_with_marker(thread, self.bot_username(), reply_marker)
        if prior is not None:
            return FindingThreadReply(
                str(prior.get("database_id") or prior.get("node_id") or "") or None,
                marker_confirmed=True,
                applied_by_bubo=False,
                state=FindingThreadState.OPEN,
            )
        root_id = root.get("database_id")
        if root_id is None:
            raise RuntimeError("GitHub Bubo root comment has no REST database id")
        created = github.reply_to_pr_review_comment(
            cfg, token, project, number, str(root_id), body
        )
        reply_id = str(created.get("id") or "")
        if not reply_id:
            raise RuntimeError("GitHub did not return an id for Bubo reconciliation reply")
        # Re-fetch through complete GraphQL pagination: a successful REST post
        # alone is not durable evidence if the response was stale or malformed.
        final = self.finding_thread(
            cfg, token, project, number, thread_id, self.bot_username(), reply_marker
        )
        if not final.reply_marker_present:
            raise RuntimeError("GitHub did not confirm Bubo reconciliation reply marker")
        return FindingThreadReply(
            reply_id,
            marker_confirmed=True,
            applied_by_bubo=True,
            state=final.state,
        )

    def resolve_finding_thread(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        bot_username: str,
    ) -> FindingThreadResolution:
        """Resolve a proven Bubo thread and return a verified provider state."""
        threads = github.get_pr_review_threads(cfg, token, project, number)
        thread = github.find_thread_for_comment(threads, thread_id)
        if thread is None:
            snapshot = self.finding_thread(
                cfg, token, project, number, thread_id, bot_username, ""
            )
            return FindingThreadResolution(snapshot.state, applied_by_bubo=False)
        if github.bubo_owned_root(thread, thread_id, bot_username) is None:
            return FindingThreadResolution(FindingThreadState.FOREIGN, applied_by_bubo=False)
        if thread.get("is_resolved"):
            return FindingThreadResolution(FindingThreadState.RESOLVED, applied_by_bubo=False)
        node_id = str(thread.get("node_id") or "")
        if not node_id:
            raise RuntimeError("GitHub review thread has no GraphQL node id")
        # Deliberately no REST fallback: only GraphQL can resolve a review
        # thread, and a failed mutation must leave the finding open.
        github.resolve_pr_review_thread(cfg, token, node_id)
        final_threads = github.get_pr_review_threads(cfg, token, project, number)
        final_thread = github.find_thread_for_comment(final_threads, thread_id)
        if final_thread is None:
            final = self.finding_thread(
                cfg, token, project, number, thread_id, bot_username, ""
            )
            if final.state is FindingThreadState.DELETED:
                return FindingThreadResolution(final.state, applied_by_bubo=False)
            raise RuntimeError("GitHub review thread was not resolved after mutation")
        if github.bubo_owned_root(final_thread, thread_id, bot_username) is None:
            return FindingThreadResolution(FindingThreadState.FOREIGN, applied_by_bubo=False)
        if not final_thread.get("is_resolved"):
            raise RuntimeError("GitHub review thread was not resolved after mutation")
        # A different resolver proves a developer/manual race; do not take
        # credit for it even though the desired end state is already reached.
        applied = str(final_thread.get("resolved_by") or "") == bot_username
        return FindingThreadResolution(FindingThreadState.RESOLVED, applied_by_bubo=applied)

    def review_prompt(
        self, project: str, change: JsonObject, cfg: ReviewConfig, *, extra_directive: str = ""
    ) -> str:
        contract = build_review_contract(cfg)
        head = change.get("head") or {}
        base = change.get("base") or {}
        suffix = f"\n\n{extra_directive}" if extra_directive else ""
        return f"""Review GitHub PR {change.get("html_url")}
Project: {project}
PR number: {change.get("number")}
Title: {change.get("title")}
source branch: {head.get("ref")}
target branch: {base.get("ref")}
head SHA: {self.head_sha(change)}

{contract}{suffix}"""
