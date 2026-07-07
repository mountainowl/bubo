"""TOML loader and runtime-environment exporter.

Two concerns live here:

1. **Loading** ``config/env.toml`` (and the example template) into a Python
   dict via :mod:`tomllib`. The only file format supported is TOML; there
   is no migration path from ``.env`` or YAML.
2. **Exporting** values from the loaded config into the process environment
   so the poller (and the credential redactor) can pick them up without
   re-parsing the TOML themselves.

The shell wrapper :file:`bin/bubo` invokes this module's ``main`` to print
``export`` lines a POSIX shell can `eval`. The Python runtime calls
:func:`apply_runtime_env` directly — both code paths read the same TOML
and produce the same env-var set.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import tomllib
from pathlib import Path
from typing import Any

from bubo.config_values import ConfigError, section
from bubo.errors import describe

# Matches POSIX-style placeholders inside TOML string values:
#   ${VAR}            — required; missing raises ConfigError
#   ${VAR:-default}   — falls back to `default` when VAR is unset/empty
#   $$                — literal `$` (escape)
_ENV_PLACEHOLDER = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

ENV_CONFIG_NAME = "env.toml"

# GitLab credential fanout: one TOML key, multiple env-var names. The provider
# reads the first that is set; the extra aliases keep `redact_secrets` and any
# operator-set environment in sync. Listed once here so exporters and the
# redactor cannot drift.
GITLAB_TOKEN_ENV_NAMES = ("GITLAB_TOKEN", "GITLAB_PERSONAL_ACCESS_TOKEN", "GLAB_TOKEN")

# GitHub credential fanout: the provider reads the first that is set; the
# aliases keep the redactor and any operator-set environment in sync.
GITHUB_TOKEN_ENV_NAMES = (
    "GITHUB_TOKEN",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "GH_TOKEN",
)

# Generic name, always exported. Wrappers that honor it find the key with
# zero per-host configuration.
LLM_API_KEY_ENV_KEYS = ("LLM_API_KEY",)


def env_config_path(root: Path) -> Path:
    """Return the canonical ``config/env.toml`` path under ``root``."""
    return root / "config" / ENV_CONFIG_NAME


def read_env_config(root: Path) -> dict[str, Any]:
    """Convenience wrapper: locate and read ``env.toml`` under ``root``."""
    return read_config_file(env_config_path(root))


def read_config_file(path: Path) -> dict[str, Any]:
    """Read a TOML file from disk and return it as a nested dict.

    String values in the loaded mapping are passed through
    :func:`expand_env_placeholders` so operators can keep secrets out of the
    config file itself:

    .. code-block:: toml

        [gitlab]
        token = "${GITLAB_TOKEN}"              # required; fails fast if missing
        url   = "${GITLAB_URL:-https://gitlab.com}"   # default if unset

    A literal ``$`` is written as ``$$``.

    Raises
    ------
    FileNotFoundError
        If ``path`` does not exist.
    tomllib.TOMLDecodeError
        If the file is malformed.
    ConfigError
        If a required ``${VAR}`` placeholder references an env var that is
        unset (and no ``:-default`` was provided).
    """
    if not path.exists():
        raise FileNotFoundError(
            describe(
                f"missing config: {path}",
                reason="the TOML config file does not exist at this path",
                fix=(
                    "create config/env.toml (copy config/env.example.toml) or point bubo at the "
                    "correct project root so this path resolves."
                ),
            )
        )
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    expanded = _expand_placeholders_in_mapping(raw, env=os.environ)
    # Top-level TOML is always a table, so the recursive walk returns a dict.
    return expanded if isinstance(expanded, dict) else {}


def expand_env_placeholders(value: str, env: dict[str, str] | os._Environ[str]) -> str:
    """Replace ``${VAR}`` / ``${VAR:-default}`` placeholders in ``value``.

    Used by :func:`read_config_file` to keep secrets out of the on-disk TOML.
    Exposed so tests can exercise the expansion without a temp file.

    Raises :class:`ConfigError` when a required placeholder has no
    corresponding environment variable.
    """

    def _replace(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        var_name = match.group(1)
        default = match.group(2)
        resolved = env.get(var_name, "")
        if not resolved:
            if default is None:
                raise ConfigError(
                    describe(
                        f"config references env var ${{{var_name}}} but it is unset",
                        reason=(
                            f"a required ${{{var_name}}} placeholder in config/env.toml has no "
                            "matching environment variable and no :-default fallback"
                        ),
                        fix=(
                            f"export {var_name} in the environment bubo runs in, or give the "
                            f"placeholder a default via ${{{var_name}:-...}} in config/env.toml."
                        ),
                    )
                )
            return default
        return resolved

    return _ENV_PLACEHOLDER.sub(_replace, value)


def _expand_placeholders_in_mapping(value: Any, *, env: dict[str, str] | os._Environ[str]) -> Any:
    """Recursively expand env-var placeholders in every string value.

    Walks nested dicts and lists. Non-string scalars (bool, int, float) pass
    through unchanged so TOML numeric/boolean fields keep their types.
    """
    if isinstance(value, str):
        return expand_env_placeholders(value, env)
    if isinstance(value, dict):
        return {key: _expand_placeholders_in_mapping(item, env=env) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_placeholders_in_mapping(item, env=env) for item in value]
    return value


def runtime_env(root: Path, cfg: dict[str, Any]) -> dict[str, str]:
    """Compute the env-var dict that the runtime should export.

    Pure function: ``root`` and ``cfg`` are the only inputs. The result is
    sourced into the shell wrapper via :func:`shell_exports` and applied to
    ``os.environ`` via :func:`apply_runtime_env`. Both paths see the same
    set of variables.
    """
    poller = section(cfg, "poller")
    agent = section(cfg, "agents")
    gitlab = section(cfg, "gitlab")
    github = section(cfg, "github")
    mcp_srv = section(cfg, "mcp_server")

    gitlab_url = str(gitlab.get("url", "https://gitlab.com")).rstrip("/")
    base_dir = _path_value(root, poller.get("state_dir", "var"))
    # Dry-run hint exported as REVIEW_DRY_RUN for the review agent CLI.
    # Independent from `[review].dry_run`, which controls the poller's own
    # posting behavior.
    manual_dry_run = agent.get("dry_run", True)
    exports = {
        "BUBO_ROOT": str(root),
        # BUBO_BASE_DIR is the documented override hook for
        # state_dir; paths.py reads it at import time. Always exported so
        # forked workers inherit the same view.
        "BUBO_BASE_DIR": str(base_dir),
        # Standardized LLM knobs. LLM_MODEL_EFFORT falls back to the deprecated
        # `reasoning_effort` key so existing configs keep working.
        "LLM_MODEL": str(agent.get("llm_model", "gpt-5.5")),
        "LLM_MODEL_EFFORT": str(
            agent.get("llm_model_effort") or agent.get("reasoning_effort") or "medium"
        ),
        "REVIEW_DRY_RUN": _bool_text(manual_dry_run),
        "POLL_INTERVAL_SECONDS": str(int(poller.get("interval_seconds", 900))),
        "CODEX_REVIEW_PROFILE": str(agent.get("codex_profile", "bubo")),
        "CODEX_SANDBOX": str(agent.get("codex_sandbox", "read-only")),
        "GITLAB_API_URL": str(gitlab.get("api_url", f"{gitlab_url}/api/v4")),
        "GITHUB_API_URL": str(github.get("api_url", "https://api.github.com")),
    }
    # Custom OpenAI-compatible endpoint (optional). Its presence is what flips
    # bubo into "base_url mode" (see reviewer_env / the Codex model-provider block).
    if agent.get("llm_base_url"):
        exports["LLM_BASE_URL"] = str(agent["llm_base_url"])
    if gitlab.get("bot_username"):
        exports["BUBO_GITLAB_USERNAME"] = str(gitlab["bot_username"])
    if github.get("bot_username"):
        exports["BUBO_GITHUB_USERNAME"] = str(github["bot_username"])
    # [mcp_server] — controls how `bubo-mcp` exposes itself. stdio
    # is the default (Codex spawns the process per session); set transport
    # to "http" plus a bearer_token for multi-host deployments.
    exports["BUBO_MCP_TRANSPORT"] = str(mcp_srv.get("transport", "stdio"))
    exports["BUBO_MCP_HOST"] = str(mcp_srv.get("host", "127.0.0.1"))
    exports["BUBO_MCP_PORT"] = str(int(mcp_srv.get("port", 8765)))
    if mcp_srv.get("bearer_token"):
        exports["BUBO_MCP_BEARER_TOKEN"] = str(mcp_srv["bearer_token"])
    exports.update(credential_env(cfg))
    return exports


def credential_env(cfg: dict[str, Any]) -> dict[str, str]:
    """Compute credential env vars from ``[gitlab].token`` and ``[agents].llm_api_key``.

    GitLab token is set under three names because each tool reads a
    different variable.

    The LLM key is exported under the generic ``LLM_API_KEY`` and,
    optionally, under one **operator-named** variable from
    ``[agents].llm_api_key_env``. Bubo is model-agnostic — it does NOT
    guess a provider-specific env var name from the model. Operators set
    ``llm_api_key_env`` to whatever their chosen LLM CLI reads
    (``OPENAI_API_KEY`` for OpenAI/Codex, ``ANTHROPIC_API_KEY`` for
    Claude, ``GEMINI_API_KEY`` for Gemini, …). Exactly one name is set,
    so a key never leaks under a provider name the host isn't using.
    """
    gitlab = section(cfg, "gitlab")
    github = section(cfg, "github")
    agent = section(cfg, "agents")
    exports: dict[str, str] = {}
    gitlab_token = gitlab.get("token")
    if gitlab_token:
        token = str(gitlab_token)
        for env_name in GITLAB_TOKEN_ENV_NAMES:
            exports[env_name] = token

    github_token = github.get("token")
    if github_token:
        gh_token = str(github_token)
        for env_name in GITHUB_TOKEN_ENV_NAMES:
            exports[env_name] = gh_token

    llm_api_key = agent.get("llm_api_key")
    if llm_api_key:
        value = str(llm_api_key)
        # Generic name first — every wrapper that honors it falls back here.
        for env_key in LLM_API_KEY_ENV_KEYS:
            exports[env_key] = value
        # Deprecated: `llm_api_key_env` named an extra env var to expose the key
        # under. The agent now authenticates via its own login (set up by
        # `bubo init`), so this is no longer needed — but it is still honored
        # when present so existing configs keep working. Blank = unset.
        key_env = agent.get("llm_api_key_env")
        if isinstance(key_env, str) and key_env.strip():
            exports[key_env.strip()] = value
    return exports


def apply_runtime_env(root: Path, cfg: dict[str, Any]) -> None:
    for key, value in runtime_env(root, cfg).items():
        os.environ.setdefault(key, value)
    if os.environ.get("GITLAB_TOKEN") and not os.environ.get("GITLAB_PERSONAL_ACCESS_TOKEN"):
        os.environ["GITLAB_PERSONAL_ACCESS_TOKEN"] = os.environ["GITLAB_TOKEN"]


def shell_exports(root: Path, cfg: dict[str, Any]) -> str:
    lines = [f"export {key}={shlex.quote(value)}" for key, value in runtime_env(root, cfg).items()]
    lines.append(
        'if [ -n "${GITLAB_TOKEN:-}" ] && [ -z "${GITLAB_PERSONAL_ACCESS_TOKEN:-}" ]; then'
    )
    lines.append('  export GITLAB_PERSONAL_ACCESS_TOKEN="$GITLAB_TOKEN"')
    lines.append("fi")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(shell_exports(args.root.resolve(), read_env_config(args.root.resolve())))
    return 0


def _path_value(root: Path, value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else root / path


def _bool_text(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).lower()


if __name__ == "__main__":
    raise SystemExit(main())
