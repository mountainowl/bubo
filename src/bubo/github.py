"""GitHub REST client used by the GitHub provider.

Stdlib-only, mirroring :mod:`bubo.gitlab` in spirit but speaking
GitHub's REST dialect:

* Auth is ``Authorization: Bearer <token>`` with the
  ``application/vnd.github+json`` Accept header and a pinned API version.
* Pagination follows the ``Link: <url>; rel="next"`` header (GitHub does
  not expose ``X-Next-Page``).
* Rate limiting surfaces as ``429`` (secondary) or ``403`` with
  ``X-RateLimit-Remaining: 0`` (primary); both are retried with
  ``Retry-After`` honored when present.

A project is a GitHub ``owner/repo`` slug. As with the GitLab client, this
module does NOT post inline comments through REST in the normal path — that
goes through the GitHub MCP server (see
:mod:`bubo.scm.github`). :func:`create_pr_review_comment` is the
REST fallback used only when MCP returns no comment ID.

The GitHub provider is selected by ``[scm].provider = "github"`` or the
``BUBO_PROVIDER=github`` environment override; the shared ``bubo-poller``
then drives it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

from bubo.review_config import ReviewConfig
from bubo.types import JsonObject

API_MAX_ATTEMPTS = 3
API_RETRY_STATUSES = {429, 500, 502, 503, 504}
API_VERSION = "2022-11-28"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "Content-Type": "application/json",
    }


def _request(
    url: str, token: str, method: str, body: JsonObject | None = None
) -> tuple[Any, dict[str, str]]:
    """Issue one GitHub REST request to an absolute URL, with retry.

    Retries on transient statuses and connection errors, honoring
    ``Retry-After``. A ``403`` with ``X-RateLimit-Remaining: 0`` is treated
    as a (retryable) rate-limit rather than a hard auth failure.
    """
    payload = None if body is None else json.dumps(body).encode()
    for attempt in range(API_MAX_ATTEMPTS):
        req = urllib.request.Request(url, data=payload, method=method, headers=_headers(token))
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read().decode() or "null"
                headers = dict(resp.headers)
            return json.loads(data), headers
        except urllib.error.HTTPError as exc:
            retryable = exc.code in API_RETRY_STATUSES or _is_rate_limited(exc)
            if not retryable or attempt == API_MAX_ATTEMPTS - 1:
                raise
            time.sleep(api_retry_delay(exc.headers, attempt))
        except urllib.error.URLError:
            if attempt == API_MAX_ATTEMPTS - 1:
                raise
            time.sleep(api_retry_delay({}, attempt))
    raise RuntimeError("GitHub API retry loop exhausted")


def _is_rate_limited(exc: urllib.error.HTTPError) -> bool:
    if exc.code != 403:
        return False
    remaining = exc.headers.get("X-RateLimit-Remaining") if hasattr(exc.headers, "get") else None
    return remaining == "0"


def api_retry_delay(headers: object, attempt: int) -> float:
    retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
    if retry_after:
        try:
            return min(60.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    delay = 0.5 * (2**attempt)
    return 10.0 if delay > 10.0 else delay


def api(
    api_url: str, token: str, method: str, path: str, body: JsonObject | None = None
) -> tuple[Any, dict[str, str]]:
    """Issue one GitHub REST call against ``api_url`` + ``path``."""
    return _request(api_url.rstrip("/") + path, token, method, body)


def _next_link(headers: dict[str, str]) -> str | None:
    """Return the ``rel="next"`` URL from a GitHub ``Link`` header, if any."""
    link = headers.get("Link") or headers.get("link")
    if not link:
        return None
    for part in link.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().strip("<>")
        if any(seg.strip() == 'rel="next"' for seg in segments[1:]):
            return url
    return None


def api_pages(api_url: str, token: str, path: str) -> list[JsonObject]:
    """Fetch all pages of a GitHub list endpoint, following ``Link`` next."""
    sep = "&" if "?" in path else "?"
    url: str | None = api_url.rstrip("/") + f"{path}{sep}per_page=100"
    out: list[JsonObject] = []
    while url:
        data, headers = _request(url, token, "GET")
        if not isinstance(data, list):
            raise RuntimeError("GitHub API page did not return a list")
        out.extend(cast(JsonObject, item) for item in data if isinstance(item, dict))
        url = _next_link(headers)
    return out


def _owner_repo(project: str) -> str:
    """Return the URL-safe ``owner/repo`` path segment for a project slug."""
    owner, _, repo = project.partition("/")
    return f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repo, safe='')}"


def open_prs(cfg: ReviewConfig, project: str, token: str) -> list[JsonObject]:
    """Return open pull requests for a GitHub repository."""
    repo = _owner_repo(project)
    return api_pages(cfg.github_api_url, token, f"/repos/{repo}/pulls?state=open")


def get_pr(cfg: ReviewConfig, token: str, project: str, number: int) -> JsonObject:
    """Return a single pull-request payload."""
    repo = _owner_repo(project)
    data, _ = api(cfg.github_api_url, token, "GET", f"/repos/{repo}/pulls/{number}")
    if not isinstance(data, dict):
        raise RuntimeError("GitHub PR response was not an object")
    return cast(JsonObject, data)


def get_pr_files(cfg: ReviewConfig, token: str, project: str, number: int) -> list[JsonObject]:
    """Return per-file diff entries for a pull request (``filename``/``patch``)."""
    repo = _owner_repo(project)
    return api_pages(cfg.github_api_url, token, f"/repos/{repo}/pulls/{number}/files")


def get_pr_commits(cfg: ReviewConfig, token: str, project: str, number: int) -> list[JsonObject]:
    """Return the commits on a pull request (each ``{sha, commit:{message, author}}``)."""
    repo = _owner_repo(project)
    return api_pages(cfg.github_api_url, token, f"/repos/{repo}/pulls/{number}/commits")


def get_pr_review_comments(
    cfg: ReviewConfig, token: str, project: str, number: int
) -> list[JsonObject]:
    """Return all inline review comments on a pull request."""
    repo = _owner_repo(project)
    return api_pages(cfg.github_api_url, token, f"/repos/{repo}/pulls/{number}/comments")


def get_pr_review_comment(
    cfg: ReviewConfig, token: str, project: str, comment_id: str
) -> JsonObject:
    """Return one inline review comment by ID."""
    repo = _owner_repo(project)
    encoded = urllib.parse.quote(comment_id, safe="")
    data, _ = api(cfg.github_api_url, token, "GET", f"/repos/{repo}/pulls/comments/{encoded}")
    if not isinstance(data, dict):
        raise RuntimeError("GitHub review-comment response was not an object")
    return cast(JsonObject, data)


def find_review_comment_by_body(
    cfg: ReviewConfig, token: str, project: str, number: int, body: str
) -> str:
    """Locate an existing review comment by exact body match; return its ID or ""."""
    for comment in get_pr_review_comments(cfg, token, project, number):
        if comment.get("body") == body and comment.get("id"):
            return str(comment["id"])
    return ""


def create_pr_review_comment(
    cfg: ReviewConfig,
    token: str,
    project: str,
    number: int,
    body: str,
    position: JsonObject,
) -> JsonObject:
    """Create an inline review comment via REST (fallback for the MCP path).

    ``position`` carries GitHub's comment anchor: ``commit_id``, ``path``,
    ``line``, and ``side`` (plus optional ``start_line``/``start_side`` for
    multi-line). See :mod:`bubo.scm.github` for how it's built.
    """
    repo = _owner_repo(project)
    payload = {"body": body, **position}
    data, _ = api(
        cfg.github_api_url, token, "POST", f"/repos/{repo}/pulls/{number}/comments", payload
    )
    return cast(JsonObject, data) if isinstance(data, dict) else {}


def get_issue_comments(
    cfg: ReviewConfig, token: str, project: str, number: int
) -> list[JsonObject]:
    """Return change-level (non-inline) comments on a pull request.

    PRs and issues share the same comments endpoint on GitHub — this is the
    canonical way to read or write a comment that is not anchored to a
    specific diff line.
    """
    repo = _owner_repo(project)
    return api_pages(cfg.github_api_url, token, f"/repos/{repo}/issues/{number}/comments")


def find_issue_comment_by_body(
    cfg: ReviewConfig,
    token: str,
    project: str,
    number: int,
    body: str,
    *,
    bot_username: str | None = None,
) -> str:
    """Locate an existing change-level comment authored by the bot.

    Returns the comment ID as a string, or ``""`` if none matches. Mirrors
    :func:`find_review_comment_by_body` for the inline path so the
    no-findings comment never stacks duplicates on re-review. When
    ``bot_username`` is provided, the match is restricted to comments
    authored by the bot — a human or other bot reproducing the body must
    not satisfy the dedup, or the reviewer would silently stop posting
    its own no-findings acknowledgement.
    """
    for comment in get_issue_comments(cfg, token, project, number):
        if bot_username and ((comment.get("user") or {}).get("login") or "") != bot_username:
            continue
        if comment.get("body") == body and comment.get("id"):
            return str(comment["id"])
    return ""


def create_issue_comment(
    cfg: ReviewConfig, token: str, project: str, number: int, body: str
) -> JsonObject:
    """Post a change-level (non-inline) comment on a pull request."""
    repo = _owner_repo(project)
    data, _ = api(
        cfg.github_api_url,
        token,
        "POST",
        f"/repos/{repo}/issues/{number}/comments",
        {"body": body},
    )
    return cast(JsonObject, data) if isinstance(data, dict) else {}


def _graphql_url(api_url: str) -> str:
    """Derive the GraphQL endpoint from the REST ``api_url``.

    github.com REST is ``https://api.github.com`` and GraphQL is
    ``https://api.github.com/graphql``. GitHub Enterprise REST is
    ``https://<host>/api/v3`` and GraphQL is ``https://<host>/api/graphql``.
    """
    base = api_url.rstrip("/")
    if base.endswith("/api/v3"):
        return base[: -len("/api/v3")] + "/api/graphql"
    return base + "/graphql"


def graphql(api_url: str, token: str, query: str, variables: JsonObject) -> JsonObject:
    """Run one GraphQL query and return its ``data`` object.

    GraphQL returns HTTP 200 even on query errors, surfacing them in an
    ``errors`` array in the body. We raise on a non-empty ``errors`` so
    callers (e.g. the provider's outcome sync) can fall back to REST.
    """
    data, _ = _request(
        _graphql_url(api_url), token, "POST", {"query": query, "variables": variables}
    )
    if not isinstance(data, dict):
        raise RuntimeError("GitHub GraphQL response was not an object")
    if data.get("errors"):
        raise RuntimeError(f"GitHub GraphQL errors: {json.dumps(data['errors'])}")
    result = data.get("data")
    if not isinstance(result, dict):
        raise RuntimeError("GitHub GraphQL response missing data")
    return cast(JsonObject, result)


# Query for a PR's review threads. ``isResolved`` is the resolution state
# REST cannot observe. Each thread's comments expose both ``databaseId``
# (the REST integer id) and ``id`` (the GraphQL node id) so we can correlate
# back to whatever id was stored at post time, plus author/body/path/line
# for marker detection and backfill.
_REVIEW_THREADS_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:50,after:$cursor){
        pageInfo{ hasNextPage endCursor }
        nodes{
          isResolved
          comments(first:100){
            nodes{ databaseId id author{login} body path line }
          }
        }
      }
    }
  }
}
"""


def _normalize_review_thread(node: JsonObject) -> JsonObject:
    """Flatten a GraphQL review-thread node into a provider-neutral dict.

    Note: inner comment pagination (``comments(first:100)``) is not
    followed — a single review thread with more than 100 comments is
    vanishingly rare. If replies ever appear missing on a huge thread, this
    is where to add ``pageInfo``/cursor handling.
    """
    comments: list[JsonObject] = []
    for item in (node.get("comments") or {}).get("nodes") or []:
        if not isinstance(item, dict):
            continue
        comments.append(
            {
                "database_id": item.get("databaseId"),
                "node_id": item.get("id"),
                "login": (item.get("author") or {}).get("login") or "",
                "body": item.get("body") or "",
                "path": item.get("path") or "",
                "line": item.get("line"),
            }
        )
    return {"is_resolved": bool(node.get("isResolved")), "comments": comments}


def get_pr_review_threads(
    cfg: ReviewConfig, token: str, project: str, number: int
) -> list[JsonObject]:
    """Return all review threads for a PR (resolution state + comments).

    Paginates the ``reviewThreads`` connection. Each returned thread is a
    normalized dict: ``{"is_resolved": bool, "comments": [...]}``.
    """
    owner, _, repo = project.partition("/")
    threads: list[JsonObject] = []
    cursor: str | None = None
    while True:
        data = graphql(
            cfg.github_api_url,
            token,
            _REVIEW_THREADS_QUERY,
            {"owner": owner, "name": repo, "number": int(number), "cursor": cursor},
        )
        pull = (data.get("repository") or {}).get("pullRequest") or {}
        review_threads = pull.get("reviewThreads") or {}
        for node in review_threads.get("nodes") or []:
            if isinstance(node, dict):
                threads.append(_normalize_review_thread(cast(JsonObject, node)))
        page = review_threads.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break
    return threads


def find_thread_for_comment(threads: list[JsonObject], comment_id: str) -> JsonObject | None:
    """Find the review thread containing the comment with ``comment_id``.

    Matches on either the REST integer ``databaseId`` or the GraphQL node
    ``id`` so it works regardless of which id was stored when the comment
    was posted (the MCP path and the REST fallback can differ).
    """
    target = str(comment_id)
    for thread in threads:
        for comment in thread.get("comments") or []:
            if str(comment.get("database_id")) == target or str(comment.get("node_id")) == target:
                return thread
    return None


def first_bot_comment(thread: JsonObject, bot_username: str) -> JsonObject | None:
    """Return the first comment in ``thread`` authored by the bot, if any."""
    for comment in thread.get("comments") or []:
        if (comment.get("login") or "") == bot_username:
            return cast(JsonObject, comment)
    return None


def classify_graphql_thread_outcome(
    thread: JsonObject, bot_username: str, pr_state: str
) -> JsonObject:
    """Classify a GraphQL review thread, including real resolution state.

    Unlike :func:`classify_review_thread_outcome` (REST, resolution-blind),
    this reads the thread's ``is_resolved`` directly. Markers and developer
    replies are detected from non-bot comments in the thread.
    """
    comments = thread.get("comments") or []
    replies = [c for c in comments if (c.get("login") or "") != bot_username]
    developer_replied = bool(replies)
    reply_text = "\n".join(str(c.get("body") or "").lower() for c in replies)
    false_positive = "[llm-review:false-positive]" in reply_text
    duplicate = "[llm-review:duplicate]" in reply_text
    disputed = "[llm-review:disputed]" in reply_text or false_positive
    resolved = bool(thread.get("is_resolved"))
    bot_comment = first_bot_comment(thread, bot_username)
    return {
        "resolved": resolved,
        "deleted": False,
        "developer_replied": developer_replied,
        "disputed": disputed,
        "false_positive": false_positive,
        "duplicate": duplicate,
        "resolved_at": None,
        "merged_unresolved": pr_state == "merged" and not resolved,
        # Original-case text for the LLM reply classifier (transient; ignored
        # by record_finding_outcome). Bot's finding + the developer replies.
        "_finding_text": str((bot_comment or {}).get("body") or ""),
        "_reply_text": "\n\n".join(str(c.get("body") or "") for c in replies),
    }


def pulls_updated_after(
    cfg: ReviewConfig, project: str, token: str, updated_after: str
) -> list[JsonObject]:
    """Return PRs updated at/after ``updated_after`` (ISO 8601), newest first.

    GitHub's ``/pulls`` endpoint has no server-side ``since`` filter, so we
    sort by ``updated`` descending and stop as soon as a page yields a PR
    older than the cutoff.
    """
    repo = _owner_repo(project)
    path = f"/repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100"
    url: str | None = cfg.github_api_url.rstrip("/") + path
    out: list[JsonObject] = []
    while url:
        data, headers = _request(url, token, "GET")
        if not isinstance(data, list):
            raise RuntimeError("GitHub API page did not return a list")
        for item in data:
            if not isinstance(item, dict):
                continue
            if str(item.get("updated_at") or "") < updated_after:
                return out
            out.append(cast(JsonObject, item))
        url = _next_link(headers)
    return out


def classify_review_thread_outcome(
    comment: JsonObject, replies: list[JsonObject], bot_username: str, pr_state: str
) -> JsonObject:
    """Classify a posted review comment + its replies for outcome sync.

    GitHub exposes review-thread *resolution* state only through GraphQL,
    not REST. This REST-based classifier therefore reports everything it
    can see — developer replies, manual markers, deletion, merged-state —
    and leaves ``resolved`` as ``False`` (a known REST limitation,
    documented in the README). ``merged_unresolved`` is true when the PR
    merged without the thread being resolved.
    """
    active_replies = [r for r in replies if not r.get("deleted")]
    developer_replies = [
        r for r in active_replies if ((r.get("user") or {}).get("login") or "") != bot_username
    ]
    developer_replied = bool(developer_replies)
    reply_text = "\n".join(str(r.get("body") or "").lower() for r in active_replies)
    false_positive = "[llm-review:false-positive]" in reply_text
    duplicate = "[llm-review:duplicate]" in reply_text
    disputed = "[llm-review:disputed]" in reply_text or false_positive
    deleted = bool(comment.get("deleted", False))
    return {
        # Resolution state is GraphQL-only; REST cannot observe it.
        "resolved": False,
        "deleted": deleted,
        "developer_replied": developer_replied,
        "disputed": disputed,
        "false_positive": false_positive,
        "duplicate": duplicate,
        "resolved_at": None,
        "merged_unresolved": pr_state == "merged",
        # Original-case text for the LLM reply classifier (transient; ignored
        # by record_finding_outcome). Bot's finding + the developer replies.
        "_finding_text": str(comment.get("body") or ""),
        "_reply_text": "\n\n".join(str(r.get("body") or "") for r in developer_replies),
    }
