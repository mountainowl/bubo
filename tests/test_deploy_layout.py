from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deployable_tree_contains_all_runtime_assets() -> None:
    required = [
        "bin/bubo-poller",
        "bin/bubo-codex",
        "bin/bubo-gh-poller",
        "bin/mcp-upstream-gitlab",
        "bin/mcp-upstream-github",
        "bin/bubo-mcp",
        "bin/bubo-env",
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
    own_wrapper = (ROOT / "bin" / "bubo-mcp").read_text()
    assert "uv run --project" in own_wrapper
    assert "bubo-mcp" in own_wrapper


def test_cron_template_uses_separate_locks_per_role() -> None:
    cron = (ROOT / "deploy" / "templates" / "bubo.cron").read_text()
    # All three roles must use distinct flock files. A single shared lock
    # caused a real production incident where the `*/5` health probe held
    # the lock at `:45` and the parallel `*/15` poll silently dropped.
    assert "flock -n" in cron
    for lock in ("poller.lock", "outcome-sync.lock", "health.lock"):
        assert lock in cron, f"cron template must use a dedicated {lock}"


def test_codex_config_carries_bubo_profile() -> None:
    config = (ROOT / "deploy" / "templates" / "codex-config.toml").read_text()
    # codex_runner.py invokes `codex --profile bubo`; without a
    # [profiles.bubo] block in the main config, Codex aborts with
    # "config profile bubo not found" and every review fails.
    assert "[profiles.bubo]" in config
    # Sanity-check the keys the wrapper depends on actually exist under
    # that profile (loose check — full validity is exercised when Codex
    # loads the profile at review time).
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
