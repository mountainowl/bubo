"""Live GitHub integration tests — strictly READ ONLY.

These hit a real GitHub instance to catch API-shape drift that
fixture-based unit tests cannot: a renamed field in the PR payload, a
changed review-comment shape, a pagination-header rename. They mirror the
GitLab live tests (``test_gitlab_live.py``) for the GitHub provider.

They are skipped unless the operator supplies credentials:

    BUBO_IT_GITHUB_TOKEN     PAT with ``repo`` (or read-only) scope
    BUBO_IT_GITHUB_PROJECT   owner/repo, e.g. "octocat/Hello-World"
    BUBO_IT_GITHUB_API_URL   optional; defaults to https://api.github.com

Nothing here mutates GitHub — no comments are posted, no threads created.
Run locally with:

    BUBO_IT_GITHUB_TOKEN=ghp_... \\
    BUBO_IT_GITHUB_PROJECT=owner/repo \\
        uv run pytest -m integration -v
"""

from __future__ import annotations

import os

import pytest

from bubo import github
from bubo.review_config import ReviewConfig
from bubo.scm.github import GitHubProvider

pytestmark = pytest.mark.integration

_TOKEN = os.environ.get("BUBO_IT_GITHUB_TOKEN")
_PROJECT = os.environ.get("BUBO_IT_GITHUB_PROJECT")
_API_URL = os.environ.get("BUBO_IT_GITHUB_API_URL", "https://api.github.com")

# Every test in this module skips when credentials are absent. This keeps a
# bare `pytest -m integration` green on a developer machine with no setup
# while still exercising the live API in CI when secrets are configured.
_skip_no_creds = pytest.mark.skipif(
    not (_TOKEN and _PROJECT),
    reason="set BUBO_IT_GITHUB_TOKEN and BUBO_IT_GITHUB_PROJECT to run",
)

# Fields the runtime reads off each payload. If GitHub renames or drops one
# of these, the relevant test fails loudly with the offending key named.
_REQUIRED_PR_KEYS = {"number", "title", "state", "head", "base"}
_OUTCOME_KEYS = {
    "resolved",
    "deleted",
    "developer_replied",
    "disputed",
    "false_positive",
    "duplicate",
    "resolved_at",
    "merged_unresolved",
}


@pytest.fixture
def cfg() -> ReviewConfig:
    return ReviewConfig(provider="github", github_api_url=_API_URL)


@_skip_no_creds
def test_open_prs_returns_list_of_pr_dicts(cfg: ReviewConfig) -> None:
    """open_prs must return a list (catches auth + Link pagination breakage)."""
    prs = github.open_prs(cfg, _PROJECT, _TOKEN)
    assert isinstance(prs, list)
    for pr in prs:
        assert isinstance(pr, dict)
        missing = _REQUIRED_PR_KEYS - pr.keys()
        assert not missing, f"open PR payload missing keys: {missing}"


@_skip_no_creds
def test_get_pr_and_files_expose_fields_the_runtime_needs(cfg: ReviewConfig) -> None:
    """get_pr exposes head.sha (build_position needs it) and files carry patches."""
    prs = github.open_prs(cfg, _PROJECT, _TOKEN)
    if not prs:
        pytest.skip("no open PRs in the configured test project")
    provider = GitHubProvider()
    number = provider.change_number(prs[0])

    pr = github.get_pr(cfg, _TOKEN, _PROJECT, number)
    assert provider.change_number(pr) == number
    assert provider.head_sha(pr), "PR head.sha missing — build_position cannot anchor comments"

    files = github.get_pr_files(cfg, _TOKEN, _PROJECT, number)
    assert isinstance(files, list)
    for entry in files:
        # changed_lines_from_files reads filename and patch text.
        assert "filename" in entry


