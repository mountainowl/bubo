from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from bubo.analytics_config import AnalyticsConfig
from bubo.update_admission import activity
from bubo.update_reporting import FailureStore
from bubo.updater import Release, UpdateEngine


class FileInstaller:
    """Real disposable files/SQLite; not evidence for production package adapters."""

    kind = "pip"

    def __init__(self, installation, fail=None):
        self.installation = installation
        self.fail = fail
        self.version_file = installation / "runtime-version"

    def stage(self, release, destination):
        if self.fail == "stage":
            raise OSError("secret /private/staging/path")
        (destination / "staged").write_text(release.version)

    def snapshot(self, destination):
        shutil.copy2(self.version_file, destination / "old-version")

    def activate(self, destination):
        shutil.copy2(destination / "staged", self.version_file)
        if self.fail == "crash":
            os._exit(9)
        if self.fail == "install":
            raise OSError("installer failed after mutation")

    def restore(self, destination):
        if self.fail == "restore":
            raise OSError("recovery failed")
        shutil.copy2(destination / "old-version", self.version_file)

    def migrate_and_check(self, states, token):
        for state in states:
            if self.version_file.read_text() == "0.28.0":
                with sqlite3.connect(state / "reviewer.sqlite") as db:
                    db.execute("CREATE TABLE IF NOT EXISTS new_schema (id INTEGER)")
                if self.fail in {"migration", "restore", "writers", "identity"}:
                    if self.fail == "identity":
                        (state / "install_id").write_text(uuid.uuid4().hex)
                    raise OSError("migration failed after mutation")

    def writers_quiescent(self):
        return self.fail != "writers"

    def generation_version(self, version):
        return version

    def maintenance(self):
        from contextlib import nullcontext

        return nullcontext()


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setattr("bubo.update_admission._PROCESS_VERSION", "0.27.0")
    install = tmp_path / "install"
    state = tmp_path / "state"
    install.mkdir()
    state.mkdir()
    for name in ("registry.lock", "activity.lock", "attempt.lock"):
        (install / name).touch()
    (install / "runtime-version").write_text("0.27.0")
    (state / "update-activity.lock").touch()
    (state / "update-registration.json").write_text(json.dumps({"installation": str(install)}))
    (state / "install_id").write_text(uuid.uuid4().hex)
    with sqlite3.connect(state / "reviewer.sqlite") as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE reviews (body TEXT)")
        db.execute("INSERT INTO reviews VALUES ('original private data')")
    return install, state


def engine(installation, *, fail=None, enabled=True):
    install, state = installation
    return UpdateEngine(
        install,
        [state],
        FileInstaller(install, fail),
        source_version="0.27.0",
        producer_version="0.27.0",
        analytics=AnalyticsConfig(enabled=False),
        enabled=lambda _: enabled,
    )


RELEASE = Release("0.28.0", "fixture", "0" * 64)


def assert_original_rows(state):
    with sqlite3.connect(state / "reviewer.sqlite") as db:
        assert db.execute("SELECT * FROM reviews").fetchall() == [("original private data",)]


def test_success_migrates_and_preserves_identity_and_data(installation, monkeypatch):
    install, state = installation
    identity = (state / "install_id").read_bytes()
    result = engine(installation).run(RELEASE)
    assert result.status == "updated"
    generation = json.loads((install / "generation.json").read_text())
    journal = json.loads((install / "attempt.json").read_text())
    assert generation["activation"] == {
        "attempt_id": journal["attempt_id"],
        "token": journal["token"],
        "outcome": "committed",
        "states": [str(state)],
    }
    assert (install / "runtime-version").read_text() == "0.28.0"
    assert (state / "install_id").read_bytes() == identity
    assert_original_rows(state)
    assert not (install / "maintenance.json").exists()
    from bubo import update_admission

    with pytest.raises(update_admission.MaintenanceInProgress, match="restart"), activity(state):
        pytest.fail("Old resident admitted against new schema")
    monkeypatch.setattr(update_admission, "_PROCESS_VERSION", "0.28.0")
    with activity(state):
        pass


