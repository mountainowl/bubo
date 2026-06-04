#!/bin/sh
set -eu

# ---------------------------------------------------------------------------
# DEPRECATED in v0.6.0 (issue #22). To be removed in v0.7.0.
# ---------------------------------------------------------------------------
# Use the new Python install path instead:
#
#     uv tool install git+https://github.com/mountainowl/ai-code-review@<tag>
#     llm-reviewer init --install-agent-config
#     llm-reviewer doctor
#
# `uv tool install` handles dependency resolution and PATH placement;
# `llm-reviewer init` is the Python equivalent of this script. The new
# path uses the same packaged templates and writes them with the same
# {{ROOT}} substitution, but it's cross-platform, idempotent, dry-runnable
# (--dry-run), version-aware, and skips when operator edits exist
# (instead of silently clobbering them).
# ---------------------------------------------------------------------------

cat >&2 <<'WARN'
WARNING: scripts/install-package.sh is deprecated as of v0.6.0 (issue #22)
         and will be removed in v0.7.0. Migrate to:

           uv tool install git+https://github.com/mountainowl/ai-code-review@<tag>
           llm-reviewer init --install-agent-config

         See docs/install-and-configure.md for the full new flow.

WARN

PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"
export PATH

ROOT="${LLM_REVIEWER_INSTALL_ROOT:-${LLM_CODE_REVIEW_ROOT:-$HOME/.local/share/llm-reviewer}}"
SOURCE=""
INSTALL_AGENT_CONFIG=0
USE_SUDO=0

usage() {
    echo "usage: $0 [--source DIR] [--root DIR] [--sudo] [--install-agent-config]" >&2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --source) SOURCE="$2"; shift 2 ;;
        --root) ROOT="$2"; shift 2 ;;
        --sudo) USE_SUDO=1; shift ;;
        --install-agent-config) INSTALL_AGENT_CONFIG=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

if [ -z "$SOURCE" ]; then
    SOURCE="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
fi

command -v uv >/dev/null 2>&1 || { echo "uv is required on target" >&2; exit 2; }

as_root() {
    if [ "$USE_SUDO" -eq 1 ]; then
        sudo "$@"
    else
        "$@"
    fi
}

tmp="$(mktemp -d)"
preserved_env=""
preserved_state=""
trap 'rm -rf "$tmp"; if [ -n "$preserved_env" ]; then rm -f "$preserved_env"; fi; if [ -n "$preserved_state" ]; then rm -rf "$preserved_state"; fi' EXIT

(cd "$SOURCE" && COPYFILE_DISABLE=1 tar \
    --exclude=.venv \
    --exclude=.git \
    --exclude=.pytest_cache \
    --exclude=__pycache__ \
    --exclude=config/env.toml \
    --exclude=var/state \
    --exclude=var/work \
    --exclude=var/log \
    --exclude=var/reports \
    --exclude=var/jobs \
    -cf - .) | tar -xf - -C "$tmp"

if as_root test -f "$ROOT/config/env.toml"; then
    preserved_env="$(mktemp)"
    as_root cp "$ROOT/config/env.toml" "$preserved_env"
fi
if as_root test -d "$ROOT/var/state"; then
    preserved_state="$(mktemp -d)"
    as_root cp -pR "$ROOT/var/state/." "$preserved_state/"
fi
if as_root test -d "$ROOT"; then
    as_root find "$ROOT" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
else
    as_root mkdir -p "$ROOT"
fi
(cd "$tmp" && tar -cf - .) | as_root tar -xf - -C "$ROOT"
if [ -n "$preserved_env" ] && [ -f "$preserved_env" ]; then
    as_root mkdir -p "$ROOT/config"
    as_root cp "$preserved_env" "$ROOT/config/env.toml"
elif [ -f "$ROOT/config/env.example.toml" ]; then
    as_root cp "$ROOT/config/env.example.toml" "$ROOT/config/env.toml"
fi
if [ -n "$preserved_state" ] && [ -d "$preserved_state" ]; then
    as_root mkdir -p "$ROOT/var/state"
    as_root cp -pR "$preserved_state/." "$ROOT/var/state/"
fi
if [ "$USE_SUDO" -eq 1 ]; then
    as_root chown -R "$(id -un):$(id -gn)" "$ROOT"
fi
chmod +x "$ROOT"/bin/* "$ROOT"/scripts/*.sh

mkdir -p "$ROOT/var/state" "$ROOT/var/log" "$ROOT/var/work" "$ROOT/var/reports" "$ROOT/var/jobs"
uv sync --locked --no-dev --project "$ROOT"
LLM_CODE_REVIEW_ROOT="$ROOT" "$ROOT/bin/mr-review-poller" --init-db

if [ "$INSTALL_AGENT_CONFIG" -eq 1 ]; then
    mkdir -p "$HOME/.codex/skills" "$HOME/.claude"
    escaped_root="$(printf '%s' "$ROOT" | sed 's/[&|]/\\&/g')"
    # codex-config.toml now carries the [profiles.llm-reviewer] block
    # inline. The earlier sibling-file pattern under ~/.codex/ never
    # worked — Codex does not auto-load sibling files for `--profile`.
    sed "s|{{ROOT}}|$escaped_root|g" "$ROOT/deploy/templates/codex-config.toml" > "$HOME/.codex/config.toml"
    sed "s|{{ROOT}}|$escaped_root|g" "$ROOT/deploy/templates/claude-settings.json" > "$HOME/.claude/settings.json"
    ln -sfn "$ROOT/skills/code-reviewer" "$HOME/.codex/skills/code-reviewer"
    # Best-effort smoke check: ask Codex to round-trip the profile so a
    # broken config fails the install rather than every later poll cycle.
    # Skipped silently if Codex isn't on PATH or doesn't accept the flags.
    if command -v codex >/dev/null 2>&1; then
        if codex exec --profile llm-reviewer --skip-git-repo-check \
            "Return exactly: profile-ok" 2>/dev/null | grep -q "profile-ok"; then
            echo "codex profile llm-reviewer verified"
        else
            echo "warning: codex --profile llm-reviewer smoke check did not return 'profile-ok'" >&2
            echo "warning: review runs may fail; verify ~/.codex/config.toml" >&2
        fi
    fi
fi

echo "installed llm-reviewer at $ROOT"
