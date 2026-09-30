"""Unit tests for the GitHub REST client and GitHub provider."""

from __future__ import annotations

from unittest.mock import patch

from bubo import github
from bubo.review_config import ReviewConfig
from bubo.scm import get_provider
from bubo.scm.base import FindingThreadReply, FindingThreadResolution, FindingThreadState
from bubo.scm.github import GitHubProvider


def test_get_provider_returns_github_for_github_config() -> None:
    provider = get_provider(ReviewConfig(provider="github"))
    assert provider.name == "github"
    assert isinstance(provider, GitHubProvider)


def test_next_link_parses_rel_next() -> None:
    headers = {
        "Link": '<https://api.github.com/x?page=2>; rel="next", '
        '<https://api.github.com/x?page=9>; rel="last"'
    }
    assert github._next_link(headers) == "https://api.github.com/x?page=2"


def test_next_link_none_when_absent() -> None:
    assert github._next_link({}) is None
    assert github._next_link({"Link": '<https://x>; rel="last"'}) is None


def test_api_pages_follows_link_header() -> None:
    pages = {
        "https://api.github.com/repos/o/r/pulls?state=open&per_page=100": (
            [{"number": 1}],
            {"Link": '<https://api.github.com/p2>; rel="next"'},
        ),
        "https://api.github.com/p2": ([{"number": 2}], {}),
    }

    def fake_request(url, token, method, body=None):
        return pages[url]

    with patch("bubo.github._request", side_effect=fake_request):
        cfg = ReviewConfig(provider="github")
        prs = github.open_prs(cfg, "o/r", "token")

    assert [pr["number"] for pr in prs] == [1, 2]


def test_provider_change_number_and_head_sha() -> None:
    provider = GitHubProvider()
    change = {"number": 42, "head": {"sha": "deadbeef"}}
    assert provider.change_number(change) == 42
    assert provider.head_sha(change) == "deadbeef"
    assert provider.head_sha({"number": 1}) == ""


def test_provider_changed_lines_from_github_files() -> None:
    provider = GitHubProvider()
    files = [{"filename": "src/A.py", "patch": "@@ -1,1 +1,2 @@\n old\n+new\n"}]
    with patch("bubo.github.get_pr_files", return_value=files):
        changed = provider.changed_lines(ReviewConfig(provider="github"), "tok", "o/r", 1)
    assert 2 in changed["src/A.py"]["new_lines"]
    assert 1 not in changed["src/A.py"]["new_lines"]


def test_provider_build_position_uses_line_and_side() -> None:
    provider = GitHubProvider()
    change = {"head": {"sha": "abc123"}}
    changed = {"src/A.py": {"new_path": "src/A.py", "old_path": "src/A.py", "new_lines": {7}}}

    position = provider.build_position(change, changed, {"file": "src/A.py", "line": 7})
    assert position == {"commit_id": "abc123", "path": "src/A.py", "line": 7, "side": "RIGHT"}

    # Line not in the diff → not placeable.
    assert provider.build_position(change, changed, {"file": "src/A.py", "line": 99}) is None
    # No head sha → not placeable.
    assert provider.build_position({}, changed, {"file": "src/A.py", "line": 7}) is None


def test_provider_post_creates_via_rest() -> None:
    provider = GitHubProvider()
    position = {"commit_id": "abc", "path": "src/A.py", "line": 7, "side": "RIGHT"}

    with patch("bubo.github.find_review_comment_by_body", return_value=""):
        with patch(
            "bubo.github.create_pr_review_comment",
            return_value={"id": "gh-comment-1"},
        ):
            comment_id = provider.post_inline_comment(
                ReviewConfig(provider="github"), "tok", "o/r", 5, "body", position
            )

    assert comment_id == "gh-comment-1"


def test_provider_post_reuses_existing_review_comment() -> None:
    provider = GitHubProvider()
    position = {"commit_id": "abc", "path": "src/A.py", "line": 7, "side": "RIGHT"}

    with patch("bubo.github.find_review_comment_by_body", return_value="existing-5"):
        comment_id = provider.post_inline_comment(
            ReviewConfig(provider="github"), "tok", "o/r", 5, "body", position
        )

    assert comment_id == "existing-5"


