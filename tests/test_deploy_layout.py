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
        "deploy/templates/codex-profile.toml",
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
    assert "llm-reviewer.config.toml" in install
    assert "skills/code-review/scripts" not in install
    assert 'skills/code-review"' not in install


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
