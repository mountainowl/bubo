"""GitLab provider posting path — MCP preferred, REST fallback (regression: #77).

When #77 consolidated the ``bin/`` launchers it deleted
``bin/mcp-upstream-gitlab`` but left ``mcp.DEFAULT_MCP_SERVER`` pointing at it.
``GitLabProvider.post_inline_comment`` called ``mcp.call_tool`` with no
try/except, so the missing launcher raised ``FileNotFoundError`` (an
``OSError``) *before* the REST fallback — crashing every real GitLab inline
post. These tests pin both the resilience and the repointed default.
"""

from __future__ import annotations

from unittest.mock import patch

from bubo import mcp
from bubo.review_config import ReviewConfig
from bubo.scm import get_provider
from bubo.scm.gitlab import GitLabProvider

_POSITION = {"new_path": "src/A.py", "new_line": 7}


def _cfg() -> ReviewConfig:
    return ReviewConfig(gitlab_url="https://gl.example")


def test_get_provider_returns_gitlab() -> None:
    provider = get_provider(ReviewConfig(provider="gitlab"))
    assert provider.name == "gitlab"
    assert isinstance(provider, GitLabProvider)


def test_default_mcp_server_targets_dispatcher_not_deleted_wrapper() -> None:
    # Regression guard for #77: the deleted bin/mcp-upstream-gitlab must not
    # be the default; the dispatcher (bin/bubo mcp-upstream gitlab) is.
    assert isinstance(mcp.DEFAULT_MCP_SERVER, list)
    assert mcp.DEFAULT_MCP_SERVER[0].endswith("/bin/bubo")
    assert mcp.DEFAULT_MCP_SERVER[1:] == ["mcp-upstream", "gitlab"]


def test_post_inline_comment_falls_back_to_rest_when_mcp_spawn_fails() -> None:
    # The exact #77 crash: a missing launcher raises FileNotFoundError from the
    # MCP spawn. Posting must recover via REST, not propagate the error.
    provider = GitLabProvider()
    with (
        patch("bubo.mcp.call_tool", side_effect=FileNotFoundError("no mcp launcher")),
        patch("bubo.gitlab.find_discussion_by_body", return_value=""),
        patch("bubo.gitlab.create_merge_request_discussion", return_value={"id": "rest-disc-1"}),
    ):
        disc_id = provider.post_inline_comment(_cfg(), "tok", "g/p", 7, "body", _POSITION)
    assert disc_id == "rest-disc-1"


def test_post_inline_comment_falls_back_to_rest_on_mcp_runtime_error() -> None:
    provider = GitLabProvider()
    with (
        patch("bubo.mcp.call_tool", side_effect=RuntimeError("tool mismatch")),
        patch("bubo.gitlab.find_discussion_by_body", return_value="existing-7"),
    ):
        disc_id = provider.post_inline_comment(_cfg(), "tok", "g/p", 7, "body", _POSITION)
    assert disc_id == "existing-7"  # reused an existing discussion via REST


def test_post_inline_comment_prefers_mcp_when_it_returns_id() -> None:
    provider = GitLabProvider()
    with patch("bubo.mcp.call_tool", return_value={"id": "mcp-disc-9"}):
        disc_id = provider.post_inline_comment(_cfg(), "tok", "g/p", 7, "body", _POSITION)
    assert disc_id == "mcp-disc-9"
