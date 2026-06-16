"""Shared stdlib HTTP transport for the REST clients.

The GitLab and GitHub clients (:mod:`bubo.gitlab`, :mod:`bubo.github`) both
speak JSON over ``urllib`` with the *same* retry/backoff policy — total
attempts, the retryable status set, ``Retry-After`` handling, and exponential
backoff were byte-for-byte duplicated across the two modules and had already
begun to drift. This module owns that shared half exactly once so the two
clients cannot diverge on retry semantics.

What stays in each client (their genuine dialect, not duplication): URL
construction, auth headers, pagination (GitLab's ``X-Next-Page`` vs GitHub's
``Link`` header), and any provider-specific retryable condition. The last is
passed in via ``extra_retryable`` — GitHub's primary rate-limit arrives as a
``403`` with ``X-RateLimit-Remaining: 0`` rather than a ``429``, and that one
difference is preserved deliberately (GitLab must NOT retry a bare 403).

Stdlib-only — no ``requests``/``httpx`` dependency, matching the rest of bubo.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from bubo.types import JsonObject

# Total attempts (initial + retries) for a single REST call before giving up.
API_MAX_ATTEMPTS = 3
# Status codes that always trigger a retry. 429 is rate-limit; 5xx are
# server-side transient errors. Other 4xx (e.g. 401) fail immediately — they
# will not fix themselves and would only burn the retry budget.
API_RETRY_STATUSES = {429, 500, 502, 503, 504}

# Predicate for provider-specific retryable HTTP errors beyond the shared
# status set (e.g. GitHub's primary rate-limit, which is a 403 not a 429).
RetryPredicate = Callable[[urllib.error.HTTPError], bool]


def retry_delay(headers: object, attempt: int) -> float:
    """Seconds to wait before the next retry of a 0-based ``attempt``.

    Honors a numeric ``Retry-After`` header when present (clamped to the
    ``[0.0, 60.0]`` range so a hostile or malformed value can't stall the
    poller); otherwise falls back to exponential backoff (``0.5 * 2**attempt``)
    capped at 10 seconds.
    """
    retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
    if retry_after:
        try:
            return min(60.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    delay = 0.5 * (2**attempt)
    return 10.0 if delay > 10.0 else delay


def request_json(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: JsonObject | None = None,
    extra_retryable: RetryPredicate | None = None,
    provider: str = "API",
) -> tuple[Any, dict[str, str]]:
    """Issue one JSON REST request to ``url`` with retry on transient failures.

    Returns ``(parsed_json, response_headers)``. Retries up to
    :data:`API_MAX_ATTEMPTS` times on statuses in :data:`API_RETRY_STATUSES`,
    on connection errors, or when ``extra_retryable`` matches the raised
    :class:`urllib.error.HTTPError`, honoring ``Retry-After`` via
    :func:`retry_delay`. Any other HTTP error propagates immediately so
    auth/permission failures fail fast rather than burning the retry budget.

    ``provider`` only labels the "retry loop exhausted" guard error, which is
    unreachable in practice (the final attempt re-raises the underlying error).
    """
    payload = None if body is None else json.dumps(body).encode()
    for attempt in range(API_MAX_ATTEMPTS):
        req = urllib.request.Request(url, data=payload, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read().decode() or "null"
                resp_headers = dict(resp.headers)
            return json.loads(data), resp_headers
        except urllib.error.HTTPError as exc:
            retryable = exc.code in API_RETRY_STATUSES or (
                extra_retryable is not None and extra_retryable(exc)
            )
            if not retryable or attempt == API_MAX_ATTEMPTS - 1:
                raise
            time.sleep(retry_delay(exc.headers, attempt))
        except urllib.error.URLError:
            if attempt == API_MAX_ATTEMPTS - 1:
                raise
            time.sleep(retry_delay({}, attempt))
    raise RuntimeError(f"{provider} API retry loop exhausted")
