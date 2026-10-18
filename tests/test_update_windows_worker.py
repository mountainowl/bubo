"""Guardian lifetime checks; native Job behavior requires Windows acceptance."""

import inspect
import json
import os
import sys
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from bubo import update_windows_worker as worker


class NativeExit(BaseException):
    pass


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    installation, state, environment = [
        tmp_path / name for name in ("installation", "state", "venv")
    ]
    for directory in (installation, state, environment):
        directory.mkdir()
    locks = [
        environment / "pyvenv.cfg",
        installation / "activity.lock",
        state / "update-activity.lock",
    ]
    for path in locks:
        path.touch()
    generation = {"version": "1.0", "attempt": "abc"}
    (installation / "generation.json").write_text(json.dumps(generation))
    (installation / "installation.json").write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "owner": "owner",
                "environment": str(environment),
                "states": [str(state)],
            }
        )
    )
    (state / "update-registration.json").write_text(
        json.dumps(
            {
                "installation": str(installation),
                "ready": True,
            }
        )
    )
    monkeypatch.setattr(worker, "current_owner", lambda: "owner")
    payload = {
        "installation": str(installation),
        "state": str(state),
        "environment": str(environment),
        "generation": generation,
        "parent": {"job": "guardian-job"},
        "args": ["worker", "--job", "42"],
    }
    events, held, membership = [], [], [True]
    original = worker._admission

    def admission(actual, leases):
        try:
            return original(actual, leases)
        finally:
            held.extend(leases)

    monkeypatch.setattr(worker, "_admission", admission)

    def alone():
        events.append("membership")
        return membership.pop(0) if len(membership) > 1 else membership[0]

    def native_exit(status):
        events.append(("exit", status))
        assert all(not stream.closed for stream in held)
        raise NativeExit(status)

    guardian = SimpleNamespace(alone=alone, exit=native_exit)
    monkeypatch.setattr(worker, "GuardianJob", lambda name: guardian)

    def finish(actual_guardian, status):
        assert actual_guardian is guardian
        events.append("retirement")
        native_exit(status)

    def adopt(actual_installation, parent, actual_guardian):
        assert actual_installation == installation
        assert actual_guardian is guardian
        assert len(held) == 2
        events.append("adoption")
        return SimpleNamespace(
            finish_guardian=finish,
            worker_started=lambda: events.append("started"),
            worker_failed=lambda code: events.append(("start-failed", code)),
        )

    monkeypatch.setattr(worker.JobOwnership, "adopt", adopt)

    def call(args, *, stdin, stdout, stderr, close_fds):
        assert args == payload["args"]
        assert stdin == worker.subprocess.DEVNULL
        assert stdout is sys.stdout
        assert stderr is sys.stderr
        assert close_fds
        events.append("work")
        return SimpleNamespace(wait=lambda: 7)

    monkeypatch.setattr(worker.subprocess, "Popen", call)
    monkeypatch.setattr(worker.time, "sleep", lambda delay: events.append("wait"))
    yield SimpleNamespace(
        payload=payload,
        events=events,
        held=held,
        membership=membership,
        locks=locks[1:],
        installation=installation,
        guardian=guardian,
    )
    for stream in held:
        stream.close()


def test_guardian_waits_for_descendants_with_all_leases_until_native_exit(runtime):
    runtime.membership[:] = [False, False, True]
    with pytest.raises(NativeExit) as exited:
        worker.run(runtime.payload)
    assert exited.value.args == (7,)
    assert runtime.events == [
        "adoption",
        "work",
        "started",
        "membership",
        "wait",
        "membership",
        "wait",
        "membership",
        "retirement",
        ("exit", 7),
    ]
    # Distinct file descriptions cannot take exclusive admission while the guardian
    # still has leases. The fake ExitProcess leaves them open for this assertion.
    with ExitStack() as stack:
        for path in runtime.locks:
            stream = stack.enter_context(path.open("rb"))
            with pytest.raises(BlockingIOError):
                worker.lock_file(stream.fileno(), exclusive=True)


@pytest.mark.parametrize("changed", ["generation", "registration", "maintenance", "missing-lock"])
def test_changed_admission_cannot_adopt_or_launch(runtime, changed):
    if changed == "generation":
        runtime.payload["generation"] = {"version": "different"}
    elif changed == "registration":
        (runtime.installation / "installation.json").write_text("{}")
    elif changed == "maintenance":
        (runtime.installation / "maintenance.json").write_text("{}")
    else:
        runtime.locks[-1].unlink()
    with pytest.raises(NativeExit) as exited:
        worker.run(runtime.payload)
    assert exited.value.args == (1,)
    assert runtime.events == [("exit", 1)]
    if changed == "missing-lock":
        assert not runtime.locks[-1].exists()


