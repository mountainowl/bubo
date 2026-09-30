"""Portable long-running Bubo service.

This intentionally does not install an OS service.  ``bubo-poller service``
owns a poll schedule and the subscription-monitor thread while it is alive;
an external supervisor is still needed for boot startup or recovery after a
machine/process death.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from bubo import analytics, paths
from bubo.events import log
from bubo.findings import extract_findings
from bubo.review_config import ReviewConfig, load_review_config
from bubo.subproc import run_bounded
from bubo.subscription import (
    CircuitState,
    acknowledge_failure_signal,
    consume_failure_signal,
    is_subscription_failure,
    lock_path,
    read_state,
    signal_path,
    singleton_lock,
    state_path,
    write_state,
)
from bubo.telemetry import ReviewTelemetry

_PROBE_PROMPT = "Subscription availability probe. Reply with exactly []."


def _pid_path() -> Path:
    return paths.SERVICE_PID


def _service_lock_path() -> Path:
    return paths.SERVICE_PID.with_suffix(".lock")


def _stop_path() -> Path:
    return paths.SERVICE_PID.with_suffix(".stop")


def _read_identity() -> tuple[int, str, float] | None:
    try:
        raw = json.loads(_pid_path().read_text(encoding="utf-8"))
        return int(raw["pid"]), str(raw["token"]), float(raw.get("heartbeat_at", 0))
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _identity_matches(pid: int, token: str) -> bool:
    """Check the exact foreground command token before acting on a PID."""
    if not _alive(pid):
        return False
    if os.name == "nt":  # pragma: no cover - Windows only
        # Windows has no portable equivalent of POSIX ps command-line
        # inspection. Stop is cooperative below, so never kill by PID.
        return False
    command = ["ps", "-p", str(pid), "-o", "command="]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=2, check=False)
        return token in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def service_status() -> tuple[str, int | None]:
    identity = _read_identity()
    if identity is not None and (
        (os.name == "nt" and time.time() - identity[2] <= 5)
        or (os.name != "nt" and _identity_matches(identity[0], identity[1]))
    ):
        pid, _token, _heartbeat = identity
        return "running", pid
    if identity is not None:
        with suppress(FileNotFoundError):
            _pid_path().unlink()
    return "stopped", None


def stop_service() -> bool:
    identity = _read_identity()
    if identity is None:
        return False
    pid, token, heartbeat = identity
    if os.name == "nt":  # pragma: no cover - Windows only
        if time.time() - heartbeat > 5:
            return False
        _stop_path().write_text(token, encoding="utf-8")
        return True
    if not _identity_matches(pid, token):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def _write_pid(token: str) -> None:
    paths.SERVICE_PID.parent.mkdir(parents=True, exist_ok=True)
    temporary = paths.SERVICE_PID.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"pid": os.getpid(), "token": token, "heartbeat_at": time.time()}),
        encoding="utf-8",
    )
    os.replace(temporary, paths.SERVICE_PID)


def _heartbeat_loop(token: str, stop: threading.Event) -> None:
    """Keep Windows token metadata fresh even while a poll blocks for hours."""
    while not stop.wait(1):
        if os.name == "nt":  # pragma: no cover - Windows only
            _write_pid(token)


def _clear_pid() -> None:
    try:
        identity = _read_identity()
        if identity is not None and identity[0] == os.getpid():
            _pid_path().unlink()
    except FileNotFoundError:
        pass


def _transition(cfg: ReviewConfig, telemetry: ReviewTelemetry, event: str) -> None:
    # No project, SHA, error text, or provider response leaves this boundary.
    log(event, component="subscription_circuit")
    analytics.record_subscription_circuit(cfg.analytics_config, event=event)
    analytics.flush()
    telemetry.record_subscription_circuit(event=event)
    with telemetry.span(
        "llm_review.subscription_circuit", component="subscription_circuit"
    ) as span:
        telemetry.set_span_attrs(span, event=event)
        telemetry.add_event(span, event)


def _probe(cfg: ReviewConfig) -> bool:
    # Import locally: poller imports service only from CLI dispatch.
    from bubo.poller import reviewer_env

    try:
        result = run_bounded(
            [*cfg.reviewer_command, _PROBE_PROMPT],
            cwd=paths.ROOT,
            env=reviewer_env(os.environ, cfg),
            timeout=cfg.subscription_circuit.probe_timeout_seconds,
        )
        output = result.stdout or ""
        if result.returncode or is_subscription_failure(
            output, cfg.subscription_circuit.error_patterns, terminal_only=True
        ):
            return False
        try:
            # Shares the reviewer's banner/fence/final-assistant parsing rules.
            return extract_findings(output) == []
        except ValueError:
            return False
    except (OSError, subprocess.SubprocessError):
        return False


def monitor_loop(
    cfg: ReviewConfig, stop: threading.Event, ready: threading.Event | None = None
) -> None:
    """Run the sole state writer until ``stop``; a second monitor exits."""
    circuit = cfg.subscription_circuit
    if not circuit.enabled:
        return
    state_file = state_path(paths.DB.parent)
    signal_file = signal_path(paths.DB.parent)
    telemetry = ReviewTelemetry.from_config(cfg.telemetry_config)
    with singleton_lock(lock_path(paths.DB.parent)) as owner:
        if not owner:
            log("subscription_monitor_already_running", component="subscription_circuit")
            return
        while not stop.is_set():
            current = time.time()
            transition_event: str | None = None
            state = read_state(state_file)
            if state is None:
                state = CircuitState(status="closed", heartbeat_at=current)
            opened = consume_failure_signal(signal_file)
            if opened and state.status != "open":
                state = CircuitState(
                    status="open", heartbeat_at=current, opened_at=current,
                    next_probe_at=current + circuit.probe_interval_seconds,
                )
                transition_event = "subscription_circuit_opened"
            elif opened:
                # A signal that arrived while already open is acknowledged
                # only after another durable open heartbeat, never by a
                # recovery transition in the same tick.
                state = CircuitState(
                    status="open",
                    heartbeat_at=current,
                    opened_at=state.opened_at or current,
                    next_probe_at=state.next_probe_at,
                    probe_attempts=state.probe_attempts,
                )
            elif (
                state.status == "open"
                and state.next_probe_at is not None
                and current >= state.next_probe_at
            ):
                if _probe(cfg):
                    state = CircuitState(status="closed", heartbeat_at=current)
                    transition_event = "subscription_circuit_recovered"
                else:
                    attempts = state.probe_attempts + 1
                    delay = min(
                        circuit.probe_interval_seconds * (2 ** min(attempts, 16)),
                        circuit.probe_backoff_max_seconds,
                    )
                    state = CircuitState(
                        status="open", heartbeat_at=current, opened_at=state.opened_at or current,
                        next_probe_at=current + delay, probe_attempts=attempts,
                    )
                    log("subscription_circuit_probe_failed", component="subscription_circuit")
            else:
                state = CircuitState(
                    status=state.status, heartbeat_at=current, opened_at=state.opened_at,
                    next_probe_at=state.next_probe_at, probe_attempts=state.probe_attempts,
                )
            write_state(state_file, state)
            # The processing signal is acknowledged only after the durable
            # open-state write. A newly appended .signal remains for the next
            # tick, keeping readers fail-closed throughout the handoff.
            if state.status == "open" and opened:
                acknowledge_failure_signal(signal_file)
            if ready is not None:
                ready.set()
            if transition_event is not None:
                _transition(cfg, telemetry, transition_event)
            stop.wait(circuit.monitor_interval_seconds)


def run_foreground(service_token: str | None = None) -> int:
    cfg = load_review_config(paths.CONFIG, log_event=log)
    token = service_token or secrets.token_urlsafe(24)
    with singleton_lock(_service_lock_path()) as owner:
        if not owner:
            log("service_already_running")
            return 1
        _write_pid(token)
        return _run_service_loop(cfg, token)


def _run_service_loop(cfg: ReviewConfig, token: str) -> int:
    stop = threading.Event()
    heartbeat_stop = threading.Event()
    heartbeat: threading.Thread | None = None

    def request_stop(*_args: object) -> None:
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(ValueError):
            signal.signal(signum, request_stop)
    if os.name == "nt":  # pragma: no cover - Windows only
        heartbeat = threading.Thread(
            target=_heartbeat_loop, args=(token, heartbeat_stop), daemon=True
        )
        heartbeat.start()
    ready = threading.Event()
    monitor = threading.Thread(target=monitor_loop, args=(cfg, stop, ready), daemon=True)
    monitor.start()
    if cfg.subscription_circuit.enabled and not ready.wait(
        timeout=max(2, cfg.subscription_circuit.heartbeat_ttl_seconds)
    ):
        stop.set()
        heartbeat_stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=2)
        monitor.join(timeout=2)
        _clear_pid()
        log("service_monitor_not_ready")
        return 1
    interval = max(1, int(getattr(cfg, "service_poll_interval_seconds", 900)))
    log("service_started", subscription_monitor=cfg.subscription_circuit.enabled)
    try:
        while not stop.is_set():
            from bubo.poller import poll

            poll()
            for _ in range(interval):
                if stop.wait(1):
                    break
                if os.name == "nt" and _consume_stop_request(token):
                    stop.set()
                    break
    finally:
        stop.set()
        heartbeat_stop.set()
        if heartbeat is not None:
            heartbeat.join(timeout=2)
        monitor.join(timeout=max(2, cfg.subscription_circuit.monitor_interval_seconds + 1))
        _clear_pid()
        log("service_stopped")
    return 0


def _consume_stop_request(token: str) -> bool:
    try:
        requested = _stop_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    with suppress(FileNotFoundError):
        _stop_path().unlink()
    return secrets.compare_digest(requested, token)


def start_detached() -> int:
    status, pid = service_status()
    if status == "running":
        log("service_already_running", pid=pid)
        return 1
    paths.LOGS.mkdir(parents=True, exist_ok=True)
    log_file = (paths.LOGS / "service.log").open("ab", buffering=0)
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": log_file, "stderr": log_file}
    if os.name == "nt":  # pragma: no cover - Windows only
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        kwargs["start_new_session"] = True
    token = secrets.token_urlsafe(24)
    try:
        subprocess.Popen(
            [
                sys.executable, "-m", "bubo.poller", "service", "start", "--foreground",
                "--service-token", token,
            ],
            **kwargs,
        )
    finally:
        log_file.close()
    return 0


__all__ = ["monitor_loop", "run_foreground", "service_status", "start_detached", "stop_service"]