@pytest.mark.parametrize("phase", ["install", "migration"])
def test_failure_restores_matched_code_and_db_preserving_report(installation, phase):
    install, state = installation
    identity = (state / "install_id").read_bytes()
    result = engine(installation, fail=phase).run(RELEASE)
    assert result.status == "failed"
    assert result.code == "restored"
    generation = json.loads((install / "generation.json").read_text())
    journal = json.loads((install / "attempt.json").read_text())
    assert generation["activation"] == {
        "attempt_id": journal["attempt_id"],
        "token": journal["token"],
        "outcome": "restored",
        "states": [str(state)],
    }
    assert (install / "runtime-version").read_text() == "0.27.0"
    assert (state / "install_id").read_bytes() == identity
    assert_original_rows(state)
    with sqlite3.connect(state / "reviewer.sqlite") as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='new_schema'").fetchall()
    assert FailureStore(state).status()
    assert FailureStore(state).status()[0]["recovery_status"] == "restored"
    assert not (install / "maintenance.json").exists()


def test_later_staging_failure_preserves_active_generation_receipt(installation):
    install, _ = installation
    assert engine(installation).run(RELEASE).status == "updated"
    before = (install / "generation.json").read_bytes()
    later = engine(installation, fail="stage")
    later.source_version = RELEASE.version
    assert later.run(Release("0.29.0", "fixture", "0" * 64)).status == "failed"
    assert (install / "generation.json").read_bytes() == before
    assert json.loads((install / "attempt.json").read_text())["phase"] == "failed"


@pytest.mark.parametrize("phase", ["restore", "writers", "identity"])
def test_unprovable_recovery_stays_fenced(installation, phase):
    install, state = installation
    result = engine(installation, fail=phase).run(RELEASE)
    assert result.code == "recovery_blocked"
    assert (install / "maintenance.json").exists()
    assert FailureStore(state).status()[-1]["recovery_status"] == "blocked"


def test_disabled_has_no_staging_but_explicit_manual_works(installation):
    _install, state = installation
    updater = engine(installation, enabled=False)
    assert updater.run(RELEASE).code == "disabled"
    assert not updater.journal_path.exists()
    assert updater.run(RELEASE, trigger="manual").status == "updated"
    assert updater.enabled(state) is False


def test_busy_update_does_not_fence_work(installation):
    install, state = installation
    with activity(state):
        result = engine(installation).run(RELEASE)
        assert result.code == "busy"
        assert not (install / "maintenance.json").exists()
        with activity(state):
            pass


def test_terminated_process_recovers_before_admission(installation):
    install, state = installation
    script = """
import sys
from pathlib import Path
sys.path.insert(0, 'tests')
from test_updater import engine, RELEASE
engine((Path(sys.argv[1]),Path(sys.argv[2])), fail='crash').run(RELEASE)
"""
    result = subprocess.run([sys.executable, "-c", script, str(install), str(state)], check=False)
    assert result.returncode == 9
    assert (install / "runtime-version").read_text() == "0.28.0"
    assert (install / "maintenance.json").exists()
    recovered = engine(installation).recover()
    assert recovered.code == "restored"
    assert (install / "runtime-version").read_text() == "0.27.0"
    assert_original_rows(state)
    assert "interrupted" in FailureStore(state).report()


def test_shared_root_optout_not_overridden_by_manual_command(installation, tmp_path):
    _install, state = installation
    other = tmp_path / "other"
    other.mkdir()
    updater = engine(installation)
    updater.states.append(other)
    updater.enabled = lambda root: root != other
    assert updater.run(RELEASE, trigger="manual", initiating_state=state).code == "disabled"
    assert not updater.journal_path.exists()


def test_recovery_proceeds_when_diagnostic_store_fails(installation, monkeypatch, capsys):
    def disk_full(*args, **kwargs):
        raise OSError("secret diagnostic path")

    monkeypatch.setattr(FailureStore, "record", disk_full)
    result = engine(installation, fail="migration").run(RELEASE)
    assert result.code == "restored"
    assert (installation[0] / "runtime-version").read_text() == "0.27.0"
    assert_original_rows(installation[1])
    assert "secret" not in capsys.readouterr().err


def test_attempt_lock_rechecks_recovery_before_overwriting_journal(installation, monkeypatch):
    from contextlib import contextmanager

    from bubo import update_admission

    install, _ = installation
    lock = update_admission._file_lock
    prior = '{"phase":"migration","important":"recovery-intent"}'

    @contextmanager
    def interrupted_competitor(path, **kwargs):
        with lock(path, **kwargs) as stream:
            if path.name == "attempt.lock":
                (install / "attempt.json").write_text(prior)
                (install / "maintenance.json").write_text("{}")
            yield stream

    monkeypatch.setattr(update_admission, "_file_lock", interrupted_competitor)
    assert engine(installation).run(RELEASE).code == "recovery_required"
    assert (install / "attempt.json").read_text() == prior


