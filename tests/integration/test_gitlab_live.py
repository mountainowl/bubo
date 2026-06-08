"""Live GitLab integration tests — strictly READ ONLY.

These hit a real GitLab instance to catch API-shape drift that
fixture-based unit tests cannot: a renamed field in the MR payload, a
changed discussions shape, a pagination-header rename. The original code
review found a ``classify_discussion_outcome`` bug that assumed fields the
real API does not return on a discussion object — exactly the class of bug
these tests exist to catch.

They are skipped unless the operator supplies credentials:

    BUBO_IT_GITLAB_TOKEN     PAT with ``read_api`` scope (read-only is enough)
    BUBO_IT_GITLAB_PROJECT   project path, e.g. "group/repo"
    BUBO_IT_GITLAB_URL       optional; defaults to https://gitlab.com

Nothing here mutates GitLab — no comments are posted, no threads created.
Run locally with:

    BUBO_IT_GITLAB_TOKEN=glpat-... \\
    BUBO_IT_GITLAB_PROJECT=group/repo \\
        uv run pytest -m integration -v
"""

from __future__ import annotations

import os
import urllib.parse

import pytest

from bubo import gitlab
from bubo.review_config import ReviewConfig

pytestmark = pytest.mark.integration

_TOKEN = os.environ.get("BUBO_IT_GITLAB_TOKEN")
_PROJECT = os.environ.get("BUBO_IT_GITLAB_PROJECT")
_URL = os.environ.get("BUBO_IT_GITLAB_URL", "https://gitlab.com")

# Every test in this module skips when credentials are absent. This keeps a
# bare `pytest -m integration` green on a developer machine with no setup
# while still exercising the live API in CI when secrets are configured.
_skip_no_creds = pytest.mark.skipif(
    not (_TOKEN and _PROJECT),
    reason="set BUBO_IT_GITLAB_TOKEN and BUBO_IT_GITLAB_PROJECT to run",
)

# Fields the runtime reads off each payload. If GitLab renames or drops one
# of these, the relevant test fails loudly with the offending key named.
_REQUIRED_MR_KEYS = {"iid", "title", "state"}
_REQUIRED_DIFF_REF_KEYS = {"base_sha", "start_sha", "head_sha"}
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
    return ReviewConfig(gitlab_url=_URL)


@_skip_no_creds
def test_open_mrs_returns_list_of_mr_dicts(cfg: ReviewConfig) -> None:
    """open_mrs must return a list (catches auth + pagination breakage)."""
    mrs = gitlab.open_mrs(cfg, _PROJECT, _TOKEN)
    assert isinstance(mrs, list)
    for mr in mrs:
        assert isinstance(mr, dict)
        missing = _REQUIRED_MR_KEYS - mr.keys()
        assert not missing, f"open MR payload missing keys: {missing}"


@_skip_no_creds
def test_get_mr_and_diffs_expose_fields_the_runtime_needs(cfg: ReviewConfig) -> None:
    """get_mr exposes diff_refs (build_position needs base/start/head sha)."""
    mrs = gitlab.open_mrs(cfg, _PROJECT, _TOKEN)
    if not mrs:
        pytest.skip("no open MRs in the configured test project")
    iid = int(mrs[0]["iid"])

    mr = gitlab.get_mr(cfg, _TOKEN, _PROJECT, iid)
    assert int(mr["iid"]) == iid
    refs = mr.get("diff_refs") or {}
    missing_refs = _REQUIRED_DIFF_REF_KEYS - refs.keys()
    assert not missing_refs, f"diff_refs missing keys build_position needs: {missing_refs}"

    diffs = gitlab.get_mr_diffs(cfg, _TOKEN, _PROJECT, iid)
    assert isinstance(diffs, list)
    for diff in diffs:
        # changed_lines_from_diffs reads new_path/old_path and diff text.
        assert "new_path" in diff or "old_path" in diff


@_skip_no_creds
def test_discussion_payload_classifies_without_error(cfg: ReviewConfig) -> None:
    """A real discussion payload must classify into the expected outcome keys.

    This is the regression guard for the original ``classify_discussion_outcome``
    field-shape bug: if GitLab's discussion/note shape drifts, the classifier
    either raises here or returns the wrong key set.
    """
    mrs = gitlab.open_mrs(cfg, _PROJECT, _TOKEN)
    if not mrs:
        pytest.skip("no open MRs in the configured test project")
    iid = int(mrs[0]["iid"])
    encoded = urllib.parse.quote(_PROJECT, safe="")
    discussions = gitlab.api_pages(
        cfg.gitlab_url,
        _TOKEN,
        f"/projects/{encoded}/merge_requests/{iid}/discussions",
    )
    if not discussions:
        pytest.skip("no discussions on the first open MR")

    outcome = gitlab.classify_discussion_outcome(
        discussions[0], bot_username="bubo", mr_state="opened"
    )
    assert outcome.keys() >= _OUTCOME_KEYS, (
        f"classifier output missing keys: {_OUTCOME_KEYS - outcome.keys()}"
    )


@_skip_no_creds
def test_pagination_terminates_and_dedupes(cfg: ReviewConfig) -> None:
    """api_pages must terminate and not loop forever on the discussions endpoint."""
    mrs = gitlab.open_mrs(cfg, _PROJECT, _TOKEN)
    if not mrs:
        pytest.skip("no open MRs in the configured test project")
    iid = int(mrs[0]["iid"])
    encoded = urllib.parse.quote(_PROJECT, safe="")
    discussions = gitlab.api_pages(
        cfg.gitlab_url,
        _TOKEN,
        f"/projects/{encoded}/merge_requests/{iid}/discussions",
    )
    ids = [d.get("id") for d in discussions if d.get("id")]
    # If pagination double-counted a page, ids would contain duplicates.
    assert len(ids) == len(set(ids)), "api_pages returned duplicate discussion IDs"
