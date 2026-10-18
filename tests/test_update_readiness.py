"""Replacement residents must prove startup while application work stays fenced."""

import json
import os
import uuid

import pytest

from bubo import update_readiness
from bubo.update_admission import _file_lock
from bubo.update_identity import atomic_json
from bubo.update_readiness import REQUEST_ENV, ResidentStartup, StartupRejected
from bubo.version import installed_version


@pytest.fixture
def startup_request(tmp_path, monkeypatch):
    state = tmp_path / "root/var/state"
    state.mkdir(parents=True)
    installation = tmp_path / "controller"
    installation.mkdir()
    (installation / "activity.lock").touch()
    (state / "update-activity.lock").touch()
    atomic_json(state / "update-registration.json", {"installation": str(installation)})
    request_id = str(uuid.uuid4())
    request = {
        "protocol_version": 1,
        "attempt_id": str(uuid.uuid4()),
        "token": str(uuid.uuid4()),
        "state": str(state),
        "kind": "service",
        "version": installed_version(),
        "outcome": "committed",
    }
    atomic_json(installation / "residents" / f"{request_id}.json", request)
    atomic_json(installation / "maintenance.json", request)
    atomic_json(
        installation / "attempt.json",
        {
            "attempt_id": request["attempt_id"],
            "token": request["token"],
            "states": [str(state)],
            "phase": "migration",
            "target": request["version"],
            "source": "0.0.1",
        },
    )
    monkeypatch.setattr(update_readiness, "_start_identity", lambda pid: "stable-start-identity")
    return state, installation, request_id


def finish(installation, **changes):
    journal = json.loads((installation / "attempt.json").read_text())
    journal.update(phase="committed", **changes)
    atomic_json(installation / "attempt.json", journal)
    atomic_json(installation / "generation.json", {"version": journal["target"]})
    (installation / "maintenance.json").unlink()


def test_normal_startup_has_no_disk_effects(tmp_path, monkeypatch):
    monkeypatch.delenv(REQUEST_ENV, raising=False)
    assert ResidentStartup.from_environment("service", tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_real_startup_acknowledges_identity_but_waits_for_commit(startup_request, monkeypatch):
    state, installation, request_id = startup_request
    monkeypatch.setenv(REQUEST_ENV, request_id)
    gate = ResidentStartup.from_environment("service", state)
    assert REQUEST_ENV not in os.environ
    gate.signal_ready()
    ready = json.loads(gate.ready_path.read_text())
    assert ready["pid"] == os.getpid()
    assert ready["process_identity"] == "stable-start-identity"
    assert ready["loaded_version"] == installed_version()
    assert gate.ready_path.stat().st_mode & 0o777 == 0o600
    assert not gate.committed()
    finish(installation)
    assert gate.committed()


def test_startup_request_is_one_use(startup_request):
    state, _, request_id = startup_request
    first = ResidentStartup(state, "service", request_id)
    second = ResidentStartup(state, "service", request_id)
    first.signal_ready()
    with pytest.raises(StartupRejected, match="already consumed"):
        second.signal_ready()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "mcp-http"),
        ("version", "0.0.0"),
        ("state", "/wrong"),
        ("token", "wrong"),
        ("attempt_id", "wrong"),
        ("protocol_version", 2),
    ],
)
def test_stale_or_wrong_request_never_publishes_readiness(startup_request, field, value):
    state, installation, request_id = startup_request
    request_path = installation / "residents" / f"{request_id}.json"
    request = json.loads(request_path.read_text())
    request[field] = value
    atomic_json(request_path, request)
    with pytest.raises(StartupRejected):
        ResidentStartup(state, "service", request_id)
    assert not request_path.with_suffix(".ready.json").exists()


def test_readiness_does_not_release_on_fence_disappearance_alone(startup_request):
    state, installation, request_id = startup_request
    gate = ResidentStartup(state, "service", request_id)
    gate.signal_ready()
    (installation / "maintenance.json").unlink()
    with pytest.raises(StartupRejected):
        gate.committed()


