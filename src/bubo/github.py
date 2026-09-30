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
import urllib.error
import urllib.parse
from typing import Any, cast

from bubo import _http
from bubo.errors import describe
from bubo.review_config import ReviewConfig
from bubo.types import JsonObject

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

    Delegates the shared retry/backoff loop to
    :func:`bubo._http.request_json`, passing :func:`_is_rate_limited` as the
    extra retryable condition: a ``403`` with ``X-RateLimit-Remaining: 0`` is
    GitHub's primary rate-limit and is retried rather than treated as a hard
    auth failure (a distinction GitLab does not need).
    """
    return _http.request_json(
        url,
        method=method,
        headers=_headers(token),
        body=body,
        extra_retryable=_is_rate_limited,
        provider="GitHub",
    )


def _is_rate_limited(exc: urllib.error.HTTPError) -> bool:
    if exc.code != 403:
        return False
    remaining = exc.headers.get("X-RateLimit-Remaining") if hasattr(exc.headers, "get") else None
    return remaining == "0"


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
            raise RuntimeError(
                describe(
                    "GitHub API page did not return a list",
                    reason=(
                        "the API returned an unexpected response shape (often an auth error "
                        "page, a redirect, or an outage rendered as non-JSON)"
                    ),
                    fix=(
                        "check the API URL, token validity/scope, and the host's status; "
                        "inspect the raw response."
                    ),
                )
            )
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
        raise RuntimeError(
            describe(
                "GitHub PR response was not an object",
                reason=(
                    "the API returned an unexpected response shape (often an auth error "
                    "page, a redirect, or an outage rendered as non-JSON)"
                ),
                fix=(
                    "check the API URL, token validity/scope, and the host's status; "
                    "inspect the raw response."
                ),
            )
        )
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
        raise RuntimeError(
            describe(
                "GitHub review-comment response was not an object",
                reason=(
                    "the API returned an unexpected response shape (often an auth error "
                    "page, a redirect, or an outage rendered as non-JSON)"
                ),
                fix=(
                    "check the API URL, token validity/scope, and the host's status; "
                    "inspect the raw response."
                ),
            )
        )
    return cast(JsonObject, data)


def pr_review_comment_deleted(
    cfg: ReviewConfig, token: str, project: str, comment_id: str
) -> bool:
    """Return whether a numeric REST review-comment identifier is gone.

    GraphQL has no ``isDeleted`` field for review comments.  This is used
    only after a GraphQL thread lookup succeeded but found no matching thread;
    it never substitutes for GraphQL when deciding to resolve a thread.
    """
    try:
        get_pr_review_comment(cfg, token, project, comment_id)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return True
        raise
    return False


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
        raise RuntimeError(
            describe(
                "GitHub GraphQL response was not an object",
                reason=(
                    "the API returned an unexpected response shape (often an auth error "
                    "page, a redirect, or an outage rendered as non-JSON)"
                ),
                fix=(
                    "check the API URL, token validity/scope, and the host's status; "
                    "inspect the raw response."
                ),
            )
        )
    if data.get("errors"):
        raise RuntimeError(
            describe(
                f"GitHub GraphQL errors: {json.dumps(data['errors'])}",
                reason="the GitHub GraphQL API reported errors in the response body",
                fix="check the token scope and the queried resource exists/is accessible.",
            )
        )
    result = data.get("data")
    if not isinstance(result, dict):
        raise RuntimeError(
            describe(
                "GitHub GraphQL response missing data",
                reason=(
                    "the API returned an unexpected response shape (often an auth error "
                    "page, a redirect, or an outage rendered as non-JSON)"
                ),
                fix=(
                    "check the API URL, token validity/scope, and the host's status; "
                    "inspect the raw response."
                ),
            )
        )
    return cast(JsonObject, result)


# Query for a PR's review threads. ``isResolved`` is the resolution state
# REST cannot observe. Each thread has its own GraphQL ``id`` for the
# ``resolveReviewThread`` mutation.  Comments expose both ``databaseId``
# (the REST integer id) and ``id`` (the GraphQL node id) so we can correlate
# back to whichever identifier was persisted when the finding was posted.
# ``replyTo`` distinguishes the original finding from its replies: a Bubo
# reply must never make a developer-owned root eligible for resolution.
_REVIEW_THREADS_QUERY = """
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:50,after:$cursor){
        pageInfo{ hasNextPage endCursor }
        nodes{
          id isResolved resolvedBy{ login }
          comments(first:100){
            pageInfo{ hasNextPage endCursor }
            nodes{
              databaseId id author{__typename login} body path line createdAt
              replyTo{ id } isMinimized
            }
          }
        }
      }
    }
  }
}
"""

_REVIEW_THREAD_COMMENTS_QUERY = """
query($threadId:ID!,$cursor:String){
  node(id:$threadId){
    ... on PullRequestReviewThread{
      comments(first:100,after:$cursor){
        pageInfo{ hasNextPage endCursor }
        nodes{
          databaseId id author{__typename login} body path line createdAt
          replyTo{ id } isMinimized
        }
      }
    }
  }
}
"""


def _normalize_review_comment(item: JsonObject) -> JsonObject:
    return {
        "database_id": item.get("databaseId"),
        "node_id": item.get("id"),
        "login": (item.get("author") or {}).get("login") or "",
        "actor_type": (item.get("author") or {}).get("__typename") or "",
        "body": item.get("body") or "",
        "path": item.get("path") or "",
        "line": item.get("line"),
        "created_at": item.get("createdAt") or "",
        "in_reply_to_node_id": ((item.get("replyTo") or {}).get("id") or ""),
        # GitHub does not expose deleted review comments in GraphQL.
        # Keep visible/minimized signal distinct from deletion so
        # reconciliation never treats an absent/deleted root as safe.
        "is_minimized": bool(item.get("isMinimized")),
    }


def _normalize_review_thread(node: JsonObject) -> JsonObject:
    """Flatten a GraphQL review-thread node into a provider-neutral dict.

    Initial comments are completed by :func:`_complete_review_thread_comments`
    before the thread is returned, so reconciliation marker lookup sees every
    reply rather than only GraphQL's first 100 comments.
    """
    comments: list[JsonObject] = []
    for item in (node.get("comments") or {}).get("nodes") or []:
        if not isinstance(item, dict):
            continue
        comments.append(_normalize_review_comment(cast(JsonObject, item)))
    page = (node.get("comments") or {}).get("pageInfo") or {}
    return {
        "node_id": node.get("id") or "",
        "is_resolved": bool(node.get("isResolved")),
        "resolved_by": ((node.get("resolvedBy") or {}).get("login") or ""),
        "comments": comments,
        "comments_has_next_page": bool(page.get("hasNextPage")),
        "comments_end_cursor": page.get("endCursor") or "",
    }


def _complete_review_thread_comments(
    cfg: ReviewConfig, token: str, thread: JsonObject
) -> None:
    """Append every review-thread comment so marker lookup has no blind spot."""
    cursor = str(thread.get("comments_end_cursor") or "")
    while thread.get("comments_has_next_page"):
        thread_id = str(thread.get("node_id") or "")
        if not thread_id or not cursor:
            raise RuntimeError("GitHub review-thread comment pagination is incomplete")
        data = graphql(
            cfg.github_api_url,
            token,
            _REVIEW_THREAD_COMMENTS_QUERY,
            {"threadId": thread_id, "cursor": cursor},
        )
        node = data.get("node") or {}
        comments_data = node.get("comments") if isinstance(node, dict) else None
        if not isinstance(comments_data, dict):
            raise RuntimeError("GitHub review-thread comments were unavailable during pagination")
        for item in comments_data.get("nodes") or []:
            if isinstance(item, dict):
                thread["comments"].append(_normalize_review_comment(cast(JsonObject, item)))
        page = comments_data.get("pageInfo") or {}
        thread["comments_has_next_page"] = bool(page.get("hasNextPage"))
        cursor = str(page.get("endCursor") or "")
        thread["comments_end_cursor"] = cursor
    thread.pop("comments_has_next_page", None)
    thread.pop("comments_end_cursor", None)


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
                thread = _normalize_review_thread(cast(JsonObject, node))
                _complete_review_thread_comments(cfg, token, thread)
                threads.append(thread)
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


def root_review_comment(thread: JsonObject) -> JsonObject | None:
    """Return the original review comment, never a reply.

    GitHub review threads are rooted at the only comment without ``replyTo``.
    Returning ``None`` for malformed data is deliberate: callers must fail
    closed rather than infer ownership from a later Bubo reply.
    """
    roots = [
        comment
        for comment in thread.get("comments") or []
        if not str(comment.get("in_reply_to_node_id") or "")
    ]
    if len(roots) != 1 or not isinstance(roots[0], dict):
        return None
    return cast(JsonObject, roots[0])


def bubo_owned_root(
    thread: JsonObject, comment_id: str, bot_username: str
) -> JsonObject | None:
    """Prove ``comment_id`` is a Bubo-authored root finding in ``thread``.

    The persisted identifier may be GitHub REST's database id or GraphQL's
    node id.  A matching reply is intentionally insufficient, preventing a
    Bubo reply on someone else's thread from granting resolve authority.
    """
    root = root_review_comment(thread)
    if root is None or (root.get("login") or "") != bot_username:
        return None
    target = str(comment_id)
    if str(root.get("database_id")) != target and str(root.get("node_id")) != target:
        return None
    return root


def bubo_reply_with_marker(
    thread: JsonObject, bot_username: str, reply_marker: str
) -> JsonObject | None:
    """Return a prior Bubo reply bearing the reconciliation idempotency marker."""
    if not reply_marker:
        return None
    for comment in thread.get("comments") or []:
        if (comment.get("login") or "") != bot_username:
            continue
        if reply_marker in str(comment.get("body") or ""):
            return cast(JsonObject, comment)
    return None


def _reply_actor_and_time(reply: JsonObject) -> tuple[str, str]:
    """Extract portable reply evidence from GraphQL or REST comment shapes."""
    actor = str(
        reply.get("login")
        or ((reply.get("user") or {}).get("login") or "")
        or ((reply.get("author") or {}).get("login") or "")
        or "unknown"
    )
    timestamp = str(reply.get("created_at") or reply.get("createdAt") or "unknown")
    return actor, timestamp


def is_human_reply(reply: JsonObject) -> bool:
    """Return true only for provider-confirmed human actors.

    Login names are not actor identity: GitHub applications and mannequins can
    post arbitrary marker text. Unknown/missing actor types fail closed.
    """
    if "actor_type" in reply:
        return str(reply.get("actor_type") or "") == "User"
    user = reply.get("user")
    return isinstance(user, dict) and str(user.get("type") or "") == "User"


def _classifier_explicitly_agrees(result: object) -> bool:
    """Accept only the literal ``agrees`` classifier verdict, never ``accepted``.

    ``accepted`` can mean the developer plans to fix a valid finding.  It is
    not evidence that the developer agrees with the finding's correctness, so
    it remains intentionally unknown for this metric.
    """
    if isinstance(result, str):
        return result.strip().lower() == "agrees"
    if isinstance(result, dict):
        return any(
            str(result.get(key) or "").strip().lower() == "agrees"
            for key in ("verdict", "disposition", "result")
        )
    return False


def developer_disposition_from_replies(
    replies: list[JsonObject],
    *,
    reply_classifier_result: object = None,
) -> JsonObject:
    """Classify developer agreement only from explicit, attributable evidence.

    Resolution, merge state, silence, and code movement are deliberately not
    inputs.  A rejection marker wins over agreement so existing dispute and
    false-positive signals can never be overwritten by a later generic
    agreement marker.
    """
    for reply in replies:
        text = str(reply.get("body") or "").lower()
        if "[llm-review:false-positive]" in text:
            actor, timestamp = _reply_actor_and_time(reply)
            return {
                "developer_agreed": False,
                "developer_disposition": "false_positive",
                "disposition_evidence": json.dumps(
                    {"kind": "human_marker", "actor": actor, "time": timestamp}, sort_keys=True
                ),
            }
        if "[llm-review:disputed]" in text:
            actor, timestamp = _reply_actor_and_time(reply)
            return {
                "developer_agreed": False,
                "developer_disposition": "disagrees",
                "disposition_evidence": json.dumps(
                    {"kind": "human_marker", "actor": actor, "time": timestamp}, sort_keys=True
                ),
            }
    for reply in replies:
        if "[llm-review:agreed]" in str(reply.get("body") or "").lower():
            actor, timestamp = _reply_actor_and_time(reply)
            return {
                "developer_agreed": True,
                "developer_disposition": "agrees",
                "disposition_evidence": json.dumps(
                    {"kind": "human_marker", "actor": actor, "time": timestamp}, sort_keys=True
                ),
            }
    if replies and _classifier_explicitly_agrees(reply_classifier_result):
        return {
            "developer_agreed": True,
            "developer_disposition": "agrees",
            "disposition_evidence": json.dumps(
                {"kind": "reply_classifier", "actor": "unknown", "time": "unknown"},
                sort_keys=True,
            ),
        }
    return {
        "developer_agreed": False,
        "developer_disposition": "unknown",
        "disposition_evidence": "unknown",
    }


_RESOLVE_REVIEW_THREAD_MUTATION = """
mutation($threadId:ID!){
  resolveReviewThread(input:{threadId:$threadId}){
    thread{ id isResolved }
  }
}
"""


def reply_to_pr_review_comment(
    cfg: ReviewConfig,
    token: str,
    project: str,
    number: int,
    comment_id: str,
    body: str,
) -> JsonObject:
    """Reply to one inline review comment through GitHub's supported REST API."""
    repo = _owner_repo(project)
    encoded = urllib.parse.quote(str(comment_id), safe="")
    data, _ = _request(
        f"{cfg.github_api_url.rstrip('/')}/repos/{repo}/pulls/{number}/comments/{encoded}/replies",
        token,
        "POST",
        {"body": body},
    )
    if not isinstance(data, dict):
        raise RuntimeError(
            describe(
                "GitHub review-comment reply response was not an object",
                reason="the API returned an unexpected response shape",
                fix="check token scope and GitHub API availability.",
            )
        )
    return cast(JsonObject, data)