def test_checkout_clones_credential_safe(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-SECRET")
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = ""

    def fake_run(args: list[str], *, cwd=None, timeout=0):
        calls.append(args)
        return _Result()

    # GitHub Enterprise api_url -> the web host bubo clones from is the netloc.
    cfg = ReviewConfig(provider="github", github_api_url="https://ghe.example.com/api/v3")
    with patch("bubo.scm.base.run_bounded", side_effect=fake_run):
        GitHubProvider().checkout(
            cfg, "o/r", {"number": 5, "head": {"sha": "abc123"}}, tmp_path / "wt"
        )

    clone = next(a for a in calls if "clone" in a)
    assert "https://ghe.example.com/o/r.git" in clone
    assert not any("ghp-SECRET" in part for part in clone)
    remote = [a for a in calls if "-c" in a]
    assert remote
    assert all(
        any(part.startswith("http.extraHeader=Authorization: Basic ") for part in a)
        for a in remote
    )
    assert calls[-1] == ["git", "checkout", "--detach", "abc123"]


def test_classify_review_thread_outcome_reads_markers_and_replies() -> None:
    comment = {"id": "c1", "body": "finding"}
    replies = [
        {"user": {"login": "dev1", "type": "User"}, "body": "[llm-review:false-positive] nope"},
    ]
    outcome = github.classify_review_thread_outcome(
        comment, replies, bot_username="bubo", pr_state="merged"
    )
    assert outcome["developer_replied"] is True
    assert outcome["false_positive"] is True
    assert outcome["disputed"] is True
    # Resolution is GraphQL-only; REST classifier reports unresolved.
    assert outcome["resolved"] is False
    assert outcome["merged_unresolved"] is True


def test_graphql_url_derivation() -> None:
    assert github._graphql_url("https://api.github.com") == "https://api.github.com/graphql"
    assert github._graphql_url("https://api.github.com/") == "https://api.github.com/graphql"
    # GitHub Enterprise: REST /api/v3 -> GraphQL /api/graphql on the same host.
    assert github._graphql_url("https://ghe.example.com/api/v3") == (
        "https://ghe.example.com/api/graphql"
    )


def test_graphql_raises_on_query_errors() -> None:
    body = {"data": None, "errors": [{"message": "Field 'foo' doesn't exist"}]}
    with patch("bubo.github._request", return_value=(body, {})):
        try:
            github.graphql("https://api.github.com", "tok", "query{}", {})
        except RuntimeError as exc:
            assert "doesn't exist" in str(exc)
        else:  # pragma: no cover - guard
            raise AssertionError("graphql() must raise on a non-empty errors array")


def test_get_pr_review_threads_paginates_and_normalizes() -> None:
    page1 = {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "C1"},
                    "nodes": [
                        {
                            "isResolved": True,
                            "comments": {
                                "nodes": [
                                    {
                                        "databaseId": 100,
                                        "id": "PRRC_a",
                                        "author": {"login": "bubo"},
                                        "body": "finding",
                                        "path": "src/A.py",
                                        "line": 7,
                                    }
                                ]
                            },
                        }
                    ],
                }
            }
        }
    }
    page2 = {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "isResolved": False,
                            "comments": {"nodes": [{"databaseId": 200, "id": "PRRC_b"}]},
                        }
                    ],
                }
            }
        }
    }
    with patch("bubo.github.graphql", side_effect=[page1, page2]) as mock_graphql:
        threads = github.get_pr_review_threads(ReviewConfig(provider="github"), "tok", "o/r", 5)

    assert mock_graphql.call_count == 2
    assert [t["is_resolved"] for t in threads] == [True, False]
    first = threads[0]["comments"][0]
    assert first["database_id"] == 100
    assert first["node_id"] == "PRRC_a"
    assert first["login"] == "bubo"
    assert first["path"] == "src/A.py"
    assert first["line"] == 7


