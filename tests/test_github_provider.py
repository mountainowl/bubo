"""Unit tests for the GitHub REST client and GitHub provider."""

from __future__ import annotations

from unittest.mock import patch

from bubo import github
from bubo.review_config import ReviewConfig
from bubo.scm import get_provider
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


def test_provider_post_falls_back_to_rest_when_mcp_fails() -> None:
    provider = GitHubProvider()
    position = {"commit_id": "abc", "path": "src/A.py", "line": 7, "side": "RIGHT"}

    with patch("bubo.mcp.call_tool", side_effect=RuntimeError("no such tool")):
        with patch("bubo.github.find_review_comment_by_body", return_value=""):
            with patch(
                "bubo.github.create_pr_review_comment",
                return_value={"id": "gh-comment-1"},
            ):
                comment_id = provider.post_inline_comment(
                    ReviewConfig(provider="github"), "tok", "o/r", 5, "body", position
                )

    assert comment_id == "gh-comment-1"


def test_provider_post_prefers_mcp_when_it_returns_id() -> None:
    provider = GitHubProvider()
    position = {"commit_id": "abc", "path": "src/A.py", "line": 7, "side": "RIGHT"}

    with patch("bubo.mcp.call_tool", return_value={"id": "mcp-comment-9"}):
        comment_id = provider.post_inline_comment(
            ReviewConfig(provider="github"), "tok", "o/r", 5, "body", position
        )

    assert comment_id == "mcp-comment-9"


def test_classify_review_thread_outcome_reads_markers_and_replies() -> None:
    comment = {"id": "c1", "body": "finding"}
    replies = [
        {"user": {"login": "dev1"}, "body": "[llm-review:false-positive] nope"},
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
            {"login": "dev1", "body": "[llm-review:false-positive] nope"},
        ],
    }
    outcome = github.classify_graphql_thread_outcome(thread, bot_username="bubo", pr_state="merged")
    assert outcome["resolved"] is True
    assert outcome["developer_replied"] is True
    assert outcome["false_positive"] is True
    assert outcome["disputed"] is True
    # Resolved before merge -> not a merged-unresolved finding.
    assert outcome["merged_unresolved"] is False


def test_classify_graphql_thread_outcome_marks_merged_unresolved() -> None:
    thread = {"is_resolved": False, "comments": [{"login": "bubo", "body": "finding"}]}
    outcome = github.classify_graphql_thread_outcome(thread, bot_username="bubo", pr_state="merged")
    assert outcome["resolved"] is False
    assert outcome["merged_unresolved"] is True


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
