"""One project the code host no longer knows must not pause every other project.

A deleted, renamed or never-existing repository answers the open-changes
listing with ``404`` (or ``410``). The poll logs that project as failed with a
structured ``poll_project_failed`` event and carries on with the rest; other
HTTP errors (bad token, server errors) still fail the cycle as before. The
``[[projects]] auto_review`` switch is unchanged: an ``auto_review = false``
project is still skipped before any SCM call.

Drives the real ``poll()`` with the real ``GitHubProvider``; only the GitHub
REST edge (:class:`test_rereview.FakeGitHub`) and the forked worker are faked.
"""

from __future__ import annotations

import io
import urllib.error

import pytest
import test_auto_review
from test_auto_review import use_parsed
from test_rereview import PROJECT, S1, FakeGitHub, status_of

from bubo import poller
from bubo.statuses import ReviewStatus

# Reuse the auto_review suite's fake GitHub edge and forked-worker recorder.
fake = test_auto_review.fake
forked = test_auto_review.forked

MISSING = "o/deleted"


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"https://api.github.com/repos/{MISSING}/pulls", code, "err", {}, io.BytesIO(b"{}")
    )


def _listing_fails_for(monkeypatch: pytest.MonkeyPatch, gh: FakeGitHub, code: int) -> list[str]:
    listed: list[str] = []

    def open_prs(cfg, project, token):
        listed.append(project)
        if project == MISSING:
            raise _http_error(code)
        return gh.open_prs(cfg, project, token)

    monkeypatch.setattr(poller.github, "open_prs", open_prs)
    return listed


def _events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(poller, "log", lambda event, **fields: events.append((event, fields)))
    return events


@pytest.mark.parametrize("code", [404, 410])
def test_missing_project_is_logged_and_the_poll_continues(monkeypatch, fake, forked, code) -> None:
    use_parsed(monkeypatch, {"path": MISSING}, {"path": PROJECT})
    listed = _listing_fails_for(monkeypatch, fake, code)
    events = _events(monkeypatch)

    assert poller.poll() == 1
    assert listed == [MISSING, PROJECT]
    assert forked == [S1]
    assert status_of(S1) == ReviewStatus.QUEUED

    failed = [fields for event, fields in events if event == "poll_project_failed"]
    assert len(failed) == 1
    assert failed[0]["project"] == MISSING
    assert failed[0]["reason"] == "not_found"
    assert failed[0]["status"] == code
    assert failed[0]["poll_run_id"]
    done = [fields for event, fields in events if event == "poll_done"]
    assert done
    assert done[0]["queued"] == 1
    assert done[0]["failed_projects"] == [MISSING]


def test_missing_project_after_a_healthy_one_is_also_logged(monkeypatch, fake, forked) -> None:
    use_parsed(monkeypatch, {"path": PROJECT}, {"path": MISSING})
    _listing_fails_for(monkeypatch, fake, 404)
    events = _events(monkeypatch)

    assert poller.poll() == 1
    assert forked == [S1]
    assert [f["project"] for e, f in events if e == "poll_project_failed"] == [MISSING]


def test_inline_run_once_reviews_the_rest_and_succeeds(monkeypatch, fake) -> None:
    use_parsed(monkeypatch, {"path": MISSING}, {"path": PROJECT})
    _listing_fails_for(monkeypatch, fake, 404)
    ran: list[str] = []

    def worker(job):
        ran.append(job.name)
        return 0

    monkeypatch.setattr(poller, "worker", worker)
    assert poller.run_once() == 0
    assert len(ran) == 1


@pytest.mark.parametrize("code", [401, 500])
def test_other_listing_errors_still_fail_the_poll(monkeypatch, fake, forked, code) -> None:
    # A bad token or a broken host is not "this one repository is gone":
    # fail fast exactly as before instead of logging every project as failed.
    use_parsed(monkeypatch, {"path": MISSING}, {"path": PROJECT})
    _listing_fails_for(monkeypatch, fake, code)

    with pytest.raises(urllib.error.HTTPError):
        poller.poll()
    assert forked == []


def test_auto_review_off_missing_project_is_skipped_not_failed(monkeypatch, fake, forked) -> None:
    use_parsed(monkeypatch, {"path": MISSING, "auto_review": False}, {"path": PROJECT})
    listed = _listing_fails_for(monkeypatch, fake, 404)
    events = _events(monkeypatch)

    assert poller.poll() == 1
    assert listed == [PROJECT]
    names = [(e, f.get("project")) for e, f in events]
    assert ("poll_project_skipped", MISSING) in names
    assert ("poll_project_failed", MISSING) not in names
