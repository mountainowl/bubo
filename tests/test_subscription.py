from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bubo.review_config import review_config_from_dict
from bubo.service import monitor_loop
from bubo.subscription import (
    CircuitConfig,
    CircuitState,
    acknowledge_failure_signal,
    consume_failure_signal,
    has_failure_signal,
    is_subscription_failure,
    read_state,
    review_gate,
    signal_subscription_failure,
    singleton_lock,
    state_path,
    write_state,
)


def test_subscription_classifier_rejects_false_positives() -> None:
    assert is_subscription_failure("Error: insufficient_quota")
    assert is_subscription_failure("You exceeded your current quota")
    assert not is_subscription_failure("401 unauthorized: subscription endpoint")
    assert not is_subscription_failure("network timeout while checking quota")
    assert not is_subscription_failure("billing address is invalid")
    assert not is_subscription_failure("rate limit exceeded")


def test_terminal_classifier_ignores_successful_source_text_and_honors_quota_code() -> None:
    successful = 'review complete\n[{"evidence":"mentions insufficient_quota in source"}]'
    assert not is_subscription_failure(successful, terminal_only=True)
    terminal = "network timeout earlier\nError: insufficient_quota"
    assert is_subscription_failure(terminal, terminal_only=True)
    provider = '{"error":{"code":"insufficient_quota","message":"quota exhausted"}}'
    assert is_subscription_failure(provider, terminal_only=True)


def test_terminal_classifier_requires_true_structured_error_envelope() -> None:
    findings = '[{"type":"issue","evidence":"insufficient_quota in source"}]'
    assert not is_subscription_failure(findings, terminal_only=True)
    object_result = '{"type":"finding","evidence":"insufficient_quota"}'
    assert not is_subscription_failure(object_result, terminal_only=True)
    assert is_subscription_failure('{"type":"error","code":"insufficient_quota"}', terminal_only=True)
    assert is_subscription_failure('{"kind":"error","code":"insufficient_quota"}', terminal_only=True)


def test_state_write_is_atomic_and_gate_fails_closed(tmp_path: Path) -> None:
    cfg = CircuitConfig(enabled=True, heartbeat_ttl_seconds=10)
    path = state_path(tmp_path)
    assert review_gate(cfg, path, at=100) == (False, "monitor_missing")
    write_state(path, CircuitState(status="closed", heartbeat_at=100))
    assert json.loads(path.read_text())["status"] == "closed"
    assert review_gate(cfg, path, at=105) == (True, "closed")
    signal_subscription_failure(tmp_path / "subscription-circuit.signal")
    assert review_gate(cfg, path, at=105) == (False, "subscription_signal_pending")
    consume_failure_signal(tmp_path / "subscription-circuit.signal")
    acknowledge_failure_signal(tmp_path / "subscription-circuit.signal")
    assert review_gate(cfg, path, at=111) == (False, "monitor_stale")
    write_state(path, CircuitState(status="open", heartbeat_at=110, opened_at=110))
    assert review_gate(cfg, path, at=111) == (False, "paused_subscription")


def test_signals_are_coalesced_without_worker_state_write(tmp_path: Path) -> None:
    signal = tmp_path / "subscription-circuit.signal"
    signal_subscription_failure(signal)
    signal_subscription_failure(signal)
    assert consume_failure_signal(signal)
    assert has_failure_signal(signal)
    # A concurrent append after claim remains pending after the old claim is
    # acknowledged, so a crash cannot lose it.
    signal_subscription_failure(signal)
    acknowledge_failure_signal(signal)
    assert has_failure_signal(signal)
    assert consume_failure_signal(signal)
    acknowledge_failure_signal(signal)
    assert not has_failure_signal(signal)
    assert read_state(tmp_path / "subscription-circuit.json") is None


def test_singleton_monitor_lock_rejects_second_owner(tmp_path: Path) -> None:
    lock = tmp_path / "subscription-monitor.lock"
    with singleton_lock(lock) as first:
        assert first
        with singleton_lock(lock) as second:
            assert not second