def test_get_pr_review_threads_fetches_marker_after_first_100_thread_comments() -> None:
    initial_comments = [
        {
            "databaseId": 100,
            "id": "PRRC_root",
            "author": {"login": "bubo"},
            "body": "finding",
        }
    ] + [
        {
            "databaseId": index,
            "id": f"PRRC_{index}",
            "author": {"login": "dev"},
            "body": "reply",
            "replyTo": {"id": "PRRC_root"},
        }
        for index in range(101, 200)
    ]
    first_page = {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "id": "PRRT_1",
                            "isResolved": False,
                            "comments": {
                                "pageInfo": {"hasNextPage": True, "endCursor": "COMMENTS_1"},
                                "nodes": initial_comments,
                            },
                        }
                    ],
                }
            }
        }
    }
    second_page = {
        "node": {
            "comments": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [
                    {
                        "databaseId": 200,
                        "id": "PRRC_marker",
                        "author": {"login": "bubo"},
                        "body": "fixed <!-- bubo-fixed:abc -->",
                        "replyTo": {"id": "PRRC_root"},
                    }
                ],
            }
        }
    }
    with patch("bubo.github.graphql", side_effect=[first_page, second_page]) as graphql:
        threads = github.get_pr_review_threads(ReviewConfig(provider="github"), "tok", "o/r", 5)
    assert graphql.call_count == 2
    assert len(threads[0]["comments"]) == 101
    assert (
        github.bubo_reply_with_marker(threads[0], "bubo", "<!-- bubo-fixed:abc -->")
        is not None
    )


def test_find_thread_for_comment_matches_database_id_and_node_id() -> None:
    threads = [
        {"is_resolved": True, "comments": [{"database_id": 100, "node_id": "PRRC_a"}]},
        {"is_resolved": False, "comments": [{"database_id": 200, "node_id": "PRRC_b"}]},
    ]
    # Integer databaseId (REST id / most MCP servers).
    by_db = github.find_thread_for_comment(threads, "100")
    assert by_db is not None
    assert by_db["is_resolved"] is True
    # GraphQL node id (some MCP servers).
    by_node = github.find_thread_for_comment(threads, "PRRC_b")
    assert by_node is not None
    assert by_node["is_resolved"] is False
    assert github.find_thread_for_comment(threads, "999") is None


def test_classify_graphql_thread_outcome_reads_resolution() -> None:
    thread = {
        "is_resolved": True,
        "comments": [
            {"login": "bubo", "body": "finding"},
            {"login": "dev1", "actor_type": "User", "body": "[llm-review:false-positive] nope"},
        ],
    }
    outcome = github.classify_graphql_thread_outcome(thread, bot_username="bubo", pr_state="merged")
    assert outcome["resolved"] is True
    assert outcome["developer_replied"] is True
    assert outcome["false_positive"] is True
    assert outcome["disputed"] is True
    assert outcome["developer_agreed"] is False
    assert outcome["developer_disposition"] == "false_positive"
    assert outcome["resolution_observed"] is True
    # Resolved before merge -> not a merged-unresolved finding.
    assert outcome["merged_unresolved"] is False


def test_classify_graphql_thread_outcome_marks_merged_unresolved() -> None:
    thread = {"is_resolved": False, "comments": [{"login": "bubo", "body": "finding"}]}
    outcome = github.classify_graphql_thread_outcome(thread, bot_username="bubo", pr_state="merged")
    assert outcome["resolved"] is False
    assert outcome["merged_unresolved"] is True


def test_classify_graphql_thread_outcome_extracts_finding_and_reply_text() -> None:
    # GitHub GraphQL path: the LLM reply classifier needs the bot's finding
    # and the developer's reply in original case.
    thread = {
        "is_resolved": True,
        "comments": [
            {"login": "bubo", "body": "The Finding Body"},
            {"login": "dev1", "actor_type": "User", "body": "Working As Intended"},
        ],
    }
    outcome = github.classify_graphql_thread_outcome(thread, bot_username="bubo", pr_state="open")
    assert outcome["_finding_text"] == "The Finding Body"
    assert "Working As Intended" in outcome["_reply_text"]


def test_graphql_outcome_records_explicit_human_agreement_evidence() -> None:
    thread = {
        "is_resolved": True,
        "comments": [
            {"login": "bubo", "body": "finding"},
            {
                "login": "dev1",
                "actor_type": "User",
                "body": "[llm-review:agreed] fixed in this branch",
                "created_at": "2026-09-30T12:00:00Z",
            },
        ],
    }
    outcome = github.classify_graphql_thread_outcome(thread, "bubo", "open")
    assert outcome["developer_agreed"] is True
    assert outcome["developer_disposition"] == "agrees"
    assert '"actor": "dev1"' in outcome["disposition_evidence"]
    assert '"time": "2026-09-30T12:00:00Z"' in outcome["disposition_evidence"]


