"""GitLab provider — wraps the GitLab REST client.

Composes :mod:`bubo.gitlab` (REST) with the GitLab-specific checkout and
position logic. Checkout uses plain ``git`` over HTTPS (credential-safe, see
:func:`bubo.scm.base.git_checkout_change`); posting and outcome sync use the
REST API.
"""

from __future__ import annotations

import os
from pathlib import Path

from bubo import gitlab
from bubo.config_values import ConfigError
from bubo.errors import describe
from bubo.findings import build_position, changed_lines_from_diffs
from bubo.review_config import ReviewConfig
from bubo.scm.base import build_review_contract, git_checkout_change
from bubo.types import JsonObject


class GitLabProvider:
    """:class:`~bubo.scm.base.ScmProvider` for GitLab merge requests."""

    name = "gitlab"

    def token(self) -> str:
        for key in ("GITLAB_TOKEN", "GITLAB_PERSONAL_ACCESS_TOKEN", "GLAB_TOKEN"):
            if os.environ.get(key):
                return os.environ[key]
        raise ConfigError(
            describe(
                "missing GitLab token",
                reason="no GitLab token found in the environment",
                fix=(
                    "set [gitlab].token in config/env.toml or export GITLAB_TOKEN "
                    "(needs api scope)."
                ),
            )
        )

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
        # Plain HTTPS clone URL; the token is supplied per-git-call as an auth
        # header (see git_checkout_change), never embedded in the URL or remote.
        # cfg.gitlab_url is the web host and carries any self-hosted host/port;
        # `project` is the full path-with-namespace (sub-groups included).
        clone_url = f"{cfg.gitlab_url.rstrip('/')}/{project}.git"
        git_checkout_change(
            clone_url=clone_url,
            ref_fetch=f"refs/merge-requests/{number}/head:refs/remotes/origin/mr-{number}",
            sha=self.head_sha(change),
            dest=dest,
            token=self.token(),
            username="oauth2",
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
        existing = gitlab.find_discussion_by_body(cfg, token, project, number, body)
        if existing:
            return existing
        created = gitlab.create_merge_request_discussion(
            cfg, token, project, number, body, position
        )
        return str(created.get("id") or "")

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