def test_stale_request_cannot_repeat_committed_update(installation):
    stale = engine(installation)
    assert engine(installation).run(RELEASE).status == "updated"
    before = stale.journal_path.read_bytes()
    assert stale.run(RELEASE).status == "current"
    assert stale.run(Release("0.29.0", "fixture", "1" * 64)).code == "generation_changed"
    assert stale.journal_path.read_bytes() == before


def test_manual_failure_uses_initiating_root_policy_and_store(installation, tmp_path):
    _install, state = installation
    other = tmp_path / "z-other"
    other.mkdir()
    (other / "install_id").write_text(uuid.uuid4().hex)
    updater = engine(installation, fail="stage")
    updater.states.append(other)
    seen = []

    def policy(root):
        seen.append(root)
        return AnalyticsConfig(enabled=False), False

    updater.reporting_policy = policy
    result = updater.run(RELEASE, trigger="manual", initiating_state=other)
    assert result.status == "failed"
    assert seen == [other]
    assert FailureStore(other).status()
    assert not FailureStore(state).status()


@pytest.mark.parametrize("trigger", ["automatic", "manual"])
def test_conflicting_state_ownership_defers_even_manual_update(installation, trigger):
    install, state = installation
    (state / "update-registration-conflict.json").write_text('{"code":"shared_state"}')
    result = engine(installation).run(RELEASE, trigger=trigger)
    assert result.status == "deferred"
    assert (install / "runtime-version").read_text() == "0.27.0"
    assert not (install / "maintenance.json").exists()
    assert_original_rows(state)


class ResidentHarness:
    """Fault injection for engine ordering; not a platform resident supervisor."""

    def __init__(self, installation, *, reject_target=False, reject_restore=False, stop_ok=True):
        self.installation = installation
        self.reject_target = reject_target
        self.reject_restore = reject_restore
        self.stop_ok = stop_ok
        self.events = []

    def quiesce(self, journal):
        assert (self.installation / "maintenance.json").exists()
        assert journal["phase"] == "quiescing"
        assert not journal["mutated"]
        self.events.append("quiesce")

    def stop(self, journal):
        self.events.append("stop")
        return self.stop_ok

    def start(self, journal, *, outcome):
        assert (self.installation / "maintenance.json").exists()
        self.events.append("start:" + outcome)

    def verify(self, journal, *, outcome):
        self.events.append("verify:" + outcome)
        return not (self.reject_target if outcome == "committed" else self.reject_restore)


def test_resident_readiness_precedes_commit_and_admission(installation):
    update = engine(installation)
    update.residents = ResidentHarness(installation[0])
    result = update.run(RELEASE)
    assert result.status == "updated"
    assert update.residents.events == ["quiesce", "start:committed", "verify:committed"]
    journal = json.loads(update.journal_path.read_text())
    assert journal["residents_required"] is True
    assert journal["phase"] == "committed"
    assert not update.fence.exists()


@pytest.mark.parametrize("outcome", ["committed", "restored"])
def test_resident_finalizer_requires_durable_terminal_decision_and_survives_retry(
    installation, outcome
):
    class FinalizingResidents(ResidentHarness):
        fail = True

        def finalize(self, journal, *, outcome):
            durable = json.loads((self.installation / "attempt.json").read_text())
            assert durable["phase"] == outcome
            assert (self.installation / "maintenance.json").exists()
            self.events.append("finalize:" + outcome)
            if self.fail:
                raise OSError("injected finalization interruption")

    update = engine(installation)
    update.residents = FinalizingResidents(installation[0], reject_target=outcome == "restored")
    assert update.run(RELEASE).code == "finalization_failed"
    assert update.residents.events[-1] == "finalize:" + outcome
    assert json.loads(update.journal_path.read_text())["phase"] == outcome
    expected_version = "0.28.0" if outcome == "committed" else "0.27.0"
    assert (installation[0] / "runtime-version").read_text() == expected_version
    recovered = engine(installation)
    recovered.residents = ResidentHarness(installation[0])
    assert recovered.recover().code == "resident_recovery_blocked"
    assert recovered.fence.exists()
    recovered.residents = FinalizingResidents(installation[0])
    recovered.residents.fail = False
    assert recovered.recover().status == ("updated" if outcome == "committed" else "restored")
    assert recovered.residents.events[-1] == "finalize:" + outcome
    assert not recovered.fence.exists()
    assert (installation[0] / "runtime-version").read_text() == expected_version