def test_outcome_keeps_silence_and_resolution_developer_disposition_unknown() -> None:
    outcome = github.classify_graphql_thread_outcome(
        {"is_resolved": True, "comments": [{"login": "bubo", "body": "finding"}]},
        "bubo",
        "merged",
    )
    assert outcome["developer_agreed"] is False
    assert outcome["developer_disposition"] == "unknown"
    assert outcome["disposition_evidence"] == "unknown"


def test_outcome_accepts_only_explicit_agrees_classifier_result() -> None:
    thread = {
        "is_resolved": False,
        "comments": [
            {"login": "bubo", "body": "finding"},
            {"login": "dev1", "actor_type": "User", "body": "I will take a look"},
        ],
    }
    outcome = github.classify_graphql_thread_outcome(
        thread, "bubo", "open", reply_classifier_result={"verdict": "agrees"}
    )
    assert outcome["developer_agreed"] is True
    assert outcome["developer_disposition"] == "agrees"
    assert '"kind": "reply_classifier"' in outcome["disposition_evidence"]
    unknown = github.classify_graphql_thread_outcome(
        thread, "bubo", "open", reply_classifier_result={"verdict": "accepted"}
    )
    assert unknown["developer_disposition"] == "unknown"


def test_graphql_outcome_ignores_other_bot_agreement_and_dispute_markers() -> None:
    thread = {
        "is_resolved": False,
        "comments": [
            {"login": "bubo", "actor_type": "Bot", "body": "finding"},
            {
                "login": "another-bot",
                "actor_type": "Bot",
                "body": "[llm-review:agreed] [llm-review:disputed]",
            },
        ],
    }
    outcome = github.classify_graphql_thread_outcome(thread, "bubo", "open")
    assert outcome["developer_replied"] is False
    assert outcome["developer_agreed"] is False
    assert outcome["developer_disposition"] == "unknown"
    assert outcome["disputed"] is False
    assert outcome["false_positive"] is False


def test_classify_review_thread_outcome_extracts_finding_and_reply_text() -> None:
    # GitHub REST fallback (GraphQL outage) must also surface the text so
    # classification still works on that path.
    comment = {"body": "The Finding Body"}
    replies = [{"user": {"login": "dev1", "type": "User"}, "body": "Working As Intended"}]
    outcome = github.classify_review_thread_outcome(
        comment, replies, bot_username="bubo", pr_state="open"
    )
    assert outcome["_finding_text"] == "The Finding Body"
    assert "Working As Intended" in outcome["_reply_text"]


def test_fetch_outcome_uses_graphql_resolution() -> None:
    provider = GitHubProvider()
    threads = [
        {
            "is_resolved": True,
            "comments": [{"database_id": 100, "node_id": "PRRC_a", "login": "bubo"}],
        }
    ]
    with patch("bubo.github.get_pr", return_value={"state": "open"}):
        with patch("bubo.github.get_pr_review_threads", return_value=threads):
            outcome = provider.fetch_outcome(
                ReviewConfig(provider="github"), "tok", "o/r", 5, "100", "bubo"
            )
    assert outcome["resolved"] is True


def test_fetch_outcome_falls_back_to_rest_on_graphql_failure() -> None:
    provider = GitHubProvider()
    with patch("bubo.github.get_pr", return_value={"state": "closed", "merged": True}):
        with patch(
            "bubo.github.get_pr_review_threads",
            side_effect=RuntimeError("graphql down"),
        ):
            with patch("bubo.github.get_pr_review_comment", return_value={"id": "100"}):
                with patch("bubo.github.get_pr_review_comments", return_value=[]):
                    outcome = provider.fetch_outcome(
                        ReviewConfig(provider="github"), "tok", "o/r", 5, "100", "bubo"
                    )
    # REST classifier is resolution-blind, but a merged PR -> merged_unresolved.
    assert outcome["resolved"] is False
    assert outcome["merged_unresolved"] is True
    assert outcome["resolution_observed"] is False


