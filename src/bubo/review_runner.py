"""Read-only filesystem boundary for model-driven review subprocesses.

The model needs network access to its provider. This boundary prevents filesystem
writes outside disposable scratch; it does not claim to filter HTTP semantics.
SCM credentials must be removed by the caller before entering this boundary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from bubo.subproc import run_bounded


class ReviewProcessResult(subprocess.CompletedProcess[str]):
    """Keep transport diagnostics separate from the final review result."""

    def __init__(self, process: subprocess.CompletedProcess[str], final_message: str):
        super().__init__(process.args, process.returncode, final_message, process.stderr)
        self.diagnostic_stdout = process.stdout


def sandbox_command(command: list[str], scratch: Path, cwd: Path) -> list[str]:
    """Fail closed when no supported OS sandbox is available."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        # JSON string quoting is also valid for Sandbox profile path strings.
        import json

        profile = (
            "(version 1)(allow default)(deny file-write*)"
            f'(allow file-write* (subpath {json.dumps(str(scratch))}) (literal "/dev/null"))'
        )
        return ["sandbox-exec", "-p", profile, *command]
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        return [
            "bwrap",
            "--die-with-parent",
            "--unshare-pid",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--bind",
            str(scratch),
            str(scratch),
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--chdir",
            str(cwd),
            "--",
            *command,
        ]
    raise RuntimeError("Read-only review requires macOS sandbox-exec or Linux bubblewrap")


def run_review(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    review_config: Any = None,
    **kwargs: Any,
):
    """Run an agent with read-only source and a disposable writable home."""
    from bubo.poller import reviewer_env  # Shared credential policy, no import-time cycle.

    source_env = dict(os.environ if env is None else env)
    child_env = reviewer_env(source_env, review_config)
    source_home = Path(source_env.get("HOME", str(Path.home())))
    target = Path(cwd or Path.cwd()).resolve()
    with tempfile.TemporaryDirectory(prefix="bubo-review-") as temporary:
        scratch = Path(temporary).resolve()
        # Share only provider login/settings files; sessions and caches stay disposable.
        for directory, names in {
            ".codex": ("auth.json", "config.toml"),
            ".claude": (".credentials.json", "settings.json"),
            ".gemini": ("oauth_creds.json", "settings.json", "google_accounts.json"),
        }.items():
            (scratch / directory).mkdir()
            override = {".codex": "CODEX_HOME", ".claude": "CLAUDE_CONFIG_DIR"}.get(directory)
            original = (
                Path(source_env[override])
                if override and source_env.get(override)
                else source_home / directory
            )
            for name in names:
                source = original / name
                if source.is_file():
                    shutil.copy2(source, scratch / directory / name)
                    (scratch / directory / name).chmod(0o600)
        if (source_home / ".claude.json").is_file():
            shutil.copy2(source_home / ".claude.json", scratch / ".claude.json")
            (scratch / ".claude.json").chmod(0o600)
        child_env.update(
            HOME=str(scratch),
            TMPDIR=str(scratch),
            TMP=str(scratch),
            TEMP=str(scratch),
            XDG_CACHE_HOME=str(scratch / "cache"),
            XDG_CONFIG_HOME=str(scratch / "config"),
            CLAUDE_CONFIG_DIR=str(scratch / ".claude"),
            CODEX_HOME=str(scratch / ".codex"),
        )
        final = scratch / "final.json"
        cmd = list(command)
        exec_index = None
        if Path(cmd[0]).name == "codex":
            index = 1
            value_options = {
                "--ask-for-approval",
                "-a",
                "--config",
                "-c",
                "--model",
                "-m",
                "--profile",
                "-p",
                "--sandbox",
                "-s",
                "--cd",
                "-C",
                "--add-dir",
            }
            while index < len(cmd):
                argument = cmd[index]
                if argument in value_options:
                    index += 2
                elif argument.startswith("-"):
                    index += 1
                else:
                    exec_index = index if argument == "exec" else None
                    break
        codex = exec_index is not None
        if exec_index is not None:
            # Only the final message is parsed, never the tool/terminal transcript.
            cmd[exec_index + 1 : exec_index + 1] = ["--output-last-message", str(final)]
        result = run_bounded(
            sandbox_command(cmd, scratch, target), cwd=target, env=child_env, **kwargs
        )
        if codex and result.returncode == 0:
            if not final.is_file() or final.stat().st_size > kwargs.get(
                "max_stdout_bytes", 2_000_000
            ):
                raise ValueError("Reviewer did not produce a bounded final message")
            return ReviewProcessResult(result, final.read_text(encoding="utf-8"))
        return result
