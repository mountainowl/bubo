"""Windows publication API contract tests; native filesystem tests are separate."""

import json

import pytest

from bubo import update_identity, update_windows_files


def test_native_move_uses_only_replace_and_write_through(tmp_path, monkeypatch):
    source, target = tmp_path / "temporary", tmp_path / "journal"
    calls = []

    def move(before, after, flags):
        calls.append((before, after, flags))
        return 1

    monkeypatch.setattr(update_windows_files, "_move_file", lambda: move)
    update_windows_files.replace_file(source, target)
    assert calls == [(str(source), str(target), 0x9)]


def test_move_rejects_different_directories_before_native_call(tmp_path, monkeypatch):
    def unexpected():
        raise AssertionError("must not request a cross-directory move")

    monkeypatch.setattr(update_windows_files, "_move_file", unexpected)
    with pytest.raises(ValueError, match="same directory"):
        update_windows_files.replace_file(tmp_path / "one" / "a", tmp_path / "two" / "b")


def test_native_error_is_not_reported_as_success(tmp_path, monkeypatch):
    monkeypatch.setattr(update_windows_files, "_move_file", lambda: lambda *_args: 0)
    monkeypatch.setattr(update_windows_files.ctypes, "get_last_error", lambda: 32, raising=False)
    monkeypatch.setattr(
        update_windows_files.ctypes,
        "WinError",
        lambda error: OSError(error, "sharing violation"),
        raising=False,
    )
    with pytest.raises(OSError, match="sharing violation") as caught:
        update_windows_files.replace_file(tmp_path / "temp", tmp_path / "target")
    assert caught.value.errno == 32


@pytest.mark.parametrize("fail_after_replace", [False, True])
def test_windows_json_flushes_before_publish_and_preserves_uncertain_outcome(
    tmp_path, monkeypatch, fail_after_replace
):
    target = tmp_path / "journal.json"
    target.write_text('{"old": true}')
    flushed = []
    original_fsync = update_identity.os.fsync

    def fsync(fd):
        original_fsync(fd)
        flushed.append(fd)

    def replace(source, destination):
        assert flushed
        assert json.loads(source.read_text()) == {"new": True}
        source.replace(destination)
        if fail_after_replace:
            raise OSError("uncertain publication")

    monkeypatch.setattr(update_identity.sys, "platform", "win32")
    monkeypatch.setattr(update_identity.os, "fsync", fsync)
    monkeypatch.setattr(update_windows_files, "replace_file", replace)
    if fail_after_replace:
        with pytest.raises(OSError, match="uncertain publication"):
            update_identity.atomic_json(target, {"new": True})
    else:
        update_identity.atomic_json(target, {"new": True})
    assert json.loads(target.read_text()) == {"new": True}
    assert list(tmp_path.iterdir()) == [target]


def test_windows_json_failed_replace_keeps_old_record_and_cleans_temp(tmp_path, monkeypatch):
    target = tmp_path / "journal.json"
    target.write_text('{"old": true}')

    def fail(*_args):
        raise OSError("sharing violation")

    monkeypatch.setattr(update_identity.sys, "platform", "win32")
    monkeypatch.setattr(update_windows_files, "replace_file", fail)
    with pytest.raises(OSError, match="sharing violation"):
        update_identity.atomic_json(target, {"new": True})
    assert json.loads(target.read_text()) == {"old": True}
    assert list(tmp_path.iterdir()) == [target]


def test_directory_sync_is_not_silently_weakened_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(update_identity.sys, "platform", "win32")
    from bubo import update_windows_files

    def denied(path):
        assert path == tmp_path
        raise OSError("native flush denied")

    monkeypatch.setattr(update_windows_files, "flush_directory", denied)
    with pytest.raises(OSError, match="native flush denied"):
        update_identity.sync_directory(tmp_path)
