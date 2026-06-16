"""GitLab provider — wraps the GitLab REST client + MCP posting.

Composes :mod:`bubo.gitlab` (REST), :mod:`bubo.mcp`
(inline posting), and the GitLab-specific checkout/position logic that
previously lived inline in the poller.
"""

from __future__ import annotations

import os
from pathlib import Path

from bubo import gitlab, mcp
from bubo.config_values import ConfigError
from bubo.findings import build_position, changed_lines_from_diffs
from bubo.review_config import ReviewConfig
from bubo.scm.base import build_review_contract
from bubo.secrets import redact_secrets
from bubo.subproc import run_bounded
from bubo.types import JsonObject


class GitLabProvider:
    """:class:`~bubo.scm.base.ScmProvider` for GitLab merge requests."""

    name = "gitlab"

    def token(self) -> str:
        for key in ("GITLAB_TOKEN", "GITLAB_PERSONAL_ACCESS_TOKEN", "GLAB_TOKEN"):
            if os.environ.get(key):
                return os.environ[key]
        raise ConfigError("missing GitLab token")

    def bot_username(self) -> str:
        return os.environ.get("BUBO_GITLAB_USERNAME", "bubo")

    def list_open_changes(self, cfg: ReviewConfig, project: str, token: str) -> list[JsonObject]:
        return gitlab.open_mrs(cfg, project, token)

    def change_number(self, change: JsonObject) -> int:
        return int(change["iid"])

    def head_sha(self, change: JsonObject) -> str:
        return change.get("sha") or change.get("diff_refs", {}).get("head_sha") or ""

    def get_change(self, cfg: ReviewConfig, token: str, project: str, number: int) -> JsonObject:
        return gitlab.get_mr(cfg, token, project, number)

    def changed_lines(
        self, cfg: ReviewConfig, token: str, project: str, number: int
    ) -> dict[str, JsonObject]:
        diffs = gitlab.get_mr_diffs(cfg, token, project, number)
        return changed_lines_from_diffs(diffs)

    def list_commits(
        self, cfg: ReviewConfig, token: str, project: str, number: int
    ) -> list[JsonObject]:
        return [
            {
                "sha": str(commit.get("id") or ""),
                "message": str(commit.get("message") or commit.get("title") or ""),
                "author": str(commit.get("author_name") or ""),
            }
            for commit in gitlab.get_mr_commits(cfg, token, project, number)
        ]

    def build_position(
        self, change: JsonObject, changed: dict[str, JsonObject], finding: JsonObject
    ) -> JsonObject | None:
        return build_position(change, changed, finding)

    def checkout(self, cfg: ReviewConfig, project: str, change: JsonObject, dest: Path) -> None:
        number = self.change_number(change)
        sha = self.head_sha(change)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not (dest / ".git").exists():
            result = run_bounded(["glab", "repo", "clone", project, str(dest)], timeout=900)
            if result.returncode:
                raise RuntimeError(redact_secrets(result.stdout[-3000:]))
        for args in (
            ["git", "fetch", "origin", "--prune"],
            [
                "git",
                "fetch",
                "origin",
                f"refs/merge-requests/{number}/head:refs/remotes/origin/mr-{number}",
            ],
            ["git", "checkout", "--detach", sha],
        ):
            result = run_bounded(args, cwd=dest, timeout=900)
            if result.returncode:
                raise RuntimeError(redact_secrets(result.stdout[-3000:]))

    def post_inline_comment(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        body: str,
        position: JsonObject,
    ) -> str:
        result = mcp.call_tool(
            "create_merge_request_thread", mcp.thread_args(project, number, body, position)
        )
        found = mcp.discussion_id(result)
        if found:
            return found
        existing = gitlab.find_discussion_by_body(cfg, token, project, number, body)
        if existing:
            return existing
        return mcp.discussion_id_from_response(
            gitlab.create_merge_request_discussion(cfg, token, project, number, body, position)
        )

    def post_change_comment(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        body: str,
    ) -> str:
        existing = gitlab.find_note_by_body(
            cfg, token, project, number, body, bot_username=self.bot_username()
        )
        if existing:
            return existing
        created = gitlab.create_mr_note(cfg, token, project, number, body)
        note_id = created.get("id")
        return "" if note_id is None else str(note_id)

    def fetch_outcome(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        bot_username: str,
    ) -> JsonObject:
        mr = self.get_change(cfg, token, project, number)
        discussion = gitlab.get_mr_discussion(cfg, token, project, number, thread_id)
        return gitlab.classify_discussion_outcome(
            discussion, bot_username=bot_username, mr_state=str(mr.get("state") or "")
        )

    def review_prompt(
        self, project: str, change: JsonObject, cfg: ReviewConfig, *, extra_directive: str = ""
    ) -> str:
        contract = build_review_contract(cfg)
        suffix = f"\n\n{extra_directive}" if extra_directive else ""
        return f"""Review GitLab MR {change.get("web_url")}
Project: {project}
MR IID: {change.get("iid")}
Title: {change.get("title")}
source branch: {change.get("source_branch")}
target branch: {change.get("target_branch")}
head SHA: {self.head_sha(change)}

{contract}{suffix}"""
