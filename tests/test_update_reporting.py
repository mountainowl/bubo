from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import replace

import pytest

from bubo.analytics_config import AnalyticsConfig
from bubo.update_reporting import PHASES, DiagnosticsUnavailable, FailureStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    (tmp_path / "install_id").write_text(uuid.uuid4().hex)
    return FailureStore(tmp_path)


def record(store, **kwargs):
    return store.record(
        **dict(
            {
                "attempt_id": str(uuid.uuid4()),
                "phase": "migration",
                "error_code": "migration_failed",
                "cfg": AnalyticsConfig(),
                "producer_version": "0.27.0",
                "from_version": "0.26.0",
                "target_version": "0.27.0",
            },
            **kwargs,
        )
    )


class Response:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.mark.parametrize("phase", sorted(PHASES))
def test_every_failure_phase_has_local_report_and_event(store, phase):
    identity = (store.state / "install_id").read_text()
    fid = record(store, phase=phase)
    report = store.report()
    assert f"# Bubo update failed: {phase}" in report
    assert fid in report
    assert identity not in report
    assert "Next action:" in report
    captured = []

    def send(request, timeout):
        assert timeout == 3
        captured.append(json.loads(request.data))
        return Response()

    assert store.drain(AnalyticsConfig(), sender=send) == 1
    event = captured[0]["batch"][0]
    assert event["properties"]["$insert_id"] == fid
    assert event["properties"]["distinct_id"] == identity
    assert event["properties"]["install_id"] == identity
    assert event["properties"]["bubo_version"] == "0.27.0"
    assert store.status()[0]["delivery"] == "acknowledged"


def test_network_failure_restart_and_ambiguous_ack_preserve_event(store):
    fid = record(store)
    events = []

    def offline(request, timeout):
        events.append(json.loads(request.data)["batch"][0])
        raise OSError("secret must not persist")

    assert store.drain(AnalyticsConfig(), sender=offline, now=100) == 0
    restored = FailureStore(store.state)

    def accepted(request, timeout):
        events.append(json.loads(request.data)["batch"][0])
        return Response()

    assert restored.drain(AnalyticsConfig(), sender=accepted, now=10000) == 1
    assert events[0] == events[1]
    assert events[0]["properties"]["$insert_id"] == fid
    assert "secret" not in restored.report()
    assert len(restored.status()) == 1  # no recursive transport failure records


def test_optout_at_capture_and_delivery_is_never_backfilled(store, monkeypatch):
    cfg = AnalyticsConfig()
    record(store, cfg=replace(cfg, enabled=False))
    record(store)
    assert store.drain(replace(cfg, enabled=False)) == 0  # no actual network request
    for row in store.status():
        store.retry(row["attempt_id"], cfg)
    assert {row["delivery"] for row in store.status()} == {"local", "suppressed"}
    assert store.drain(cfg) == 0


def test_unavailable_helper_consent_holds_payload_without_sending(store):
    record(store)
    assert store.drain(AnalyticsConfig(), permitted=lambda: False) == 0
    assert store.status()[0]["delivery"] == "pending"


@pytest.mark.parametrize("code", [400, 401, 403])
def test_permanent_rejection_explicit_retry_does_not_relabel(store, code):
    fid = record(store)
    sent = []

    def send(request, timeout):
        sent.append(json.loads(request.data))
        return Response(code if len(sent) == 1 else 202)

    cfg = AnalyticsConfig()
    assert store.drain(cfg, sender=send, now=1) == 0
    assert store.status()[0]["delivery"] == "quarantined"
    store.retry(store.status()[0]["attempt_id"], cfg)
    assert store.drain(cfg, sender=send, now=2) == 1
    assert sent[0] == sent[1]
    assert sent[1]["batch"][0]["properties"]["$insert_id"] == fid


@pytest.mark.parametrize("code", [408, 429, 500, 503])
def test_transient_rejection_remains_pending(store, code):
    record(store)
    assert store.drain(AnalyticsConfig(), sender=lambda *a, **k: Response(code), now=1) == 0
    row = store.status()[0]
    assert row["delivery"] == "pending"
    assert row["delivery_attempts"] == 1


def test_arbitrary_fields_cannot_leak_into_report_or_payload(store):
    secret = "token secret /private/project source-code\n"
    record(
        store,
        error_code=secret,
        install_type=secret,
        phase=secret,
        from_version=secret,
        target_version=secret,
        recovery_status=secret,
    )
    assert secret not in store.report()
    with sqlite3.connect(store.path) as db:
        row = db.execute("SELECT record,payload FROM failures").fetchone()
    assert all(secret not in text for text in row)


def test_idempotent_crash_reconstruction(store):
    failure = str(uuid.uuid4())
    attempt = str(uuid.uuid4())
    record(store, failure_id=failure, attempt_id=attempt)
    record(store, failure_id=failure, attempt_id=attempt)
    assert len(store.status()) == 1


def test_reports_survive_application_database_rollback(store):
    app_db = store.state / "reviewer.sqlite"
    app_db.write_bytes(b"old")
    record(store)
    app_db.write_bytes(b"new")
    app_db.write_bytes(b"old")
    assert "migration_failed" in FailureStore(store.state).report()


def test_capacity_does_not_evict_unacknowledged_failures(store):
    record(store)
    store.capacity = 1
    with pytest.raises(DiagnosticsUnavailable, match="capacity"):
        store.ensure_capacity()
    assert len(store.status()) == 1
    assert store.status()[0]["delivery"] == "pending"


def test_unknown_schema_keeps_data_and_refuses_writes(store):
    record(store)
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(DiagnosticsUnavailable, match="schema"):
        record(store)
    assert store.status()[0]["delivery"] == "pending"


def test_missing_report_is_readonly(tmp_path):
    store = FailureStore(tmp_path / "missing")
    assert store.report() == "No update failures recorded.\n"
    assert not store.state.exists()


def test_latest_report_does_not_require_lookup(store):
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    record(store, attempt_id=first)
    record(store, attempt_id=second)
    assert second in store.report()
    assert first not in store.report()
    assert first in store.report(first)


def test_guarded_identity_change_never_exports_replacement(store):
    from bubo.update_identity import protect_identity

    protect_identity(store.state)
    replacement = uuid.uuid4().hex
    (store.state / "install_id").write_text(replacement)
    record(store)
    assert store.status()[0]["delivery"] == "local"
    assert replacement not in store.report()
    assert store.drain(AnalyticsConfig()) == 0


def test_final_local_recovery_outcome_preserves_original_payload(store):
    record(store, recovery_status="pending")
    with sqlite3.connect(store.path) as db:
        before = db.execute("SELECT payload FROM failures").fetchone()[0]
    attempt = store.status()[0]["attempt_id"]
    store.finish_recovery(attempt, "restored")
    assert "recovery_status: restored" in store.report()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT payload FROM failures").fetchone()[0] == before
