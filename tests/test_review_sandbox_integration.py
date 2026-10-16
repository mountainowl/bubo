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


def test_codex_native_sandbox_runs_inside_linux_outer_gate(tmp_path: Path):
    """Exercise sandbox nesting without a provider, login files, or model calls."""
    import shutil

    required = os.environ.get("BUBO_REQUIRE_CODEX_SANDBOX_TEST") == "1"
    if not required:
        pytest.skip("Opt-in pinned Codex runtime integration check")
    if not sys.platform.startswith("linux"):
        if required:
            pytest.fail("The mandatory nested Codex check requires Linux")
        pytest.skip("Linux-specific sandbox nesting check")
    codex = shutil.which("codex")
    if not codex:
        if required:
            pytest.fail("The mandatory nested Codex check requires Codex CLI")
        pytest.skip("Codex CLI is not installed")
        return
    source = tmp_path.resolve() / "nested-source"
    scratch = tmp_path.resolve() / "nested-scratch"
    source.mkdir()
    scratch.mkdir()
    protected = source / "source.txt"
    protected.write_text("original", encoding="utf-8")
    sentinel = scratch / "native-protected.txt"
    sentinel.write_text("original", encoding="utf-8")
    home = scratch / "home"
    temporary = scratch / "tmp"
    home.mkdir()
    temporary.mkdir()
    # Sentinel is writable through the outer gate, outside native temp/home paths.
    env = {
        "PATH": os.environ.get("PATH", os.defpath), "HOME": str(home),
        "CODEX_HOME": str(home), "TMPDIR": str(temporary),
    }
    version = subprocess.run(
        [codex, "--version"], env=env, capture_output=True, text=True, timeout=15,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == "codex-cli 0.154.0"
    probe = r'''
import sys
from pathlib import Path
for p in (Path("source.txt"), Path(sys.argv[1])):
    assert p.read_text() == "original"
    try:
        p.write_text("changed")
    except OSError:
        pass
    else:
        raise AssertionError(f"Native sandbox permitted write to {p}")
print("native-read-ok-write-denied")
'''
    child = [
        codex, "sandbox", "-c", 'sandbox_mode="read-only"', "--",
        sys.executable, "-I", "-c", probe, str(sentinel),
    ]
    try:
        nested = sandbox_command(child, scratch, source)
        control = sandbox_command([
            sys.executable, "-I", "-c",
            "import sys; from pathlib import Path; Path(sys.argv[1]).write_text('original')",
            str(sentinel),
        ], scratch, source)
    except RuntimeError as exc:
        if required:
            pytest.fail(str(exc))
        pytest.skip(str(exc))
        return
    outer = subprocess.run(
        control, cwd=source, env=env, capture_output=True, text=True, timeout=30,
    )
    assert outer.returncode == 0, outer.stderr
    for command in (child, nested):
        result = subprocess.run(
            command, cwd=source, env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "native-read-ok-write-denied" in result.stdout
        assert protected.read_text(encoding="utf-8") == "original"
        assert sentinel.read_text(encoding="utf-8") == "original"
