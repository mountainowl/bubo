from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deployable_tree_contains_all_runtime_assets() -> None:
    required = [
        "bin/bubo",
        "config/env.example.toml",
        "deploy/templates/codex-config.toml",
        "deploy/templates/codex-bubo.config.toml",
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
    assert "service)" in dispatcher
    assert "poll)" not in dispatcher
    assert "mcp-upstream" not in dispatcher


def test_public_container_wrapper_and_bug_template_use_service_foreground() -> None:
    assert (
        'CMD ["bubo-poller", "service", "start", "--foreground"]'
        in (ROOT / "Dockerfile").read_text()
    )
    issue = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text()
    assert "uv run bubo-poller service start --foreground" in issue


def test_legacy_scheduler_assets_are_not_shipped() -> None:
    templates = ROOT / "deploy" / "templates"
    for name in ("bubo.cron", "bubo.service", "bubo.timer"):
        assert not (templates / name).exists()


def test_github_action_surface_uses_synchronous_ci_path() -> None:
    action = (ROOT / "action.yml").read_text()

    assert "bubo-poller run-once" in action
    assert "bubo-poller service start" not in action
    assert (ROOT / "docs" / "pages" / "github-action.mdx").is_file()


def test_codex_profile_ships_as_its_own_file() -> None:
    templates = ROOT / "deploy" / "templates"
    config = (templates / "codex-config.toml").read_text()
    profile = (templates / "codex-bubo.config.toml").read_text()
    # The default reviewer_command invokes `codex --profile bubo`. Codex
    # >= 0.134.0 reads that profile from ~/.codex/bubo.config.toml and aborts
    # ("--profile `bubo` cannot be used while config.toml contains legacy
    # [profiles.bubo]") if the main config still carries the table.
    assert not any(line.lstrip().startswith("[profiles") for line in config.splitlines())
    assert not any(line.lstrip().startswith("[") for line in profile.splitlines())
    # Sanity-check the keys the wrapper depends on are top-level profile keys
    # (full validity is exercised when Codex loads the profile at review time).
    for key in ("model", "approval_policy", "sandbox_mode"):
        assert f"\n{key} = " in profile
    # The orphaned sibling file is gone.
    assert not (templates / "codex-profile.toml").exists()
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert (
        '"deploy/templates/codex-bubo.config.toml" = "bubo/_assets/codex-bubo.config.toml"'
        in pyproject
    )


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