def test_config_is_opt_in_and_parses_circuit_controls() -> None:
    assert not review_config_from_dict({}).subscription_circuit.enabled
    config = review_config_from_dict(
        {"subscription_circuit": {"enabled": True, "monitor_interval_seconds": 2,
         "error_patterns": ["quota exhausted"]}, "poller": {
             "interval_seconds": 7, "outcome_sync_interval_seconds": 11,
             "outcome_sync_limit": 13,
         }}
    )
    assert config.subscription_circuit.enabled
    assert config.subscription_circuit.monitor_interval_seconds == 2
    assert config.subscription_circuit.error_patterns == ("quota exhausted",)
    assert config.service_poll_interval_seconds == 7
    assert config.service_outcome_sync_interval_seconds == 11
    assert config.service_outcome_sync_limit == 13


def test_service_outcome_sync_isolated_from_review_polling(monkeypatch) -> None:
    from bubo import service

    cfg = review_config_from_dict({"poller": {"outcome_sync_limit": 17}})
    calls: list[int] = []
    monkeypatch.setattr("bubo.poller.sync_outcomes", lambda limit: calls.append(limit))
    service._sync_outcomes(cfg)
    assert calls == [17]

    monkeypatch.setattr("bubo.poller.sync_outcomes", lambda _limit: (_ for _ in ()).throw(RuntimeError()))
    logged = MagicMock()
    monkeypatch.setattr(service, "log", logged)
    service._sync_outcomes(cfg)
    logged.assert_called_once_with("service_outcome_sync_failed")


def test_service_deadlines_choose_each_due_task_order_and_do_not_drift() -> None:
    from bubo import service

    assert service._next_due_task(current=11, poll_deadline=10, outcome_deadline=12) == "poll"
    assert service._next_due_task(current=11, poll_deadline=12, outcome_deadline=10) == "outcome_sync"
    assert service._next_due_task(current=12, poll_deadline=12, outcome_deadline=12) == "poll"
    # A slow task skips missed periods from its original fixed deadline rather
    # than moving the next execution relative to completion time.
    assert service._advance_deadline(10, 5, 23) == 25


def test_service_poll_failure_is_redacted_and_next_deadline_remains_fixed(monkeypatch) -> None:
    from bubo import service

    logged = MagicMock()
    monkeypatch.setattr(service, "log", logged)
    monkeypatch.setattr(
        "bubo.poller.poll", lambda: (_ for _ in ()).throw(RuntimeError("OPENAI_API_KEY=sk-secret"))
    )
    service._poll_once()
    logged.assert_called_once_with("service_poll_failed", error="OPENAI_API_KEY=<redacted>")
    assert service._advance_deadline(100, 60, 161) == 220


def test_bare_poller_is_rejected_before_discovery(monkeypatch, capsys) -> None:
    from bubo import poller

    invoked = MagicMock()
    monkeypatch.setattr(poller, "poll", invoked)
    monkeypatch.setattr("sys.argv", ["bubo-poller"])
    with pytest.raises(SystemExit) as excinfo:
        poller.main()
    assert excinfo.value.code == 2
    assert "service start [--foreground]" in capsys.readouterr().err
    invoked.assert_not_called()


def test_health_remains_an_on_demand_poller_command(monkeypatch) -> None:
    from bubo import poller

    health = MagicMock(return_value=0)
    monkeypatch.setattr(poller, "check_health", health)
    monkeypatch.setattr("sys.argv", ["bubo-poller", "--health"])
    assert poller.main() == 0
    health.assert_called_once()