def test_uncertain_adoption_exits_without_work_or_normal_cleanup(runtime, monkeypatch):
    def uncertain(*args):
        raise OSError("uncertain ownership publication")

    monkeypatch.setattr(worker.JobOwnership, "adopt", uncertain)
    with pytest.raises(NativeExit) as exited:
        worker.run(runtime.payload)
    assert exited.value.args == (1,)
    assert runtime.events == [("exit", 1)]
    assert len(runtime.held) == 2


@pytest.mark.parametrize("updated_before_admission", [False, True])
def test_first_registration_generation_is_optional_but_cannot_change_before_admission(
    runtime, updated_before_admission
):
    generation = runtime.installation / "generation.json"
    generation.unlink()
    runtime.payload["generation"] = None
    if updated_before_admission:
        generation.write_text(json.dumps({"version": "new-release"}))
    with pytest.raises(NativeExit) as exited:
        worker.run(runtime.payload)
    assert exited.value.args == ((1,) if updated_before_admission else (7,))
    assert ("adoption" in runtime.events) is not updated_before_admission


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (FileNotFoundError, "missing_executable"),
        (PermissionError, "permission_denied"),
        (OSError, "launch_failed"),
    ],
)
def test_workload_start_failure_still_uses_terminal_guardian_path(
    runtime, monkeypatch, error, code
):
    def failed(*args, **kwargs):
        raise error("private command details must not be journaled")

    monkeypatch.setattr(worker.subprocess, "Popen", failed)
    with pytest.raises(NativeExit) as exited:
        worker.run(runtime.payload)
    assert exited.value.args == (1,)
    assert runtime.events == ["adoption", ("start-failed", code), "retirement", ("exit", 1)]


def test_private_launch_envelope_is_removed_before_work(runtime, monkeypatch):
    monkeypatch.setenv("PRIVATE_GUARDIAN_ENVELOPE", json.dumps(runtime.payload))
    monkeypatch.setattr(
        worker,
        "run",
        lambda payload: (
            runtime.events.append("PRIVATE_GUARDIAN_ENVELOPE" in os.environ),
            runtime.events.append(payload == runtime.payload),
        ),
    )
    worker.main("PRIVATE_GUARDIAN_ENVELOPE")
    assert runtime.events == [False, True]


@pytest.fixture
def launcher(runtime, monkeypatch):
    from bubo import update_admission, update_windows_jobs

    archive = runtime.installation / "helper.pyz"
    archive.touch()
    registration_path = runtime.installation / "installation.json"
    registration = json.loads(registration_path.read_text())
    registration.update(archive=str(archive), python="retained-python")
    registration_path.write_text(json.dumps(registration))
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda descriptor: descriptor)
    )
    monkeypatch.setattr(update_admission, "_check_generation", lambda path: None)
    monkeypatch.setattr(update_windows_jobs, "current_owner", lambda: "owner")
    monkeypatch.setattr(
        update_windows_jobs,
        "windows_process_identity",
        lambda pid: "parent-start" if pid == os.getpid() else "child-start",
    )
    events, launches = [], []
    scenario = {"transfer": "pending", "exit": None, "stop_failure": False}
    native_signature = inspect.signature(worker.JobProcess)

    class Child:
        pid = os.getpid() + 1

        def __init__(self, args, *, name, on_job_created, handles, env, cwd=None):
            native_signature.bind(
                args,
                name=name,
                on_job_created=on_job_created,
                handles=handles,
                env=env,
                cwd=cwd,
            )
            self.name, self.job = name, object()
            on_job_created()
            payload = json.loads(env[args[-1]])
            assert args[:4] == ["retained-python", "-I", "-S", "-c"]
            assert args[-2] == str(archive)
            assert all(isinstance(handle, int) for handle in handles)
            self.path = runtime.installation / "windows-jobs" / (name[-32:] + ".json")
            original = json.loads(self.path.read_text())
            assert original == payload["parent"]
            scenario["env"], scenario["cwd"] = env, cwd
            launches.append(payload)
            if scenario["transfer"] != "absent":
                self.path.write_text(
                    json.dumps(
                        {
                            **original,
                            "owner_pid": self.pid,
                            "owner_start": "child-start",
                            "state": scenario["transfer"],
                            "worker_started": scenario.get("started", True),
                            "worker_error": scenario.get("error"),
                        }
                    )
                )
            events.append("spawn")

        def identity(self):
            assert self.job is not None
            return "child-start"

        def poll(self):
            return scenario["exit"]

        def detach(self, acknowledge):
            acknowledge(self)
            events.append("detach")
            self.job = None

        def close(self):
            assert all(not stream.closed for stream in runtime.held[-2:])
            events.append("stop" if self.job is not None else "handle-cleanup")
            if scenario["stop_failure"]:
                raise OSError("native close failed")
            self.job = None

    monkeypatch.setattr(worker, "JobProcess", Child)
    with open(os.devnull, "wb") as stdout:

        def spawn(*, worker_args=None, **kwargs):
            return worker.spawn(
                worker_args or ["worker", "--job", "42"],
                runtime.installation,
                runtime.locks[-1].parent,
                stdout=stdout,
                timeout=0,
                **kwargs,
            )

        yield SimpleNamespace(
            runtime=runtime,
            scenario=scenario,
            events=events,
            launches=launches,
            spawn=spawn,
            child_pid=Child.pid,
        )