def test_failed_resident_requires_stop_restore_and_restored_readiness(installation):
    update = engine(installation)
    update.residents = ResidentHarness(installation[0], reject_target=True)
    result = update.run(RELEASE)
    assert result.code == "restored"
    assert update.residents.events == [
        "quiesce",
        "start:committed",
        "verify:committed",
        "stop",
        "start:restored",
        "verify:restored",
    ]
    assert (installation[0] / "runtime-version").read_text() == "0.27.0"
    assert_original_rows(installation[1])
    assert not update.fence.exists()


def test_unproven_resident_stop_prevents_code_and_db_restore(installation):
    update = engine(installation)
    update.residents = ResidentHarness(installation[0], reject_target=True, stop_ok=False)
    result = update.run(RELEASE)
    assert result.code == "recovery_blocked"
    assert update.fence.exists()
    assert (installation[0] / "runtime-version").read_text() == "0.28.0"
    with sqlite3.connect(installation[1] / "reviewer.sqlite") as database:
        assert database.execute("SELECT name FROM sqlite_master WHERE name='new_schema'").fetchone()
    assert update.residents.events[-1] == "stop"


def test_missing_controller_cannot_recover_resident_transaction(installation):
    update = engine(installation)
    update.residents = ResidentHarness(installation[0], reject_target=True, stop_ok=False)
    assert update.run(RELEASE).code == "recovery_blocked"
    restarted = engine(installation)
    assert restarted.recover().code == "recovery_blocked"
    assert restarted.fence.exists()
    assert (installation[0] / "runtime-version").read_text() == "0.28.0"


def test_failed_restored_resident_keeps_admission_closed(installation):
    update = engine(installation)
    update.residents = ResidentHarness(installation[0], reject_target=True, reject_restore=True)
    assert update.run(RELEASE).code == "recovery_blocked"
    assert update.fence.exists()
    assert (installation[0] / "runtime-version").read_text() == "0.27.0"


def test_commit_finalization_failure_never_rolls_back_committed_runtime(installation, monkeypatch):
    update = engine(installation)
    update.residents = ResidentHarness(installation[0])
    monkeypatch.setattr(update, "_reopen", lambda: (_ for _ in ()).throw(OSError("injected")))
    assert update.run(RELEASE).code == "finalization_failed"
    assert update.fence.exists()
    assert (installation[0] / "runtime-version").read_text() == "0.28.0"
    assert json.loads(update.journal_path.read_text())["phase"] == "committed"
    restarted = engine(installation)
    assert restarted.recover().code == "resident_recovery_blocked"
    assert json.loads(update.journal_path.read_text())["phase"] == "committed"
    restarted.residents = ResidentHarness(installation[0])
    assert restarted.recover().status == "updated"
    assert restarted.residents.events == ["stop", "start:committed", "verify:committed"]
    assert (installation[0] / "runtime-version").read_text() == "0.28.0"
    assert not restarted.fence.exists()


def test_retrying_finalization_failure_preserves_durable_commit(installation, monkeypatch):
    update = engine(installation)
    monkeypatch.setattr(update, "_reopen", lambda: (_ for _ in ()).throw(OSError("injected")))
    assert update.run(RELEASE).code == "finalization_failed"
    assert update.recover().code == "finalization_failed"
    assert json.loads(update.journal_path.read_text())["phase"] == "committed"
    assert (installation[0] / "runtime-version").read_text() == "0.28.0"
    assert update.fence.exists()


