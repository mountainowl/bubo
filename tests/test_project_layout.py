from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_project_uses_uv_src_layout() -> None:
    pyproject = ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())

    assert (ROOT / "LICENSE").is_file()
    assert data["project"]["license"] == "MIT"
    assert (ROOT / "src" / "bubo" / "poller.py").is_file()
    assert data["tool"]["uv"]["package"] is True
    assert data["project"]["scripts"]["bubo-poller"] == "bubo.poller:main"


def test_project_tree_keeps_config_but_not_runtime_checkouts() -> None:
    assert (ROOT / "config" / "env.example.toml").is_file()
    assert "config/env.toml" in (ROOT / ".gitignore").read_text()
    assert not any(path.name.startswith("secrets.") for path in (ROOT / "config").iterdir())
    assert not (ROOT / "config" / "config.env").exists()
    assert not (ROOT / "config" / "poller.toml").exists()
    assert (ROOT / "prompts" / "00-meta.md").is_file()
    assert (ROOT / "skills" / "code-reviewer" / "SKILL.md").is_file()
    assert not (ROOT / "skills" / "code-review").exists()
    assert not (ROOT / "root" / "usr" / "local" / "llm-code-review").exists()
    assert not any((ROOT / "var").glob("work/**/.git"))


def test_launch_readiness_files_exist() -> None:
    required = [
        "CONTRIBUTING.md",
        "SECURITY.md",
        "SUPPORT.md",
        "CODE_OF_CONDUCT.md",
        ".github/workflows/ci.yml",
        ".github/workflows/scorecard.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
    ]

    missing = [path for path in required if not (ROOT / path).is_file()]
    assert missing == []

    readme = (ROOT / "README.md").read_text()
    # The quickstart in README is the canonical install path. After #22
    # this points at `uv tool install` + `bubo init` rather than
    # the deprecated shell installer. The shell installer reference
    # moves to docs/install-and-configure.md under "Option 3 (deprecated)".
    assert "uv tool install" in readme
    assert "bubo init" in readme
    assert "bubo doctor" in readme
    assert "actions/workflows/ci.yml/badge.svg" in readme
    assert "https://mountainowl.github.io/bubo/recipes/" in readme
    assert "/install-and-configure/" not in readme
    # Scorecard badge via shields' direct endpoint — the api.scorecard.dev
    # URL 302-redirects, which GitHub's image proxy renders as a broken image.
    assert "img.shields.io/ossf-scorecard/github.com/mountainowl/bubo" in readme

    preview_source = (ROOT / "assets" / "social-preview.html").read_text()
    assert "github.com/mountainowl/bubo" in preview_source
    assert "mountainowl/ai-code-review" not in preview_source
    assert (ROOT / "assets" / "social-preview.png").stat().st_size > 0


def test_docs_site_present() -> None:
    # The mkdocs split docs were replaced by the Nextra site under docs/,
    # published to GitHub Pages via .github/workflows/deploy-docs.yml.
    docs = ROOT / "docs"
    assert (docs / "package.json").is_file()
    assert (docs / "next.config.mjs").is_file()
    assert (docs / "theme.config.tsx").is_file()
    for page in ("configuration", "operate", "telemetry", "troubleshooting", "mcp"):
        assert (docs / "pages" / f"{page}.mdx").is_file(), f"missing docs page: {page}.mdx"
    # the old mkdocs layout is gone
    assert not (docs / "configuration.md").exists()
    assert not (ROOT / "mkdocs.yml").exists()

    next_config = (docs / "next.config.mjs").read_text()
    theme_config = (docs / "theme.config.tsx").read_text()
    deploy_workflow = (ROOT / ".github" / "workflows" / "deploy-docs.yml").read_text()
    assert "../pyproject.toml" in next_config
    assert "NEXT_PUBLIC_BUBO_VERSION" in next_config
    assert "NEXT_PUBLIC_BUBO_VERSION" in theme_config
    assert "v0.24.2" not in theme_config
    assert "pyproject.toml" in deploy_workflow


def test_meta_prompt_includes_concise_review_style_example() -> None:
    prompt = (ROOT / "prompts" / "00-meta.md").read_text()

    assert "<style_examples>" in prompt
    assert "trim is not applied downstream" in prompt
    assert "Remove filler such as" in prompt
    assert "Actual output must still be JSON" in prompt
    assert "Issue (nit" not in prompt