def test_other_attempt_commit_is_rejected(startup_request):
    state, installation, request_id = startup_request
    gate = ResidentStartup(state, "service", request_id)
    gate.signal_ready()
    finish(installation, attempt_id=str(uuid.uuid4()))
    with pytest.raises(StartupRejected):
        gate.committed()


def test_gate_revalidates_with_shared_admission_lock(startup_request):
    state, installation, request_id = startup_request
    gate = ResidentStartup(state, "service", request_id)
    gate.signal_ready()
    finish(installation)
    with _file_lock(installation / "activity.lock", exclusive=True):
        assert not gate.committed()
    assert gate.committed()


def test_target_candidate_rejects_recovery_of_same_attempt(startup_request):
    state, installation, request_id = startup_request
    gate = ResidentStartup(state, "service", request_id)
    gate.signal_ready()
    journal = json.loads((installation / "attempt.json").read_text())
    journal["phase"] = "recovery"
    atomic_json(installation / "attempt.json", journal)
    with pytest.raises(StartupRejected):
        gate.committed()


def test_gate_timeout_leaves_fence_closed(startup_request):
    state, installation, request_id = startup_request
    gate = ResidentStartup(state, "service", request_id)
    gate.signal_ready()
    with pytest.raises(StartupRejected, match="timed out"):
        gate.wait(timeout=0)
    assert (installation / "maintenance.json").is_file()


def test_no_process_identity_means_no_readiness(startup_request, monkeypatch):
    state, _, request_id = startup_request
    gate = ResidentStartup(state, "service", request_id)
    monkeypatch.setattr(update_readiness, "_start_identity", lambda pid: None)
    with pytest.raises(StartupRejected, match="identity"):
        gate.signal_ready()
    assert not gate.ready_path.exists()


def test_service_initializes_locks_without_polling_or_db_writes_before_commit(
    startup_request, monkeypatch
):
    import threading
    import time

    from bubo import paths, service, update_helper
    from bubo.review_config import review_config_from_dict
    from bubo.subscription import state_path

    state, installation, request_id = startup_request
    monkeypatch.setattr(paths, "DB", state / "reviewer.sqlite")
    monkeypatch.setattr(paths, "SERVICE_PID", state / "service.pid")
    monkeypatch.setattr(update_helper, "ensure_registered", lambda *args, **kwargs: None)
    polled = threading.Event()
    monkeypatch.setattr(service, "_poll_once", polled.set)
    monkeypatch.setattr(service, "_sync_outcomes", lambda cfg: None)
    monkeypatch.setattr(service, "_wait_for_next_cycle", lambda *args: True)
    cfg = review_config_from_dict({"subscription_circuit": {"enabled": True}})
    gate = ResidentStartup(state, "service", request_id)
    results = []
    thread = threading.Thread(
        target=lambda: results.append(service._run_service_loop(cfg, "token", startup=gate))
    )
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not gate.ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert gate.ready_path.exists()
        assert thread.is_alive()
        assert not polled.is_set()
        assert not paths.DB.exists()
        assert not state_path(state).exists()
        finish(installation)
        thread.join(timeout=3)
        assert results == [0]
        assert polled.is_set()
    finally:
        if (installation / "maintenance.json").exists():
            finish(installation)
        thread.join(timeout=3)


def test_async_wait_cancels_without_leaving_a_background_thread(startup_request):
    import asyncio

    state, _, request_id = startup_request
    gate = ResidentStartup(state, "service", request_id)
    gate.signal_ready()

    async def cancel():
        task = asyncio.create_task(gate.wait_async())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())


