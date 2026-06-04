from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_package_deploy_scripts_exist_and_parse() -> None:
    scripts = [
        ROOT / "scripts" / "install-package.sh",
        ROOT / "scripts" / "deploy-package.sh",
    ]

    for script in scripts:
        assert script.is_file()
        assert script.stat().st_mode & 0o111
        subprocess.run(["sh", "-n", str(script)], check=True)


def test_deployable_tree_contains_all_runtime_assets() -> None:
    required = [
        "bin/mr-review-poller",
        "bin/code-review-codex",
        "bin/gh-review-poller",
        "bin/mcp-upstream-gitlab",
        "bin/mcp-upstream-github",
        "bin/mcp-llm-reviewer",
        "bin/env",
        "config/env.example.toml",
        "deploy/templates/codex-config.toml",
        "deploy/templates/claude-settings.json",
        "prompts/00-meta.md",
        "skills/code-reviewer/SKILL.md",
        "plugins/superpowers/.codex-plugin/plugin.json",
        "pyproject.toml",
        "uv.lock",
    ]

    missing = [path for path in required if not (ROOT / path).exists()]
    assert missing == []
    # The two upstream-MCP wrappers locate the actual MCP server binary on
    # PATH (those names are upstream's, not ours — do not rename them).
    gitlab_wrapper = (ROOT / "bin" / "mcp-upstream-gitlab").read_text()
    assert "command -v mcp-gitlab" in gitlab_wrapper
    assert "command -v gitlab-mcp" in gitlab_wrapper

    github_wrapper = (ROOT / "bin" / "mcp-upstream-github").read_text()
    assert "command -v github-mcp-server" in github_wrapper

    # The reviewer's own MCP server uses the same uv-run launcher pattern as
    # the other entry points.
    own_wrapper = (ROOT / "bin" / "mcp-llm-reviewer").read_text()
    assert "uv run --project" in own_wrapper
    assert "mcp-llm-reviewer" in own_wrapper


def test_deploy_archive_excludes_runtime_noise() -> None:
    deploy = (ROOT / "scripts" / "deploy-package.sh").read_text()
    install = (ROOT / "scripts" / "install-package.sh").read_text()
    env_wrapper = (ROOT / "bin" / "env").read_text()

    for pattern in [
        ".venv",
        ".git",
        "config/env.toml",
        "var/state",
        "var/work",
        "var/log",
        "var/reports",
        "__pycache__",
    ]:
        assert f"--exclude={pattern}" in deploy

    assert 'rm -rf "$ROOT"' not in install
    assert 'find "$ROOT" -mindepth 1 -maxdepth 1 -exec rm -rf {} +' in install
    assert 'PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"' in install
    assert 'PATH="/usr/local/bin:/opt/homebrew/bin:$HOME/.local/bin:$PATH"' in env_wrapper
    assert "preserved_state" in install
    assert "COPYFILE_DISABLE=1 tar" in deploy
    assert "COPYFILE_DISABLE=1 tar" in install
    # The orphaned ~/.codex/llm-reviewer.config.toml is no longer written —
    # the profile lives inline in codex-config.toml under [profiles.llm-reviewer].
    assert "llm-reviewer.config.toml" not in install
    assert "skills/code-review/scripts" not in install
    assert 'skills/code-review"' not in install


def test_install_package_sh_prints_deprecation_warning() -> None:
    install = (ROOT / "scripts" / "install-package.sh").read_text()
    # Required by #22 Phase 3: operators reaching for the shell installer
    # must be redirected to the `uv tool install` + `llm-reviewer init`
    # path. The warning makes the deprecation discoverable without
    # forcing a doc lookup.
    assert "deprecated" in install.lower()
    assert "uv tool install" in install
    assert "llm-reviewer init" in install


def test_cron_template_uses_separate_locks_per_role() -> None:
    cron = (ROOT / "deploy" / "templates" / "llm-reviewer.cron").read_text()
    # All three roles must use distinct flock files. A single shared lock
    # caused a real production incident where the `*/5` health probe held
    # the lock at `:45` and the parallel `*/15` poll silently dropped.
    assert "flock -n" in cron
    for lock in ("poller.lock", "outcome-sync.lock", "health.lock"):
        assert lock in cron, f"cron template must use a dedicated {lock}"


def test_codex_config_carries_llm_reviewer_profile() -> None:
    config = (ROOT / "deploy" / "templates" / "codex-config.toml").read_text()
    # codex_runner.py invokes `codex --profile llm-reviewer`; without a
    # [profiles.llm-reviewer] block in the main config, Codex aborts with
    # "config profile llm-reviewer not found" and every review fails.
    assert "[profiles.llm-reviewer]" in config
    # Sanity-check the keys the wrapper depends on actually exist under
    # that profile (loose check — full validity is exercised by the
    # post-install smoke step in install-package.sh).
    for key in ("model", "approval_policy", "sandbox_mode"):
        assert key in config
    # The orphaned sibling file is gone.
    assert not (ROOT / "deploy" / "templates" / "codex-profile.toml").exists()


def test_deploy_is_not_cron_or_single_host_coupled() -> None:
    paths = [
        ROOT / "README.md",
        *sorted((ROOT / "scripts").glob("*.sh")),
        *sorted((ROOT / "bin").iterdir()),
        *sorted((ROOT / "skills").glob("**/*")),
    ]
    text = "\n".join(path.read_text() for path in paths if path.is_file())

    assert "/etc/cron.d" not in text
    assert "192.168.0.157" not in text
    assert "/usr/local/llm-code-review" not in text
    assert not (ROOT / "deploy" / "etc" / "cron.d" / "llm-code-review-poller").exists()