@_skip_no_creds
def test_changed_lines_and_build_position_round_trip(cfg: ReviewConfig) -> None:
    """changed_lines + build_position must produce a placeable GitHub anchor."""
    prs = github.open_prs(cfg, _PROJECT, _TOKEN)
    if not prs:
        pytest.skip("no open PRs in the configured test project")
    provider = GitHubProvider()
    number = provider.change_number(prs[0])
    change = github.get_pr(cfg, _TOKEN, _PROJECT, number)

    changed = provider.changed_lines(cfg, _TOKEN, _PROJECT, number)
    placeable = next(
        ((path, line) for path, info in changed.items() for line in info.get("new_lines", set())),
        None,
    )
    if placeable is None:
        pytest.skip("no added lines in the first open PR's diff")
    path, line = placeable

    position = provider.build_position(change, changed, {"file": path, "line": line})
    assert position is not None, "an added diff line must map to a GitHub position"
    assert position["path"] == path
    assert position["line"] == line
    assert position["side"] == "RIGHT"
    assert position["commit_id"], "position must carry the head commit_id"


@_skip_no_creds
def test_review_comment_payload_classifies_without_error(cfg: ReviewConfig) -> None:
    """A real review-comment payload must classify into the expected keys.

    This is the GitHub analogue of the GitLab discussion-shape regression
    guard: if GitHub's review-comment shape drifts, the classifier either
    raises here or returns the wrong key set.
    """
    prs = github.open_prs(cfg, _PROJECT, _TOKEN)
    if not prs:
        pytest.skip("no open PRs in the configured test project")
    provider = GitHubProvider()
    number = provider.change_number(prs[0])

    comments = github.get_pr_review_comments(cfg, _TOKEN, _PROJECT, number)
    if not comments:
        pytest.skip("no review comments on the first open PR")
    root = comments[0]
    root_id = root.get("id")
    replies = [c for c in comments if c.get("in_reply_to_id") == root_id]

    outcome = github.classify_review_thread_outcome(
        root, replies, bot_username="bubo", pr_state=str(prs[0].get("state") or "open")
    )
    assert outcome.keys() >= _OUTCOME_KEYS, (
        f"classifier output missing keys: {_OUTCOME_KEYS - outcome.keys()}"
    )


@_skip_no_creds
def test_review_threads_graphql_exposes_resolution(cfg: ReviewConfig) -> None:
    """GraphQL review threads must expose isResolved + comment ids.

    Regression guard for the resolution-sync path: if GitHub's GraphQL
    reviewThreads shape drifts (isResolved renamed, databaseId dropped),
    the normalized thread loses the fields fetch_outcome depends on.
    """
    prs = github.open_prs(cfg, _PROJECT, _TOKEN)
    if not prs:
        pytest.skip("no open PRs in the configured test project")
    number = GitHubProvider().change_number(prs[0])

    threads = github.get_pr_review_threads(cfg, _TOKEN, _PROJECT, number)
    assert isinstance(threads, list)
    if not threads:
        pytest.skip("no review threads on the first open PR")
    for thread in threads:
        assert isinstance(thread["is_resolved"], bool)
        for comment in thread["comments"]:
            # At least one stable id must be present to correlate at sync time.
            assert comment.get("database_id") is not None or comment.get("node_id")

    outcome = github.classify_graphql_thread_outcome(
        threads[0], bot_username="bubo", pr_state=str(prs[0].get("state") or "open")
    )
    assert outcome.keys() >= _OUTCOME_KEYS, (
        f"classifier output missing keys: {_OUTCOME_KEYS - outcome.keys()}"
    )


@_skip_no_creds
def test_pagination_terminates_and_dedupes(cfg: ReviewConfig) -> None:
    """api_pages must terminate and not double-count on the comments endpoint."""
    prs = github.open_prs(cfg, _PROJECT, _TOKEN)
    if not prs:
        pytest.skip("no open PRs in the configured test project")
    number = GitHubProvider().change_number(prs[0])
    comments = github.get_pr_review_comments(cfg, _TOKEN, _PROJECT, number)
    ids = [c.get("id") for c in comments if c.get("id")]
    # If Link pagination double-counted a page, ids would contain duplicates.
    assert len(ids) == len(set(ids)), "api_pages returned duplicate comment IDs"
