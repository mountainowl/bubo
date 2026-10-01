from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deployable_tree_contains_all_runtime_assets() -> None:
    required = [
        "bin/bubo",
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
    # The bin/bubo dispatcher runs bubo's own poller / MCP server via uv. There
    # is no upstream-SCM-MCP path any more — checkout uses git and posting uses
    # the REST API.
    dispatcher = (ROOT / "bin" / "bubo").read_text()
    assert "uv run --project" in dispatcher
    assert "bubo-poller" in dispatcher
    assert "bubo-mcp" in dispatcher
    assert 'service)' in dispatcher
    assert 'poll)' not in dispatcher
    assert "mcp-upstream" not in dispatcher


def test_public_container_wrapper_and_bug_template_use_service_foreground() -> None:
    assert 'CMD ["bubo-poller", "service", "start", "--foreground"]' in (
        ROOT / "Dockerfile"
    ).read_text()
    issue = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text()
    assert "uv run bubo-poller service start --foreground" in issue


def test_legacy_scheduler_assets_are_not_shipped() -> None:
    templates = ROOT / "deploy" / "templates"
    for name in ("bubo.cron", "bubo.service", "bubo.timer"):
        assert not (templates / name).exists()


def test_github_action_surface_is_not_shipped() -> None:
    assert not (ROOT / "action.yml").exists()
    assert not (ROOT / "docs" / "pages" / "github-action.mdx").exists()


def test_codex_config_carries_bubo_profile() -> None:
    config = (ROOT / "deploy" / "templates" / "codex-config.toml").read_text()
    # The default reviewer_command invokes `codex --profile bubo`; without
    # a [profiles.bubo] block in the main config, Codex aborts with
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
