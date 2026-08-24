from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from bubo import analytics
from bubo.analytics_config import AnalyticsConfig


@pytest.fixture(autouse=True)
def _reset_analytics_singletons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with a clean module state and no env opt-outs."""
    monkeypatch.setattr(analytics, "_pending_events", [])
    monkeypatch.setattr(analytics, "_install_id", None)
    monkeypatch.delenv("BUBO_ANALYTICS", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)


# ---------------------------------------------------------------------------
# The allowlist chokepoint — the structural privacy guarantee.
# ---------------------------------------------------------------------------

# Every field that exists in the SQLite schema / review pipeline and must NEVER
# leave the machine. If any of these survive `_clean`, the anonymization
# promise is broken.
_NEVER_SEND = [
    "project",
    "repo",
    "iid",
    "sha",
    "file",
    "line",
    "body",
    "report",
    "error",
    "discussion_id",
    "note_id",
    "fingerprint",
    "matched_rule",
    "reason",
    "sensitive_paths",
    "token",
    "gitlab_token",
    "llm_api_key",
    "url",
    "prompt",
]


def test_clean_drops_every_never_send_key() -> None:
    payload = {key: "secret-or-identifying-value" for key in _NEVER_SEND}
    cleaned = analytics._clean(payload)
    assert cleaned == {}, f"leaked: {sorted(cleaned)}"


def test_clean_keeps_allowlisted_scalars() -> None:
    cleaned = analytics._clean(
        {
            "scm_provider": "gitlab",
            "status": "success",
            "dry_run": True,
            "lines_changed": 42,
            "cost_usd": 0.13,
            "project": "acme/secret-repo",  # not allowlisted -> dropped
        }
    )
    assert cleaned == {
        "scm_provider": "gitlab",
        "status": "success",
        "dry_run": True,
        "lines_changed": 42,
        "cost_usd": 0.13,
    }


def test_clean_drops_none_and_rejects_multiword_or_long_strings() -> None:
    cleaned = analytics._clean(
        {
            "model": "gpt-5.5",  # ok
            "status": None,  # dropped (None)
            "tone": "has space",  # dropped (whitespace -> could smuggle content)
            "scm_provider": "x" * 100,  # dropped (too long)
        }
    )
    assert cleaned == {"model": "gpt-5.5"}


# ---------------------------------------------------------------------------
# Opt-out precedence: DO_NOT_TRACK > BUBO_ANALYTICS > config; blank dest = off.
# ---------------------------------------------------------------------------


def test_enabled_by_default() -> None:
    assert analytics.analytics_enabled(AnalyticsConfig()) is True


def test_config_opt_out() -> None:
    assert analytics.analytics_enabled(AnalyticsConfig(enabled=False)) is False


def test_do_not_track_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("BUBO_ANALYTICS", "1")  # would enable, but DNT wins
    assert analytics.analytics_enabled(AnalyticsConfig()) is False


def test_bubo_analytics_env_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUBO_ANALYTICS", "0")
    assert analytics.analytics_enabled(AnalyticsConfig()) is False


def test_bubo_analytics_env_enables_over_config_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUBO_ANALYTICS", "true")
    assert analytics.analytics_enabled(AnalyticsConfig(enabled=False)) is True


def test_blank_destination_disables() -> None:
    assert analytics.analytics_enabled(AnalyticsConfig(endpoint="")) is False
    assert analytics.analytics_enabled(AnalyticsConfig(api_key="")) is False


# ---------------------------------------------------------------------------
# Anonymous install id.
# ---------------------------------------------------------------------------


def test_install_id_is_stable_and_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analytics.paths, "DB", tmp_path / "state" / "reviewer.sqlite")
    first = analytics.install_id()
    assert first
    assert len(first) == 32  # uuid4 hex
    # cached within the process
    assert analytics.install_id() == first
    # persisted to disk and reused by a fresh process (cache cleared)
    monkeypatch.setattr(analytics, "_install_id", None)
    assert analytics.install_id() == first
    assert (tmp_path / "state" / "install_id").read_text().strip() == first


def test_install_id_falls_back_when_unwritable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analytics.paths, "DB", Path("/proc/nonexistent/reviewer.sqlite"))
    assert len(analytics.install_id()) == 32  # ephemeral, no crash


# ---------------------------------------------------------------------------
# Enum normalization — a custom command/provider can never leak a path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["codex", "exec"], "codex"),
        (["/usr/local/bin/claude", "-p"], "claude"),
        (["my-secret-internal-tool"], "other"),
        ([], "other"),
    ],
)
def test_agent_label(command: list[str], expected: str) -> None:
    assert analytics.agent_label(command) == expected


def test_provider_normalization() -> None:
    assert analytics._provider("GitLab") == "gitlab"
    assert analytics._provider("github") == "github"
    assert analytics._provider("/some/path") == "other"


# ---------------------------------------------------------------------------
# End-to-end shaping in the Product Analytics queue — no PII.
# ---------------------------------------------------------------------------


def _only_queued_event() -> tuple[str, dict[str, object]]:
    assert len(analytics._pending_events) == 1
    _, _, event = analytics._pending_events[0]
    return event["event"], event["properties"]


def test_record_review_completed_emits_only_allowlisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analytics.paths, "DB", tmp_path / "state" / "reviewer.sqlite")

    analytics.record_review_completed(
        AnalyticsConfig(),
        scm_provider="gitlab",
        agent="codex",
        model="gpt-5.5",
        status="success",
        dry_run=False,
        review_mode="diff",
        tone="terse",
        duration_seconds=12.5,
        tokens_input=100,
        tokens_output=50,
        tokens_cached=0,
        tokens_total=150,
        cost_usd=0.01,
        findings_posted=2,
        findings_planned=0,
        findings_skipped=1,
        files_changed=3,
        lines_changed=120,
    )

    event, attrs = _only_queued_event()
    assert event == "review_completed"
    # Every emitted key must be in the allowlist (the structural guarantee).
    assert set(attrs).issubset(analytics._ALLOWED_ATTRS)
    assert attrs["scm_provider"] == "gitlab"
    assert attrs["lines_changed"] == 120
    assert attrs["files_changed"] == 3
    # Base attrs are present.
    assert "install_id" in attrs
    assert "bubo_version" in attrs
    assert "os" in attrs
    assert attrs["$geoip_disable"] is True
    assert attrs["$process_person_profile"] is False


def test_posthog_privacy_controls_cannot_be_overridden() -> None:
    analytics._emit(
        AnalyticsConfig(),
        "session_start",
        {"$geoip_disable": False, "$process_person_profile": True},
    )

    _, attrs = _only_queued_event()
    assert attrs["$geoip_disable"] is True
    assert attrs["$process_person_profile"] is False


def test_record_finding_outcome_emits_allowlisted_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analytics.record_finding_outcome(AnalyticsConfig(), scm_provider="gitlab", outcome="resolved")

    event, attrs = _only_queued_event()
    assert event == "finding_outcome"
    assert attrs["outcome"] == "resolved"
    assert attrs["scm_provider"] == "gitlab"
    assert set(attrs).issubset(analytics._ALLOWED_ATTRS)


def test_record_finding_outcome_normalizes_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Defense in depth: an outcome name outside the known set (and a junk
    # provider) collapse to "other" rather than leaking an arbitrary string.
    analytics.record_finding_outcome(
        AnalyticsConfig(), scm_provider="weird-scm", outcome="merged_unresolved"
    )

    _, attrs = _only_queued_event()
    assert attrs["outcome"] == "other"
    assert attrs["scm_provider"] == "other"


def test_emit_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A disabled config must never queue an event."""
    analytics.record_session_start(
        AnalyticsConfig(enabled=False), scm_provider="gitlab", projects_count=1
    )
    assert analytics._pending_events == []


