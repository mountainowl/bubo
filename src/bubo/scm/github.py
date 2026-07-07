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
from bubo.scm.base import build_review_contract, git_checkout_change
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