@pytest.mark.parametrize(
    ("fail", "phase", "status"),
    [(None, "committed", "updated"), ("install", "restored", "restored")],
)
def test_terminal_recovery_resolves_local_report(installation, monkeypatch, fail, phase, status):
    update = engine(installation, fail=fail)
    monkeypatch.setattr(update, "_reopen", lambda: (_ for _ in ()).throw(OSError("injected")))
    assert update.run(RELEASE).code == "finalization_failed"
    store = FailureStore(installation[1])
    assert any(row["recovery_status"] == "blocked" for row in store.status())
    with sqlite3.connect(store.path) as db:
        payloads = db.execute(
            "SELECT failure_id,payload FROM failures ORDER BY failure_id"
        ).fetchall()
    assert engine(installation).recover().status == status
    assert all(row["recovery_status"] == phase for row in store.status())
    assert f"recovery_status: {phase}" in store.report()
    with sqlite3.connect(store.path) as db:
        assert (
            db.execute("SELECT failure_id,payload FROM failures ORDER BY failure_id").fetchall()
            == payloads
        )


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("keep_fence", [False, True])
@pytest.mark.parametrize("busy", [False, True])
def test_context_exit_failure_cannot_overwrite_terminal_decision(
    installation, monkeypatch, restored, keep_fence, busy
):
    from contextlib import contextmanager

    from bubo.update_admission import UpdateBusy

    update = engine(installation)
    update.residents = ResidentHarness(installation[0], reject_target=restored)

    @contextmanager
    def fail_on_exit():
        yield
        raise UpdateBusy() if busy else OSError("injected teardown failure")

    monkeypatch.setattr(update.installer, "maintenance", fail_on_exit)
    if keep_fence:
        monkeypatch.setattr(update, "_reopen", lambda: None)
    result = update.run(RELEASE)
    assert result.code == "finalization_failed"
    expected = "restored" if restored else "committed"
    assert json.loads(update.journal_path.read_text())["phase"] == expected
    assert (installation[0] / "runtime-version").read_text() == ("0.27.0" if restored else "0.28.0")
    assert update.fence.exists() == keep_fence


def test_persisted_commit_wins_over_in_memory_phase(installation):
    update = engine(installation)
    assert update.run(RELEASE).status == "updated"
    journal = json.loads(update.journal_path.read_text())
    journal["phase"] = "readiness"
    result = update._failed(journal, Path(journal["work"]))
    assert result.code == "finalization_failed"
    assert json.loads(update.journal_path.read_text())["phase"] == "committed"
    assert (installation[0] / "runtime-version").read_text() == "0.28.0"


