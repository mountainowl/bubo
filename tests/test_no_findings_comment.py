"""Tests for the "no issues found" change-level comment.

Covers the three layers added for issue #13:

* Config parsing — defaults, overrides, and strict type validation of
  ``post_no_findings_comment`` and ``no_findings_comment_body`` in
  ``[agents]``.
* REST helpers — ``find_note_by_body`` / ``create_mr_note`` on GitLab and
  ``find_issue_comment_by_body`` / ``create_issue_comment`` on GitHub,
  including bot-author filtering so a foreign author's identical body
  cannot satisfy the dedup.
* Provider integration — ``GitLabProvider.post_change_comment`` and
  ``GitHubProvider.post_change_comment`` dedup by exact body match scoped
  to the bot.
* Poller helper — ``post_no_findings_comment`` honors the flag, the
  dry-run gate, an empty message, soft-fails on provider errors instead
  of crashing the review, and surfaces a ``posted_pending_id`` verdict
  when the provider call succeeded without returning an ID.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bubo.config_values import ConfigError
from bubo.poller import post_no_findings_comment
from bubo.review_config import (
    DEFAULT_NO_FINDINGS_COMMENT,
    ReviewConfig,
    review_config_from_dict,
)

# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_review_config_defaults_no_findings_comment_to_on() -> None:
    cfg = review_config_from_dict({})

    assert cfg.post_no_findings_comment is True
    assert cfg.no_findings_comment_body == DEFAULT_NO_FINDINGS_COMMENT
    # Sanity: the default body actually communicates the signal.
    assert "no issues" in DEFAULT_NO_FINDINGS_COMMENT.lower()


def test_review_config_parses_custom_no_findings_comment_block() -> None:
    cfg = review_config_from_dict(
        {
            "agents": {
                "post_no_findings_comment": False,
                "no_findings_comment_body": "All good ✅",
            }
        }
    )

    assert cfg.post_no_findings_comment is False
    assert cfg.no_findings_comment_body == "All good ✅"


def test_review_config_rejects_string_for_bool_key() -> None:
    # Operator's classic mistake: `post_no_findings_comment = "false"`.
    # Plain bool() coerces any non-empty string to True, silently
    # inverting intent. ConfigError makes the typo loud.
    with pytest.raises(ConfigError, match="post_no_findings_comment"):
        review_config_from_dict({"agents": {"post_no_findings_comment": "false"}})


def test_review_config_rejects_non_string_for_body_key() -> None:
    # An array or table would otherwise be coerced via str() into a
    # misleading repr and posted verbatim as the comment body.
    with pytest.raises(ConfigError, match="no_findings_comment_body"):
        review_config_from_dict({"agents": {"no_findings_comment_body": ["a", "b"]}})


# ---------------------------------------------------------------------------
# Poller helper — verdict matrix
# ---------------------------------------------------------------------------


def test_post_no_findings_comment_disabled_when_flag_off() -> None:
    cfg = ReviewConfig(post_no_findings_comment=False, dry_run=False)
    provider = MagicMock()

    verdict, detail = post_no_findings_comment(
        cfg=cfg, token="tok", project="grp/repo", number=1, provider=provider
    )

    assert verdict == "disabled"
    assert detail == ""
    provider.post_change_comment.assert_not_called()


def test_post_no_findings_comment_disabled_when_body_is_blank() -> None:
    cfg = ReviewConfig(
        post_no_findings_comment=True,
        no_findings_comment_body="   ",
        dry_run=False,
    )
    provider = MagicMock()

    verdict, detail = post_no_findings_comment(
        cfg=cfg, token="tok", project="grp/repo", number=1, provider=provider
    )

    assert verdict == "disabled"
    assert detail == ""
    provider.post_change_comment.assert_not_called()


def test_post_no_findings_comment_skipped_in_dry_run() -> None:
    cfg = ReviewConfig(post_no_findings_comment=True, dry_run=True)
    provider = MagicMock()

    verdict, detail = post_no_findings_comment(
        cfg=cfg, token="tok", project="grp/repo", number=1, provider=provider
    )

    assert verdict == "skipped_dry_run"
    assert detail == ""
    provider.post_change_comment.assert_not_called()


def test_post_no_findings_comment_posts_when_enabled_and_not_dry_run() -> None:
    cfg = ReviewConfig(
        post_no_findings_comment=True,
        no_findings_comment_body="Reviewer pass ✅",
        dry_run=False,
    )
    provider = MagicMock()
    provider.post_change_comment.return_value = "note-7"

    verdict, detail = post_no_findings_comment(
        cfg=cfg, token="tok", project="grp/repo", number=1, provider=provider
    )

    assert verdict == "posted"
    assert detail == "note-7"
    provider.post_change_comment.assert_called_once_with(
        cfg, "tok", "grp/repo", 1, "Reviewer pass ✅"
    )


def test_post_no_findings_comment_strips_body_before_posting() -> None:
    # Operator left a trailing newline. The gate accepts the stripped
    # body (still non-empty); the post MUST send the stripped form too,
    # or the next dedup check (which compares against the platform's
    # stored body — usually whitespace-normalized) will miss and stack
    # a duplicate comment.
    cfg = ReviewConfig(
        post_no_findings_comment=True,
        no_findings_comment_body="   Reviewer pass ✅   \n",
        dry_run=False,
    )
    provider = MagicMock()
    provider.post_change_comment.return_value = "note-7"

    post_no_findings_comment(cfg=cfg, token="tok", project="grp/repo", number=1, provider=provider)

    provider.post_change_comment.assert_called_once_with(
        cfg, "tok", "grp/repo", 1, "Reviewer pass ✅"
    )


def test_post_no_findings_comment_surfaces_posted_pending_when_id_blank() -> None:
    # Provider succeeded but returned "" (2xx without `id`). The helper
    # must distinguish this from a healthy post so observability still
    # reflects the partial state.
    cfg = ReviewConfig(post_no_findings_comment=True, dry_run=False)
    provider = MagicMock()
    provider.post_change_comment.return_value = ""

    verdict, detail = post_no_findings_comment(
        cfg=cfg, token="tok", project="grp/repo", number=1, provider=provider
    )

    assert verdict == "posted_pending_id"
    assert detail == ""


def test_post_no_findings_comment_soft_fails_on_provider_error() -> None:
    # Pre-fix: a transient 5xx from the comment-post API flipped a
    # successful clean review to FAILED via the worker's outer except.
    # Post-fix: the helper catches the exception, returns "errored",
    # the worker still records NO_FINDINGS.
    cfg = ReviewConfig(post_no_findings_comment=True, dry_run=False)
    provider = MagicMock()
    provider.post_change_comment.side_effect = RuntimeError("GitLab 502")

    verdict, detail = post_no_findings_comment(
        cfg=cfg, token="tok", project="grp/repo", number=1, provider=provider
    )

    assert verdict == "errored"
    assert "GitLab 502" in detail


# ---------------------------------------------------------------------------
# GitLab REST helpers
# ---------------------------------------------------------------------------


def test_gitlab_find_note_by_body_matches_exact_body() -> None:
    from bubo import gitlab

    notes = [
        {"id": 1, "system": True, "body": "matched marker"},  # ignored: system note
        {"id": 2, "body": "other body"},
        {"id": 3, "body": "matched marker"},
    ]
    with patch("bubo.gitlab.get_mr_notes", return_value=notes):
        note_id = gitlab.find_note_by_body(ReviewConfig(), "tok", "grp/repo", 1, "matched marker")

    assert note_id == "3"


def test_gitlab_find_note_by_body_returns_empty_when_no_match() -> None:
    from bubo import gitlab

    with patch("bubo.gitlab.get_mr_notes", return_value=[{"id": 1, "body": "nope"}]):
        note_id = gitlab.find_note_by_body(ReviewConfig(), "tok", "grp/repo", 1, "missing")

    assert note_id == ""


def test_gitlab_find_note_by_body_filters_by_bot_username() -> None:
    # A human reply that quotes the bot's body must NOT satisfy the
    # dedup, or the bot will silently stop posting its own
    # acknowledgement on subsequent re-reviews.
    from bubo import gitlab

    notes = [
        {"id": 7, "body": "match", "author": {"username": "human-reviewer"}},
        {"id": 8, "body": "match", "author": {"username": "bubo"}},
    ]
    with patch("bubo.gitlab.get_mr_notes", return_value=notes):
        note_id = gitlab.find_note_by_body(
            ReviewConfig(), "tok", "grp/repo", 1, "match", bot_username="bubo"
        )

    assert note_id == "8"


# ---------------------------------------------------------------------------
# GitLab provider — dedup vs. create
# ---------------------------------------------------------------------------


def test_gitlab_provider_post_change_comment_reuses_existing_note() -> None:
    from bubo.scm.gitlab import GitLabProvider

    with patch("bubo.gitlab.find_note_by_body", return_value="note-existing") as finder:
        with patch("bubo.gitlab.create_mr_note") as creator:
            note_id = GitLabProvider().post_change_comment(
                ReviewConfig(), "tok", "grp/repo", 1, "body"
            )

    assert note_id == "note-existing"
    finder.assert_called_once()
    # Bot-username filter is threaded through — fail loud if it ever stops being passed.
    assert finder.call_args.kwargs.get("bot_username")
    creator.assert_not_called()


def test_gitlab_provider_post_change_comment_creates_when_missing() -> None:
    from bubo.scm.gitlab import GitLabProvider

    with patch("bubo.gitlab.find_note_by_body", return_value=""):
        with patch("bubo.gitlab.create_mr_note", return_value={"id": 42}) as creator:
            note_id = GitLabProvider().post_change_comment(
                ReviewConfig(), "tok", "grp/repo", 1, "body"
            )

    assert note_id == "42"
    creator.assert_called_once_with(ReviewConfig(), "tok", "grp/repo", 1, "body")


def test_gitlab_provider_post_change_comment_returns_blank_when_create_lacks_id() -> None:
    from bubo.scm.gitlab import GitLabProvider

    with patch("bubo.gitlab.find_note_by_body", return_value=""):
        with patch("bubo.gitlab.create_mr_note", return_value={}):
            note_id = GitLabProvider().post_change_comment(
                ReviewConfig(), "tok", "grp/repo", 1, "body"
            )

    assert note_id == ""


# ---------------------------------------------------------------------------
# GitHub REST helpers + provider
# ---------------------------------------------------------------------------


def test_github_find_issue_comment_by_body_matches_exact_body() -> None:
    from bubo import github

    comments = [
        {"id": 11, "body": "different"},
        {"id": 22, "body": "match"},
    ]
    with patch("bubo.github.get_issue_comments", return_value=comments):
        comment_id = github.find_issue_comment_by_body(
            ReviewConfig(provider="github"), "tok", "owner/repo", 5, "match"
        )

    assert comment_id == "22"


def test_github_find_issue_comment_by_body_filters_by_bot_username() -> None:
    # Foreign-bot or human comments that quote the body must not match.
    from bubo import github

    comments = [
        {"id": 100, "body": "match", "user": {"login": "dependabot"}},
        {"id": 200, "body": "match", "user": {"login": "bubo"}},
    ]
    with patch("bubo.github.get_issue_comments", return_value=comments):
        comment_id = github.find_issue_comment_by_body(
            ReviewConfig(provider="github"),
            "tok",
            "owner/repo",
            5,
            "match",
            bot_username="bubo",
        )

    assert comment_id == "200"


def test_github_provider_post_change_comment_reuses_existing_comment() -> None:
    from bubo.scm.github import GitHubProvider

    with patch("bubo.github.find_issue_comment_by_body", return_value="comment-existing") as finder:
        with patch("bubo.github.create_issue_comment") as creator:
            comment_id = GitHubProvider().post_change_comment(
                ReviewConfig(provider="github"), "tok", "owner/repo", 5, "body"
            )

    assert comment_id == "comment-existing"
    finder.assert_called_once()
    assert finder.call_args.kwargs.get("bot_username")
    creator.assert_not_called()


def test_github_provider_post_change_comment_creates_when_missing() -> None:
    from bubo.scm.github import GitHubProvider

    with patch("bubo.github.find_issue_comment_by_body", return_value=""):
        with patch("bubo.github.create_issue_comment", return_value={"id": 99}) as creator:
            comment_id = GitHubProvider().post_change_comment(
                ReviewConfig(provider="github"), "tok", "owner/repo", 5, "body"
            )

    assert comment_id == "99"
    creator.assert_called_once_with(ReviewConfig(provider="github"), "tok", "owner/repo", 5, "body")


# ---------------------------------------------------------------------------
# env.example.toml documents the new keys
# ---------------------------------------------------------------------------


def test_env_example_documents_no_findings_comment_keys() -> None:
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / "config" / "env.example.toml").read_text())
    agents = cfg.get("agents", {})

    assert agents.get("post_no_findings_comment") is True
    assert agents.get("no_findings_comment_body") == DEFAULT_NO_FINDINGS_COMMENT
