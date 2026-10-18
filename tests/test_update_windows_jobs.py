"""Ownership state-machine checks; native job/reboot acceptance is separate."""

import json
import os
from types import SimpleNamespace

import pytest

from bubo import update_windows_jobs as jobs
from bubo.update_identity import atomic_json


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "current_owner", lambda: "S-1-test")
    monkeypatch.setattr(jobs, "windows_process_identity", lambda _pid: "owner-start")
    recovered = []
    monkeypatch.setattr(jobs, "recover_job", recovered.append)
    return tmp_path, recovered


def test_empty_registry_drain_is_read_only(tmp_path):
    jobs.drain(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_job_creation_is_durable_before_workload_and_close_retires(registry):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    initial = json.loads(lease.path.read_text())
    assert initial["job_created"] is False
    assert initial["owner_pid"] == os.getpid()
    assert set(initial) == {
        "protocol",
        "installation",
        "job",
        "owner_sid",
        "owner_pid",
        "owner_start",
        "job_created",
        "state",
    }
    lease.created()
    assert json.loads(lease.path.read_text())["job_created"] is True
    events = []
    lease.close(SimpleNamespace(name=lease.name, close=lambda: events.append("closed")))
    assert events == ["closed"]
    assert not lease.path.exists()
    jobs.drain(installation)
    assert not lease.path.exists()
    assert recovered == []


@pytest.mark.parametrize("created", [False, True])
def test_live_owner_defers_without_touching_job(registry, created):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    if created:
        lease.created()
    with pytest.raises(jobs.JobsBusy):
        jobs.drain(installation)
    assert lease.path.exists()
    assert recovered == []


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("identity", [None, "reused-pid-start"])
def test_dead_or_reused_owner_drains_only_confirmed_created_job(
    registry, monkeypatch, created, identity
):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    if created:
        lease.created()
    monkeypatch.setattr(jobs, "windows_process_identity", lambda _pid: identity)
    jobs.drain(installation)
    assert recovered == ([lease.name] if created else [])
    assert not lease.path.exists()


def test_uncertain_owner_liveness_preserves_job(registry, monkeypatch):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    lease.created()

    def denied(_pid):
        raise PermissionError("process access denied")

    monkeypatch.setattr(jobs, "windows_process_identity", denied)
    with pytest.raises(PermissionError):
        jobs.drain(installation)
    assert lease.path.exists()
    assert recovered == []


def test_construction_collision_does_not_terminate_existing_job(registry):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    lease.close()  # JobProcess never committed its creation callback.
    assert recovered == []
    jobs.drain(installation)
    assert not lease.path.exists()


def test_construction_failure_after_owned_creation_recovers_job(registry):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    lease.created()
    lease.close()
    assert recovered == [lease.name]
    assert not lease.path.exists()


def test_failed_child_quiescence_preserves_abandoned_record(registry):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    lease.created()

    def fail():
        raise TimeoutError("descendants remain")

    with pytest.raises(TimeoutError):
        lease.close(SimpleNamespace(name=lease.name, close=fail))
    assert json.loads(lease.path.read_text())["state"] == "abandoned"
    assert recovered == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol", 2),
        ("installation", "/other"),
        ("owner_sid", "foreign"),
        ("owner_pid", -1),
        ("owner_pid", True),
        ("owner_pid", 2**32),
        ("owner_start", ""),
        ("job_created", "true"),
        ("state", "unknown"),
        ("job", "foreign"),
    ],
)
def test_corrupt_ownership_never_authorizes_recovery(registry, monkeypatch, field, value):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    atomic_json(lease.path, {**lease.record, field: value})
    monkeypatch.setattr(jobs, "windows_process_identity", lambda _pid: None)
    with pytest.raises(OSError, match=r"Invalid Windows|identity changed"):
        jobs.drain(installation)
    assert lease.path.exists()
    assert recovered == []


def test_changed_receipt_prevents_owner_cleanup(registry):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    lease.created()
    atomic_json(lease.path, {**lease.record, "owner_start": "changed"})
    with pytest.raises(OSError, match="owner changed"):
        lease.close()
    assert recovered == []


