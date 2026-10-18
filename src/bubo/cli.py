"""Top-level ``bubo`` CLI — install, doctor, and asset placement.

Replaces the hand-rolled ``scripts/install-package.sh`` shell installer
(deprecated in v0.6.0 and now removed; see issue #22) for users who
installed via ``uv tool install`` / ``pipx install``.

Two subcommands so far:

* ``bubo init`` — write the deployable assets the runtime needs
  but Python packaging cannot place itself: ``~/.codex/config.toml``,
  the load-bearing ``~/.codex/bubo.config.toml`` Codex profile (a legacy
  ``[profiles.bubo]`` table in ``config.toml`` is moved there),
  ``~/.claude/settings.json``, and the per-host workspace under
  ``$BUBO_ROOT`` (config seed, var/ tree, SQLite schema,
  copies of the bundled prompts/skills). Idempotent on re-run;
  ``--dry-run`` lists every action it would take without touching disk;
  ``--force`` overwrites operator-edited files; ``--no-agent-config``
  skips the ``~/.codex`` and ``~/.claude`` writes for hosts with
  hand-rolled agent configs.
* ``bubo doctor`` — non-mutating health check. Returns non-zero
  on any verification failure (missing ``~/.codex/config.toml``,
  missing ``~/.codex/bubo.config.toml`` or a leftover legacy
  ``[profiles.bubo]`` table, missing ``env.toml``,
  uninitialized DB). Suitable for cron / monitoring smoke tests.

The CLI uses :mod:`importlib.resources` to read packaged templates from
``bubo/_assets/`` inside the wheel, so it works the same whether
the project was installed via ``uv tool install``, ``pipx install``,
``pip install <sdist>``, or an editable ``uv sync --dev``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from bubo import paths
from bubo.config_values import section
from bubo.db import init_db
from bubo.env_config import DEFAULT_CLAUDE_MODEL, DEFAULT_CODEX_MODEL
from bubo.errors import describe
from bubo.events import log
from bubo.update_admission import MaintenanceInProgress, activity

# ---------------------------------------------------------------------------
# Asset resolution — packaged via importlib.resources
# ---------------------------------------------------------------------------

_ASSETS_PACKAGE = "bubo._assets"

# Editable-install fallback. Hatchling's force-include places the deploy
# assets at bubo/_assets/ inside the WHEEL, but an editable install
# (uv sync --dev) sees only the source tree where these files still live
# at their natural repo-root paths. Mapping keeps the in-wheel layout the
# single source of truth — when assets are eventually moved into
# src/bubo/_assets/ this fallback becomes a no-op (and can be
# deleted), but until then the developer/test path needs it.
_EDITABLE_FALLBACKS: dict[tuple[str, ...], str] = {
    ("codex-config.toml",): "deploy/templates/codex-config.toml",
    ("codex-bubo.config.toml",): "deploy/templates/codex-bubo.config.toml",
    ("claude-settings.json",): "deploy/templates/claude-settings.json",
    ("env.example.toml",): "config/env.example.toml",
    ("prompts",): "prompts",
    ("skills",): "skills",
    # Built operator-UI SPA assets. Force-included into the wheel under
    # bubo/_assets/ui/ (see pyproject.toml); the editable/source install reads
    # the committed Vite build output at ui/dist/ instead.
    ("ui",): "ui/dist",
}

_LEGACY_GENERATED_TEMPLATES = ("bubo.cron", "bubo.service", "bubo.timer")

# Codex profile the packaged templates configure. Codex >= 0.134.0 reads a
# profile from ``~/.codex/<name>.config.toml`` and refuses ``--profile <name>``
# while ``config.toml`` still holds the legacy ``[profiles.<name>]`` table.
_CODEX_PROFILE = "bubo"
_CODEX_PROFILE_FILE = f"{_CODEX_PROFILE}.config.toml"
_CODEX_MIN_VERSION = (0, 134, 0)


def _repo_root() -> Path:
    """Repo root inferred from this file's path (editable installs only)."""
    return Path(__file__).resolve().parents[2]


