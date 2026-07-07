"""GitLab provider — REST-only posting + credential-safe ``git`` checkout.

Posting goes straight through the REST API (no MCP). Checkout clones over HTTPS
with the token supplied per-invocation as an auth header, so the credential is
never embedded in the remote URL or written to ``.git/config``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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


def test_post_inline_comment_reuses_existing_discussion() -> None:
    provider = GitLabProvider()
    with patch("bubo.gitlab.find_discussion_by_body", return_value="existing-7"):
        disc_id = provider.post_inline_comment(_cfg(), "tok", "g/p", 7, "body", _POSITION)
    assert disc_id == "existing-7"


def test_post_inline_comment_creates_via_rest() -> None:
    provider = GitLabProvider()
    with (
        patch("bubo.gitlab.find_discussion_by_body", return_value=""),
        patch("bubo.gitlab.create_merge_request_discussion", return_value={"id": "rest-disc-1"}),
    ):
        disc_id = provider.post_inline_comment(_cfg(), "tok", "g/p", 7, "body", _POSITION)
    assert disc_id == "rest-disc-1"


def test_checkout_clones_credential_safe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-SECRET")
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""

    def fake_run(args: list[str], *, cwd=None, timeout=0):
        calls.append(args)
        return _Result()

    dest = tmp_path / "wt"
    with patch("bubo.scm.base.run_bounded", side_effect=fake_run):
        GitLabProvider().checkout(_cfg(), "grp/sub/proj", {"iid": 7, "sha": "abc123"}, dest)

    clone = next(a for a in calls if "clone" in a)
    # Plain HTTPS URL (sub-groups preserved), and the raw token never appears in argv.
    assert "https://gl.example/grp/sub/proj.git" in clone
    assert not any("glpat-SECRET" in part for part in clone)
    # Every remote-touching call carries the per-invocation auth header.
    remote = [a for a in calls if "-c" in a]
    assert remote
    assert all(
        any(part.startswith("http.extraHeader=Authorization: Basic ") for part in a)
        for a in remote
    )
    # The final step detaches at the head SHA and is local (no auth header).
    assert calls[-1] == ["git", "checkout", "--detach", "abc123"]