# ---------------------------------------------------------------------------
# Egress must never touch the stdlib logging tree (root or otherwise).
# ---------------------------------------------------------------------------


def test_otel_env_attributes_never_enter_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES", "host.name=secret-host,deployment.environment=acme-prod"
    )
    monkeypatch.setenv("OTEL_SERVICE_NAME", "acme-svc")
    analytics.record_session_start(AnalyticsConfig(), scm_provider="gitlab", projects_count=1)
    _, attrs = _only_queued_event()
    assert "host.name" not in attrs
    assert "deployment.environment" not in attrs


def test_distinct_id_equals_install_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(analytics.paths, "DB", tmp_path / "state" / "reviewer.sqlite")
    analytics.record_session_start(AnalyticsConfig(), scm_provider="gitlab", projects_count=1)
    _, attrs = _only_queued_event()
    assert attrs["distinct_id"] == attrs["install_id"]
    assert attrs["distinct_id"] == analytics.install_id()


class _FakeResponse:
    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_flush_posts_product_analytics_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[tuple[object, int]] = []

    def _urlopen(request: object, timeout: int) -> _FakeResponse:
        requests.append((request, timeout))
        return _FakeResponse()

    monkeypatch.setattr(analytics, "urlopen", _urlopen)
    cfg = AnalyticsConfig(endpoint="https://example.test/batch/", api_key="phc_test")
    analytics.record_session_start(cfg, scm_provider="gitlab", projects_count=1)
    analytics.record_finding_outcome(cfg, scm_provider="gitlab", outcome="resolved")

    analytics.flush()

    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == "https://example.test/batch/"
    assert request.headers["Content-type"] == "application/json"
    assert timeout == 3
    body = json.loads(request.data)
    assert body["api_key"] == "phc_test"
    assert [event["event"] for event in body["batch"]] == [
        "session_start",
        "finding_outcome",
    ]
    assert analytics._pending_events == []


def test_flush_failure_is_nonfatal(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(request: object, timeout: int) -> None:
        raise OSError("offline")

    monkeypatch.setattr(analytics, "urlopen", _fail)
    analytics.record_session_start(AnalyticsConfig(), scm_provider="gitlab", projects_count=1)
    analytics.flush()
    assert analytics._pending_events == []


def test_product_analytics_transport_uses_no_stdlib_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(analytics, "urlopen", lambda request, timeout: _FakeResponse())
    analytics.record_session_start(AnalyticsConfig(), scm_provider="gitlab", projects_count=1)
    analytics.flush()
    assert root.handlers == before
