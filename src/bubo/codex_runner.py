"""Wrapper for one-off (non-poller) Codex code reviews.

Provides the ``bubo-codex`` CLI entry point. The poller forks this
binary as a subprocess for each MR review, but it can also be invoked by
hand:

    uv run bubo-codex "Review the current changes."

Compared to :mod:`bubo.poller`, this module is intentionally
narrow: it renders the meta prompt with the configured ``max_findings``
cap, builds a Superpowers-style review task, and execs the ``codex`` CLI
with the right profile and noninteractive flags. It does not touch the
SQLite state file and never talks to GitLab directly — any GitLab access
the agent performs goes through the MCP server it controls.
"""

from __future__ import annotations

import os
import sys
import tomllib
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from bubo.config_values import positive_int, section
from bubo.env_config import apply_runtime_env, read_config_file
from bubo.paths import CONFIG as ENV_CONFIG
from bubo.paths import RENDERED_PROMPTS, ROOT
from bubo.prompt import write_rendered_meta_prompt
from bubo.subproc import run_bounded


def load_runtime_config() -> None:
    if os.environ.get("BUBO_SKIP_AGENT_CONFIG_ENV") == "1":
        return
    if ENV_CONFIG.is_file():
        with suppress(OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
            apply_runtime_env(ROOT, read_config_file(ENV_CONFIG))


load_runtime_config()

PROMPT_FILE = Path(os.environ.get("BUBO_PROMPT", ROOT / "prompts" / "00-meta.md"))
LOG_DIR = ROOT / "var" / "log" / "codex"
CODEX_PROFILE = os.environ.get("CODEX_REVIEW_PROFILE", "bubo")


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def configured_max_findings() -> int:
    """Return the active findings cap for a manual review.

    Resolution order:

    1. ``LLM_REVIEW_MAX_FINDINGS`` env var (set by the poller for its
       worker children; an operator may also set it for a hand-run).
    2. ``MAX_FINDINGS_PER_REVIEW`` env var (legacy alias still honored
       for compatibility with older wrapper scripts).
    3. ``[review].max_findings_per_merge_request`` from ``env.toml``.
    4. Hardcoded fallback of ``5``.
    """
    for key in ("LLM_REVIEW_MAX_FINDINGS", "MAX_FINDINGS_PER_REVIEW"):
        value = os.environ.get(key)
        if value:
            return positive_int(value, key)
    config = ENV_CONFIG
    if config.is_file():
        with suppress(OSError, tomllib.TOMLDecodeError, TypeError):
            review = section(tomllib.loads(config.read_text(encoding="utf-8")), "review")
            return positive_int(
                review.get("max_findings_per_merge_request", 5), "max_findings_per_merge_request"
            )
    return 5


def configured_timeout_seconds() -> int:
    """Return the active wall-clock timeout for a manual review.

    Matches the poller's ``[review].timeout_seconds`` field so a manual
    review and a poller-driven review run under the same budget. Falls
    back to a generous default (1800s) when the TOML is missing or the
    field is absent.
    """
    config = ENV_CONFIG
    if config.is_file():
        with suppress(OSError, tomllib.TOMLDecodeError, TypeError):
            review = section(tomllib.loads(config.read_text(encoding="utf-8")), "review")
            return positive_int(review.get("timeout_seconds", 1800), "timeout_seconds")
    return 1800


def review_task_prompt(review_task: str, prompt_file: Path | None = None) -> str:
    prompt_path = prompt_file or PROMPT_FILE
    return f"""/using-superpowers
$code-reviewer

Fetch the GitLab merge request using GitLab MCP and local git/glab as needed.
Pass the code changes to the $code-reviewer skill for analysis.
Use this rendered meta prompt file for the review contract: {prompt_path}

Review task:
{review_task}

Use diff review unless the task explicitly asks for full code review.
Do not post comments to GitLab.
Return only the final review findings allowed by the review task and the $code-reviewer skill.
Do not include CLI transcript, tool logs, token counts, status commentary, or summaries."""


def codex_command() -> list[str]:
    return [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "--profile",
        CODEX_PROFILE,
        "--skip-git-repo-check",
    ]


def main() -> int:
    if not PROMPT_FILE.is_file():
        print(f"code-review prompt is not readable: {PROMPT_FILE}", file=sys.stderr)
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rendered_prompt = write_rendered_meta_prompt(
        PROMPT_FILE, RENDERED_PROMPTS, configured_max_findings()
    )
    review_task = " ".join(sys.argv[1:]).strip() or "Review the current changes."
    prompt = review_task_prompt(review_task, rendered_prompt)
    transcript_path = LOG_DIR / f"codex-transcript-{stamp()}-{os.getpid()}.log"
    cmd = [*codex_command(), prompt]
    env = os.environ.copy()
    env["BUBO_PROMPT"] = str(rendered_prompt)

    # Use the shared bounded helper — same capture shape as the poller,
    # plus a wall-clock timeout and process-group kill on expiry so a
    # hung codex (or MCP grandchild) cannot wedge a manual review forever.
    result = run_bounded(
        cmd,
        cwd=Path(os.getcwd()),
        env=env,
        timeout=configured_timeout_seconds(),
    )

    output = result.stdout or ""
    transcript_path.write_text(output, encoding="utf-8")
    if output:
        print(output.rstrip())

    if result.returncode:
        print(f"codex exited {result.returncode}; transcript={transcript_path}", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
