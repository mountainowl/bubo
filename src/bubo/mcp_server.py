"""MCP server exposing bubo's review state and trigger.

Two interfaces on either stdio or HTTP transport:

* **Metrics** — ``health``, ``list_recent_reviews``, ``get_review``,
  ``get_findings``, ``get_finding_outcomes``, ``get_metrics``. Read-only
  against the SQLite state :mod:`bubo.db` owns.
* **Review** — ``review_change`` runs the configured
  ``[agents].reviewer_command`` against one MR/PR (by URL or
  ``(provider, project, number)``) and returns the parsed findings JSON.

MCP-triggered reviews intentionally do not write to ``reviewed_mrs``, so
the metrics tools reflect only poller-driven reviews. This keeps ad-hoc
chat reviews from polluting operator dashboards; lift if the use case
clarifies.

Transport selection at startup via env vars (set by ``apply_runtime_env``
from ``[mcp_server]`` in ``config/env.toml``):

* ``BUBO_MCP_TRANSPORT=stdio`` (default) — Codex spawns the
  process per session; no auth (filesystem-scoped).
* ``BUBO_MCP_TRANSPORT=http`` — long-lived HTTP+SSE server bound
  to ``BUBO_MCP_HOST:BUBO_MCP_PORT``. Every request must
  carry ``Authorization: Bearer <BUBO_MCP_BEARER_TOKEN>``; missing
  or mismatched returns ``401``. No TLS — put behind a reverse proxy.

Launch: ``bubo-mcp`` console script (also wrapped by
``bin/bubo mcp``).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from dataclasses import replace
from typing import Any

from mcp.server.fastmcp import FastMCP

from bubo import db
from bubo.config_values import ConfigError
from bubo.events import log
from bubo.paths import CONFIG as ENV_CONFIG
from bubo.review_config import (
    DEFAULT_PROVIDER,
    SUPPORTED_PROVIDERS,
    load_review_config,
)
from bubo.scm import get_provider
from bubo.secrets import redact_secrets
from bubo.subproc import run_bounded

mcp: FastMCP = FastMCP("bubo")

# URL parsers for the two supported providers. Loose host check on the
# GitLab regex so self-hosted GitLab instances (gitlab.example.com,
# gitlab.internal, …) parse without an allowlist.
_GITHUB_PR_URL = re.compile(
    r"^https?://github\.com/(?P<project>[^/]+/[^/]+)/pull/(?P<number>\d+)\b"
)
_GITLAB_MR_URL = re.compile(
    r"^https?://(?P<host>[^/]+)/(?P<project>.+?)/-/merge_requests/(?P<number>\d+)\b"
)


def _parse_change_url(url: str) -> tuple[str, str, int]:
    """Extract ``(provider, project, number)`` from an MR/PR web URL.

    Supports both ``github.com`` PR URLs and any-host ``…/-/merge_requests/N``
    GitLab URLs (handles self-hosted instances and nested groups via the
    non-greedy capture).

    Raises ``ValueError`` for any URL that does not match either shape, so
    the caller can return a clean MCP error instead of silently mis-routing
    the request.
    """
    match = _GITHUB_PR_URL.match(url)
    if match:
        return "github", match.group("project"), int(match.group("number"))
    match = _GITLAB_MR_URL.match(url)
    if match:
        # The "host" group is captured for completeness even though we do
        # not currently propagate it — the reviewer reads its GitLab base
        # URL from `[scm].gitlab_url` in env.toml.
        return "gitlab", match.group("project"), int(match.group("number"))
    raise ValueError(
        f"unrecognized MR/PR URL: {url!r} "
        "(expected github.com/.../pull/N or .../-/merge_requests/N)"
    )


def _resolve_provider(provider: str) -> str:
    """Apply the ``provider=auto`` resolution rules for the no-URL path.

    Caller is responsible for short-circuiting when a URL was given —
    ``_parse_change_url`` already returned the authoritative provider
    there, and this helper would only re-validate it.

    * ``"auto"`` → ``[scm].provider`` from ``config/env.toml``, falling
      back to :data:`DEFAULT_PROVIDER` when the config is missing or
      unreadable.
    * Any other value → validated against :data:`SUPPORTED_PROVIDERS`
      and returned as-is.
    """
    if provider == "auto":
        try:
            return load_review_config(ENV_CONFIG).provider
        except ConfigError, OSError:
            return DEFAULT_PROVIDER
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"provider must be one of {SUPPORTED_PROVIDERS} or 'auto', got {provider!r}"
        )
    return provider


def _extract_findings(raw_output: str) -> list[dict[str, Any]] | None:
    """Best-effort parse of the codex JSON output.

    The meta prompt instructs the agent to return *only* a JSON array of
    findings, but real Codex transcripts can include log lines, tool
    output, or empty leading whitespace. We:

    1. Find the first ``[`` in the output.
    2. Try to decode the suffix as JSON.

    Returns ``None`` (not an exception) when no JSON array is recoverable
    — the caller surfaces ``raw_output`` so the MCP client can still see
    what happened.
    """
    start = raw_output.find("[")
    if start < 0:
        return None
    try:
        parsed = json.loads(raw_output[start:])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


@mcp.tool()
def health() -> dict[str, Any]:
    """Report whether the reviewer has produced state recently.

    Mirrors ``bubo-poller --health`` but returns structured data
    instead of an exit code. The MCP client decides what "stale" means
    for its display.

    Returns a dict with:

    * ``status``: ``"empty"`` if no reviews have ever been recorded;
      otherwise ``"ok"``.
    * ``last_status``: the most recent ``reviewed_mrs.status`` (e.g.
      ``"success"``, ``"no_findings"``, ``"failed"``) — present only
      when ``status != "empty"``.
    * ``last_updated_at``: ISO-8601 timestamp of the most recent row —
      present only when ``status != "empty"``.
    * ``age_seconds``: float, seconds since ``last_updated_at`` —
      present only when ``status != "empty"``.
    """
    db.init_db()
    row = db.latest_reviewed_row()
    if row is None:
        return {"status": "empty", "message": "no reviews recorded yet"}
    status, updated_at = row
    return {
        "status": "ok",
        "last_status": status,
        "last_updated_at": updated_at,
        "age_seconds": db.status_age_seconds(updated_at),
    }


@mcp.tool()
def list_recent_reviews(
    limit: int = 20,
    status: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` rows from ``reviewed_mrs`` newest-first.

    Args:
        limit: Maximum rows to return. Clamped to ``[1, 200]``.
        status: Optional exact-match filter against ``reviewed_mrs.status``
            (e.g. ``"success"``, ``"no_findings"``, ``"failed"``,
            ``"running"``, ``"queued"``). Case-sensitive — pass the value
            the writer used.
        project: Optional exact-match filter on the project field
            (GitLab path-with-namespace or GitHub ``owner/repo``).
    """
    db.init_db()
    return db.list_recent_reviews(limit=limit, status=status, project=project)


@mcp.tool()
def get_review(project: str, iid: int, sha: str | None = None) -> dict[str, Any]:
    """Return the most recent ``reviewed_mrs`` row for one MR/PR.

    If ``sha`` is omitted, the row with the freshest ``updated_at`` for
    ``(project, iid)`` wins — this is what "what does the reviewer
    currently think about MR 42" usually means. Pass ``sha`` explicitly
    to pin a specific revision.

    Returns ``{"found": False, "project": ..., "iid": ..., "sha": ...}``
    when no row matches, so the client can distinguish "no review yet"
    from a tool error.
    """
    db.init_db()
    row = db.get_review_row(project=project, iid=iid, sha=sha)
    if row is None:
        return {"found": False, "project": project, "iid": iid, "sha": sha}
    return {"found": True, **row}


@mcp.tool()
def get_findings(project: str, iid: int, sha: str | None = None) -> list[dict[str, Any]]:
    """Return one row per finding for ``(project, iid[, sha])``.

    When ``sha`` is omitted, findings for the most recent reviewed SHA
    are returned (matches :func:`get_review`'s default).

    Each row carries the structured fields the reviewer extracted
    (``file``, ``line``, ``severity``, ``category``, ``confidence``,
    ``type``, ``status``) plus the rendered ``body`` actually posted as
    a comment, and the ``discussion_id`` / ``note_id`` from the SCM.
    """
    db.init_db()
    return db.findings_for(project=project, iid=iid, sha=sha)


@mcp.tool()
def get_finding_outcomes(project: str, iid: int, sha: str | None = None) -> list[dict[str, Any]]:
    """Return per-finding resolution state for ``(project, iid[, sha])``.

    Populated by ``bubo-poller --sync-outcomes`` (and the GitHub
    equivalent). Each row tells you whether the developer resolved /
    deleted / replied to / disputed / marked false-positive / marked
    duplicate the finding's discussion, plus ``merged_unresolved`` for
    findings that were merged in without resolution.

    Empty when no sync has run for this MR/PR yet — that is **not** an
    error, just "we don't know the outcome state yet."
    """
    db.init_db()
    return db.outcomes_for(project=project, iid=iid, sha=sha)


@mcp.tool()
def get_metrics(since_hours: int = 24, project: str | None = None) -> dict[str, Any]:
    """Aggregate counts and totals across recent reviews.

    Args:
        since_hours: Look-back window. Clamped to ``[1, 720]`` (one month
            of history).
        project: Optional exact-match project filter.

    Returns a dict with:

    * ``window_hours`` / ``project`` — the resolved query parameters.
    * ``reviews_total`` — count of ``reviewed_mrs`` rows in the window.
    * ``by_status`` — ``{status: count}`` for every status seen.
    * ``findings_total`` — count of ``review_findings`` rows in the
      window.
    * ``tokens_total_sum`` — sum of ``review_runs.tokens_total`` in the
      window (LLM tokens billed).
    * ``cost_usd_sum`` — sum of ``review_runs.cost_usd`` in the window.
    """
    db.init_db()
    return db.metrics_summary(since_hours=since_hours, project=project)


@mcp.tool()
def review_change(
    url: str | None = None,
    number: int | None = None,
    provider: str = "auto",
    project: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, Any]:
    """Trigger a one-shot review of an MR/PR by URL or by (provider, project, number).

    Two input shapes are accepted:

    1. ``url`` — full web URL of the MR/PR. ``provider``, ``project``,
       and ``number`` are derived from it; any explicit values are
       overridden so the URL is the single source of truth.
    2. ``provider`` + ``project`` + ``number`` — explicit identifiers.
       ``provider="auto"`` (the default) falls back to ``[scm].provider``
       in ``config/env.toml``.

    Args:
        url: ``https://github.com/<owner>/<repo>/pull/<n>`` or
            ``https://<gitlab-host>/<group>/<proj>/-/merge_requests/<n>``.
        number: MR (GitLab) or PR (GitHub) integer identifier.
        provider: ``"gitlab"`` | ``"github"`` | ``"auto"``.
        project: Project path-with-namespace (GitLab) or
            ``owner/repo`` (GitHub).
        timeout_seconds: Wall-clock budget for the underlying
            ``reviewer_command`` subprocess. Defaults to
            ``[review].timeout_seconds`` from ``config/env.toml``
            (typically 1800).

    Returns:
        ``{provider, project, number, exit_code, duration_seconds,
        findings, raw_output}``. ``findings`` is the parsed JSON array
        when Codex returned valid JSON; ``None`` otherwise (in which case
        ``raw_output`` contains the full transcript for debugging).

    Note: MCP-triggered reviews do **not** write to ``reviewed_mrs`` —
    the metrics tools will not see them. Use the poller for state-tracked
    reviews. This is by design (see module docstring).
    """
    if url:
        provider, project, number = _parse_change_url(url)
    else:
        provider = _resolve_provider(provider)
    if not project or number is None:
        raise ValueError(
            "must provide either `url` or (`project` and `number`); "
            f"got provider={provider!r}, project={project!r}, number={number!r}"
        )
    # Build the same contract-carrying prompt the poller uses, then run the
    # operator's configured reviewer_command directly (no bundled wrapper).
    cfg = replace(load_review_config(ENV_CONFIG), provider=provider)
    scm = get_provider(cfg)
    token = scm.token()
    change = scm.get_change(cfg, token, project, number)
    prompt = scm.review_prompt(project, change, cfg)
    timeout = int(timeout_seconds) if timeout_seconds is not None else cfg.timeout_seconds
    started = time.monotonic()
    result = run_bounded([*cfg.reviewer_command, prompt], timeout=timeout)
    duration = time.monotonic() - started
    raw = redact_secrets(result.stdout or "")

    return {
        "provider": provider,
        "project": project,
        "number": number,
        "exit_code": result.returncode,
        "duration_seconds": round(duration, 3),
        "findings": _extract_findings(raw),
        "raw_output": raw,
    }


class _BearerAuthASGI:
    """Tiny ASGI wrapper that rejects HTTP requests without a matching bearer.

    Uses :func:`secrets.compare_digest` so token comparison is
    constant-time. Lifespan events (startup/shutdown) bypass auth — those
    have no incoming credentials and the inner app needs them to wire up
    SSE properly.

    Bypasses FastMCP's built-in ``token_verifier`` / ``AuthSettings`` path
    on purpose: the SDK assumes an OAuth flow with ``issuer_url`` /
    ``resource_server_url`` set, which is overkill for "static bearer,
    any caller with the token gets all tools."
    """

    def __init__(self, inner: Any, expected_token: str) -> None:
        # Pre-format the comparison string so the per-request hot path is
        # a single compare_digest call.
        self._inner = inner
        self._expected = f"Bearer {expected_token}"

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            # Headers are list[tuple[bytes, bytes]] in ASGI.
            header_value = b""
            for name, value in scope.get("headers", []):
                if name == b"authorization":
                    header_value = value
                    break
            presented = header_value.decode("latin1", errors="replace")
            if not secrets.compare_digest(presented, self._expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self._inner(scope, receive, send)


def _http_settings() -> tuple[str, int, str]:
    """Resolve HTTP host/port/bearer-token from env. Fails loud on misconfig.

    Returns ``(host, port, token)``. Raises :class:`SystemExit` rather than
    starting an unauthenticated server when ``BUBO_MCP_BEARER_TOKEN``
    is empty — there is no safe default here.
    """
    host = os.environ.get("BUBO_MCP_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port_raw = os.environ.get("BUBO_MCP_PORT", "8765").strip() or "8765"
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise SystemExit(f"BUBO_MCP_PORT must be an integer, got {port_raw!r}") from exc
    token = os.environ.get("BUBO_MCP_BEARER_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "BUBO_MCP_BEARER_TOKEN must be set when "
            "BUBO_MCP_TRANSPORT=http (the static bearer token "
            "every request must present)"
        )
    return host, port, token


# Hosts that bind only to the local machine; anything else exposes the
# bearer-protected endpoint to whatever network the interface is on. Kept
# as a module constant so :func:`_bind_is_external` and tests can share it.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _bind_is_external(host: str) -> bool:
    """Return ``True`` if ``host`` reaches beyond the local machine.

    A bind to ``0.0.0.0`` / ``::`` / any external interface name is
    treated as external; the operator owes the network in front of it
    TLS termination (see the env.example.toml comment).
    """
    return host.strip().lower() not in _LOOPBACK_HOSTS


def _run_http() -> None:
    """Run the streamable-HTTP transport with bearer-token auth.

    Imports of ``uvicorn`` and Starlette plumbing are deferred to this
    function so the stdio path (the common one) does not pay the
    import-time cost.
    """
    import uvicorn

    host, port, token = _http_settings()
    if _bind_is_external(host):
        # Single structured warning at boot so an operator who skimmed
        # the env.example.toml comment still gets a visible "you are
        # exposing this externally" signal in their journalctl tail.
        log("mcp_http_bound_external", host=host, port=port)
    app = _BearerAuthASGI(mcp.streamable_http_app(), expected_token=token)
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> None:
    """Entry point for the ``bubo-mcp`` console script.

    Reads ``BUBO_MCP_TRANSPORT`` (default ``"stdio"``) and dispatches:

    * ``"stdio"`` — blocks until the parent (Codex / Claude Desktop)
      closes the pipes.
    * ``"http"`` — binds an HTTP+SSE server; blocks until interrupted.

    Any other value exits with a clear error.
    """
    transport = os.environ.get("BUBO_MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        mcp.run()
    elif transport == "http":
        _run_http()
    else:
        raise SystemExit(f"BUBO_MCP_TRANSPORT must be 'stdio' or 'http', got {transport!r}")


__all__ = [
    "get_finding_outcomes",
    "get_findings",
    "get_metrics",
    "get_review",
    "health",
    "list_recent_reviews",
    "main",
    "mcp",
    "review_change",
]
