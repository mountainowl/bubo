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
from bubo.scm.base import (
    FindingThread,
    FindingThreadReply,
    FindingThreadResolution,
    FindingThreadState,
    build_review_contract,
    git_checkout_change,
)
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

    def _finding_thread_snapshot(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        bot_username: str,
        reply_marker: str,
    ) -> tuple[FindingThread, str, str | None]:
        """Read and validate one persisted Bubo discussion before a write.

        GitLab returns the original note first.  We deliberately require that
        note to belong to the configured Bubo account: a stored discussion ID
        must never give Bubo permission to reply to or resolve someone else's
        thread.
        """
        discussion = gitlab.get_mr_discussion(cfg, token, project, number, thread_id)
        raw_notes = discussion.get("notes") or []
        notes = [note for note in raw_notes if isinstance(note, dict)]

        if bool(discussion.get("deleted")):
            return FindingThread(FindingThreadState.DELETED), "", None
        if not notes:
            return FindingThread(FindingThreadState.FOREIGN), "", None

        root = notes[0]
        root_author = (root.get("author") or {}).get("username")
        if root.get("deleted"):
            return FindingThread(FindingThreadState.DELETED), "", None
        if root_author != bot_username:
            return FindingThread(FindingThreadState.FOREIGN), "", None

        marker_found = False
        marker_note_id = ""
        for note in notes[1:]:
            if note.get("deleted"):
                continue
            author = (note.get("author") or {}).get("username")
            if (
                reply_marker
                and author == bot_username
                and reply_marker in str(note.get("body") or "")
            ):
                marker_found = True
                marker_note_id = str(note.get("id") or "")
                break
        resolved_notes = [
            note for note in notes if bool(note.get("resolvable")) and bool(note.get("resolved"))
        ]
        resolved = bool(discussion.get("resolved")) or bool(resolved_notes)
        state = FindingThreadState.RESOLVED if resolved else FindingThreadState.OPEN
        # GitLab carries resolution attribution on the resolvable note. A few
        # self-managed versions have also returned it at discussion level, so
        # retain that fallback only when the note payload lacks a username.
        resolver = None
        for note in resolved_notes:
            resolver = (note.get("resolved_by") or {}).get("username")
            if resolver:
                break
        if not resolver:
            resolver = (discussion.get("resolved_by") or {}).get("username")
        return (
            FindingThread(state, marker_found),
            marker_note_id,
            str(resolver) if resolver else None,
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
        thread, _, _ = self._finding_thread_snapshot(
            cfg, token, project, number, thread_id, bot_username, reply_marker
        )
        return thread

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
        """Reply once to a still-open Bubo-owned discussion."""
        if reply_marker not in body:
            raise ValueError("Bubo reconciliation reply is missing its idempotency marker")
        thread, marker_note_id, _ = self._finding_thread_snapshot(
            cfg,
            token,
            project,
            number,
            thread_id,
            self.bot_username(),
            reply_marker,
        )
        if thread.state is not FindingThreadState.OPEN:
            return FindingThreadReply(None, thread.reply_marker_present, False, thread.state)
        if thread.reply_marker_present:
            return FindingThreadReply(marker_note_id or None, True, False, FindingThreadState.OPEN)
        created = gitlab.create_mr_discussion_note(cfg, token, project, number, thread_id, body)
        reply_id = str(created.get("id") or "")
        if not reply_id:
            raise RuntimeError("GitLab did not return an ID for Bubo reconciliation reply")
        final, _, _ = self._finding_thread_snapshot(
            cfg,
            token,
            project,
            number,
            thread_id,
            self.bot_username(),
            reply_marker,
        )
        if not final.reply_marker_present:
            raise RuntimeError("GitLab did not confirm Bubo reconciliation reply marker")
        return FindingThreadReply(reply_id, True, True, final.state)

    def resolve_finding_thread(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        bot_username: str,
    ) -> FindingThreadResolution:
        """Resolve an open Bubo-owned discussion, then confirm server state."""
        thread, _, _ = self._finding_thread_snapshot(
            cfg, token, project, number, thread_id, bot_username, ""
        )
        if thread.state is not FindingThreadState.OPEN:
            return FindingThreadResolution(thread.state, False)
        gitlab.resolve_mr_discussion(cfg, token, project, number, thread_id)
        final, _, resolved_by = self._finding_thread_snapshot(
            cfg, token, project, number, thread_id, bot_username, ""
        )
        if final.state is not FindingThreadState.RESOLVED:
            raise RuntimeError("GitLab did not confirm Bubo discussion resolution")
        return FindingThreadResolution(FindingThreadState.RESOLVED, resolved_by == bot_username)

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
