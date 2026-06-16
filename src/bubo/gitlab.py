"""GitLab REST client used by the poller and outcome sync.

Stdlib-only — no `requests` or `httpx` dependency, deliberately. Everything
the poller needs (list MRs, fetch diffs, fetch and create discussions) is a
handful of GET/POST calls against ``/api/v4``. The trade-off is that
pagination lives here as explicit code rather than coming "for free" from a
library; the shared retry/backoff and ``Retry-After`` handling live in
:mod:`bubo._http`.

Two things this module does NOT do, despite reading like a full client:

* It does NOT post inline review comments — that path goes through the MCP
  server (see :func:`bubo.poller.mcp_call_tool`). The REST
  ``create_merge_request_discussion`` here is only a fallback for the case
  where MCP returns no discussion ID.
* It does NOT manage credentials. Callers pass the token explicitly; we
  never read ``GITLAB_TOKEN`` from the environment.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, cast

from bubo import _http
from bubo.review_config import ReviewConfig
from bubo.types import JsonObject


def api(
    base: str, token: str, method: str, path: str, body: JsonObject | None = None
) -> tuple[Any, dict[str, str]]:
    """Issue a single GitLab REST call with retry on transient errors.

    Returns ``(parsed_json, response_headers)``. The retry/backoff policy
    (attempts, retryable statuses, ``Retry-After`` handling) lives in
    :func:`bubo._http.request_json`; this wrapper only supplies GitLab's
    ``/api/v4`` URL and ``PRIVATE-TOKEN`` auth header. Other HTTP errors
    propagate immediately so auth/permission failures fail fast.
    """
    return _http.request_json(
        base.rstrip("/") + "/api/v4" + path,
        method=method,
        headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"},
        body=body,
        provider="GitLab",
    )


def api_pages(base: str, token: str, path: str) -> list[JsonObject]:
    out: list[JsonObject] = []
    page = 1
    sep = "&" if "?" in path else "?"
    while True:
        data, headers = api(base, token, "GET", f"{path}{sep}per_page=100&page={page}")
        if not isinstance(data, list):
            raise RuntimeError("GitLab API page did not return a list")
        out.extend(cast(JsonObject, item) for item in data if isinstance(item, dict))
        next_page = headers.get("X-Next-Page") or headers.get("x-next-page")
        if not next_page:
            return out
        page = int(next_page)


def open_mrs(cfg: ReviewConfig, project: str, token: str) -> list[JsonObject]:
    encoded = urllib.parse.quote(project, safe="")
    qs = urllib.parse.urlencode({"state": "opened", "scope": "all"})
    return api_pages(cfg.gitlab_url, token, f"/projects/{encoded}/merge_requests?{qs}")


def merge_requests_updated_after(
    cfg: ReviewConfig, project: str, token: str, updated_after: str
) -> list[JsonObject]:
    encoded = urllib.parse.quote(project, safe="")
    qs = urllib.parse.urlencode({"state": "all", "scope": "all", "updated_after": updated_after})
    return api_pages(cfg.gitlab_url, token, f"/projects/{encoded}/merge_requests?{qs}")


def get_mr(cfg: ReviewConfig, token: str, project: str, iid: int) -> JsonObject:
    encoded = urllib.parse.quote(project, safe="")
    data, _ = api(cfg.gitlab_url, token, "GET", f"/projects/{encoded}/merge_requests/{iid}")
    if not isinstance(data, dict):
        raise RuntimeError("GitLab MR response was not an object")
    return cast(JsonObject, data)


def get_mr_diffs(cfg: ReviewConfig, token: str, project: str, iid: int) -> list[JsonObject]:
    encoded = urllib.parse.quote(project, safe="")
    return api_pages(cfg.gitlab_url, token, f"/projects/{encoded}/merge_requests/{iid}/diffs")


def get_mr_commits(cfg: ReviewConfig, token: str, project: str, iid: int) -> list[JsonObject]:
    """Return the commits on a merge request (each carries ``message``/``title``)."""
    encoded = urllib.parse.quote(project, safe="")
    return api_pages(cfg.gitlab_url, token, f"/projects/{encoded}/merge_requests/{iid}/commits")


def get_mr_discussion(
    cfg: ReviewConfig, token: str, project: str, iid: int, discussion_id: str
) -> JsonObject:
    encoded = urllib.parse.quote(project, safe="")
    encoded_discussion = urllib.parse.quote(discussion_id, safe="")
    data, _ = api(
        cfg.gitlab_url,
        token,
        "GET",
        f"/projects/{encoded}/merge_requests/{iid}/discussions/{encoded_discussion}",
    )
    if not isinstance(data, dict):
        raise RuntimeError("GitLab discussion response was not an object")
    return cast(JsonObject, data)


def get_mr_discussions(cfg: ReviewConfig, token: str, project: str, iid: int) -> list[JsonObject]:
    encoded = urllib.parse.quote(project, safe="")
    return api_pages(cfg.gitlab_url, token, f"/projects/{encoded}/merge_requests/{iid}/discussions")


def find_discussion_by_body(
    cfg: ReviewConfig, token: str, project: str, iid: int, body: str
) -> str:
    encoded = urllib.parse.quote(project, safe="")
    for discussion in api_pages(
        cfg.gitlab_url, token, f"/projects/{encoded}/merge_requests/{iid}/discussions"
    ):
        discussion_id = discussion.get("id")
        for note in discussion.get("notes") or []:
            if note.get("body") == body and discussion_id:
                return str(discussion_id)
    return ""


def create_merge_request_discussion(
    cfg: ReviewConfig,
    token: str,
    project: str,
    iid: int,
    body: str,
    position: JsonObject,
) -> JsonObject:
    encoded = urllib.parse.quote(project, safe="")
    data, _ = api(
        cfg.gitlab_url,
        token,
        "POST",
        f"/projects/{encoded}/merge_requests/{iid}/discussions",
        {"body": body, "position": position},
    )
    return cast(JsonObject, data) if isinstance(data, dict) else {}


def get_mr_notes(cfg: ReviewConfig, token: str, project: str, iid: int) -> list[JsonObject]:
    """Return all notes (non-inline comments) on a merge request.

    Used to dedup the change-level "no issues found" comment so a re-review
    of the same MR (after rebase or polling) does not stack duplicates.
    """
    encoded = urllib.parse.quote(project, safe="")
    return api_pages(cfg.gitlab_url, token, f"/projects/{encoded}/merge_requests/{iid}/notes")


def find_note_by_body(
    cfg: ReviewConfig,
    token: str,
    project: str,
    iid: int,
    body: str,
    *,
    bot_username: str | None = None,
) -> str:
    """Locate an existing non-positional MR note authored by the bot.

    Returns the note ID as a string, or ``""`` if none matches. Mirrors
    :func:`find_discussion_by_body` for the inline path. Filters out
    GitLab system notes (so "Foo approved this MR" never matches) and,
    when ``bot_username`` is provided, restricts the match to notes the
    bot itself authored — a human or other bot reproducing the body must
    not satisfy the dedup, or the reviewer would silently stop posting
    its own no-findings acknowledgement.
    """
    for note in get_mr_notes(cfg, token, project, iid):
        if note.get("system"):
            continue
        if bot_username and ((note.get("author") or {}).get("username") or "") != bot_username:
            continue
        if note.get("body") == body and note.get("id"):
            return str(note["id"])
    return ""


def create_mr_note(cfg: ReviewConfig, token: str, project: str, iid: int, body: str) -> JsonObject:
    """Post a non-positional ("general") note on a merge request."""
    encoded = urllib.parse.quote(project, safe="")
    data, _ = api(
        cfg.gitlab_url,
        token,
        "POST",
        f"/projects/{encoded}/merge_requests/{iid}/notes",
        {"body": body},
    )
    return cast(JsonObject, data) if isinstance(data, dict) else {}


def classify_discussion_outcome(
    discussion: JsonObject, bot_username: str, mr_state: str
) -> JsonObject:
    notes = discussion.get("notes") or []
    resolved = bool(discussion.get("resolved", False)) or any(
        bool(note.get("resolvable")) and bool(note.get("resolved")) for note in notes
    )
    active_notes = [note for note in notes if not note.get("deleted")]
    reply_notes = [
        note
        for note in active_notes
        if ((note.get("author") or {}).get("username") or "") != bot_username
    ]
    bot_notes = [
        note
        for note in active_notes
        if ((note.get("author") or {}).get("username") or "") == bot_username
    ]
    developer_replied = bool(reply_notes)
    note_text = "\n".join(str(note.get("body") or "").lower() for note in active_notes)
    false_positive = "[llm-review:false-positive]" in note_text
    duplicate = "[llm-review:duplicate]" in note_text
    disputed = "[llm-review:disputed]" in note_text or false_positive
    return {
        "resolved": resolved,
        "deleted": bool(discussion.get("deleted", False)) or (bool(notes) and not active_notes),
        "developer_replied": developer_replied,
        "disputed": disputed,
        "false_positive": false_positive,
        "duplicate": duplicate,
        "resolved_at": discussion.get("resolved_at"),
        "merged_unresolved": mr_state == "merged" and not resolved,
        # Original-case text for the LLM reply classifier (transient; ignored
        # by record_finding_outcome). Bot's finding + the developer replies.
        "_finding_text": str(bot_notes[0].get("body") or "") if bot_notes else "",
        "_reply_text": "\n\n".join(str(note.get("body") or "") for note in reply_notes),
    }