@pytest.mark.parametrize("kill_candidate", [False, True])
def test_engine_gates_real_child_and_reaps_before_restoration(installation, kill_candidate):
    """Exercise engine/receipt/gate composition, not native supervisor discovery."""
    from bubo.update_readiness import ResidentReadiness

    install, state = installation
    update = engine(installation)
    identity = (state / "install_id").read_bytes()
    children = []
    released = install / "resident-released"
    # FileInstaller models code generations; use its version in the child too.
    script = """
import sys, time
from pathlib import Path
from bubo import update_admission, update_readiness
state, runtime, released = map(Path, sys.argv[1:])
version = runtime.read_text()
update_readiness.installed_version = lambda: version
update_admission._PROCESS_VERSION = version
gate = update_readiness.ResidentStartup.from_environment('service', state)
gate.signal_ready()
gate.wait()
released.write_text(version)
time.sleep(60)
"""

    class Controller:
        receipt = None

        def quiesce(self, journal):
            assert update.fence.exists()

        def stop(self, journal):
            for child in children:
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
            return all(child.poll() is not None for child in children)

        def start(self, journal, *, outcome):
            self.receipt = ResidentReadiness(state, "service", outcome=outcome)
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(state),
                    str(install / "runtime-version"),
                    str(released),
                ],
                env=self.receipt.environment(dict(os.environ)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            children.append(child)
            self.receipt.bind(child)
            self.receipt.wait(timeout=10)
            assert not released.exists(), "Candidate performed work before commit"
            if outcome == "committed" and kill_candidate:
                child.kill()
                child.wait(timeout=5)

        def verify(self, journal, *, outcome):
            assert not released.exists()
            return self.receipt.verify()

    controller = Controller()
    update.residents = controller
    restore = update.installer.restore

    def checked_restore(work):
        assert children
        assert all(child.poll() is not None for child in children)
        restore(work)

    update.installer.restore = checked_restore
    try:
        result = update.run(RELEASE)
        expected = "0.27.0" if kill_candidate else "0.28.0"
        assert result.status == ("failed" if kill_candidate else "updated")
        if kill_candidate:
            assert result.code == "restored"
        import time

        deadline = time.monotonic() + 5
        while not released.exists() and time.monotonic() < deadline:
            assert children[-1].poll() is None
            time.sleep(0.01)
        assert released.read_text() == expected
        assert (install / "runtime-version").read_text() == expected
        assert (state / "install_id").read_bytes() == identity
        assert_original_rows(state)
        assert not update.fence.exists()
    finally:
        controller.stop({})
        for child in children:
            child.communicate(timeout=5)


@pytest.mark.parametrize("fail", [None, "install"])
@pytest.mark.parametrize("parent_loss", [False, True])
def test_retained_parent_replaces_real_resident(installation, fail, parent_loss):
    import time

    def after_admission(operation):
        deadline = time.monotonic() + 10
        while True:
            result = operation()
            if result.status != "deferred" or result.code != "busy":
                return result
            assert time.monotonic() < deadline, result
            time.sleep(0.02)

    from bubo.update_helper import _bundle
    from bubo.update_identity import atomic_json
    from bubo.update_supervisor import COMMAND_ENV, ResidentController

    install, state = installation
    initial = install / "initial-resident"
    released = install / "replacement-resident"
    secret = "fixture-only-env-credential-never-persist"
    child_script = """
import os, sys, time
from pathlib import Path
from bubo import update_readiness, update_admission
state, runtime, initial, released = map(Path, sys.argv[1:])
assert os.environ.get('BUBO_TEST_RESIDENT_CREDENTIAL')
version = runtime.read_text()
update_readiness.installed_version = lambda: version
update_admission._PROCESS_VERSION = version
gate = update_readiness.ResidentStartup.from_environment('service', state)
if gate:
    gate.signal_ready()
    gate.wait()
    released.write_text(version)
else:
    initial.write_text(str(os.getpid()))
time.sleep(60)
"""
    update = engine(installation, fail=fail)
    update.residents = ResidentController(install, timeout=10)
    command = [
        sys.executable,
        "-c",
        child_script,
        str(state),
        str(install / "runtime-version"),
        str(initial),
        str(released),
    ]
    archive = _bundle(install, Path(sys._base_executable))
    atomic_json(
        install / "installation.json",
        {
            "states": [str(state)],
            "protocol_version": 1,
            "owner": os.getuid(),
        },
    )
    parent = subprocess.Popen(
        [
            sys._base_executable,
            "-I",
            archive,
            "--installation",
            str(install),
            "supervise",
            "service",
        ],
        env={
            **os.environ,
            "BUBO_TEST_RESIDENT_CREDENTIAL": secret,
            COMMAND_ENV: json.dumps(command),
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not initial.exists() and time.monotonic() < deadline:
            assert parent.poll() is None
            time.sleep(0.02)
        assert initial.exists()
        old_pid = int(initial.read_text())
        identity = (state / "install_id").read_bytes()
        if parent_loss:
            activate = update.installer.activate

            def lose_parent(destination):
                parent.kill()
                parent.communicate(timeout=5)
                try:
                    activate(destination)
                finally:
                    raise OSError("injected parent loss after code mutation")

            update.installer.activate = lose_parent
        result = after_admission(lambda: update.run(RELEASE))
        if parent_loss:
            assert result.code == "service_restart_required"
            assert update.fence.exists()
            parent = subprocess.Popen(
                parent.args,
                env={
                    **os.environ,
                    "BUBO_TEST_RESIDENT_CREDENTIAL": secret,
                    COMMAND_ENV: json.dumps(command),
                },
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            owner_path = next((install / "supervisors").glob("*/owner.json"))
            deadline = time.monotonic() + 10
            while (
                json.loads(owner_path.read_text())["pid"] != parent.pid
                and time.monotonic() < deadline
            ):
                assert parent.poll() is None
                time.sleep(0.02)
            assert json.loads(owner_path.read_text())["pid"] == parent.pid
            # Publishing the adopted parent precedes release of its admission
            # locks. A scheduler may safely retry while that handoff completes.
            result = after_admission(update.recover)
            fail = "parent_loss"
        assert result.status == ("failed" if fail else "updated")
        assert result.code == ("restored" if fail else None)
        deadline = time.monotonic() + 10
        while not released.exists() and time.monotonic() < deadline:
            assert parent.poll() is None
            time.sleep(0.02)
        assert released.read_text() == ("0.27.0" if fail else "0.28.0")
        owner = json.loads(next((install / "supervisors").glob("*/owner.json")).read_text())
        assert owner["child"]["pid"] != old_pid
        assert parent.poll() is None
        assert_original_rows(state)
        assert (state / "install_id").read_bytes() == identity
        for path in (install / "supervisors").rglob("*.json"):
            assert secret not in path.read_text()
        assert secret not in update.journal_path.read_text()
    finally:
        parent.terminate()
        _, error = parent.communicate(timeout=15)
        assert parent.returncode == 128 + 15, error.decode()


def test_retained_stdio_parent_probes_privately_then_requires_reconnect(installation):
    import select

    from bubo.update_helper import _bundle
    from bubo.update_identity import atomic_json
    from bubo.update_supervisor import COMMAND_ENV, ResidentController

    install, state = installation
    root = state.parent / "root"
    (root / "config").mkdir(parents=True)
    (root / "config/env.toml").write_text("auto_update = false\n[analytics]\nenabled = false\n")
    script = """
import sys
from pathlib import Path
from bubo import update_readiness, update_admission
version = Path(sys.argv[1]).read_text()
update_readiness.installed_version = lambda: version
update_admission._PROCESS_VERSION = version
from bubo.mcp_server import main
main()
"""
    command = [sys.executable, "-c", script, str(install / "runtime-version")]
    archive = _bundle(install, Path(sys._base_executable))
    atomic_json(
        install / "installation.json",
        {
            "states": [str(state)],
            "protocol_version": 1,
            "owner": os.getuid(),
        },
    )
    parent = subprocess.Popen(
        [
            sys._base_executable,
            "-I",
            archive,
            "--installation",
            str(install),
            "supervise",
            "mcp-stdio",
        ],
        env={
            **os.environ,
            COMMAND_ENV: json.dumps(command),
            "BUBO_ROOT": str(root),
            "BUBO_BASE_DIR": str(state.parent),
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1"},
            },
        }
        parent.stdin.write((json.dumps(request) + "\n").encode())
        parent.stdin.flush()
        assert select.select([parent.stdout], [], [], 10)[0]
        assert json.loads(parent.stdout.readline())["id"] == 1
        update = engine(installation)
        update.residents = ResidentController(install, timeout=10)
        assert update.run(RELEASE).status == "updated"
        # Keep the original client input open: the parent must still exit, and
        # replacement initialization must never appear on the original session.
        assert parent.wait(timeout=10) == 75
        assert parent.stdout.read() == b""
        assert not update.fence.exists()
        owner = json.loads(next((install / "supervisors").glob("*/owner.json")).read_text())
        assert owner["closed"] is True
        assert_original_rows(state)
    finally:
        if parent.poll() is None:
            parent.terminate()
        parent.communicate(timeout=15)


@pytest.mark.parametrize("kind", ["service", "mcp-http"])
@pytest.mark.parametrize("fail", [None, "install"])
def test_retained_actual_service_and_http_replacement(installation, kind, fail):
    """Use real transport/service lifecycle with fixture code activation and work."""
    import asyncio
    import socket
    import time

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from bubo.update_helper import _bundle
    from bubo.update_identity import atomic_json
    from bubo.update_supervisor import COMMAND_ENV, ResidentController

    install, state = installation
    root = state.parent / "root"
    (root / "config").mkdir(parents=True)
    (root / "config/env.toml").write_text(
        "auto_update = false\n[analytics]\nenabled = false\n[subscription_circuit]\nenabled = false\n"
    )
    marker = state / "poll-version"
    script = """
import sys
from pathlib import Path
from bubo import update_readiness, update_admission
version = Path(sys.argv[1]).read_text()
update_readiness.installed_version = lambda: version
update_admission._PROCESS_VERSION = version
if sys.argv[2] == 'service':
    from bubo import poller, service
    # Keep real service singleton/monitor/gate/signal handling. Substitute only
    # external polling work, preserving its actual admission boundary.
    poller.poll = update_admission.admitted(lambda: Path(sys.argv[3]).write_text(version))
    poller.sync_outcomes = lambda *_: None
    raise SystemExit(service.run_foreground('fixture-token'))
from bubo.mcp_server import main
main()
"""
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    command = [sys.executable, "-c", script, str(install / "runtime-version"), kind, str(marker)]
    archive = _bundle(install, Path(sys._base_executable))
    atomic_json(
        install / "installation.json",
        {
            "states": [str(state)],
            "protocol_version": 1,
            "owner": os.getuid(),
        },
    )
    parent = subprocess.Popen(
        [sys._base_executable, "-I", archive, "--installation", str(install), "supervise", kind],
        env={
            **os.environ,
            COMMAND_ENV: json.dumps(command),
            "BUBO_ROOT": str(root),
            "BUBO_BASE_DIR": str(state.parent),
            "BUBO_MCP_TRANSPORT": "http",
            "BUBO_MCP_HOST": "127.0.0.1",
            "BUBO_MCP_PORT": str(port),
            "BUBO_MCP_BEARER_TOKEN": "fixture-token",
        },
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    async def health():
        async with httpx.AsyncClient(
            headers={"Authorization": "Bearer fixture-token"}, timeout=5
        ) as http:
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (
                reader,
                writer,
                _,
            ):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    assert not (await client.call_tool("health")).isError

    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert parent.poll() is None
            if kind == "service" and marker.exists():
                break
            if kind == "mcp-http":
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    # The server is not listening yet; poll again.
                    pass
            time.sleep(0.02)
        if kind == "mcp-http":
            asyncio.run(health())
        else:
            assert marker.read_text() == "0.27.0"
        owner_path = next((install / "supervisors").glob("*/owner.json"))
        old_pid = json.loads(owner_path.read_text())["child"]["pid"]
        update = engine(installation, fail=fail)
        update.residents = ResidentController(install, timeout=10)
        result = update.run(RELEASE)
        assert result.status == ("failed" if fail else "updated")
        assert result.code == ("restored" if fail else None)
        assert json.loads(owner_path.read_text())["child"]["pid"] != old_pid
        if kind == "mcp-http":
            asyncio.run(health())
        else:
            expected = "0.27.0" if fail else "0.28.0"
            deadline = time.monotonic() + 5
            while marker.read_text() != expected and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.read_text() == expected
        assert parent.poll() is None
        assert_original_rows(state)
    finally:
        if parent.poll() is None:
            parent.terminate()
        _, errors = parent.communicate(timeout=15)
        assert parent.returncode == 143, errors.decode()


@pytest.mark.parametrize(
    ("kind", "code", "instruction"),
    [
        ("service", "service_restart_required", "Restart the same Bubo service"),
        ("mcp-http", "service_restart_required", "Restart the same Bubo service"),
        ("mcp-stdio", "mcp_reconnect_required", "Reconnect the same MCP client/server"),
    ],
)
def test_recovery_reports_fresh_invocation_once_and_resolves(installation, kind, code, instruction):
    from bubo.updater import ResidentRestartRequired

    class LostParent(ResidentHarness):
        missing = True

        def start(self, journal, *, outcome):
            if self.missing:
                raise ResidentRestartRequired(kind)
            super().start(journal, outcome=outcome)

    update = engine(installation, fail="install")
    update.residents = LostParent(installation[0])
    assert update.run(RELEASE).code == code
    assert update.recover().code == code
    store = FailureStore(installation[1])
    rows = [row for row in store.status() if row["error_code"] == code]
    assert len(rows) == 1
    assert rows[0]["recovery_status"] == "blocked"
    assert instruction in store.report()
    assert update.fence.exists()
    update.residents.missing = False
    assert update.recover().code == "restored"
    assert not update.fence.exists()
    assert all(row["recovery_status"] == "restored" for row in store.status())
    assert instruction not in store.report()
    assert "Recovery completed; no restart is required." in store.report()


def test_terminal_recovery_reports_restart_without_rolling_back(installation, monkeypatch):
    from bubo.updater import ResidentRestartRequired

    class MissingParent(ResidentHarness):
        def start(self, journal, *, outcome):
            raise ResidentRestartRequired("mcp-stdio")

    update = engine(installation)
    update.residents = ResidentHarness(installation[0])
    reopen = update._reopen
    monkeypatch.setattr(update, "_reopen", lambda: (_ for _ in ()).throw(OSError("interrupted")))
    assert update.run(RELEASE).code == "finalization_failed"
    assert json.loads(update.journal_path.read_text())["phase"] == "committed"
    monkeypatch.setattr(update, "_reopen", reopen)
    update.residents = MissingParent(installation[0])
    assert update.recover().code == "mcp_reconnect_required"
    assert json.loads(update.journal_path.read_text())["phase"] == "committed"
    assert (installation[0] / "runtime-version").read_text() == "0.28.0"
    update.residents = ResidentHarness(installation[0])
    assert update.recover().status == "updated"
    assert not update.fence.exists()