def resolve_pr_review_thread(cfg: ReviewConfig, token: str, thread_node_id: str) -> None:
    """Resolve a GitHub review thread through GraphQL only.

    There is no REST fallback: REST cannot resolve a review thread safely.
    Callers must re-fetch the thread afterward to verify GitHub persisted it.
    """
    data = graphql(
        cfg.github_api_url,
        token,
        _RESOLVE_REVIEW_THREAD_MUTATION,
        {"threadId": thread_node_id},
    )
    result = (data.get("resolveReviewThread") or {}).get("thread") or {}
    if not result.get("id") or not result.get("isResolved"):
        raise RuntimeError(
            describe(
                "GitHub did not confirm review-thread resolution",
                reason="the resolveReviewThread mutation returned an incomplete result",
                fix="retry after checking GitHub API status and token permissions.",
            )
        )


def classify_graphql_thread_outcome(
    thread: JsonObject,
    bot_username: str,
    pr_state: str,
    *,
    reply_classifier_result: object = None,
) -> JsonObject:
    """Classify a GraphQL review thread, including real resolution state.

    Unlike :func:`classify_review_thread_outcome` (REST, resolution-blind),
    this reads the thread's ``is_resolved`` directly. Markers and developer
    replies are detected from non-bot comments in the thread.
    """
    comments = thread.get("comments") or []
    replies = [
        c
        for c in comments
        if (c.get("login") or "") != bot_username and is_human_reply(cast(JsonObject, c))
    ]
    developer_replied = bool(replies)
    reply_text = "\n".join(str(c.get("body") or "").lower() for c in replies)
    false_positive = "[llm-review:false-positive]" in reply_text
    duplicate = "[llm-review:duplicate]" in reply_text
    disputed = "[llm-review:disputed]" in reply_text or false_positive
    resolved = bool(thread.get("is_resolved"))
    bot_comment = first_bot_comment(thread, bot_username)
    disposition = developer_disposition_from_replies(
        replies, reply_classifier_result=reply_classifier_result
    )
    return {
        "resolved": resolved,
        "deleted": False,
        "developer_replied": developer_replied,
        "disputed": disputed,
        "false_positive": false_positive,
        "duplicate": duplicate,
        "resolved_at": None,
        "merged_unresolved": pr_state == "merged" and not resolved,
        "resolution_observed": True,
        **disposition,
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
            raise RuntimeError(
                describe(
                    "GitHub API page did not return a list",
                    reason=(
                        "the API returned an unexpected response shape (often an auth error "
                        "page, a redirect, or an outage rendered as non-JSON)"
                    ),
                    fix=(
                        "check the API URL, token validity/scope, and the host's status; "
                        "inspect the raw response."
                    ),
                )
            )
        for item in data:
            if not isinstance(item, dict):
                continue
            if str(item.get("updated_at") or "") < updated_after:
                return out
            out.append(cast(JsonObject, item))
        url = _next_link(headers)
    return out


def classify_review_thread_outcome(
    comment: JsonObject,
    replies: list[JsonObject],
    bot_username: str,
    pr_state: str,
    *,
    reply_classifier_result: object = None,
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
        r
        for r in active_replies
        if ((r.get("user") or {}).get("login") or "") != bot_username
        and is_human_reply(r)
    ]
    developer_replied = bool(developer_replies)
    reply_text = "\n".join(str(r.get("body") or "").lower() for r in developer_replies)
    false_positive = "[llm-review:false-positive]" in reply_text
    duplicate = "[llm-review:duplicate]" in reply_text
    disputed = "[llm-review:disputed]" in reply_text or false_positive
    deleted = bool(comment.get("deleted", False))
    disposition = developer_disposition_from_replies(
        developer_replies, reply_classifier_result=reply_classifier_result
    )
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
        "resolution_observed": False,
        **disposition,
        # Original-case text for the LLM reply classifier (transient; ignored
        # by record_finding_outcome). Bot's finding + the developer replies.
        "_finding_text": str(comment.get("body") or ""),
        "_reply_text": "\n\n".join(str(r.get("body") or "") for r in developer_replies),
    }