@pytest.mark.parametrize("transfer", ["pending", "quiescent"])
def test_parent_launch_commits_running_or_already_completed_guardian(launcher, transfer):
    launcher.scenario["transfer"] = transfer
    assert launcher.spawn() == launcher.child_pid
    assert launcher.events == ["spawn", "detach", "handle-cleanup"]
    assert all(stream.closed for stream in launcher.runtime.held)
    assert launcher.launches[0]["args"] == ["worker", "--job", "42"]
    if transfer == "quiescent":
        assert list((launcher.runtime.installation / "windows-jobs").iterdir()) == []


@pytest.mark.parametrize("problem", ["timeout", "exit", "abandoned"])
def test_uncommitted_parent_launch_stops_tree_before_releasing_admission(launcher, problem):
    launcher.scenario["transfer"] = "abandoned" if problem == "abandoned" else "absent"
    launcher.scenario["exit"] = 1 if problem == "exit" else None
    with pytest.raises(OSError, match=r"timed out|exited before|abandoned startup"):
        launcher.spawn()
    assert launcher.events == ["spawn", "stop"]
    assert all(stream.closed for stream in launcher.runtime.held)
    assert list((launcher.runtime.installation / "windows-jobs").iterdir()) == []


def test_unresolved_parent_stop_preserves_receipt(launcher):
    launcher.scenario.update(transfer="absent", stop_failure=True)
    with pytest.raises(OSError, match="handoff cleanup unresolved"):
        launcher.spawn()
    receipts = list((launcher.runtime.installation / "windows-jobs").glob("*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["state"] == "pending"
    assert launcher.events == ["spawn", "stop"]


def test_post_commit_bookkeeping_and_handle_cleanup_cannot_fail_launch(launcher, monkeypatch):
    original = worker.JobOwnership.handoff

    def failed_after_commit(owner, child):
        original(owner, child)
        raise OSError("late bookkeeping error")

    monkeypatch.setattr(worker.JobOwnership, "handoff", failed_after_commit)
    launcher.scenario["stop_failure"] = True
    assert launcher.spawn() == launcher.child_pid
    assert launcher.events == ["spawn", "detach", "handle-cleanup"]
    assert all(stream.closed for stream in launcher.runtime.held)


def test_missing_retained_helper_prevents_any_job_launch(launcher):
    (launcher.runtime.installation / "helper.pyz").unlink()
    with pytest.raises(OSError, match="retained helper is unavailable"):
        launcher.spawn()
    assert launcher.events == []
    assert not (launcher.runtime.installation / "windows-jobs").exists()


@pytest.mark.parametrize(
    ("code", "exception"),
    [
        ("missing_executable", FileNotFoundError),
        ("permission_denied", PermissionError),
        ("launch_failed", OSError),
    ],
)
def test_parent_reports_worker_creation_failure_then_cleans_job(launcher, code, exception):
    launcher.scenario.update(started=False, error=code, transfer="quiescent")
    with pytest.raises(exception, match=r"executable|process creation"):
        launcher.spawn()
    assert launcher.events == ["spawn", "stop"]
    assert list((launcher.runtime.installation / "windows-jobs").iterdir()) == []


def test_parent_does_not_acknowledge_guardian_before_worker_creation(launcher):
    launcher.scenario["started"] = False
    with pytest.raises(TimeoutError, match="handoff timed out"):
        launcher.spawn()
    assert launcher.events == ["spawn", "stop"]


@pytest.mark.parametrize("created", [False, True])
def test_constructor_failure_cleans_only_the_owned_job(launcher, monkeypatch, created):
    from bubo import update_windows_jobs

    recovered = []
    monkeypatch.setattr(update_windows_jobs, "recover_job", recovered.append)

    def failed(*args, **kwargs):
        if created:
            kwargs["on_job_created"]()
        raise OSError("guardian constructor failed")

    monkeypatch.setattr(worker, "JobProcess", failed)
    with pytest.raises(OSError, match="constructor failed"):
        launcher.spawn()
    assert len(recovered) == int(created)
    assert all(stream.closed for stream in launcher.runtime.held)
    assert list((launcher.runtime.installation / "windows-jobs").iterdir()) == []


@pytest.mark.parametrize("failure", [False, True])
def test_registered_windows_poller_uses_guardian_and_closes_log(tmp_path, monkeypatch, failure):
    from bubo import paths, poller

    monkeypatch.setattr(poller.sys, "platform", "win32")
    monkeypatch.setattr(paths, "LOGS", tmp_path)
    monkeypatch.setattr(paths, "DB", tmp_path / "bubo.db")
    monkeypatch.setenv("BUBO_WORKER_COMMAND", 'custom-worker --setting "two words"')
    (tmp_path / "update-registration.json").write_text(json.dumps({"installation": str(tmp_path)}))
    monkeypatch.setattr(
        poller.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Unprotected worker")
    )
    logs = []

    def guarded(args, installation, state, *, stdout):
        assert args == ["custom-worker", "--setting", "two words", "--worker", "review.json"]
        assert installation == state == tmp_path
        assert not stdout.closed
        logs.append(stdout)
        if failure:
            raise OSError("handoff failed")
        return 42

    monkeypatch.setattr(worker, "spawn", guarded)
    if failure:
        with pytest.raises(OSError, match="handoff failed"):
            poller.fork_worker(worker.Path("review.json"))
    else:
        assert poller.fork_worker(worker.Path("review.json")) == 42
    assert len(logs) == 1
    assert logs[0].closed


def test_broker_launch_uses_resident_version_environment_and_directory(launcher, monkeypatch):
    from bubo import update_admission

    def stale_parent(*args):
        pytest.fail("retained supervisor version must not replace resident version")

    monkeypatch.setattr(update_admission, "_check_generation", stale_parent)
    version = launcher.runtime.payload["generation"]["version"]
    assert launcher.spawn(
        process_version=version, env={"RESIDENT_CONFIG": "value"}, cwd="/resident"
    )
    assert launcher.scenario["env"]["RESIDENT_CONFIG"] == "value"
    assert launcher.scenario["cwd"] == "/resident"
    handoffs = [key for key in launcher.scenario["env"] if key.startswith("BUBO_WORKER_HANDOFF_")]
    assert len(handoffs) == 1
    assert set(launcher.scenario["env"]) == {"RESIDENT_CONFIG", handoffs[0]}


def test_broker_launch_rejects_stale_resident_before_native_creation(launcher):
    with pytest.raises(OSError, match="resident version changed"):
        launcher.spawn(process_version="0.0.0-stale")
    assert not launcher.launches
    assert all(stream.closed for stream in launcher.runtime.held)


def test_requeued_review_reuses_guardian_without_executing_again(launcher):
    args = ["worker", "--worker", "/reviews/project-42-sha.json"]
    first = launcher.spawn(worker_args=args)
    assert launcher.spawn(worker_args=args) == first
    assert len(launcher.launches) == 1
    receipt = launcher.launches[0]["parent"]
    assert len(receipt["review_key"]) == 64
    assert "project" not in receipt["review_key"]
    assert all(stream.closed for stream in launcher.runtime.held)


def test_different_review_is_not_suppressed_by_live_guardian(launcher):
    launcher.spawn(worker_args=["worker", "--worker", "/reviews/project-42-sha.json"])
    launcher.spawn(worker_args=["worker", "--worker", "/reviews/project-43-sha.json"])
    assert len(launcher.launches) == 2
    assert (
        launcher.launches[0]["parent"]["review_key"] != launcher.launches[1]["parent"]["review_key"]
    )


def test_poll_queue_ttl_after_lost_ack_does_not_duplicate_guardian(launcher, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from bubo import paths, poller
    from bubo.review_config import ReviewConfig
    from bubo.signals import reset_for_tests
    from bubo.update_windows_broker import LaunchOutcomeUnknown

    reset_for_tests()
    monkeypatch.setattr(paths, "DB", tmp_path / "poll.sqlite")
    monkeypatch.setattr(paths, "JOBS", tmp_path / "jobs")
    cfg = ReviewConfig(projects=["owner/repo"])
    change = {"number": 7, "sha": "abcdef123456"}
    provider = SimpleNamespace(
        name="gitlab",
        token=lambda: "test-token",
        list_open_changes=lambda *args: [change],
        change_number=lambda change: change["number"],
        is_ready=lambda *args: True,
    )
    monkeypatch.setattr(poller, "read_config", lambda: cfg)
    monkeypatch.setattr(poller, "get_provider", lambda cfg: provider)
    monkeypatch.setattr(poller, "subscription_gate", lambda cfg: (True, "closed"))
    monkeypatch.setattr(poller, "count_inflight_workers", lambda: 0)
    monkeypatch.setattr(poller, "sha_for", lambda change: change["sha"])
    monkeypatch.setattr(poller.analytics, "record_session_start", lambda *args, **kwargs: None)
    monkeypatch.setattr(poller.analytics, "flush", lambda: None)
    lost = True

    def launch(job):
        nonlocal lost
        pid = launcher.spawn(worker_args=["worker", "--worker", str(job)])
        if lost:
            lost = False
            raise LaunchOutcomeUnknown("reply lost after guardian commitment")
        return pid

    monkeypatch.setattr(poller, "fork_worker", launch)
    try:
        with pytest.raises(LaunchOutcomeUnknown, match="reply lost"):
            poller.poll()
        assert len(launcher.launches) == 1
        with poller.connect_db() as db:
            db.execute("update reviewed_mrs set updated_at = '2020-01-01T00:00:00+00:00'")
        assert not poller.already_seen("owner/repo", 7, change["sha"], queued_ttl_seconds=1)
        job = next(paths.JOBS.glob("*.json"))
        original_payload = json.loads(job.read_text())
        observed = []
        dump = json.dump

        def paused_dump(data, stream, **kwargs):
            if isinstance(data, dict) and "queued_at" in data:
                encoded = json.dumps(data, **kwargs)
                midpoint = len(encoded) // 2
                stream.write(encoded[:midpoint])
                stream.flush()
                # Original worker wakes before the retry has finished writing.
                observed.append(json.loads(job.read_text()))
                stream.write(encoded[midpoint:])
            else:
                dump(data, stream, **kwargs)

        monkeypatch.setattr(json, "dump", paused_dump)
        assert poller.poll() == 1
        assert observed == [original_payload]
        assert json.loads(job.read_text())["mr"] == change
        assert len(launcher.launches) == 1
    finally:
        reset_for_tests()


def test_interrupted_job_republication_preserves_complete_original(tmp_path, monkeypatch):
    from bubo import paths, poller

    monkeypatch.setattr(paths, "JOBS", tmp_path)
    monkeypatch.setattr(poller, "change_number_of", lambda change: 7)
    monkeypatch.setattr(poller, "sha_for", lambda change: "abcdef123456")
    original = {"number": 7, "title": "original"}
    path = poller.write_job("owner/repo", original)
    saved = path.read_bytes()

    def fail_dump(data, stream, **kwargs):
        stream.write('{"partial":')
        stream.flush()
        assert path.read_bytes() == saved
        raise OSError("publication interrupted")

    monkeypatch.setattr(json, "dump", fail_dump)
    with pytest.raises(OSError, match="publication interrupted"):
        poller.write_job("owner/repo", {"number": 7, "title": "retry"})
    assert path.read_bytes() == saved
    assert json.loads(path.read_text())["mr"] == original
    assert list(tmp_path.iterdir()) == [path]


def test_detached_worker_does_not_inherit_http_control_channel(launcher):
    launcher.spawn(env={"BUBO_RESIDENT_CONTROL_STDIN": "1", "APP_CONFIG": "value"})
    assert "BUBO_RESIDENT_CONTROL_STDIN" not in launcher.scenario["env"]
    assert launcher.scenario["env"]["APP_CONFIG"] == "value"
