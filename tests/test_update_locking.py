import ctypes
import sys
from types import SimpleNamespace

import pytest

from bubo import update_locking


@pytest.mark.parametrize("exclusive", [False, True])
@pytest.mark.parametrize("error", [0, 33, 5])
def test_windows_lock_uses_nonblocking_native_byte_range(monkeypatch, exclusive, error):
    calls = []

    def lock(handle, flags, reserved, low, high, position):
        calls.append((handle, flags, reserved, low, high))
        assert position._obj.Offset == 0
        assert position._obj.OffsetHigh == 0
        return not error

    api = SimpleNamespace(LockFileEx=lock)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: api, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 2**40 + fd))
    monkeypatch.setattr(update_locking, "fcntl", None)
    if error:
        kind = BlockingIOError if error == 33 else OSError
        with pytest.raises(kind):
            update_locking.lock_file(7, exclusive=exclusive)
    else:
        update_locking.lock_file(7, exclusive=exclusive)
    assert calls == [(2**40 + 7, 3 if exclusive else 1, 0, 1, 0)]
    assert api.LockFileEx.argtypes[0] == ctypes.wintypes.HANDLE


def test_identity_persists_with_windows_lock_backend(tmp_path, monkeypatch):
    from bubo import analytics

    calls = []
    monkeypatch.setattr(update_locking, "fcntl", None)
    monkeypatch.setattr(update_locking, "_windows_lock", lambda fd, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(analytics.paths, "DB", tmp_path / "state/reviewer.sqlite")
    monkeypatch.setattr(analytics, "_install_id", None)
    monkeypatch.setattr(analytics, "_install_path", None)
    first = analytics.install_id()
    assert first is not None
    monkeypatch.setattr(analytics, "_install_id", None)
    assert analytics.install_id() == first
    assert calls == [{"exclusive": True}, {"exclusive": True}]