def _asset(*parts: str) -> Traversable | Path:
    """Return a handle to a packaged asset under ``_assets/``.

    Tries the packaged location first via :func:`importlib.resources.files`
    so wheel installs (the production path) hit a fast read. Falls back to
    the repo-root layout for editable installs (the developer/test path).
    Both branches return a :class:`Traversable` — ``Path`` satisfies the
    Traversable protocol — so callers don't need to care which won.
    """
    try:
        root = resources.files(_ASSETS_PACKAGE)
        for part in parts:
            root = root / part
        if root.is_file() or root.is_dir():
            return root
    except ModuleNotFoundError, FileNotFoundError:
        # Editable install: the packaged `_assets/` subpackage doesn't exist
        # on disk (hatchling force-includes it only into the built wheel), so
        # resolution falls through to the repo-root fallback below.
        pass
    fallback = _EDITABLE_FALLBACKS.get(parts)
    if fallback is not None:
        candidate = _repo_root() / fallback
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        describe(
            f"packaged asset not found: {'/'.join(parts)}",
            reason="a file bundled with the package is missing",
            fix="reinstall bubo (the install may be corrupt).",
        )
    )


# ---------------------------------------------------------------------------
# Plan / Action shape — dry-runnable, idempotent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """A single filesystem mutation produced by ``bubo init``.

    Stays a data class so the dry-run path can describe everything the
    real run would do without doing any of it.

    ``source`` carries either the asset-parts tuple (for write_file /
    copy_tree against a packaged asset) or a freeform string (for
    symlink targets). Empty for actions with no source.
    """

    kind: str  # "write_file", "mkdir", "copy_tree", "init_db", "symlink", "skip",
    # "migrate_codex_profile"
    target: Path
    source: tuple[str, ...] | str = ""
    note: str = ""


def _plan_workspace(root: Path) -> list[Action]:
    """Directories the runtime expects under ``$BUBO_ROOT``."""
    return [
        Action(kind="mkdir", target=root / "config", note="env.toml lives here"),
        Action(kind="mkdir", target=root / "var" / "state", note="SQLite DB + state"),
        Action(kind="mkdir", target=root / "var" / "work", note="per-MR worktrees"),
        Action(kind="mkdir", target=root / "var" / "log", note="JSON-line event logs"),
        Action(kind="mkdir", target=root / "var" / "reports", note="agent transcripts"),
        Action(kind="mkdir", target=root / "var" / "jobs", note="forked-worker job payloads"),
        Action(
            kind="mkdir",
            target=root / "var" / "rendered-prompts",
            note="meta prompts with {{MAX_FINDINGS_PER_REVIEW}} filled in",
        ),
    ]


def _plan_env_seed(root: Path, force: bool) -> list[Action]:
    """Seed ``config/env.toml`` from the packaged example unless one exists."""
    target = root / "config" / "env.toml"
    if target.exists() and not force:
        return [
            Action(
                kind="skip",
                target=target,
                note=(
                    "config/env.toml exists; keep operator-edited values (use --force to overwrite)"
                ),
            )
        ]
    return [
        Action(
            kind="write_file",
            target=target,
            source=("env.example.toml",),
            note="seed env.toml from the packaged example — fill in tokens before first run",
        )
    ]


def _plan_packaged_runtime_copies(root: Path) -> list[Action]:
    """Place prompts and skills on disk under ``$ROOT/``.

    These need to be on a real filesystem because Codex's skill loader
    and the meta-prompt renderer expect a directory tree, not an
    importlib resource handle. Re-copied every run so packaged updates
    win over stale on-disk copies (the alternative — only-on-first-init
    — would silently drift on upgrades).
    """
    return [
        Action(
            kind="copy_tree",
            target=root / "prompts",
            source=("prompts",),
            note="overwrite on every init so packaged updates take effect",
        ),
        Action(
            kind="copy_tree",
            target=root / "skills",
            source=("skills",),
            note="overwrite on every init",
        ),
    ]


def _plan_legacy_template_cleanup(root: Path) -> list[Action]:
    """Warn about only known generated scheduler files under this Bubo root."""
    generated = root / "deploy" / "templates"
    return [
        Action(
            kind="notice",
            target=generated / name,
            note=(
                "retired generated scheduler template remains; remove it manually. "
                "Installed host cron or units are never touched"
            ),
        )
        for name in _LEGACY_GENERATED_TEMPLATES
        if (generated / name).is_file() or (generated / name).is_symlink()
    ]


def _plan_agent_config(home: Path, root: Path, force: bool) -> list[Action]:
    """``~/.codex/config.toml``, the Codex ``bubo`` profile and
    ``~/.claude/settings.json`` from templates."""
    codex_target = home / ".codex" / "config.toml"
    profile_target = home / ".codex" / _CODEX_PROFILE_FILE
    claude_target = home / ".claude" / "settings.json"
    skills_link = home / ".codex" / "skills" / "code-reviewer"
    actions: list[Action] = []
    targets: list[tuple[Path, str]] = [
        (codex_target, "codex-config.toml"),
        (profile_target, "codex-bubo.config.toml"),
        (claude_target, "claude-settings.json"),
    ]

    # An existing config.toml written by an older init carries the profile as
    # a [profiles.bubo] table, which current Codex rejects. --force rewrites
    # config.toml from the template anyway; otherwise move the table into the
    # profile file, keeping the operator's values.
    if not force and _has_legacy_codex_profile(codex_target):
        actions.append(
            Action(
                kind="migrate_codex_profile",
                target=codex_target,
                source=str(profile_target),
                note=(
                    f"move legacy [profiles.{_CODEX_PROFILE}] into {profile_target.name} "
                    "(Codex >= 0.134.0 rejects it in config.toml); backup kept as config.toml.bak"
                ),
            )
        )
        if not profile_target.exists():
            # The migration writes the profile file from the legacy table.
            targets.remove((profile_target, "codex-bubo.config.toml"))

    for target, source_name in targets:
        if target.exists() and not force:
            actions.append(
                Action(
                    kind="skip",
                    target=target,
                    note=(
                        f"{target.name} exists; pass --force to overwrite "
                        "(will clobber local edits)"
                    ),
                )
            )
        else:
            actions.append(
                Action(
                    kind="write_file",
                    target=target,
                    source=(source_name,),
                    note=f"substitute {{{{ROOT}}}} = {root}",
                )
            )

    # Older inits wrote the Codex model into the Claude settings, which Claude
    # Code rejects on every run. Swap in the Claude model, keeping the rest.
    if not force and _claude_settings_have_openai_model(claude_target):
        actions.append(
            Action(
                kind="repair_claude_model",
                target=claude_target,
                note="replace the OpenAI model id an older init wrote with [agents].claude_model",
            )
        )

    actions.append(
        Action(
            kind="symlink",
            target=skills_link,
            source=str(root / "skills" / "code-reviewer"),
            note="interactive $code-reviewer skill for Codex (reviews inline the rules)",
        )
    )
    return actions


def _claude_settings_have_openai_model(target: Path) -> bool:
    if not target.is_file():
        return False
    try:
        model = json.loads(target.read_text()).get("model")
    except ValueError, AttributeError:
        return False
    return isinstance(model, str) and model.startswith("gpt-")


def _repair_claude_model(target: Path, model: str) -> None:
    settings = json.loads(target.read_text())
    settings["model"] = model
    target.write_text(json.dumps(settings, indent=2) + "\n")


_TABLE_HEADER = re.compile(r"^\s*\[")
_LEGACY_PROFILE_HEADER = re.compile(
    r'^\s*\[\s*profiles\s*\.\s*"?' + re.escape(_CODEX_PROFILE) + r'"?\s*(\.[^\]]*)?\]\s*(#.*)?$'
)
_LEGACY_PROFILE_SELECTOR = re.compile(
    r'^\s*profile\s*=\s*["\']' + re.escape(_CODEX_PROFILE) + r'["\']\s*(#.*)?$'
)


def _has_legacy_codex_profile(target: Path) -> bool:
    if not target.is_file():
        return False
    return any(_LEGACY_PROFILE_HEADER.match(line) for line in target.read_text().splitlines())


def _split_legacy_codex_profile(text: str) -> tuple[str, str] | None:
    """Split a legacy ``[profiles.bubo]`` table out of a Codex ``config.toml``.

    Returns ``(config_without_profile, profile_file_body)`` or ``None`` when
    there is no legacy table. The table's own lines (and those of any
    ``[profiles.bubo.<sub>]`` subtable, re-headed as ``[<sub>]``) move verbatim,
    so operator edits survive. A top-level ``profile = "bubo"`` selector is
    dropped too: Codex rejects it alongside the table, and the reviewer
    command passes ``--profile`` explicitly.
    """
    kept: list[str] = []
    main: list[str] = []
    subtables: list[str] = []
    current: list[str] | None = None
    found = False
    before_first_table = True
    for line in text.splitlines():
        match = _LEGACY_PROFILE_HEADER.match(line)
        if match:
            found = True
            before_first_table = False
            # Comment lines glued to the header document the legacy table.
            while kept and kept[-1].lstrip().startswith("#"):
                kept.pop()
            if match.group(1):
                current = subtables
                current.append(f"[{match.group(1)[1:].strip()}]")
            else:
                current = main
            continue
        if _TABLE_HEADER.match(line):
            before_first_table = False
            current = None
        elif before_first_table and _LEGACY_PROFILE_SELECTOR.match(line):
            continue
        (current if current is not None else kept).append(line)
    if not found:
        return None
    while main and not main[-1].strip():
        main.pop()
    body = "\n".join(main) + "\n"
    if subtables:
        body += "\n" + "\n".join(subtables).rstrip() + "\n"
    return "\n".join(kept).rstrip() + "\n", body


def _migrate_codex_profile(config: Path, profile: Path) -> None:
    """Move a legacy ``[profiles.bubo]`` table into the profile file.

    Leaves both files untouched (and warns) when the result would not parse,
    so a hand-edited config is never half-migrated.
    """
    split = _split_legacy_codex_profile(config.read_text())
    if split is None:
        return
    remaining, body = split
    try:
        tomllib.loads(remaining)
        tomllib.loads(body)
    except tomllib.TOMLDecodeError as exc:
        print(
            f"warning: could not migrate [profiles.{_CODEX_PROFILE}] out of {config} "
            f"({exc}); move it to {profile} by hand or re-run `bubo init --force`",
            file=sys.stderr,
        )
        return
    shutil.copy2(config, config.with_name(config.name + ".bak"))
    if not profile.exists():
        profile.write_text(body)
    config.write_text(remaining)


def plan_init(
    root: Path,
    *,
    force: bool,
    install_agent_config: bool,
    home: Path | None = None,
) -> list[Action]:
    """Compute the full action list a real ``init`` run would execute."""
    home = home or Path.home()
    actions: list[Action] = []
    actions.extend(_plan_workspace(root))
    actions.extend(_plan_env_seed(root, force))
    actions.extend(_plan_packaged_runtime_copies(root))
    actions.extend(_plan_legacy_template_cleanup(root))
    if install_agent_config:
        actions.extend(_plan_agent_config(home, root, force))
    actions.append(
        Action(
            kind="init_db",
            target=paths.DB,
            note="create or migrate the SQLite schema; safe on re-run",
        )
    )
    return actions


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _render_template(parts: tuple[str, ...], subs: dict[str, str]) -> str:
    """Substitute ``{{KEY}}`` placeholders in a packaged template.

    ``subs`` always carries ``ROOT``; agent-config templates additionally
    carry ``LLM_MODEL`` / ``LLM_MODEL_EFFORT`` (Codex), ``CLAUDE_MODEL`` (Claude)
    and the conditional
    model-provider pieces. Placeholders a given template doesn't contain are
    simply no-ops.
    """
    text = _asset(*parts).read_text()
    for key, value in subs.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def _read_agent_config(root: Path) -> dict[str, Any]:
    """Best-effort read of the ``[agents]`` section for init templating.

    Reads the on-disk ``env.toml`` when present, else the packaged example
    (which is what a fresh init is about to seed). Parsed WITHOUT placeholder
    expansion — only the literal ``llm_model`` / ``claude_model`` / ``llm_model_effort`` /
    ``llm_base_url`` are needed here, never the API key.
    """
    cfg_path = root / "config" / "env.toml"
    text = (
        cfg_path.read_text(encoding="utf-8")
        if cfg_path.exists()
        else _asset("env.example.toml").read_text()
    )
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {}
    return section(raw, "agents")


def _agent_template_subs(root: Path, agent: dict[str, Any]) -> dict[str, str]:
    """Build the placeholder substitutions for the agent-config templates.

    When ``[agents].llm_base_url`` is set, point the Codex ``bubo`` profile at a
    custom OpenAI-compatible provider that reads the key from ``LLM_API_KEY`` at
    request time (the env var that ``reviewer_env`` lets through in base_url
    mode). When it is unset, both provider placeholders render empty and Codex
    uses its built-in provider.
    """
    model = str(agent.get("llm_model") or DEFAULT_CODEX_MODEL)
    claude_model = str(agent.get("claude_model") or DEFAULT_CLAUDE_MODEL)
    effort = str(agent.get("llm_model_effort") or agent.get("reasoning_effort") or "medium")
    base_url = str(agent.get("llm_base_url") or "").strip()
    if base_url:
        provider_line = 'model_provider = "bubo"'
        provider_block = (
            "[model_providers.bubo]\n"
            'name = "bubo custom endpoint"\n'
            f'base_url = "{base_url}"\n'
            'env_key = "LLM_API_KEY"\n'
            'wire_api = "chat"\n'
        )
    else:
        provider_line = ""
        provider_block = ""
    return {
        "ROOT": str(root),
        "LLM_MODEL": model,
        "CLAUDE_MODEL": claude_model,
        "LLM_MODEL_EFFORT": effort,
        "MODEL_PROVIDER_LINE": provider_line,
        "MODEL_PROVIDER_BLOCK": provider_block,
    }


def _copy_traversable(source: Traversable | Path, target: Path) -> None:
    """Recursively copy a Traversable directory onto the filesystem.

    Recurses on the Traversable directly rather than re-resolving the
    asset path each level — Path and importlib.resources both expose
    the iterdir/is_dir/read_bytes shape this needs.
    """
    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        dest = target / entry.name
        if entry.is_dir():
            _copy_traversable(entry, dest)
        else:
            dest.write_bytes(entry.read_bytes())


def _execute(action: Action, subs: dict[str, str]) -> None:
    if action.kind == "skip":
        return
    if action.kind == "mkdir":
        action.target.mkdir(parents=True, exist_ok=True)
        return
    if action.kind == "write_file":
        assert isinstance(action.source, tuple), "write_file expects a parts tuple"
        action.target.parent.mkdir(parents=True, exist_ok=True)
        action.target.write_text(_render_template(action.source, subs))
        return
    if action.kind == "copy_tree":
        assert isinstance(action.source, tuple), "copy_tree expects a parts tuple"
        if action.target.exists() and action.target.is_symlink():
            action.target.unlink()
        elif action.target.exists():
            shutil.rmtree(action.target)
        _copy_traversable(_asset(*action.source), action.target)
        return
    if action.kind == "notice":
        return
    if action.kind == "symlink":
        assert isinstance(action.source, str), "symlink expects a string target"
        action.target.parent.mkdir(parents=True, exist_ok=True)
        if action.target.is_symlink() or action.target.exists():
            action.target.unlink()
        action.target.symlink_to(action.source)
        return
    if action.kind == "repair_claude_model":
        _repair_claude_model(action.target, subs["CLAUDE_MODEL"])
        return
    if action.kind == "migrate_codex_profile":
        assert isinstance(action.source, str), "migrate_codex_profile expects a path"
        _migrate_codex_profile(action.target, Path(action.source))
        return
    if action.kind == "init_db":
        init_db()
        return
    raise ValueError(
        describe(
            f"unknown action kind: {action.kind!r}",
            reason="an internal action kind was not handled",
            fix="this is a bug — report it (include the kind).",
        )
    )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _retarget_paths(root: Path) -> None:
    """Mutate the ``paths`` module to point at ``root``.

    ``paths.ROOT``, ``paths.DB``, and friends are computed at import time
    from ``BUBO_ROOT``. When ``--root`` overrides that in a
    single ``bubo init`` invocation, the module-level constants
    don't auto-refresh. Updating them directly is cheap and matches what
    the test suite already does in its fixtures.
    """
    paths.ROOT = root
    paths.CONFIG = root / "config" / "env.toml"
    from .update_helper import _state_for_root

    state = _state_for_root(root).parent
    paths.DB = state / "state" / "reviewer.sqlite"
    paths.WORK = state / "work"
    paths.REPORTS = state / "reports"
    paths.JOBS = state / "jobs"
    paths.LOGS = state / "log"
    paths.RENDERED_PROMPTS = state / "rendered-prompts"


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else paths.ROOT
    os.environ["BUBO_ROOT"] = str(root)
    _retarget_paths(root)
    actions = plan_init(
        root,
        force=args.force,
        install_agent_config=not args.no_agent_config,
    )
    subs = _agent_template_subs(root, _read_agent_config(root))
    if args.dry_run:
        print(f"# bubo init --dry-run (root={root})")
        for action in actions:
            note = f"  # {action.note}" if action.note else ""
            print(f"{action.kind:>11}  {action.target}{note}")
        return 0
    with activity(writable=True):
        for action in actions:
            _execute(action, subs)
            if action.kind == "notice":
                print(f"warning: {action.note}: {action.target}", file=sys.stderr)
            log(
                "init_action",
                kind=action.kind,
                target=str(action.target),
                note=action.note,
            )
    print(f"bubo initialized under {root}", file=sys.stderr)
    from bubo.update_helper import ensure_registered

    ensure_registered(root)
    if not args.no_agent_config:
        print(
            "next steps: edit config/env.toml (tokens), then run "
            "`bubo doctor` to verify the install",
            file=sys.stderr,
        )
    return 0


@dataclass(frozen=True)
class Check:
    """One diagnostic the ``doctor`` subcommand runs."""

    label: str
    passed: bool
    detail: str = ""


def _check_workspace(root: Path) -> Iterable[Check]:
    for sub in ("config", "var/state", "var/work", "var/log"):
        target = root / sub
        yield Check(
            label=f"workspace: {sub}",
            passed=target.is_dir(),
            detail=str(target),
        )


def _check_env_toml(root: Path) -> Check:
    target = root / "config" / "env.toml"
    return Check(
        label="config: env.toml present",
        passed=target.is_file(),
        detail=f"{target} — run `bubo init` if missing",
    )


def _check_db(root: Path) -> Check:
    return Check(
        label="state: SQLite DB exists",
        passed=paths.DB.is_file(),
        detail=str(paths.DB),
    )


def _check_codex_profile(home: Path | None = None) -> list[Check]:
    home = home or Path.home()
    target = home / ".codex" / "config.toml"
    profile = home / ".codex" / _CODEX_PROFILE_FILE
    if not target.is_file():
        return [
            Check(
                label="codex: ~/.codex/config.toml present",
                passed=False,
                detail=f"{target} missing — run `bubo init` (with agent config)",
            )
        ]
    legacy = _has_legacy_codex_profile(target)
    return [
        Check(
            label=f"codex: no legacy [profiles.{_CODEX_PROFILE}] table in config.toml",
            passed=not legacy,
            detail=(
                f"present — codex --profile {_CODEX_PROFILE} aborts on Codex >= 0.134.0; "
                "re-run `bubo init` to move it"
                if legacy
                else f"{target}"
            ),
        ),
        Check(
            label=f"codex: ~/.codex/{_CODEX_PROFILE_FILE} profile present",
            passed=profile.is_file(),
            detail=(
                f"{profile}"
                if profile.is_file()
                else f"{profile} missing — codex --profile {_CODEX_PROFILE} will abort; "
                "re-run `bubo init`"
            ),
        ),
    ]


def _check_codex_version() -> list[Check]:
    """Fail on a Codex CLI too old to load ``~/.codex/bubo.config.toml``.

    Skipped when ``codex`` is not on PATH (a Claude-only host) or its version
    can't be read.
    """
    codex = shutil.which("codex")
    if codex is None:
        return []
    try:
        out = subprocess.run(
            [codex, "--version"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except OSError, subprocess.TimeoutExpired:
        return []
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    if match is None:
        return []
    version = tuple(int(part) for part in match.groups())
    minimum = ".".join(map(str, _CODEX_MIN_VERSION))
    ok = version >= _CODEX_MIN_VERSION
    return [
        Check(
            label=f"codex: CLI >= {minimum}",
            passed=ok,
            detail=(
                f"{match.group(0)}"
                if ok
                else f"{match.group(0)} can't load ~/.codex/{_CODEX_PROFILE_FILE} "
                "(config profile `bubo` not found); `npm install -g @openai/codex`"
            ),
        )
    ]


def cmd_doctor(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else paths.ROOT
    _retarget_paths(root)
    checks: list[Check] = []
    checks.extend(_check_workspace(root))
    checks.append(_check_env_toml(root))
    checks.append(_check_db(root))
    if not args.no_agent_config:
        checks.extend(_check_codex_profile())
        checks.extend(_check_codex_version())
    failures = [c for c in checks if not c.passed]
    for check in checks:
        mark = "OK  " if check.passed else "FAIL"
        print(f"{mark}  {check.label}  ({check.detail})")
    if failures:
        print(f"\n{len(failures)} check(s) failed.", file=sys.stderr)
        return 1
    print("\nall checks passed", file=sys.stderr)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """Build and print the governance report — read-only, never inits the DB.

    Mirrors :func:`cmd_doctor`'s ``--root`` handling (``_retarget_paths`` so
    the readers point at the right SQLite DB) but does not set
    ``BUBO_ROOT`` or call ``init_db``: reporting must stay non-mutating.
    Builds the nested report via :mod:`bubo.report`, then emits JSON
    (default) or a single CSV section to stdout. On a missing or
    uninitialized DB the readers raise ``sqlite3.OperationalError`` /
    ``FileNotFoundError``; we turn those into a clear stderr message and a
    non-zero exit instead of a traceback.
    """
    # Deferred import: bubo.report is written in parallel and may be absent
    # while init/doctor/the MCP tools must keep importing this module.
    from bubo import report

    root = Path(args.root) if args.root else paths.ROOT
    _retarget_paths(root)
    with activity():
        try:
            rep = report.build_report(
                since_hours=args.since_hours,
                since=args.since,
                until=args.until,
                project=args.project,
                limit=args.limit,
            )
            output = (
                report.to_csv(rep, section=args.section)
                if args.format == "csv"
                else report.to_json(rep)
            )
        except (sqlite3.OperationalError, FileNotFoundError) as exc:
            print(
                f"bubo report: cannot read state DB at {paths.DB} ({exc}); run `bubo init` first",
                file=sys.stderr,
            )
            return 1
        except ValueError as exc:
            # Bad --since/--until (unparseable date) or --section (not CSV-renderable).
            print(f"bubo report: {exc}", file=sys.stderr)
            return 2
        print(output)
        return 0


def cmd_ui_serve(args: argparse.Namespace) -> int:
    """Serve the local dashboard while retaining safe package ownership."""
    from bubo.ui_server import serve

    if getattr(args, "root", None) is not None:
        _retarget_paths(args.root)
    with activity():
        return serve(_asset("ui").joinpath("index.html").read_bytes(), port=args.port)


def cmd_ui_export(args: argparse.Namespace) -> int:
    """Write a self-contained static operator-UI bundle — read-only.

    Builds ``data.json`` from :func:`bubo.ui_export.build_data` (report +
    recent reviews + config schema + version) and copies the bundled static
    SPA next to it, so ``--out`` is a directory you can open directly
    (``file://``), host on GitHub Pages / S3, or drop in an iframe. No server,
    no network, no posting.

    Mirrors :func:`cmd_report`'s ``--root`` handling (``_retarget_paths`` so
    the readers point at the right DB) and stays strictly non-mutating: it
    never sets ``BUBO_ROOT`` or calls ``init_db``, and a missing DB yields a
    valid empty ``data.json`` rather than an error. The SPA reads ``data.json``
    via ``fetch`` when served, and falls back to an inlined
    ``window.__BUBO_DATA__`` global on ``file://`` (where ``fetch`` is blocked).
    """
    from bubo import report, ui_export

    root = Path(args.root) if args.root else paths.ROOT
    _retarget_paths(root)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    with activity():
        try:
            data = ui_export.build_data()
        except OSError as exc:
            # An EXISTING DB file that cannot be READ (permission denied, unreadable
            # mount) raises OSError from the read-only snapshot copy. Surfacing this
            # is deliberate: silently writing an empty data.json would hide real
            # data in an auditable tool.
            print(
                f"bubo ui-export: cannot read state DB at {paths.DB} ({exc}); "
                "check permissions / mount — nothing was written",
                file=sys.stderr,
            )
            return 1
        data_json = report.to_json(data)
        (out_dir / "data.json").write_text(data_json)
        # file:// fallback: browsers block fetch() of a sibling file under the
        # file: scheme, so also inline the same bytes as a global the SPA reads
        # when fetch fails. Written as a JS assignment, not JSON.
        (out_dir / "data.js").write_text(f"window.__BUBO_DATA__ = {data_json};\n")

        try:
            spa = _asset("ui")
        except FileNotFoundError:
            print(
                "bubo ui-export: built UI assets not found — wrote data.json only. "
                "Build the SPA with `npm --prefix ui install && npm --prefix ui run build` "
                "and re-run, or install a release wheel (which ships the assets).",
                file=sys.stderr,
            )
            print(f"bubo ui-export: wrote {out_dir / 'data.json'}", file=sys.stderr)
            return 0

        # Copy every built asset (index.html + assets/) next to data.json so the
        # directory is self-contained and openable. Reuses the same Traversable
        # copy the init runtime-copies use.
        for entry in spa.iterdir():
            dest = out_dir / entry.name
            if entry.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                _copy_traversable(entry, dest)
            else:
                dest.write_bytes(entry.read_bytes())

        print(
            f"bubo ui-export: wrote static UI to {out_dir} — open {out_dir / 'index.html'}",
            file=sys.stderr,
        )
        return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def cmd_update(args: argparse.Namespace) -> int:
    """The control dispatcher must not hold a shared activity lease."""
    from bubo.update_helper import cli_command

    root = Path(args.root) if args.root is not None else paths.ROOT
    if (args.container or args.docker_socket) and args.root is None:
        print("Docker update configuration requires an explicit --root host path.")
        return 1
    return cli_command(
        root,
        args.update_action or "update",
        args.attempt_id,
        container=args.container,
        docker_socket=args.docker_socket,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bubo",
        description="Install and verify the bubo runtime on this host.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    update_p = subparsers.add_parser(
        "update", help="Update safely, even when automatic updates are off."
    )
    update_p.add_argument("--root", type=Path, default=None, help="Bubo runtime root")
    update_p.add_argument("--container", help="Docker container name or ID for host setup")
    update_p.add_argument("--docker-socket", type=Path, help="Local Docker socket for host setup")
    update_p.add_argument(
        "update_action",
        nargs="?",
        choices=["status", "report", "retry-failures", "configure", "uninstall-helper"],
    )
    update_p.add_argument("attempt_id", nargs="?")
    update_p.set_defaults(func=cmd_update)

    init_p = subparsers.add_parser(
        "init", help="Place runtime assets and initialize state for this host."
    )
    init_p.add_argument(
        "--root",
        type=Path,
        default=None,
        help="install root (default: $BUBO_ROOT or ~/.local/share/bubo)",
    )
    init_p.add_argument(
        "--dry-run",
        action="store_true",
        help="print every action that would run, without touching disk",
    )
    init_p.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing config/env.toml and ~/.codex/config.toml — clobbers local edits",
    )
    init_p.add_argument(
        "--no-agent-config",
        action="store_true",
        help="skip ~/.codex/config.toml and ~/.claude/settings.json writes",
    )
    init_p.set_defaults(func=cmd_init)

    doctor_p = subparsers.add_parser(
        "doctor", help="Verify the install — non-mutating, returns non-zero on any failure."
    )
    doctor_p.add_argument("--root", type=Path, default=None)
    doctor_p.add_argument(
        "--no-agent-config",
        action="store_true",
        help="skip Codex / Claude config checks (hosts that hand-roll agent config)",
    )
    doctor_p.set_defaults(func=cmd_doctor)

    report_p = subparsers.add_parser(
        "report",
        help="Print the governance report — read-only, never mutates state.",
    )
    report_p.add_argument("--root", type=Path, default=None)
    report_p.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="output format (default: json)",
    )
    report_p.add_argument(
        "--section",
        default="audit",
        help="report section to emit as CSV (only used with --format csv; default: audit)",
    )
    report_p.add_argument(
        "--since-hours",
        type=int,
        default=24,
        help="look-back window in hours (default: 24); ignored when --since is given",
    )
    report_p.add_argument(
        "--since",
        default=None,
        help="ISO-8601 start of the reporting window (overrides --since-hours)",
    )
    report_p.add_argument(
        "--until",
        default=None,
        help="ISO-8601 end of the reporting window (default: now)",
    )
    report_p.add_argument(
        "--project",
        default=None,
        help="exact-match project filter (GitLab path-with-namespace or owner/repo)",
    )
    report_p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap the audit trail to the N most recent runs (rollups still cover the full window)",
    )
    report_p.set_defaults(func=cmd_report)

    ui_export_p = subparsers.add_parser(
        "ui-export",
        help="Write a self-contained static operator UI (data.json + SPA) — read-only.",
    )
    ui_export_p.add_argument("--root", type=Path, default=None)
    ui_export_p.add_argument(
        "--out",
        type=Path,
        default=Path("bubo-ui"),
        help="output directory for the static bundle (default: ./bubo-ui)",
    )
    ui_export_p.set_defaults(func=cmd_ui_export)
    ui_serve_p = subparsers.add_parser(
        "ui-serve", help="Open a read-only live local metrics dashboard"
    )
    ui_serve_p.add_argument(
        "--root", type=Path, default=None, help="Bubo root containing config/ and var/"
    )
    ui_serve_p.add_argument(
        "--port", type=int, default=0, help="Loopback port (default: choose a free port)"
    )
    ui_serve_p.set_defaults(func=cmd_ui_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except MaintenanceInProgress as exc:
        print(str(exc), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