def test_shared_shutdown_interrupts_a_multi_change_poll(monkeypatch) -> None:
    from bubo import poller
    from bubo.review_config import ReviewConfig
    from bubo.signals import request_shutdown, reset_for_tests

    reset_for_tests()
    cfg = ReviewConfig(projects=["owner/repo"], max_merge_requests_per_poll=2)
    provider = MagicMock()
    provider.name = "gitlab"
    first = {"number": 1, "sha": "one"}
    second = {"number": 2, "sha": "two"}

    def changes(*_args):
        yield first
        request_shutdown(source="windows_stop")
        yield second

    provider.list_open_changes.side_effect = changes
    provider.token.return_value = "token"
    provider.change_number.side_effect = lambda change: change["number"]
    monkeypatch.setattr(poller, "init_db", lambda: None)
    monkeypatch.setattr(poller, "read_config", lambda: cfg)
    monkeypatch.setattr(poller, "subscription_gate", lambda _cfg: (True, "closed"))
    monkeypatch.setattr(poller, "get_provider", lambda _cfg: provider)
    monkeypatch.setattr(poller, "count_inflight_workers", lambda: 0)
    monkeypatch.setattr(poller, "already_seen", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(poller, "sha_for", lambda change: change["sha"])
    recorded: list[int] = []
    monkeypatch.setattr(poller, "record", lambda _project, number, *_args: recorded.append(number))
    monkeypatch.setattr(poller, "write_job", lambda *_args: Path("job"))
    monkeypatch.setattr(poller, "fork_worker", lambda _job: 1)
    monkeypatch.setattr(poller.analytics, "record_session_start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(poller.analytics, "flush", lambda: None)
    try:
        assert poller.poll() == 1
        assert recorded == [1]
    finally:
        reset_for_tests()


def test_monitor_opens_then_recovers(tmp_path: Path, monkeypatch) -> None:
    from bubo import paths, service

    monkeypatch.setattr(paths, "DB", tmp_path / "reviewer.sqlite")
    cfg = review_config_from_dict(
        {"subscription_circuit": {"enabled": True, "monitor_interval_seconds": 1,
          "probe_interval_seconds": 1, "probe_backoff_max_seconds": 2}}
    )
    # No exporter or PostHog is configured; test transitions without network.
    monkeypatch.setattr(service, "_probe", lambda _cfg: True)
    monkeypatch.setattr(service, "log", MagicMock())
    monkeypatch.setattr(service, "_transition", MagicMock())
    stop = threading.Event()
    ready = threading.Event()
    thread = threading.Thread(target=monitor_loop, args=(cfg, stop, ready), daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    deadline = time.monotonic() + 3
    state = None
    while time.monotonic() < deadline:
        state = read_state(state_path(tmp_path))
        if state is not None:
            break
        time.sleep(0.02)
    assert state is not None
    assert state.status == "closed"
    signal_subscription_failure(tmp_path / "subscription-circuit.signal")
    deadline = time.monotonic() + 4
    observed_open = False
    while time.monotonic() < deadline:
        state = read_state(state_path(tmp_path))
        observed_open = observed_open or bool(state and state.status == "open")
        if observed_open and state and state.status == "closed":
            break
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=12)
    assert not thread.is_alive()
    assert observed_open
    assert state is not None
    assert state.status == "closed"
    assert not has_failure_signal(tmp_path / "subscription-circuit.signal")


def test_poll_gate_blocks_discovery_before_provider(tmp_path: Path, monkeypatch) -> None:
    from bubo import paths, poller

    monkeypatch.setattr(paths, "DB", tmp_path / "reviewer.sqlite")
    cfg = review_config_from_dict({"subscription_circuit": {"enabled": True}})
    monkeypatch.setattr(poller, "read_config", lambda: cfg)
    provider = MagicMock()
    monkeypatch.setattr(poller, "get_provider", provider)
    assert poller.poll() == 0
    provider.assert_not_called()


def test_health_distinguishes_paused_from_dead_monitor(tmp_path: Path, monkeypatch) -> None:
    from bubo import paths, poller

    monkeypatch.setattr(paths, "DB", tmp_path / "reviewer.sqlite")
    cfg = review_config_from_dict(
        {"subscription_circuit": {"enabled": True, "heartbeat_ttl_seconds": 9999}}
    )
    monkeypatch.setattr(poller, "read_config", lambda: cfg)
    write_state(state_path(tmp_path), CircuitState(status="open", heartbeat_at=time.time()))
    assert poller.check_health() == 0
    write_state(state_path(tmp_path), CircuitState(status="closed", heartbeat_at=time.time()))
    signal_subscription_failure(tmp_path / "subscription-circuit.signal")
    assert poller.check_health() == 0
    consume_failure_signal(tmp_path / "subscription-circuit.signal")
    acknowledge_failure_signal(tmp_path / "subscription-circuit.signal")
    write_state(state_path(tmp_path), CircuitState(status="closed", heartbeat_at=0))
    assert poller.check_health() == 1


def test_no_findings_write_rechecks_open_circuit(tmp_path: Path, monkeypatch) -> None:
    from bubo import paths, poller

    monkeypatch.setattr(paths, "DB", tmp_path / "reviewer.sqlite")
    cfg = review_config_from_dict({"subscription_circuit": {"enabled": True}, "review": {"dry_run": False}})
    write_state(state_path(tmp_path), CircuitState(status="open", heartbeat_at=time.time()))
    provider = MagicMock()
    verdict, _detail = poller.post_no_findings_comment(
        cfg=cfg, token="token", project="owner/repo", number=5, provider=provider
    )
    assert verdict == "disabled"
    provider.post_change_comment.assert_not_called()


def test_service_status_cleans_stale_pid_and_stop_is_safe(tmp_path: Path, monkeypatch) -> None:
    from bubo import paths, service

    monkeypatch.setattr(paths, "SERVICE_PID", tmp_path / "bubo-service.pid")
    paths.SERVICE_PID.write_text('{"pid":999999,"token":"old"}', encoding="utf-8")
    monkeypatch.setattr(service, "_alive", lambda _pid: False)
    assert service.service_status() == ("stopped", None)
    assert not paths.SERVICE_PID.exists()
    assert not service.stop_service()


def test_service_never_stops_pid_without_matching_start_token(tmp_path: Path, monkeypatch) -> None:
    from bubo import paths, service

    monkeypatch.setattr(paths, "SERVICE_PID", tmp_path / "bubo-service.pid")
    paths.SERVICE_PID.write_text('{"pid":42,"token":"wrong"}', encoding="utf-8")
    monkeypatch.setattr(service, "_identity_matches", lambda *_args: False)
    kill = MagicMock()
    monkeypatch.setattr(service.os, "kill", kill)
    assert not service.stop_service()
    kill.assert_not_called()


def test_windows_stop_is_cooperative_and_never_kills_pid(tmp_path: Path, monkeypatch) -> None:
    from bubo import paths, service

    monkeypatch.setattr(paths, "SERVICE_PID", tmp_path / "bubo-service.pid")
    paths.SERVICE_PID.write_text(
        json.dumps({"pid": 42, "token": "token", "heartbeat_at": time.time()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(service.os, "name", "nt")
    kill = MagicMock()
    monkeypatch.setattr(service.os, "kill", kill)
    assert service.service_status() == ("running", 42)
    assert service.stop_service()
    assert (tmp_path / "bubo-service.stop").read_text(encoding="utf-8") == "token"
    kill.assert_not_called()


def test_windows_heartbeat_keeps_status_and_stop_live_during_long_poll(tmp_path: Path, monkeypatch) -> None:
    from bubo import paths, service

    monkeypatch.setattr(paths, "SERVICE_PID", tmp_path / "bubo-service.pid")
    monkeypatch.setattr(service.os, "name", "nt")
    monkeypatch.setattr(service.os, "getpid", lambda: 42)
    service._write_pid("token")
    stop = threading.Event()
    heartbeat = threading.Thread(target=service._heartbeat_loop, args=("token", stop), daemon=True)
    heartbeat.start()
    # Simulate a long blocking poll: heartbeat runs independently of it.
    time.sleep(1.1)
    assert service.service_status() == ("running", 42)
    assert service.stop_service()
    stop.set()
    heartbeat.join(timeout=2)
    assert not heartbeat.is_alive()


def test_windows_heartbeat_starts_before_blocked_monitor_readiness(tmp_path: Path, monkeypatch) -> None:
    from bubo import paths, poller, service

    monkeypatch.setattr(paths, "SERVICE_PID", tmp_path / "bubo-service.pid")
    monkeypatch.setattr(service.os, "name", "nt")
    cfg = review_config_from_dict(
        {"subscription_circuit": {"enabled": True, "heartbeat_ttl_seconds": 6}, "poller": {"interval_seconds": 1}}
    )
    ready_seen: list[threading.Event] = []

    def blocked_monitor(_cfg, stop: threading.Event, ready: threading.Event) -> None:
        ready_seen.append(ready)
        stop.wait()

    monkeypatch.setattr(service, "monitor_loop", blocked_monitor)
    monkeypatch.setattr(poller, "poll", lambda: 0)
    service._write_pid("token")
    runner = threading.Thread(target=service._run_service_loop, args=(cfg, "token"), daemon=True)
    runner.start()
    deadline = time.monotonic() + 2
    while not ready_seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready_seen
    # Readiness remains blocked beyond the five-second Windows status window.
    time.sleep(5.1)
    assert service.service_status()[0] == "running"
    assert service.stop_service()
    ready_seen[0].set()
    runner.join(timeout=3)
    assert not runner.is_alive()
    assert not paths.SERVICE_PID.exists()


def test_windows_wait_observes_cooperative_stop_without_waiting_for_poll_interval(
    monkeypatch,
) -> None:
    from bubo import service

    monkeypatch.setattr(service.os, "name", "nt")
    monkeypatch.setattr(service, "_consume_stop_request", lambda _token: True)
    stop = threading.Event()
    assert service._wait_for_next_cycle(stop, 900, "token")
    assert stop.is_set()


def test_windows_stop_watcher_interrupts_active_poll(monkeypatch) -> None:
    from bubo import service
    from bubo.signals import reset_for_tests, shutdown_requested

    reset_for_tests()
    stop = threading.Event()
    monkeypatch.setattr(service, "_consume_stop_request", lambda _token: True)
    watcher = threading.Thread(target=service._watch_windows_stop, args=("token", stop))
    watcher.start()
    watcher.join(timeout=2)
    try:
        assert stop.is_set()
        assert shutdown_requested()
    finally:
        reset_for_tests()


def test_detached_service_returns_only_after_matching_ready_token(monkeypatch) -> None:
    from bubo import service

    child = MagicMock()
    child.poll.return_value = None
    popen = MagicMock(return_value=child)
    monkeypatch.setattr(service, "service_status", lambda: ("stopped", None))
    monkeypatch.setattr(service.subprocess, "Popen", popen)
    monkeypatch.setattr(service.secrets, "token_urlsafe", lambda _size: "expected-token")
    seen_tokens: list[str] = []

    def ready(token: str) -> tuple[str, str] | None:
        seen_tokens.append(token)
        return ("ready", "")

    monkeypatch.setattr(service, "_read_startup_result", ready)
    assert service.start_detached() == 0
    assert seen_tokens == ["expected-token"]
    assert popen.call_args.args[0][-1] == "expected-token"


def test_detached_service_ready_command_uses_foreground_child(monkeypatch) -> None:
    from bubo import service

    popen = MagicMock()
    child = MagicMock()
    child.poll.return_value = None
    popen.return_value = child
    monkeypatch.setattr(service, "service_status", lambda: ("stopped", None))
    monkeypatch.setattr(service.subprocess, "Popen", popen)
    monkeypatch.setattr(service, "_read_startup_result", lambda _token: ("ready", ""))
    assert service.start_detached() == 0
    command = popen.call_args.args[0]
    assert command[-5:-1] == ["service", "start", "--foreground", "--service-token"]
    assert popen.call_args.kwargs["stdout"].name.endswith("service.log")


def test_detached_service_timeout_terminates_child_and_cleans_owned_state(monkeypatch) -> None:
    from bubo import service

    child = MagicMock()
    child.poll.return_value = None
    monkeypatch.setattr(service, "service_status", lambda: ("stopped", None))
    monkeypatch.setattr(service.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(service, "_read_startup_result", lambda _token: None)
    monkeypatch.setattr(service, "_STARTUP_TIMEOUT_SECONDS", 0)
    cleanup = MagicMock()
    monkeypatch.setattr(service, "_cleanup_failed_start", cleanup)
    assert service.start_detached() == 1
    child.terminate.assert_called_once()
    cleanup.assert_called_once()


def test_detached_service_reports_immediate_child_error_without_orphan(monkeypatch) -> None:
    from bubo import service

    child = MagicMock()
    child.poll.return_value = 2
    monkeypatch.setattr(service, "service_status", lambda: ("stopped", None))
    monkeypatch.setattr(service.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(service, "_read_startup_result", lambda _token: ("error", "OPENAI_API_KEY=sk-secret"))
    cleanup = MagicMock()
    logged = MagicMock()
    monkeypatch.setattr(service, "_cleanup_failed_start", cleanup)
    monkeypatch.setattr(service, "log", logged)
    assert service.start_detached() == 1
    child.terminate.assert_not_called()
    cleanup.assert_called_once()
    logged.assert_called_once_with("service_start_failed", error="OPENAI_API_KEY=<redacted>")


def test_detached_service_reports_child_launch_error_without_state(monkeypatch) -> None:
    from bubo import service

    monkeypatch.setattr(service, "service_status", lambda: ("stopped", None))
    monkeypatch.setattr(service.subprocess, "Popen", MagicMock(side_effect=OSError("sk-secret")))
    cleanup = MagicMock()
    logged = MagicMock()
    monkeypatch.setattr(service, "_cleanup_failed_start", cleanup)
    monkeypatch.setattr(service, "log", logged)
    assert service.start_detached() == 1
    cleanup.assert_called_once()
    logged.assert_called_once_with("service_start_failed", error="<redacted>")


def test_failed_start_cleanup_only_removes_matching_token_state(tmp_path: Path, monkeypatch) -> None:
    from bubo import paths, service

    monkeypatch.setattr(paths, "SERVICE_PID", tmp_path / "service.pid")
    paths.SERVICE_PID.write_text('{"pid":42,"token":"ours"}', encoding="utf-8")
    lock = paths.SERVICE_PID.with_suffix(".lock")
    lock.write_text("stale", encoding="utf-8")
    service._write_startup_result("ours", "error")
    service._write_startup_result("other", "ready")
    service._cleanup_failed_start("ours")
    assert not paths.SERVICE_PID.exists()
    assert not lock.exists()
    assert not service._ready_path("ours").exists()
    assert service._ready_path("other").exists()
    service._clear_startup_result("other")

    paths.SERVICE_PID.write_text('{"pid":43,"token":"other"}', encoding="utf-8")
    lock.write_text("other", encoding="utf-8")
    service._cleanup_failed_start("ours")
    assert paths.SERVICE_PID.exists()
    assert lock.exists()


def test_concurrent_detached_starts_keep_token_handshakes_isolated(tmp_path: Path, monkeypatch) -> None:
    from bubo import paths, service

    monkeypatch.setattr(paths, "SERVICE_PID", tmp_path / "service.pid")
    monkeypatch.setattr(paths, "LOGS", tmp_path / "log")
    monkeypatch.setattr(service, "service_status", lambda: ("stopped", None))
    tokens = iter(("winner", "loser"))
    monkeypatch.setattr(service.secrets, "token_urlsafe", lambda _size: next(tokens))
    children: dict[str, MagicMock] = {}

    def spawn(command, **_kwargs):
        token = command[-1]
        child = MagicMock()
        child.poll.return_value = None if token == "winner" else 2
        children[token] = child
        return child

    monkeypatch.setattr(service.subprocess, "Popen", spawn)
    monkeypatch.setattr(
        service,
        "_read_startup_result",
        lambda token: ("ready", "") if token == "winner" else ("error", "already running"),
    )
    results: list[int] = []
    threads = [threading.Thread(target=lambda: results.append(service.start_detached())) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert sorted(results) == [0, 1]
    assert children["winner"].poll() is None
    children["winner"].terminate.assert_not_called()
    assert not service._ready_path("winner").exists()
    assert not service._ready_path("loser").exists()


def test_probe_requires_clean_expected_response(monkeypatch) -> None:
    from bubo import service

    cfg = review_config_from_dict({"subscription_circuit": {"enabled": True}})
    monkeypatch.setattr(
        service,
        "run_bounded",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "insufficient_quota", None),
    )
    assert not service._probe(cfg)
    monkeypatch.setattr(
        service,
        "run_bounded",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "not-json", None),
    )
    assert not service._probe(cfg)
    monkeypatch.setattr(
        service,
        "run_bounded",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "[]", None),
    )
    assert service._probe(cfg)


def test_probe_accepts_production_banner_with_final_empty_response(monkeypatch) -> None:
    from bubo import service

    cfg = review_config_from_dict({"subscription_circuit": {"enabled": True}})
    transcript = "OpenAI Codex banner\nmetadata\ncodex\n[]\n"
    monkeypatch.setattr(
        service,
        "run_bounded",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, transcript, None),
    )
    assert service._probe(cfg)


def test_transition_flushes_posthog_promptly(monkeypatch) -> None:
    from bubo import service

    cfg = review_config_from_dict({})
    telemetry = MagicMock()
    monkeypatch.setattr(service.analytics, "record_subscription_circuit", MagicMock())
    flushed = MagicMock()
    monkeypatch.setattr(service.analytics, "flush", flushed)
    monkeypatch.setattr(service, "log", MagicMock())
    service._transition(cfg, telemetry, "subscription_circuit_opened")
    flushed.assert_called_once()


def test_state_write_failure_emits_no_transition(monkeypatch, tmp_path: Path) -> None:
    from bubo import paths, service

    monkeypatch.setattr(paths, "DB", tmp_path / "reviewer.sqlite")
    cfg = review_config_from_dict({"subscription_circuit": {"enabled": True}})
    signal_subscription_failure(tmp_path / "subscription-circuit.signal")
    transition = MagicMock()
    monkeypatch.setattr(service, "_transition", transition)
    monkeypatch.setattr(service, "write_state", MagicMock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        service.monitor_loop(cfg, threading.Event())
    transition.assert_not_called()
