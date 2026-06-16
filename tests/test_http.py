"""Unit tests for the shared stdlib HTTP transport (:mod:`bubo._http`).

The retry/backoff loop was previously duplicated (and drifting) between the
GitLab and GitHub clients; before this extraction it had no direct coverage —
only an end-to-end GitLab retry case in ``test_poller_telemetry_state``. These
tests pin the shared policy: which statuses retry, how ``Retry-After`` and the
exponential backoff are computed, that non-retryable errors fail fast, and that
the ``extra_retryable`` seam (GitHub's 403 primary rate-limit) works without
making GitLab retry a bare 403.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import pytest

from bubo import _http, github


class _Resp:
    """Minimal stand-in for the ``urlopen`` context-manager response."""

    def __init__(self, body: bytes = b'{"ok": true}', headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {}

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _seq(*items: object):
    """A ``urlopen`` side-effect that yields ``items`` in order (raising any
    that are exceptions), modelling a sequence of transient failures."""
    calls = list(items)

    def fake_urlopen(*_a: object, **_k: object) -> object:
        item = calls.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return fake_urlopen


def _http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x/y", code, "err", headers or {}, None)  # type: ignore[arg-type]


# --- retry_delay ---------------------------------------------------------


def test_retry_delay_honors_retry_after() -> None:
    assert _http.retry_delay({"Retry-After": "2"}, 0) == 2.0


def test_retry_delay_clamps_retry_after_to_bounds() -> None:
    assert _http.retry_delay({"Retry-After": "999"}, 0) == 60.0
    assert _http.retry_delay({"Retry-After": "-5"}, 0) == 0.0


def test_retry_delay_invalid_retry_after_falls_back_to_backoff() -> None:
    assert _http.retry_delay({"Retry-After": "soon"}, 0) == 0.5


def test_retry_delay_exponential_backoff_and_cap() -> None:
    assert _http.retry_delay({}, 0) == 0.5
    assert _http.retry_delay({}, 1) == 1.0
    assert _http.retry_delay({}, 2) == 2.0
    assert _http.retry_delay({}, 10) == 10.0  # capped


def test_retry_delay_headers_without_get_use_backoff() -> None:
    assert _http.retry_delay(object(), 0) == 0.5


# --- request_json success / payload --------------------------------------


def test_request_json_success_no_retry() -> None:
    with patch("bubo._http.urllib.request.urlopen", side_effect=_seq(_Resp(headers={"A": "b"}))):
        with patch("bubo._http.time.sleep") as sleep:
            data, headers = _http.request_json("https://x/y", method="GET", headers={})
    assert data == {"ok": True}
    assert headers == {"A": "b"}
    sleep.assert_not_called()


def test_request_json_empty_body_parses_as_none() -> None:
    with patch("bubo._http.urllib.request.urlopen", side_effect=_seq(_Resp(body=b""))):
        data, _ = _http.request_json("https://x/y", method="GET", headers={})
    assert data is None


def test_request_json_serializes_body_and_method() -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(req: object, *_a: object, **_k: object) -> _Resp:
        captured["data"] = req.data  # type: ignore[attr-defined]
        captured["method"] = req.get_method()  # type: ignore[attr-defined]
        captured["url"] = req.full_url  # type: ignore[attr-defined]
        return _Resp()

    with patch("bubo._http.urllib.request.urlopen", side_effect=fake_urlopen):
        _http.request_json("https://x/y", method="POST", headers={"H": "1"}, body={"a": 1})

    assert captured["data"] == b'{"a": 1}'
    assert captured["method"] == "POST"
    assert captured["url"] == "https://x/y"


# --- request_json retry policy -------------------------------------------


def test_request_json_retries_on_429_then_succeeds() -> None:
    with patch(
        "bubo._http.urllib.request.urlopen",
        side_effect=_seq(_http_error(429, {"Retry-After": "0"}), _Resp()),
    ):
        with patch("bubo._http.time.sleep") as sleep:
            data, _ = _http.request_json("https://x/y", method="GET", headers={})
    assert data == {"ok": True}
    sleep.assert_called_once_with(0.0)


def test_request_json_retries_on_500_then_succeeds() -> None:
    with patch(
        "bubo._http.urllib.request.urlopen",
        side_effect=_seq(_http_error(500), _Resp()),
    ):
        with patch("bubo._http.time.sleep") as sleep:
            data, _ = _http.request_json("https://x/y", method="GET", headers={})
    assert data == {"ok": True}
    sleep.assert_called_once_with(0.5)  # backoff attempt 0


def test_request_json_retries_on_urlerror_then_succeeds() -> None:
    with patch(
        "bubo._http.urllib.request.urlopen",
        side_effect=_seq(urllib.error.URLError("boom"), _Resp()),
    ):
        with patch("bubo._http.time.sleep") as sleep:
            data, _ = _http.request_json("https://x/y", method="GET", headers={})
    assert data == {"ok": True}
    sleep.assert_called_once_with(0.5)


def test_request_json_does_not_retry_non_retryable_status() -> None:
    with patch("bubo._http.urllib.request.urlopen", side_effect=_seq(_http_error(401))):
        with patch("bubo._http.time.sleep") as sleep:
            with pytest.raises(urllib.error.HTTPError):
                _http.request_json("https://x/y", method="GET", headers={})
    sleep.assert_not_called()


def test_request_json_does_not_retry_bare_403_without_predicate() -> None:
    # The GitLab guarantee: a 403 with no extra_retryable is a hard failure,
    # NOT a rate-limit. (GitHub passes its own predicate; GitLab must not.)
    with patch("bubo._http.urllib.request.urlopen", side_effect=_seq(_http_error(403))):
        with patch("bubo._http.time.sleep") as sleep:
            with pytest.raises(urllib.error.HTTPError):
                _http.request_json("https://x/y", method="GET", headers={})
    sleep.assert_not_called()


def test_request_json_retries_when_extra_retryable_matches() -> None:
    with patch(
        "bubo._http.urllib.request.urlopen",
        side_effect=_seq(_http_error(403), _Resp()),
    ):
        with patch("bubo._http.time.sleep") as sleep:
            data, _ = _http.request_json(
                "https://x/y",
                method="GET",
                headers={},
                extra_retryable=lambda exc: exc.code == 403,
            )
    assert data == {"ok": True}
    sleep.assert_called_once()


def test_request_json_exhausts_attempts_and_reraises() -> None:
    with patch(
        "bubo._http.urllib.request.urlopen",
        side_effect=_seq(_http_error(429), _http_error(429), _http_error(429)),
    ):
        with patch("bubo._http.time.sleep") as sleep:
            with pytest.raises(urllib.error.HTTPError):
                _http.request_json("https://x/y", method="GET", headers={})
    # Slept after the first two failures, then re-raised on the final attempt.
    assert sleep.call_count == _http.API_MAX_ATTEMPTS - 1


# --- the wired GitHub rate-limit predicate (drift guard) -----------------


def test_github_rate_limit_predicate_distinguishes_primary_limit() -> None:
    assert github._is_rate_limited(_http_error(403, {"X-RateLimit-Remaining": "0"})) is True
    assert github._is_rate_limited(_http_error(403, {"X-RateLimit-Remaining": "57"})) is False
    assert github._is_rate_limited(_http_error(403)) is False
    assert github._is_rate_limited(_http_error(401, {"X-RateLimit-Remaining": "0"})) is False


def test_github_predicate_drives_retry_on_primary_rate_limit() -> None:
    with patch(
        "bubo._http.urllib.request.urlopen",
        side_effect=_seq(_http_error(403, {"X-RateLimit-Remaining": "0"}), _Resp()),
    ):
        with patch("bubo._http.time.sleep") as sleep:
            data, _ = _http.request_json(
                "https://x/y",
                method="GET",
                headers={},
                extra_retryable=github._is_rate_limited,
            )
    assert data == {"ok": True}
    sleep.assert_called_once()
