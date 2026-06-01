"""TOML loader and runtime-environment exporter.

Two concerns live here:

1. **Loading** ``config/env.toml`` (and the example template) into a Python
   dict via :mod:`tomllib`. The only file format supported is TOML; there
   is no migration path from ``.env`` or YAML.
2. **Exporting** values from the loaded config into the process environment
   so child processes (the agent CLI, the GitLab MCP server, the `glab`
   tool, etc.) can pick them up without re-parsing the TOML themselves.

The shell wrapper :file:`bin/env` invokes this module's ``main`` to print
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

from llm_reviewer.config_values import ConfigError, section

# Matches POSIX-style placeholders inside TOML string values:
#   ${VAR}            — required; missing raises ConfigError
#   ${VAR:-default}   — falls back to `default` when VAR is unset/empty
#   $$                — literal `$` (escape)
_ENV_PLACEHOLDER = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

ENV_CONFIG_NAME = "env.toml"

# GitLab credential fanout: one TOML key, multiple env-var names because
# downstream tools (`glab`, GitLab MCP server, our own poller) each read a
# different variable. Listed once here so `redact_secrets` and exporters
# stay in sync.
GITLAB_TOKEN_ENV_NAMES = ("GITLAB_TOKEN", "GITLAB_PERSONAL_ACCESS_TOKEN", "GLAB_TOKEN")

# GitHub credential fanout: `gh` reads GH_TOKEN; the GitHub MCP server and
# most tooling read GITHUB_TOKEN / GITHUB_PERSONAL_ACCESS_TOKEN.
GITHUB_TOKEN_ENV_NAMES = (
    "GITHUB_TOKEN",
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "GH_TOKEN",
)

# Always set. Provider-agnostic — every supported review CLI honors it.
LLM_API_KEY_ENV_KEYS = ("LLM_API_KEY",)

# Provider-specific env var names. Selected at export time by ``llm_model``
# substring matching so a host configured for Anthropic does not leak its
# key into ``OPENAI_API_KEY`` (which the env allowlist would then forward
# wholesale into a Codex/Claude subprocess).
#
# Kept intentionally short: only the providers whose CLI wrappers ship in
# this repo. Add a row here when a new wrapper lands.
_PROVIDER_ENV_BY_MODEL_PREFIX = (
    ("gpt", "OPENAI_API_KEY"),
    ("claude", "ANTHROPIC_API_KEY"),
)


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
        raise FileNotFoundError(f"missing config: {path}")
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
                raise ConfigError(f"config references env var ${{{var_name}}} but it is unset")
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
    prompt = _path_value(root, agent.get("prompt_file", "prompts/00-meta.md"))
    # Manual-wrapper dry-run flag for `code-review-codex`. Independent
    # from `[review].dry_run`, which controls the poller's posting
    # behavior.
    manual_dry_run = agent.get("dry_run", True)
    exports = {
        "LLM_CODE_REVIEW_ROOT": str(root),
        # LLM_CODE_REVIEW_BASE_DIR is the documented override hook for
        # state_dir; paths.py reads it at import time. Always exported so
        # forked workers inherit the same view.
        "LLM_CODE_REVIEW_BASE_DIR": str(base_dir),
        "LLM_CODE_REVIEW_PROMPT": str(prompt),
        "REVIEW_MODEL": str(agent.get("llm_model", "gpt-5.5")),
        "REVIEW_REASONING_EFFORT": str(agent.get("reasoning_effort", "medium")),
        "REVIEW_DRY_RUN": _bool_text(manual_dry_run),
        "POLL_INTERVAL_SECONDS": str(int(poller.get("interval_seconds", 900))),
        "CODEX_REVIEW_PROFILE": str(agent.get("codex_profile", "llm-reviewer")),
        "CODEX_SANDBOX": str(agent.get("codex_sandbox", "read-only")),
        "GITLAB_API_URL": str(gitlab.get("api_url", f"{gitlab_url}/api/v4")),
        "GITLAB_DENIED_TOOLS_REGEX": str(
            gitlab.get("denied_tools_regex", "^(delete_.*|merge_merge_request|push_files)$")
        ),
        "GITHUB_API_URL": str(github.get("api_url", "https://api.github.com")),
    }
    if gitlab.get("bot_username"):
        exports["LLM_REVIEWER_GITLAB_USERNAME"] = str(gitlab["bot_username"])
    if github.get("bot_username"):
        exports["LLM_REVIEWER_GITHUB_USERNAME"] = str(github["bot_username"])
    # [mcp_server] — controls how `mcp-llm-reviewer` exposes itself. stdio
    # is the default (Codex spawns the process per session); set transport
    # to "http" plus a bearer_token for multi-host deployments.
    exports["LLM_REVIEWER_MCP_TRANSPORT"] = str(mcp_srv.get("transport", "stdio"))
    exports["LLM_REVIEWER_MCP_HOST"] = str(mcp_srv.get("host", "127.0.0.1"))
    exports["LLM_REVIEWER_MCP_PORT"] = str(int(mcp_srv.get("port", 8765)))
    if mcp_srv.get("bearer_token"):
        exports["LLM_REVIEWER_MCP_BEARER_TOKEN"] = str(mcp_srv["bearer_token"])
    exports.update(credential_env(cfg))
    return exports


def credential_env(cfg: dict[str, Any]) -> dict[str, str]:
    """Compute credential env vars from ``[gitlab].token`` and ``[agents].llm_api_key``.

    GitLab token is set under three names because each tool reads a
    different variable. The LLM key is set under the **provider-specific**
    name (matched from ``[agents].llm_model``) plus the generic
    ``LLM_API_KEY``, rather than fanned out into every known provider name
    — that earlier behavior leaked an Anthropic key into ``OPENAI_API_KEY``
    on hosts configured for Claude.
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
        # Generic name first — every wrapper falls back to this.
        for env_key in LLM_API_KEY_ENV_KEYS:
            exports[env_key] = value
        # Provider-specific name, selected from llm_model. Skipped when
        # no model is configured — leaving the generic name as the only
        # signal to the wrapper.
        provider_env = _provider_env_for(agent.get("llm_model"))
        if provider_env is not None:
            exports[provider_env] = value
    return exports


def _provider_env_for(model: object) -> str | None:
    """Return the provider-specific env var name for ``model``, or ``None``.

    Matching is by lowercase prefix on the model name. Unknown models fall
    through and only the generic ``LLM_API_KEY`` is exported — safer than
    guessing and leaking the key under multiple names.
    """
    if not isinstance(model, str) or not model:
        return None
    lowered = model.strip().lower()
    for prefix, env_name in _PROVIDER_ENV_BY_MODEL_PREFIX:
        if lowered.startswith(prefix):
            return env_name
    return None


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
