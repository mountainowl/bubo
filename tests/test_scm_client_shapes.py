"""SCM REST clients: response-shape errors and GitHub paging, pinned.

The GitHub and GitLab clients share one unexpected-shape error
(``_http.expect_dict`` / ``expect_list``) and GitHub's list endpoints share one
lazy page iterator. These tests pin the exact operator-facing messages and
the exact request sequence so the shared helpers stay byte-identical to the
per-call-site code they replaced. Only ``_http.request_json`` is faked.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

from bubo import github, gitlab
from bubo.review_config import ReviewConfig

CFG = ReviewConfig(github_api_url="https://api.github.test", gitlab_url="https://gl.test")
SHAPE = (
    " | why: the API returned an unexpected response shape (often an auth error page, a "
    "redirect, or an outage rendered as non-JSON) | fix: check the API URL, token "
    "validity/scope, and the host's status; inspect the raw response."
)


@pytest.mark.parametrize(
    ("call", "response", "summary"),
    [
        (
            lambda: github.api_pages(CFG.github_api_url, "t", "/x"),
            {"message": "Bad credentials"},
            "GitHub API page did not return a list",
        ),
        (lambda: github.get_pr(CFG, "t", "o/r", 1), [], "GitHub PR response was not an object"),
        (
            lambda: github.get_pr_review_comment(CFG, "t", "o/r", "5"),
            [],
            "GitHub review-comment response was not an object",
        ),
        (
            lambda: github.graphql(CFG.github_api_url, "t", "q", {}),
            [],
            "GitHub GraphQL response was not an object",
        ),
        (
            lambda: github.graphql(CFG.github_api_url, "t", "q", {}),
            {"data": None},
            "GitHub GraphQL response missing data",
        ),
        (
            lambda: github.pulls_updated_after(CFG, "o/r", "t", "2026"),
            {},
            "GitHub API page did not return a list",
        ),
        (
            lambda: gitlab.api_pages(CFG.gitlab_url, "t", "/x"),
            {},
            "GitLab API page did not return a list",
        ),
        (lambda: gitlab.get_mr(CFG, "t", "o/r", 1), [], "GitLab MR response was not an object"),
        (
            lambda: gitlab.get_mr_discussion(CFG, "t", "o/r", 1, "d"),
            [],
            "GitLab discussion response was not an object",
        ),
    ],
)
def test_unexpected_shape_message(call: Callable[[], Any], response: Any, summary: str) -> None:
    with (
        patch("bubo._http.request_json", return_value=(response, {})),
        pytest.raises(RuntimeError) as exc,
    ):
        call()
    assert str(exc.value) == summary + SHAPE


# Three pages of PRs, newest first (30..22), each with a non-object entry that
# must be ignored.
PAGES = [
    [{"number": n, "updated_at": f"2026-10-{n:02d}"} for n in range(start, start - 3, -1)]
    + ["junk"]
    for start in (30, 27, 24)
]
FIRST = "https://api.github.test/repos/o/r/pulls?state=all&sort=updated&direction=desc&per_page=100"


def _paged(trace: list[str]):
    def respond(url: str, **_: Any) -> tuple[Any, dict[str, str]]:
        trace.append(url)
        index = len(trace) - 1
        more = index < len(PAGES) - 1
        link = {"Link": f'<https://api.github.test/next?page={index + 2}>; rel="next"'}
        return PAGES[index], link if more else {}

    return respond


@pytest.mark.parametrize(
    ("cutoff", "numbers", "requests"),
    [
        ("2026-10-29", [30, 29], 1),  # stops inside page 1
        ("2026-10-28", [30, 29, 28], 2),  # page 1 all newer: page 2 needed to see 27
        ("2026-10-25", [30, 29, 28, 27, 26, 25], 3),
        ("2026-10-01", [30, 29, 28, 27, 26, 25, 24, 23, 22], 3),  # never stops early
    ],
)
def test_pulls_updated_after_stops_paging_at_cutoff(
    cutoff: str, numbers: list[int], requests: int
) -> None:
    trace: list[str] = []
    with patch("bubo._http.request_json", side_effect=_paged(trace)):
        got = github.pulls_updated_after(CFG, "o/r", "t", cutoff)
    assert [pr["number"] for pr in got] == numbers
    expected = [FIRST, "https://api.github.test/next?page=2", "https://api.github.test/next?page=3"]
    assert trace == expected[:requests]


def test_api_pages_follows_every_next_link() -> None:
    trace: list[str] = []
    with patch("bubo._http.request_json", side_effect=_paged(trace)):
        got = github.api_pages(CFG.github_api_url, "t", "/x?a=1")
    assert len(got) == 9
    assert trace[0] == "https://api.github.test/x?a=1&per_page=100"
    assert len(trace) == 3
