"""GitLab provider — REST-only posting + credential-safe ``git`` checkout.

Posting goes straight through the REST API (no MCP). Checkout clones over HTTPS
with the token supplied per-invocation as an auth header, so the credential is
never embedded in the remote URL or written to ``.git/config``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bubo import gitlab
from bubo.review_config import ReviewConfig
from bubo.scm import get_provider
from bubo.scm.base import FindingThreadState
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
        any(part.startswith("http.extraHeader=Authorization: Basic ") for part in a) for a in remote
    )
    # The final step detaches at the head SHA and is local (no auth header).
    assert calls[-1] == ["git", "checkout", "--detach", "abc123"]


def _discussion(
    *,
    root_author: str = "bubo",
    resolved: bool = False,
    deleted: bool = False,
    marker: str = "",
    resolved_by: str | None = "dev",
) -> dict:
    notes = [
        {
            "id": 1,
            "author": {"username": root_author},
            "body": "finding",
            "resolvable": True,
            "resolved": resolved,
            "resolved_by": {"username": resolved_by}
            if resolved_by is not None and resolved
            else None,
        }
    ]
    if marker:
        notes.append({"id": 2, "author": {"username": "bubo"}, "body": marker})
    return {
        "id": "discussion-7",
        "resolved": resolved,
        "deleted": deleted,
        "notes": notes,
    }


def test_finding_thread_rejects_foreign_root() -> None:
    provider = GitLabProvider()
    with patch("bubo.gitlab.get_mr_discussion", return_value=_discussion(root_author="developer")):
        thread = provider.finding_thread(_cfg(), "tok", "g/p", 7, "discussion-7", "bubo", "marker")
    assert thread.state is FindingThreadState.FOREIGN
    assert not thread.reply_marker_present


def test_finding_thread_keeps_unresolved_resolvable_note_open() -> None:
    provider = GitLabProvider()
    discussion = _discussion()
    root = discussion["notes"][0]
    assert root["resolvable"] is True
    assert root["resolved"] is False
    with patch("bubo.gitlab.get_mr_discussion", return_value=discussion):
        thread = provider.finding_thread(_cfg(), "tok", "g/p", 7, "discussion-7", "bubo", "marker")
    assert thread.state is FindingThreadState.OPEN


def test_reply_to_finding_thread_is_write_free_when_resolved() -> None:
    provider = GitLabProvider()
    with (
        patch("bubo.gitlab.get_mr_discussion", return_value=_discussion(resolved=True)),
        patch("bubo.gitlab.create_mr_discussion_note") as create,
    ):
        reply = provider.reply_to_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", "fixed <!-- marker -->", "<!-- marker -->"
        )
    assert reply.reply_id is None
    assert reply.marker_confirmed is False
    assert reply.applied_by_bubo is False
    assert reply.state is FindingThreadState.RESOLVED
    create.assert_not_called()


def test_reply_to_finding_thread_reuses_own_marker() -> None:
    provider = GitLabProvider()
    marker = "<!-- bubo-reconciled:1 -->"
    with (
        patch("bubo.gitlab.get_mr_discussion", return_value=_discussion(marker=marker)),
        patch("bubo.gitlab.create_mr_discussion_note") as create,
    ):
        reply = provider.reply_to_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", f"fixed {marker}", marker
        )
    assert reply.reply_id == "2"
    assert reply.marker_confirmed is True
    assert reply.applied_by_bubo is False
    assert reply.state is FindingThreadState.OPEN
    create.assert_not_called()


def test_reply_to_finding_thread_posts_only_to_owned_open_root() -> None:
    provider = GitLabProvider()
    marker = "<!-- bubo-reconciled:1 -->"
    with (
        patch(
            "bubo.gitlab.get_mr_discussion",
            side_effect=[_discussion(), _discussion(marker=marker)],
        ),
        patch("bubo.gitlab.create_mr_discussion_note", return_value={"id": 44}) as create,
    ):
        reply = provider.reply_to_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", f"fixed {marker}", marker
        )
    assert reply.reply_id == "44"
    assert reply.marker_confirmed is True
    assert reply.applied_by_bubo is True
    create.assert_called_once_with(_cfg(), "tok", "g/p", 7, "discussion-7", f"fixed {marker}")


def test_reply_to_finding_thread_rejects_empty_provider_reply_id() -> None:
    provider = GitLabProvider()
    marker = "<!-- bubo-reconciled:1 -->"
    with (
        patch("bubo.gitlab.get_mr_discussion", return_value=_discussion()),
        patch("bubo.gitlab.create_mr_discussion_note", return_value={}),
        pytest.raises(RuntimeError, match="did not return an ID"),
    ):
        provider.reply_to_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", f"fixed {marker}", marker
        )


def test_reply_to_finding_thread_requires_refetched_marker_confirmation() -> None:
    provider = GitLabProvider()
    marker = "<!-- bubo-reconciled:1 -->"
    with (
        patch(
            "bubo.gitlab.get_mr_discussion",
            side_effect=[_discussion(), _discussion()],
        ),
        patch("bubo.gitlab.create_mr_discussion_note", return_value={"id": 44}),
        pytest.raises(RuntimeError, match="did not confirm Bubo reconciliation reply marker"),
    ):
        provider.reply_to_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", f"fixed {marker}", marker
        )


def test_reply_reports_developer_resolved_state_after_post() -> None:
    provider = GitLabProvider()
    marker = "<!-- bubo-reconciled:1 -->"
    with (
        patch(
            "bubo.gitlab.get_mr_discussion",
            side_effect=[_discussion(), _discussion(resolved=True, marker=marker)],
        ),
        patch("bubo.gitlab.create_mr_discussion_note", return_value={"id": 44}),
    ):
        reply = provider.reply_to_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", f"fixed {marker}", marker
        )
    assert reply.reply_id == "44"
    assert reply.marker_confirmed is True
    assert reply.applied_by_bubo is True
    assert reply.state is FindingThreadState.RESOLVED


def test_reply_reports_foreign_thread_as_a_non_mutating_noop() -> None:
    provider = GitLabProvider()
    marker = "<!-- bubo-reconciled:1 -->"
    with (
        patch("bubo.gitlab.get_mr_discussion", return_value=_discussion(root_author="developer")),
        patch("bubo.gitlab.create_mr_discussion_note") as create,
    ):
        reply = provider.reply_to_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", f"fixed {marker}", marker
        )
    assert reply.state is FindingThreadState.FOREIGN
    assert reply.applied_by_bubo is False
    create.assert_not_called()


def test_resolve_finding_thread_refetches_and_confirms_resolution() -> None:
    provider = GitLabProvider()
    with (
        patch(
            "bubo.gitlab.get_mr_discussion",
            side_effect=[_discussion(), _discussion(resolved=True, resolved_by="bubo")],
        ) as get_discussion,
        patch(
            "bubo.gitlab.resolve_mr_discussion", return_value=_discussion(resolved=True)
        ) as resolve,
    ):
        resolution = provider.resolve_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", "bubo"
        )
    resolve.assert_called_once_with(_cfg(), "tok", "g/p", 7, "discussion-7")
    assert get_discussion.call_count == 2
    assert resolution.state is FindingThreadState.RESOLVED
    assert resolution.applied_by_bubo is True


@pytest.mark.parametrize("resolved_by", ["developer", None])
def test_resolve_does_not_claim_bubo_attribution_without_matching_resolver(
    resolved_by: str | None,
) -> None:
    provider = GitLabProvider()
    with (
        patch(
            "bubo.gitlab.get_mr_discussion",
            side_effect=[_discussion(), _discussion(resolved=True, resolved_by=resolved_by)],
        ),
        patch("bubo.gitlab.resolve_mr_discussion"),
    ):
        resolution = provider.resolve_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", "bubo"
        )
    assert resolution.state is FindingThreadState.RESOLVED
    assert resolution.applied_by_bubo is False


def test_resolve_finding_thread_does_not_write_foreign_root() -> None:
    provider = GitLabProvider()
    with (
        patch("bubo.gitlab.get_mr_discussion", return_value=_discussion(root_author="developer")),
        patch("bubo.gitlab.resolve_mr_discussion") as resolve,
    ):
        resolution = provider.resolve_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", "bubo"
        )
    resolve.assert_not_called()
    assert resolution.state is FindingThreadState.FOREIGN
    assert resolution.applied_by_bubo is False


def test_developer_resolution_race_after_bubo_reply_does_not_put() -> None:
    provider = GitLabProvider()
    marker = "<!-- bubo-reconciled:1 -->"
    with (
        patch(
            "bubo.gitlab.get_mr_discussion",
            side_effect=[_discussion(), _discussion(marker=marker), _discussion(resolved=True)],
        ),
        patch("bubo.gitlab.create_mr_discussion_note", return_value={"id": 44}),
        patch("bubo.gitlab.resolve_mr_discussion") as resolve,
    ):
        reply = provider.reply_to_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", f"fixed {marker}", marker
        )
        resolution = provider.resolve_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", "bubo"
        )
    assert reply.applied_by_bubo is True
    assert resolution.state is FindingThreadState.RESOLVED
    assert resolution.applied_by_bubo is False
    resolve.assert_not_called()


def test_resolve_finding_thread_is_write_free_when_already_resolved() -> None:
    provider = GitLabProvider()
    with (
        patch("bubo.gitlab.get_mr_discussion", return_value=_discussion(resolved=True)),
        patch("bubo.gitlab.resolve_mr_discussion") as resolve,
    ):
        resolution = provider.resolve_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", "bubo"
        )
    resolve.assert_not_called()
    assert resolution.state is FindingThreadState.RESOLVED
    assert resolution.applied_by_bubo is False


def test_deleted_thread_is_not_writable() -> None:
    provider = GitLabProvider()
    marker = "<!-- bubo-reconciled:1 -->"
    with (
        patch("bubo.gitlab.get_mr_discussion", return_value=_discussion(deleted=True)),
        patch("bubo.gitlab.create_mr_discussion_note") as reply_write,
        patch("bubo.gitlab.resolve_mr_discussion") as resolve_write,
    ):
        reply = provider.reply_to_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", f"fixed {marker}", marker
        )
        resolution = provider.resolve_finding_thread(
            _cfg(), "tok", "g/p", 7, "discussion-7", "bubo"
        )
    assert reply.applied_by_bubo is False
    assert reply.marker_confirmed is False
    assert reply.state is FindingThreadState.DELETED
    assert resolution.state is FindingThreadState.DELETED
    assert resolution.applied_by_bubo is False
    reply_write.assert_not_called()
    resolve_write.assert_not_called()


def test_discussion_reply_and_resolve_use_gitlab_thread_endpoints() -> None:
    calls: list[tuple] = []

    def fake_api(base, token, method, path, body=None):
        calls.append((base, token, method, path, body))
        return {"id": "note-8"}, {}

    with patch("bubo.gitlab.api", side_effect=fake_api):
        gitlab.create_mr_discussion_note(_cfg(), "tok", "group/proj", 7, "disc/a", "reply")
        gitlab.resolve_mr_discussion(_cfg(), "tok", "group/proj", 7, "disc/a")

    assert calls == [
        (
            "https://gl.example",
            "tok",
            "POST",
            "/projects/group%2Fproj/merge_requests/7/discussions/disc%2Fa/notes",
            {"body": "reply"},
        ),
        (
            "https://gl.example",
            "tok",
            "PUT",
            "/projects/group%2Fproj/merge_requests/7/discussions/disc%2Fa",
            {"resolved": True},
        ),
    ]


def test_discussion_outcome_records_only_explicit_human_agreement() -> None:
    outcome = gitlab.classify_discussion_outcome(
        {
            "resolved": True,
            "notes": [
                {"author": {"username": "bubo"}, "body": "finding"},
                {
                    "author": {"username": "dev1"},
                    "body": "[llm-review:agreed] fixed",
                    "created_at": "2026-09-30T12:00:00Z",
                },
            ],
        },
        bot_username="bubo",
        mr_state="merged",
    )

    assert outcome["developer_agreed"] is True
    assert outcome["developer_disposition"] == "agrees"
    assert outcome["developer_disposition_evidence_kind"] == "explicit_marker"
    assert outcome["developer_disposition_actor"] == "dev1"
    assert outcome["developer_disposition_at"] == "2026-09-30T12:00:00Z"


def test_discussion_outcome_marks_explicit_dispute_as_disagreement() -> None:
    outcome = gitlab.classify_discussion_outcome(
        {
            "notes": [
                {"author": {"username": "bubo"}, "body": "finding"},
                {
                    "author": {"username": "dev1"},
                    "body": "[llm-review:false-positive] not applicable",
                },
            ],
        },
        bot_username="bubo",
        mr_state="open",
    )

    assert outcome["developer_agreed"] is False
    assert outcome["developer_disposition"] == "false_positive"
    assert outcome["disputed"] is True
    assert outcome["false_positive"] is True


def test_discussion_outcome_does_not_infer_agreement_from_resolution() -> None:
    outcome = gitlab.classify_discussion_outcome(
        {
            "resolved": True,
            "notes": [
                {"author": {"username": "bubo"}, "body": "finding"},
                {"author": {"username": "dev1"}, "body": "implemented"},
            ],
        },
        bot_username="bubo",
        mr_state="merged",
    )

    assert outcome["developer_agreed"] is None
    assert outcome["developer_disposition"] == "unknown"
    assert outcome["disposition_evidence"] == "unknown"