def test_pulls_updated_after_stops_at_cutoff() -> None:
    url = (
        "https://api.github.com/repos/o/r/pulls?state=all&sort=updated&direction=desc&per_page=100"
    )
    pages = {
        url: (
            [
                {"number": 3, "updated_at": "2026-05-29T00:00:00Z"},
                {"number": 2, "updated_at": "2026-05-20T00:00:00Z"},  # older than cutoff -> stop
                {"number": 1, "updated_at": "2026-05-10T00:00:00Z"},
            ],
            {},
        ),
    }

    def fake_request(req_url, token, method, body=None):
        return pages[req_url]

    with patch("bubo.github._request", side_effect=fake_request):
        prs = github.pulls_updated_after(
            ReviewConfig(provider="github"), "o/r", "tok", "2026-05-25T00:00:00Z"
        )
    assert [pr["number"] for pr in prs] == [3]


def test_provider_review_prompt_mentions_github_pr() -> None:
    provider = GitHubProvider()
    change = {
        "html_url": "https://github.com/o/r/pull/5",
        "number": 5,
        "title": "Fix bug",
        "head": {"ref": "feature", "sha": "abc"},
        "base": {"ref": "main"},
    }
    prompt = provider.review_prompt("o/r", change, ReviewConfig(provider="github"))
    assert "GitHub PR" in prompt
    assert "PR number: 5" in prompt
    assert "Use the `code-reviewer` skill" in prompt


def _open_bubo_thread(*, marker: str = "") -> dict:
    comments = [
        {
            "database_id": 100,
            "node_id": "PRRC_root",
            "login": "bubo",
            "body": "finding",
            "in_reply_to_node_id": "",
        }
    ]
    if marker:
        comments.append(
            {
                "database_id": 101,
                "node_id": "PRRC_reply",
                "login": "bubo",
                "body": f"verified {marker}",
                "in_reply_to_node_id": "PRRC_root",
            }
        )
    return {"node_id": "PRRT_1", "is_resolved": False, "comments": comments}


def test_finding_thread_requires_bubo_owned_root_and_detects_marker() -> None:
    provider = GitHubProvider()
    thread = _open_bubo_thread(marker="<!-- bubo-fixed:abc -->")
    with patch("bubo.github.get_pr_review_threads", return_value=[thread]):
        result = provider.finding_thread(
            ReviewConfig(provider="github"),
            "tok",
            "o/r",
            5,
            "100",
            "bubo",
            "<!-- bubo-fixed:abc -->",
        )
    assert result.state is FindingThreadState.OPEN
    assert result.reply_marker_present is True


def test_finding_thread_rejects_bubo_reply_on_developer_root() -> None:
    provider = GitHubProvider()
    thread = {
        "node_id": "PRRT_1",
        "is_resolved": False,
        "comments": [
            {
                "database_id": 100,
                "node_id": "PRRC_root",
                "login": "dev1",
                "body": "developer finding",
                "in_reply_to_node_id": "",
            },
            {
                "database_id": 101,
                "node_id": "PRRC_bubo_reply",
                "login": "bubo",
                "body": "<!-- bubo-fixed:abc -->",
                "in_reply_to_node_id": "PRRC_root",
            },
        ],
    }
    with patch("bubo.github.get_pr_review_threads", return_value=[thread]):
        result = provider.finding_thread(
            ReviewConfig(provider="github"),
            "tok",
            "o/r",
            5,
            "100",
            "bubo",
            "<!-- bubo-fixed:abc -->",
        )
    assert result.state is FindingThreadState.FOREIGN
    assert result.reply_marker_present is False


def test_finding_thread_marks_missing_numeric_comment_deleted_on_rest_404() -> None:
    provider = GitHubProvider()
    with patch("bubo.github.get_pr_review_threads", return_value=[]):
        with patch("bubo.github.pr_review_comment_deleted", return_value=True) as deleted:
            result = provider.finding_thread(
                ReviewConfig(provider="github"), "tok", "o/r", 5, "100", "bubo", "marker"
            )
    assert result.state is FindingThreadState.DELETED
    deleted.assert_called_once_with(ReviewConfig(provider="github"), "tok", "o/r", "100")