def test_failed_recovery_keeps_receipt(registry, monkeypatch):
    installation, _recovered = registry
    lease = jobs.JobOwnership(installation)
    lease.created()
    monkeypatch.setattr(jobs, "windows_process_identity", lambda _pid: None)

    def fail(_name):
        raise TimeoutError("job still has members")

    monkeypatch.setattr(jobs, "recover_job", fail)
    with pytest.raises(TimeoutError):
        jobs.drain(installation)
    assert lease.path.exists()


def test_quiescent_receipt_after_lost_deletion_can_be_drained(registry):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    atomic_json(lease.path, {**lease.record, "state": "quiescent"})
    jobs.drain(installation)
    assert not lease.path.exists()
    assert recovered == []


def test_exclusive_admission_drains_after_all_locks_and_releases_on_busy(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from bubo import update_admission as admission

    events = []
    installation, state = tmp_path / "controller", tmp_path / "state"

    @contextmanager
    def lock(path, *, exclusive):
        assert exclusive
        events.append(("enter", path.name))
        try:
            yield
        finally:
            events.append(("exit", path.name))

    def busy(path):
        assert path == installation
        assert events == [
            ("enter", "registry.lock"),
            ("enter", "activity.lock"),
            ("enter", "update-activity.lock"),
        ]
        raise jobs.JobsBusy("owner active")

    monkeypatch.setattr(admission, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(admission, "_file_lock", lock)
    monkeypatch.setattr(jobs, "drain", busy)
    with pytest.raises(admission.UpdateBusy), admission.exclusive(installation, [state]):
        pytest.fail("exclusive admission must not open before drain")
    assert events[-3:] == [
        ("exit", "update-activity.lock"),
        ("exit", "activity.lock"),
        ("exit", "registry.lock"),
    ]


def test_crash_before_atomic_publication_leaves_non_authoritative_temporary(registry):
    import subprocess
    import sys
    import uuid

    installation, recovered = registry
    directory = installation / "windows-jobs"
    directory.mkdir()
    receipt = directory / (uuid.uuid4().hex + ".json")
    code = """
import os, sys
from pathlib import Path
from bubo.update_identity import atomic_json
os.replace = lambda *args: os._exit(71)
atomic_json(Path(sys.argv[1]), {'state': 'uncommitted'})
"""
    result = subprocess.run([sys.executable, "-c", code, str(receipt)], check=False, timeout=5)
    assert result.returncode == 71
    assert not receipt.exists()
    leftovers = list(directory.iterdir())
    assert len(leftovers) == 1
    assert leftovers[0].name.startswith("." + receipt.name + ".")
    jobs.drain(installation)
    assert recovered == []
    assert list(directory.iterdir()) == leftovers


def test_live_owner_abandoned_job_is_recovered_before_admission(registry):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    lease.created()

    def fail():
        raise TimeoutError("cleanup failed")

    with pytest.raises(TimeoutError):
        lease.close(SimpleNamespace(name=lease.name, close=fail))
    with pytest.raises(OSError, match="launch rights"):
        lease.created()
    jobs.drain(installation)  # Owner is still alive; abandonment surrendered control.
    assert recovered == [lease.name]
    assert not lease.path.exists()


def test_abandoned_uncreated_collision_never_authorizes_termination(registry):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    atomic_json(lease.path, {**lease.record, "state": "abandoned"})
    jobs.drain(installation)
    assert recovered == []
    assert not lease.path.exists()


@pytest.mark.parametrize("after_replace", [False, True])
def test_uncertain_abandonment_preserves_evidence_and_permanently_revokes_launch(
    registry, monkeypatch, after_replace
):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    lease.created()
    original = jobs.atomic_json

    def publication(path, record):
        if record["state"] == "abandoned":
            if after_replace:
                original(path, record)
            raise OSError("publication uncertain")
        original(path, record)

    def close_failure():
        raise TimeoutError("job still active")

    monkeypatch.setattr(jobs, "atomic_json", publication)
    with pytest.raises(OSError, match="restart the owning Bubo"):
        lease.close(SimpleNamespace(name=lease.name, close=close_failure))
    assert lease.path.exists()
    assert recovered == []
    with pytest.raises(OSError, match="launch rights"):
        lease.created()
    if after_replace:
        jobs.drain(installation)  # Re-read committed abandonment; never infer quiet.
        assert recovered == [lease.name]
    else:
        with pytest.raises(jobs.JobsBusy):
            jobs.drain(installation)
        assert recovered == []
        assert lease.path.exists()


def test_creation_callback_cannot_be_reused(registry):
    installation, _recovered = registry
    lease = jobs.JobOwnership(installation)
    lease.created()
    with pytest.raises(OSError, match="launch rights"):
        lease.created()


@pytest.mark.parametrize("after_replace", [False, True])
def test_uncertain_creation_callback_reconciles_only_its_exact_transition(
    registry, monkeypatch, after_replace
):
    installation, recovered = registry
    lease = jobs.JobOwnership(installation)
    original = jobs.atomic_json

    def publication(path, record):
        if record["job_created"] and record["state"] == "pending":
            if after_replace:
                original(path, record)
            raise OSError("creation publication uncertain")
        original(path, record)

    monkeypatch.setattr(jobs, "atomic_json", publication)
    with pytest.raises(OSError, match="creation publication uncertain"):
        lease.created()
    with pytest.raises(OSError, match="launch rights"):
        lease.created()
    lease.close()
    assert recovered == ([lease.name] if after_replace else [])
    assert not lease.path.exists()


class NativeExit(BaseException):
    pass


@pytest.fixture
def transfer(registry, monkeypatch):
    installation, recovered = registry
    parent_pid = os.getpid()
    child_pid = parent_pid + 1
    identities = {parent_pid: "parent-start", child_pid: "child-start"}
    monkeypatch.setattr(jobs, "windows_process_identity", identities.get)
    parent = jobs.JobOwnership(installation)
    parent.created()
    current = [parent_pid]
    monkeypatch.setattr(jobs.os, "getpid", lambda: current[0])

    def native_exit(status):
        raise NativeExit(status)

    guardian = SimpleNamespace(name=parent.name, alone=lambda: True, exit=native_exit)
    child = SimpleNamespace(name=parent.name, pid=child_pid, identity=lambda: "child-start")
    child.detach = lambda acknowledge: acknowledge(child)
    return SimpleNamespace(
        installation=installation,
        recovered=recovered,
        parent=parent,
        child=child,
        guardian=guardian,
        current=current,
        parent_pid=parent_pid,
        child_pid=child_pid,
        identities=identities,
    )


def adopt(transfer):
    transfer.current[0] = transfer.child_pid
    return jobs.JobOwnership.adopt(
        transfer.installation,
        transfer.parent.record,
        transfer.guardian,
    )


def test_guardian_adoption_transfers_exact_identity_and_revokes_launch(transfer):
    owner = adopt(transfer)
    assert owner.record == {
        **transfer.parent.record,
        "owner_pid": transfer.child_pid,
        "owner_start": "child-start",
    }
    assert json.loads(owner.path.read_text()) == owner.record
    with pytest.raises(OSError, match="launch rights"):
        owner.created()
    transfer.current[0] = transfer.parent_pid
    with pytest.raises(OSError, match="owner changed"):
        transfer.parent.close()
    assert transfer.recovered == []


@pytest.mark.parametrize(
    "problem", ["parent", "uncreated", "abandoned", "job", "members", "receipt"]
)
def test_invalid_guardian_adoption_leaves_receipt_unchanged(transfer, problem):
    transfer.current[0] = transfer.child_pid
    record = dict(transfer.parent.record)
    before = transfer.parent.path.read_bytes()
    if problem == "parent":
        transfer.current[0] = transfer.parent_pid
    elif problem == "uncreated":
        record["job_created"] = False
    elif problem == "abandoned":
        record["state"] = "abandoned"
    elif problem == "job":
        transfer.guardian.name = jobs.new_job_name()
    elif problem == "members":
        transfer.guardian.alone = lambda: False
    else:
        record["owner_start"] = "different-parent"
    with pytest.raises(OSError, match=r"cannot adopt|ownership changed"):
        jobs.JobOwnership.adopt(transfer.installation, record, transfer.guardian)
    assert transfer.parent.path.read_bytes() == before


def test_parent_first_handoff_is_cleaned_by_finishing_guardian(transfer):
    owner = adopt(transfer)
    transfer.current[0] = transfer.parent_pid
    transfer.parent.handoff(transfer.child)
    marker = owner.path.with_suffix(".ack")
    assert json.loads(marker.read_text()) == owner.record
    transfer.current[0] = transfer.child_pid
    with pytest.raises(NativeExit) as exit_info:
        owner.finish_guardian(transfer.guardian, 7)
    assert exit_info.value.args == (7,)
    assert not owner.path.exists()
    assert not marker.exists()


def test_fast_completion_retains_receipt_until_paused_parent_acknowledges(transfer):
    owner = adopt(transfer)
    with pytest.raises(NativeExit) as exit_info:
        owner.finish_guardian(transfer.guardian, 0)
    assert exit_info.value.args == (0,)
    assert json.loads(owner.path.read_text())["state"] == "quiescent"
    transfer.identities[transfer.child_pid] = (
        None  # Already exited; retained handle still identifies it.
    )
    transfer.current[0] = transfer.parent_pid
    transfer.parent.handoff(transfer.child)
    assert not owner.path.exists()
    assert not owner.path.with_suffix(".ack").exists()


@pytest.mark.parametrize("problem", ["job", "pid", "start", "abandoned"])
def test_parent_rejects_nonmatching_transferred_child(transfer, problem):
    owner = adopt(transfer)
    if problem == "job":
        transfer.child.name = jobs.new_job_name()
    elif problem == "pid":
        transfer.child.pid += 1
    elif problem == "start":
        transfer.child.identity = lambda: "reused-process"
    else:
        owner._publish(state="abandoned")
    transfer.current[0] = transfer.parent_pid
    with pytest.raises(OSError, match="not acknowledged"):
        transfer.parent.handoff(transfer.child)
    assert not owner.path.with_suffix(".ack").exists()


def test_surviving_descendants_are_abandoned_before_native_exit(transfer):
    owner = adopt(transfer)
    transfer.guardian.alone = lambda: False
    with pytest.raises(NativeExit) as exit_info:
        owner.finish_guardian(transfer.guardian, 0)
    assert exit_info.value.args == (1,)
    assert json.loads(owner.path.read_text())["state"] == "abandoned"
    jobs.drain(transfer.installation)
    assert transfer.recovered == [owner.name]


@pytest.mark.parametrize("after_replace", [False, True])
def test_uncertain_retirement_still_exits_and_retains_recovery_evidence(
    transfer, monkeypatch, after_replace
):
    owner = adopt(transfer)
    original = jobs.atomic_json

    def uncertain(path, record):
        if after_replace:
            original(path, record)
        raise OSError("uncertain retirement")

    monkeypatch.setattr(jobs, "atomic_json", uncertain)
    with pytest.raises(NativeExit) as exit_info:
        owner.finish_guardian(transfer.guardian, 0)
    assert exit_info.value.args == (1,)
    assert json.loads(owner.path.read_text())["state"] == (
        "quiescent" if after_replace else "pending"
    )
    transfer.identities[transfer.child_pid] = None
    jobs.drain(transfer.installation)
    assert transfer.recovered == ([] if after_replace else [owner.name])


@pytest.mark.parametrize("after_replace", [False, True])
def test_ack_publication_failure_never_turns_committed_handoff_into_retry(
    transfer, monkeypatch, after_replace
):
    owner = adopt(transfer)
    transfer.current[0] = transfer.parent_pid
    original = jobs.atomic_json

    def uncertain(path, record):
        if after_replace:
            original(path, record)
        raise OSError("uncertain acknowledgement")

    monkeypatch.setattr(jobs, "atomic_json", uncertain)
    transfer.parent.handoff(transfer.child)  # Must return success.
    monkeypatch.setattr(jobs, "atomic_json", original)
    transfer.current[0] = transfer.child_pid
    with pytest.raises(NativeExit):
        owner.finish_guardian(transfer.guardian, 0)
    jobs.drain(transfer.installation)
    assert list(owner.path.parent.iterdir()) == []
    assert transfer.recovered == []


def test_orphan_ack_marker_has_no_recovery_authority(transfer):
    marker = transfer.parent.path.with_suffix(".ack")
    marker.write_text("not an ownership receipt")
    transfer.identities[transfer.parent_pid] = None
    jobs.drain(transfer.installation)
    assert transfer.recovered == [transfer.parent.name]
    assert not marker.exists()


@pytest.mark.parametrize("after_replace", [False, True])
def test_uncertain_adoption_keeps_exact_parent_or_dead_guardian_authority(
    transfer, monkeypatch, after_replace
):
    original = jobs.atomic_json

    def uncertain(path, record):
        if after_replace:
            original(path, record)
        raise OSError("uncertain adoption")

    monkeypatch.setattr(jobs, "atomic_json", uncertain)
    with pytest.raises(OSError, match="uncertain adoption"):
        adopt(transfer)
    recorded = json.loads(transfer.parent.path.read_text())
    assert recorded["owner_pid"] == (transfer.child_pid if after_replace else transfer.parent_pid)
    # The runtime exits natively on either failure. The parent remains alive;
    # recovery must use whichever exact ownership publication actually happened.
    transfer.identities[transfer.child_pid] = None
    transfer.current[0] = transfer.parent_pid
    monkeypatch.setattr(jobs, "atomic_json", original)
    if after_replace:
        jobs.drain(transfer.installation)
    else:
        transfer.parent.close()
    assert transfer.recovered == [transfer.parent.name]
    assert not transfer.parent.path.exists()


@pytest.mark.parametrize("state", ["parent", "pending", "quiescent", "abandoned"])
def test_abort_handoff_reconciles_only_after_whole_job_stop(transfer, state):
    if state != "parent":
        owner = adopt(transfer)
        owner._publish(state=state)
    transfer.current[0] = transfer.parent_pid
    before = transfer.parent.path.read_bytes()
    stopped = []

    def close():
        assert transfer.parent.path.read_bytes() == before
        stopped.append(True)

    transfer.child.close = close
    transfer.parent.abort_handoff(transfer.child)
    assert stopped == [True]
    assert not transfer.parent.path.exists()
    assert transfer.recovered == []


def test_guardian_can_transfer_during_parent_stop_before_receipt_reconciliation(transfer):
    def close():
        adopt(transfer)
        transfer.current[0] = transfer.parent_pid

    transfer.child.close = close
    transfer.parent.abort_handoff(transfer.child)
    assert not transfer.parent.path.exists()


def test_failed_stop_does_not_race_guardian_with_receipt_writes(transfer):
    def failed_close():
        adopt(transfer)
        transfer.current[0] = transfer.parent_pid
        raise OSError("termination unavailable")

    transfer.child.close = failed_close
    with pytest.raises(OSError, match="handoff cleanup unresolved"):
        transfer.parent.abort_handoff(transfer.child)
    assert json.loads(transfer.parent.path.read_text()) == {
        **transfer.parent.record,
        "owner_pid": transfer.child_pid,
        "owner_start": "child-start",
    }


def test_unavailable_child_identity_still_stops_tree_without_fabricating_receipt(transfer):
    before = transfer.parent.path.read_bytes()
    stopped = []

    def unavailable():
        raise OSError("creation identity unavailable")

    transfer.child.identity = unavailable
    transfer.child.close = lambda: stopped.append(True)
    with pytest.raises(OSError, match="stopped handoff identity is unavailable"):
        transfer.parent.abort_handoff(transfer.child)
    assert stopped == [True]
    assert transfer.parent.path.read_bytes() == before


def test_abort_preserves_nonmatching_receipt_after_stopping_tree(transfer):
    owner = adopt(transfer)
    owner._publish(owner_start="unexpected")
    transfer.current[0] = transfer.parent_pid
    before = owner.path.read_bytes()
    stopped = []
    transfer.child.close = lambda: stopped.append(True)
    with pytest.raises(OSError, match="stopped handoff receipt changed"):
        transfer.parent.abort_handoff(transfer.child)
    assert stopped == [True]
    assert owner.path.read_bytes() == before


def worker_owner(transfer):
    transfer.parent.record.update(worker_started=False, worker_error=None)
    jobs.atomic_json(transfer.parent.path, transfer.parent.record)
    return adopt(transfer)


def test_actual_worker_creation_is_required_for_handoff(transfer):
    owner = worker_owner(transfer)
    transfer.current[0] = transfer.parent_pid
    assert not transfer.parent.worker_ready(transfer.child)
    with pytest.raises(OSError, match="not acknowledged"):
        transfer.parent.handoff(transfer.child)
    transfer.current[0] = transfer.child_pid
    owner.worker_started()
    with pytest.raises(OSError, match="startup owner changed"):
        owner.worker_started()
    transfer.current[0] = transfer.parent_pid
    assert transfer.parent.worker_ready(transfer.child)
    transfer.parent.handoff(transfer.child)


def test_startup_failure_receipt_contains_only_bounded_error_code(transfer):
    owner = worker_owner(transfer)
    with pytest.raises(ValueError, match="Unknown Windows worker launch failure"):
        owner.worker_failed("private path or exception text")
    owner.worker_failed("permission_denied")
    with pytest.raises(NativeExit):
        owner.finish_guardian(transfer.guardian, 1)
    transfer.current[0] = transfer.parent_pid
    with pytest.raises(PermissionError, match="permission denied"):
        transfer.parent.worker_ready(transfer.child)
    assert "private path or exception text" not in owner.path.read_text()
    transfer.child.close = lambda: None
    transfer.parent.abort_handoff(transfer.child)
    assert not owner.path.exists()


def test_start_acknowledgement_during_cancellation_is_reconciled_after_stop(transfer):
    owner = worker_owner(transfer)
    transfer.current[0] = transfer.parent_pid

    def stop():
        transfer.current[0] = transfer.child_pid
        owner.worker_started()
        transfer.current[0] = transfer.parent_pid

    transfer.child.close = stop
    transfer.parent.abort_handoff(transfer.child)
    assert not owner.path.exists()


def review_receipt(registry):
    installation, _ = registry
    owner = jobs.JobOwnership(installation, worker=True, review_key="a" * 64)
    owner.created()
    return owner


def test_live_review_guardian_is_reused(registry):
    owner = review_receipt(registry)
    owner.worker_started()
    assert jobs.existing_review(owner.installation, "a" * 64) == os.getpid()
    assert jobs.existing_review(owner.installation, "b" * 64) is None


@pytest.mark.parametrize("state", ["pending", "abandoned"])
def test_unresolved_live_launch_blocks_duplicate(registry, state):
    owner = review_receipt(registry)
    owner._publish(state=state)
    with pytest.raises(jobs.JobsBusy, match="unresolved ownership"):
        jobs.existing_review(owner.installation, "a" * 64)


def test_dead_parent_with_live_descendants_is_never_killed_to_retry(registry, monkeypatch):
    owner = review_receipt(registry)
    monkeypatch.setattr(jobs, "windows_process_identity", lambda pid: None)
    monkeypatch.setattr(jobs, "job_quiescent", lambda name: False)
    with pytest.raises(jobs.JobsBusy, match="descendants are still active"):
        jobs.existing_review(owner.installation, "a" * 64)
    assert registry[1] == []
    assert owner.path.exists()


@pytest.mark.parametrize("created", [False, True])
def test_proven_dead_empty_launch_permits_retry(registry, monkeypatch, created):
    owner = jobs.JobOwnership(registry[0], worker=True, review_key="a" * 64)
    if created:
        owner.created()
    monkeypatch.setattr(jobs, "windows_process_identity", lambda pid: None)
    monkeypatch.setattr(jobs, "job_quiescent", lambda name: True)
    assert jobs.existing_review(owner.installation, "a" * 64) is None
    assert registry[1] == []


def test_concurrent_adoption_invalidates_dead_parent_snapshot(registry, monkeypatch):
    owner = review_receipt(registry)

    def adopt(pid):
        owner._publish(owner_pid=pid + 1, owner_start="new-guardian")
        return None

    monkeypatch.setattr(jobs, "windows_process_identity", adopt)
    with pytest.raises(jobs.JobsBusy, match="ownership is changing"):
        jobs.existing_review(owner.installation, "a" * 64)
    assert registry[1] == []


def test_review_launch_mutex_is_nonblocking_and_released(registry):
    with jobs.serialized_review_launch(registry[0]):
        with pytest.raises(BlockingIOError), jobs.serialized_review_launch(registry[0]):
            pytest.fail("concurrent launch acquired the mutex")
    with jobs.serialized_review_launch(registry[0]):
        pass
