"""Real kernel filesystem checks; no model, network, or account credentials.

Set BUBO_REQUIRE_SANDBOX_TEST=1 to make unavailable/nested sandboxes fail CI.
This validates filesystem isolation only, not network or remote-service policy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bubo.review_runner import sandbox_command


def test_kernel_blocks_writes_outside_disposable_scratch(tmp_path: Path):
    root = tmp_path.resolve()
    source = root / "source"
    scratch = root / "scratch"
    source.mkdir()
    scratch.mkdir()
    protected = [
        source / "app.py",
        source / "ignored.cache",
        source / ".git" / "index",
        source / ".git" / "refs" / "heads" / "main",
        root / "original-home" / ".codex" / "config.toml",
    ]
    for path in protected:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original", encoding="utf-8")
    (source / ".gitignore").write_text("ignored.cache\n", encoding="utf-8")
    (scratch / "source-link").symlink_to(protected[0])

    required = os.environ.get("BUBO_REQUIRE_SANDBOX_TEST") == "1"
    try:
        baseline_command = sandbox_command(
            [sys.executable, "-I", "-c", "print('sandbox baseline')"], scratch, source
        )
    except RuntimeError as exc:
        if required:
            pytest.fail(str(exc))
        pytest.skip(str(exc))
        return  # Explicit exit for static analyzers unaware that pytest.skip raises.
    # Give the child no account/provider environment or interpreter path overrides.
    env = {"PATH": os.defpath, "HOME": str(scratch), "TMPDIR": str(scratch)}
    baseline = subprocess.run(
        baseline_command, cwd=source, env=env, capture_output=True, text=True, timeout=15,
    )
    if baseline.returncode:
        message = f"Kernel sandbox unavailable: {baseline.stderr.strip()}"
        if required:
            pytest.fail(message)
        pytest.skip(message)

    probe = r'''
import json, os, sys
from pathlib import Path
paths = [Path(p) for p in json.loads(sys.argv[1])]
scratch = Path(sys.argv[2])
assert all(p.read_text() == "original" for p in paths)
(scratch / "allowed").write_text("scratch works")
denied = []
for target in [*paths, scratch / "source-link", paths[0].parent / "new-file"]:
    try:
        target.write_text("MUTATED")
    except OSError:
        denied.append(str(target))
    else:
        raise AssertionError(f"write escaped sandbox: {target}")
try:
    os.link(paths[0], scratch / "hardlink")
    (scratch / "hardlink").write_text("MUTATED")
except OSError:
    pass
else:
    raise AssertionError("hardlink write escaped sandbox")
print(json.dumps({"denied": len(denied)}))
'''
    command = sandbox_command(
        [sys.executable, "-I", "-c", probe, json.dumps([str(p) for p in protected]), str(scratch)],
        scratch, source,
    )
    result = subprocess.run(
        command, cwd=source, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["denied"] == len(protected) + 2
    assert (scratch / "allowed").read_text() == "scratch works"
    for path in protected:
        assert path.read_text() == "original"
    assert not (source / "new-file").exists()