def test_reply_to_finding_thread_reuses_marker_before_rest_write() -> None:
    provider = GitHubProvider()
    marker = "<!-- bubo-fixed:abc -->"
    with patch("bubo.github.get_pr_review_threads", return_value=[_open_bubo_thread(marker=marker)]):
        with patch("bubo.github.reply_to_pr_review_comment") as reply:
            result = provider.reply_to_finding_thread(
                ReviewConfig(provider="github"), "tok", "o/r", 5, "100", f"fixed\n{marker}", marker
            )
    assert result == FindingThreadReply(
        "101", marker_confirmed=True, applied_by_bubo=False, state=FindingThreadState.OPEN
    )
    reply.assert_not_called()


def test_reply_to_finding_thread_posts_only_to_bubo_root() -> None:
    provider = GitHubProvider()
    marker = "<!-- bubo-fixed:abc -->"
    with patch(
        "bubo.github.get_pr_review_threads",
        side_effect=[[_open_bubo_thread()], [_open_bubo_thread(marker=marker)]],
    ):
        with patch(
            "bubo.github.reply_to_pr_review_comment", return_value={"id": 222}
        ) as reply:
            result = provider.reply_to_finding_thread(
                ReviewConfig(provider="github"),
                "tok",
                "o/r",
                5,
                "100",
                f"verified fixed\n{marker}",
                marker,
            )
    assert result == FindingThreadReply(
        "222", marker_confirmed=True, applied_by_bubo=True, state=FindingThreadState.OPEN
    )
    reply.assert_called_once_with(
        ReviewConfig(provider="github"),
        "tok",
        "o/r",
        5,
        "100",
        f"verified fixed\n{marker}",
    )


def test_reply_to_finding_thread_requires_refetched_marker_confirmation() -> None:
    provider = GitHubProvider()
    marker = "<!-- bubo-fixed:abc -->"
    with patch(
        "bubo.github.get_pr_review_threads",
        side_effect=[[_open_bubo_thread()], [_open_bubo_thread()]],
    ):
        with patch("bubo.github.reply_to_pr_review_comment", return_value={"id": 222}):
            try:
                provider.reply_to_finding_thread(
                    ReviewConfig(provider="github"), "tok", "o/r", 5, "100", marker, marker
                )
            except RuntimeError as exc:
                assert "marker" in str(exc)
            else:
                raise AssertionError("missing refetched marker must fail")


def test_reply_to_finding_thread_requires_nonempty_rest_reply_id() -> None:
    provider = GitHubProvider()
    marker = "<!-- bubo-fixed:abc -->"
    with patch("bubo.github.get_pr_review_threads", return_value=[_open_bubo_thread()]):
        with patch("bubo.github.reply_to_pr_review_comment", return_value={}):
            try:
                provider.reply_to_finding_thread(
                    ReviewConfig(provider="github"), "tok", "o/r", 5, "100", marker, marker
                )
            except RuntimeError as exc:
                assert "id" in str(exc)
            else:
                raise AssertionError("empty REST reply id must fail")


def test_reply_to_finding_thread_reports_developer_resolved_race() -> None:
    provider = GitHubProvider()
    marker = "<!-- bubo-fixed:abc -->"
    developer_resolved = {**_open_bubo_thread(marker=marker), "is_resolved": True}
    with patch(
        "bubo.github.get_pr_review_threads",
        side_effect=[[_open_bubo_thread()], [developer_resolved]],
    ):
        with patch("bubo.github.reply_to_pr_review_comment", return_value={"id": 222}):
            result = provider.reply_to_finding_thread(
                ReviewConfig(provider="github"), "tok", "o/r", 5, "100", marker, marker
            )
    assert result == FindingThreadReply(
        "222", marker_confirmed=True, applied_by_bubo=True, state=FindingThreadState.RESOLVED
    )


def test_resolve_finding_thread_refetches_and_verifies_final_state() -> None:
    provider = GitHubProvider()
    open_thread = _open_bubo_thread()
    resolved_thread = {**open_thread, "is_resolved": True}
    with patch(
        "bubo.github.get_pr_review_threads", side_effect=[[open_thread], [resolved_thread]]
    ):
        with patch("bubo.github.resolve_pr_review_thread") as resolve:
            result = provider.resolve_finding_thread(
                ReviewConfig(provider="github"), "tok", "o/r", 5, "100", "bubo"
            )
    resolve.assert_called_once_with(ReviewConfig(provider="github"), "tok", "PRRT_1")
    assert result == FindingThreadResolution(FindingThreadState.RESOLVED, applied_by_bubo=False)