def resident_environment(startup_request, kind):
    state, installation, request_id = startup_request
    root = state.parent.parent
    (root / "config").mkdir(parents=True)
    (root / "config/env.toml").write_text("auto_update = false\n[analytics]\nenabled = false\n")
    request_path = installation / "residents" / f"{request_id}.json"
    request = json.loads(request_path.read_text())
    request["kind"] = kind
    atomic_json(request_path, request)
    return dict(
        os.environ,
        BUBO_ROOT=str(root),
        BUBO_BASE_DIR=str(state.parent),
        BUBO_UPDATE_START_REQUEST=request_id,
    ), request_path.with_suffix(".ready.json")


@pytest.mark.parametrize("controlled", [False, True])
def test_real_http_resident_is_bound_but_tools_are_fenced_until_commit(
    startup_request, monkeypatch, controlled
):
    import asyncio
    import socket
    import subprocess
    import sys
    import time

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    state, installation, _ = startup_request
    env, ready = resident_environment(startup_request, "mcp-http")
    from bubo import update_installers
    from bubo.update_readiness import ResidentReadiness

    monkeypatch.setattr(update_readiness, "_start_identity", update_installers._start_identity)
    controller = ResidentReadiness(state, "mcp-http")
    env = controller.environment(env)
    ready = controller.ready_path
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    env.update(
        BUBO_MCP_HOST="127.0.0.1", BUBO_MCP_PORT=str(port), BUBO_MCP_BEARER_TOKEN="test-token"
    )
    if controlled:
        env["BUBO_RESIDENT_CONTROL_STDIN"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-c", "from bubo.mcp_server import _run_http; _run_http()"],
        env=env,
        stdin=subprocess.PIPE if controlled else None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        controller.bind(process)
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), f"HTTP resident exited with {process.poll()}"
        assert json.loads(ready.read_text())["pid"] == process.pid
        assert process.poll() is None

        async def exercise():
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer test-token"}, timeout=5
            ) as http:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp", http_client=http
                ) as (reader, writer, _):
                    async with ClientSession(reader, writer) as client:
                        await client.initialize()
                        before = await client.call_tool("health")
                        assert before.isError
                        assert not (state / "reviewer.sqlite").exists()
                        assert controller.verify()
                        finish(installation)
                        after = await client.call_tool("health")
                        assert not after.isError

        asyncio.run(exercise())
        if controlled:
            process.stdin.close()
            assert process.wait(timeout=5) == 0
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_occupied_http_port_never_publishes_readiness(startup_request):
    import socket
    import subprocess
    import sys

    env, ready = resident_environment(startup_request, "mcp-http")
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        env.update(
            BUBO_MCP_HOST="127.0.0.1",
            BUBO_MCP_PORT=str(occupied.getsockname()[1]),
            BUBO_MCP_BEARER_TOKEN="test-token",
        )
        result = subprocess.run(
            [sys.executable, "-c", "from bubo.mcp_server import _run_http; _run_http()"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    assert result.returncode != 0
    assert not ready.exists()


def test_real_stdio_lifespan_waits_for_commit_before_initializing(startup_request):
    import asyncio
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _, installation, _ = startup_request
    env, ready = resident_environment(startup_request, "mcp-stdio")
    env["BUBO_MCP_TRANSPORT"] = "stdio"

    async def exercise():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from bubo.mcp_server import main; main()"],
            env=env,
        )
        async with stdio_client(parameters) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                initialized = asyncio.create_task(client.initialize())
                try:
                    async with asyncio.timeout(10):
                        while not ready.exists():
                            await asyncio.sleep(0.02)
                    assert not initialized.done()
                    finish(installation)
                    await asyncio.wait_for(initialized, timeout=5)
                    result = await client.call_tool("health")
                    assert not result.isError
                finally:
                    if not initialized.done():
                        initialized.cancel()

    asyncio.run(exercise())


def test_unknown_installed_version_rejects_without_readiness(startup_request, monkeypatch):
    state, installation, request_id = startup_request
    monkeypatch.setattr(update_readiness, "installed_version", lambda: None)
    with pytest.raises(StartupRejected, match="version is unavailable"):
        ResidentStartup(state, "service", request_id)
    assert not (installation / "residents" / f"{request_id}.ready.json").exists()
    assert not (installation / "residents" / f"{request_id}.claim").exists()


@pytest.mark.parametrize("kind", ["mcp-http", "mcp-stdio"])
def test_real_resident_rejects_changed_attempt_and_exits_75(startup_request, kind):
    import socket
    import subprocess
    import sys
    import time

    _, installation, _ = startup_request
    env, ready = resident_environment(startup_request, kind)
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    env.update(
        BUBO_MCP_TRANSPORT="http" if kind == "mcp-http" else "stdio",
        BUBO_MCP_HOST="127.0.0.1",
        BUBO_MCP_PORT=str(port),
        BUBO_MCP_BEARER_TOKEN="test-token",
    )
    process = subprocess.Popen(
        [sys.executable, "-c", "from bubo.mcp_server import main; main()"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), f"Resident exited with {process.poll()}"
        journal = json.loads((installation / "attempt.json").read_text())
        journal["token"] = str(uuid.uuid4())
        atomic_json(installation / "attempt.json", journal)
        assert process.wait(timeout=5) == 75
        with socket.socket() as rebound:
            rebound.bind(("127.0.0.1", port))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()


def test_recovery_uses_fresh_request_and_waits_for_restored_generation(startup_request):
    state, installation, old_id = startup_request
    old = ResidentStartup(state, "service", old_id)
    old.signal_ready()
    new_id = str(uuid.uuid4())
    request = dict(old.request, outcome="restored")
    atomic_json(installation / "residents" / f"{new_id}.json", request)
    journal = json.loads((installation / "attempt.json").read_text())
    journal.update(phase="recovery", source=installed_version(), target="9.9.9")
    atomic_json(installation / "attempt.json", journal)
    restored = ResidentStartup(state, "service", new_id)
    restored.signal_ready()
    assert not restored.committed()
    journal["phase"] = "restored"
    atomic_json(installation / "attempt.json", journal)
    atomic_json(installation / "generation.json", {"version": installed_version()})
    (installation / "maintenance.json").unlink()
    assert restored.committed()
    with pytest.raises(StartupRejected):
        old.committed()


def test_service_stop_cancels_readiness_wait(startup_request):
    state, _, request_id = startup_request
    gate = ResidentStartup(state, "service", request_id)
    gate.signal_ready()
    with pytest.raises(StartupRejected, match="cancelled"):
        gate.wait(cancelled=lambda: True)


def test_stdio_bad_config_exits_without_readiness_even_with_open_stdin(startup_request):
    import subprocess
    import sys

    state, _, _ = startup_request
    env, ready = resident_environment(startup_request, "mcp-stdio")
    env["BUBO_MCP_TRANSPORT"] = "stdio"
    (state.parent.parent / "config/env.toml").write_text("[invalid")
    process = subprocess.Popen(
        [sys.executable, "-c", "from bubo.mcp_server import main; main()"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert process.wait(timeout=5) == 75
        assert not ready.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdin.close()


def test_controller_request_strips_maintenance_bypass_and_never_persists_secrets(startup_request):
    from bubo.update_readiness import ResidentReadiness

    state, _, _ = startup_request
    controller = ResidentReadiness(state, "service")
    inherited = {"BUBO_MAINTENANCE_TOKEN": "bypass", "GITHUB_TOKEN": "test-secret"}
    child = controller.environment(inherited)
    assert child[REQUEST_ENV] == controller.request_id
    assert "BUBO_MAINTENANCE_TOKEN" not in child
    assert child["GITHUB_TOKEN"] == "test-secret"
    assert inherited["BUBO_MAINTENANCE_TOKEN"] == "bypass"
    assert "test-secret" not in controller.path.read_text()
    assert controller.path.stat().st_mode & 0o777 == 0o600


def test_controller_rejects_receipt_without_owned_child(startup_request):
    from bubo.update_readiness import ResidentReadiness

    state, _, _ = startup_request
    controller = ResidentReadiness(state, "service")
    gate = ResidentStartup(state, "service", controller.request_id)
    gate.signal_ready()
    with pytest.raises(StartupRejected, match="ownership"):
        controller.verify()


def test_controller_proves_readiness_of_real_child_and_rejects_its_death(
    startup_request, monkeypatch
):
    import subprocess
    import sys

    from bubo import update_installers
    from bubo.update_readiness import ResidentReadiness

    state, _, _ = startup_request
    monkeypatch.setattr(update_readiness, "_start_identity", update_installers._start_identity)
    env, _ = resident_environment(startup_request, "service")
    controller = ResidentReadiness(state, "service")
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from bubo.update_readiness import ResidentStartup; g=ResidentStartup.from_environment('service'); g.signal_ready(); g.wait()",
        ],
        env=controller.environment(env),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        controller.bind(child)
        controller.wait(timeout=5)
        assert controller.verify()
        owner = json.loads(controller.owner_path.read_text())
        assert owner["pid"] == child.pid
        assert owner["process_identity"] == update_installers._start_identity(child.pid)
        child.terminate()
        child.wait(timeout=5)
        with pytest.raises(StartupRejected, match="no longer"):
            controller.verify()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pid", -1),
        ("process_identity", "another-process"),
        ("loaded_version", "9.9.9"),
        ("attempt_id", "another-attempt"),
        ("token", "another-token"),
    ],
)
def test_controller_rejects_forged_receipts(startup_request, field, value):
    from unittest.mock import Mock

    from bubo.update_readiness import ResidentReadiness

    state, _, _ = startup_request
    controller = ResidentReadiness(state, "service")
    child = Mock(pid=os.getpid())
    child.poll.return_value = None
    controller.bind(child)
    gate = ResidentStartup(state, "service", controller.request_id)
    gate.signal_ready()
    assert controller.verify()
    receipt = json.loads(controller.ready_path.read_text())
    receipt[field] = value
    atomic_json(controller.ready_path, receipt)
    with pytest.raises(StartupRejected, match="does not match"):
        controller.verify()


def test_controller_rejects_version_change_with_same_attempt(startup_request):
    from bubo.update_readiness import ResidentReadiness

    state, installation, _ = startup_request
    controller = ResidentReadiness(state, "service")
    journal = json.loads((installation / "attempt.json").read_text())
    journal["target"] = "9.9.9"
    atomic_json(installation / "attempt.json", journal)
    with pytest.raises(StartupRejected, match="attempt changed"):
        controller.verify()
    with pytest.raises(StartupRejected, match="attempt changed"):
        ResidentStartup(state, "service", controller.request_id)


@pytest.mark.parametrize("shutdown", ["pipe", "server"])
def test_normal_http_resident_exits_cleanly_with_control_pipe(startup_request, shutdown):
    import socket
    import subprocess
    import sys
    import time

    env, _ = resident_environment(startup_request, "mcp-http")
    env.pop(update_readiness.REQUEST_ENV, None)
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    env.update(
        BUBO_MCP_HOST="127.0.0.1",
        BUBO_MCP_PORT=str(port),
        BUBO_MCP_BEARER_TOKEN="test-token",
        BUBO_RESIDENT_CONTROL_STDIN="1",
    )
    code = "from bubo.mcp_server import _run_http; _run_http()"
    if shutdown == "server":
        code = (
            "import uvicorn\n"
            "async def stop_after_start(self, counter):\n    return counter >= 1\n"
            "uvicorn.Server.on_tick = stop_after_start\n" + code
        )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if shutdown == "pipe":
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    assert time.monotonic() < deadline, "HTTP resident did not bind"
                    time.sleep(0.02)
            process.stdin.close()
        # The independent server shutdown intentionally leaves the writer open.
        assert process.wait(timeout=5) == 0
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
