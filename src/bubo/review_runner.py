"""Read-only filesystem boundary for model-driven review subprocesses.

The model needs network access to its provider. This boundary prevents filesystem
writes outside disposable scratch; it does not claim to filter HTTP semantics.
SCM credentials must be removed by the caller before entering this boundary.
"""

from __future__ import annotations

import json
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


def supported_agent_command(
    command: list[str], *, target: Path, final: Path, review_config: Any = None
) -> tuple[list[str], bool]:
    """Allow only supported CLI inputs; callers cannot weaken the review policy.

    This reduces ambient tool exposure, not a guarantee about all remote effects.
    The native Codex sandbox must additionally constrain shell tool networking.
    """
    if not command or Path(command[0]).name not in {"codex", "claude"}:
        raise ValueError("Read-only review supports only codex exec or claude -p")
    codex = Path(command[0]).name == "codex"
    args = command[1:]
    options: list[str] = []
    prompt: list[str] = []
    exec_seen = False
    index = 0
    while index < len(args):
        argument = args[index]
        if codex and argument == "exec" and not exec_seen and not prompt:
            exec_seen = True
            index += 1
            continue
        if argument in {"--model", "-m"}:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                raise ValueError("Missing review model")
            options.extend(["--model", args[index + 1]])
            index += 2
            continue
        fixed = (
            {
                "--sandbox": "read-only",
                "-s": "read-only",
                "--ask-for-approval": "never",
                "-a": "never",
                "--profile": "bubo",
            }
            if codex
            else {}
        )
        if argument in fixed:
            if index + 1 >= len(args) or args[index + 1] != fixed[argument]:
                raise ValueError(f"Unsupported review policy override: {argument}")
            index += 2
            continue
        flags = (
            {"--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"}
            if codex
            else {"-p", "--print"}
        )
        if argument in flags:
            if not codex:
                exec_seen = True
            index += 1
            continue
        if argument.startswith("-") or prompt or (codex and not exec_seen):
            raise ValueError(f"Unsupported review command argument: {argument.split('=', 1)[0]}")
        prompt.append(argument)
        index += 1
    if not exec_seen:
        raise ValueError("Read-only review requires codex exec or claude -p")
    model = getattr(review_config, "model", None)
    if model and "--model" not in options:
        options.extend(["--model", model])
    if not codex:
        if getattr(review_config, "llm_base_url", None):
            raise ValueError("Custom OpenAI-compatible endpoints require the Codex reviewer")
        return [
            command[0],
            "--safe-mode",
            "--restricted",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--tools",
            "Read,Grep,Glob",
            "--permission-prompts",
            "none",
            *options,
            "-p",
            *prompt,
        ], False
    policy = [
        "features.apps=false",
        "features.plugins=false",
        "features.hooks=false",
        "features.skill_mcp_dependency_install=false",
        'web_search="disabled"',
        "project_doc_max_bytes=0",
    ]
    # Project trust can be inherited from an ancestor; override each enclosing path.
    for directory in (target, *target.parents):
        policy.append(f'projects.{json.dumps(str(directory))}.trust_level="untrusted"')
    effort = getattr(review_config, "model_effort", None)
    if effort:
        policy.append(f"model_reasoning_effort={json.dumps(effort)}")
    endpoint = getattr(review_config, "llm_base_url", None)
    if endpoint:
        policy.extend(
            [
                'model_provider="bubo"',
                'model_providers.bubo.name="Bubo trusted provider"',
                f"model_providers.bubo.base_url={json.dumps(endpoint)}",
                'model_providers.bubo.env_key="LLM_API_KEY"',
                'model_providers.bubo.wire_api="responses"',
            ]
        )
    config_args = [value for item in policy for value in ("-c", item)]
    return [
        command[0],
        "--ask-for-approval",
        "never",
        *config_args,
        "exec",
        "--output-last-message",
        str(final),
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        *options,
        *prompt,
    ], True


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
    if endpoint := getattr(review_config, "llm_base_url", None):
        child_env["LLM_BASE_URL"] = endpoint
    source_home = Path(source_env.get("HOME", str(Path.home())))
    target = Path(cwd or Path.cwd()).resolve()
    with tempfile.TemporaryDirectory(prefix="bubo-review-") as temporary:
        scratch = Path(temporary).resolve()
        final = scratch / "final.json"
        cmd, codex = supported_agent_command(
            command, target=target, final=final, review_config=review_config
        )
        # Copy authentication only. Tool settings and project/user configuration are untrusted.
        for directory, names in {
            ".codex": ("auth.json",),
            ".claude": (".credentials.json",),
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