def test_resolve_finding_thread_is_noop_when_already_resolved() -> None:
    provider = GitHubProvider()
    resolved_thread = {**_open_bubo_thread(), "is_resolved": True}
    with patch("bubo.github.get_pr_review_threads", return_value=[resolved_thread]) as get_threads:
        with patch("bubo.github.resolve_pr_review_thread") as resolve:
            result = provider.resolve_finding_thread(
                ReviewConfig(provider="github"), "tok", "o/r", 5, "100", "bubo"
            )
    assert get_threads.call_count == 1
    resolve.assert_not_called()
    assert result == FindingThreadResolution(FindingThreadState.RESOLVED, applied_by_bubo=False)


def test_resolve_finding_thread_reports_developer_resolution_race() -> None:
    provider = GitHubProvider()
    open_thread = _open_bubo_thread()
    developer_resolved = {**open_thread, "is_resolved": True, "resolved_by": "dev1"}
    with patch(
        "bubo.github.get_pr_review_threads", side_effect=[[open_thread], [developer_resolved]]
    ):
        with patch("bubo.github.resolve_pr_review_thread"):
            result = provider.resolve_finding_thread(
                ReviewConfig(provider="github"), "tok", "o/r", 5, "100", "bubo"
            )
    assert result == FindingThreadResolution(FindingThreadState.RESOLVED, applied_by_bubo=False)


def test_resolve_finding_thread_attributes_only_bubo_resolver() -> None:
    provider = GitHubProvider()
    open_thread = _open_bubo_thread()
    bubo_resolved = {**open_thread, "is_resolved": True, "resolved_by": "bubo"}
    with patch(
        "bubo.github.get_pr_review_threads", side_effect=[[open_thread], [bubo_resolved]]
    ):
        with patch("bubo.github.resolve_pr_review_thread"):
            result = provider.resolve_finding_thread(
                ReviewConfig(provider="github"), "tok", "o/r", 5, "100", "bubo"
            )
    assert result == FindingThreadResolution(FindingThreadState.RESOLVED, applied_by_bubo=True)


def test_resolve_finding_thread_returns_deleted_or_foreign_noop_state() -> None:
    provider = GitHubProvider()
    with patch("bubo.github.get_pr_review_threads", return_value=[]):
        with patch("bubo.github.pr_review_comment_deleted", return_value=True):
            deleted = provider.resolve_finding_thread(
                ReviewConfig(provider="github"), "tok", "o/r", 5, "100", "bubo"
            )
    foreign_thread = {
        "node_id": "PRRT_1",
        "is_resolved": False,
        "comments": [
            {
                "database_id": 100,
                "node_id": "PRRC_root",
                "login": "dev1",
                "body": "finding",
                "in_reply_to_node_id": "",
            }
        ],
    }
    with patch("bubo.github.get_pr_review_threads", return_value=[foreign_thread]):
        foreign = provider.resolve_finding_thread(
            ReviewConfig(provider="github"), "tok", "o/r", 5, "100", "bubo"
        )
    assert deleted == FindingThreadResolution(FindingThreadState.DELETED, applied_by_bubo=False)
    assert foreign == FindingThreadResolution(FindingThreadState.FOREIGN, applied_by_bubo=False)


def test_resolution_graphql_failure_has_no_rest_fallback() -> None:
    provider = GitHubProvider()
    with patch("bubo.github.get_pr_review_threads", side_effect=RuntimeError("graphql down")):
        with patch("bubo.github.reply_to_pr_review_comment") as reply:
            with patch("bubo.github.resolve_pr_review_thread") as resolve:
                try:
                    provider.resolve_finding_thread(
                        ReviewConfig(provider="github"), "tok", "o/r", 5, "100", "bubo"
                    )
                except RuntimeError as exc:
                    assert str(exc) == "graphql down"
                else:
                    raise AssertionError("GraphQL failure must be raised")
    reply.assert_not_called()
    resolve.assert_not_called()
