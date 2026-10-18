"""Real transient transport with signature-checked native launch boundaries."""

import inspect
import os
import socket
import struct
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from bubo import update_admission, update_platform
from bubo import update_windows_broker as broker
from bubo import update_windows_process as native
from bubo import update_windows_worker as worker


@pytest.fixture
def running(tmp_path, monkeypatch):
    state = SimpleNamespace(launches=[], owners=[], failure=False, close_failure=False)
    spawn_signature = inspect.signature(worker.spawn)
    output_signature = inspect.signature(native.duplicate_output)
    output = (tmp_path / "worker.log").open("ab", buffering=0)

    @contextmanager
    def duplicate(*args, **kwargs):
        output_signature.bind(*args, **kwargs)
        state.owners.append((args, kwargs))
        try:
            yield output
        finally:
            if state.close_failure:
                raise OSError("borrowed stream close failed")

    def spawn(*args, **kwargs):
        spawn_signature.bind(*args, **kwargs)
        state.launches.append((args, kwargs))
        if state.failure:
            raise OSError("SECRET_NATIVE_ERROR")
        return 1234

    monkeypatch.setattr(native, "duplicate_output", duplicate)
    monkeypatch.setattr(worker, "spawn", spawn)
    monkeypatch.setattr(update_admission, "_PROCESS_VERSION", "1.2.3")
    monkeypatch.setattr(update_platform, "windows_process_identity", lambda pid: "windows:123")
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 99))
    server = broker.WorkerBroker(tmp_path / "installation", tmp_path / "state")
    server.bind(70)
    for key, value in server.environment().items():
        monkeypatch.setenv(key, value)
    state.server, state.output = server, output
    try:
        yield state
    finally:
        server.close()
        output.close()


def test_real_transport_preserves_launch_configuration_without_propagating_credentials(running):
    assert broker.spawn_via_broker(["python", "--worker", "job"], stdout=running.output) == 1234
    args, kwargs = running.launches[0]
    assert args == (
        ["python", "--worker", "job"],
        running.server.installation,
        running.server.state,
    )
    assert kwargs["cwd"] == os.getcwd()
    assert kwargs["process_version"] == "1.2.3"
    assert kwargs["stdout"] is running.output
    assert kwargs["env"] == {
        k: v for k, v in os.environ.items() if k not in {broker.ENDPOINT, broker.TOKEN}
    }
    assert running.owners == [((os.getpid(), "windows:123", 99), {"resident_job": 70})]


def test_wrong_token_never_launches(running, monkeypatch):
    monkeypatch.setenv(broker.TOKEN, "wrong")
    with pytest.raises(broker.LaunchOutcomeUnknown, match="check worker status"):
        broker.spawn_via_broker(["worker"], stdout=running.output)
    assert not running.launches
    assert not running.owners


def test_failure_response_excludes_native_details(running):
    running.failure = True
    with pytest.raises(broker.LaunchOutcomeUnknown, match="check worker status") as failure:
        broker.spawn_via_broker(["worker", "SECRET_ARGUMENT"], stdout=running.output)
    assert "SECRET" not in str(failure.value)
    assert len(running.launches) == 1


def test_lost_reply_after_commit_reports_unknown_without_retry(running, monkeypatch):
    send = broker._send

    def disconnect(connection, message):
        if message.get("status") == "started":
            raise ConnectionResetError("lost response")
        send(connection, message)

    monkeypatch.setattr(broker, "_send", disconnect)
    with pytest.raises(broker.LaunchOutcomeUnknown, match="check worker status"):
        broker.spawn_via_broker(["worker"], stdout=running.output)
    assert len(running.launches) == 1


def test_post_commit_stream_cleanup_cannot_fail_launch(running):
    running.close_failure = True
    assert broker.spawn_via_broker(["worker"], stdout=running.output) == 1234
    assert len(running.launches) == 1


def test_oversized_envelope_is_rejected_before_launch(running):
    port = int(running.server.environment()[broker.ENDPOINT])
    with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
        connection.sendall(struct.pack("!I", broker._LIMIT + 1))
        assert connection.recv(1) == b""
    assert not running.launches


def test_close_retires_endpoint_and_is_idempotent(running):
    running.server.close()
    running.server.close()
    assert not running.server.thread.is_alive()
    with pytest.raises(OSError, match=r".+"):
        broker.spawn_via_broker(["worker"], stdout=running.output)
    assert not running.launches


@pytest.mark.parametrize("committed", [False, True])
def test_generic_transport_oserror_is_unknown(running, monkeypatch, committed):
    send = broker._send

    def send_error(connection, message):
        if "args" in message and not committed:
            connection.sendall(b"\x00")
            raise OSError("generic partial send error")
        send(connection, message)

    receive = broker._receive

    def receive_error(connection):
        response = receive(connection)
        if isinstance(response, dict) and response.get("status") == "started":
            raise OSError("generic receive error after commitment")
        return response

    monkeypatch.setattr(broker, "_send", send_error)
    monkeypatch.setattr(broker, "_receive", receive_error)
    with pytest.raises(broker.LaunchOutcomeUnknown, match="check worker status"):
        broker.spawn_via_broker(["worker"], stdout=running.output)
    assert len(running.launches) == int(committed)


def test_verified_prelaunch_rejection_is_definite_failure(running, monkeypatch):
    @contextmanager
    def reject(*args, **kwargs):
        raise OSError("identity changed")
        yield  # pragma: no cover

    monkeypatch.setattr(native, "duplicate_output", reject)
    with pytest.raises(OSError, match="Worker launch failed") as failure:
        broker.spawn_via_broker(["worker"], stdout=running.output)
    assert not isinstance(failure.value, broker.LaunchOutcomeUnknown)
    assert not running.launches
