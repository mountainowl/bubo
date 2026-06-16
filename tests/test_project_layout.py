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
    readme = (ROOT / "README.md").read_text()

    assert (ROOT / "config" / "env.example.toml").is_file()
    # Asset guard: every image/doc the README links to must exist on disk, so a
    # docs edit can never leave a broken link. The list tracks what the README
    # *currently references* — #100 dropped the hero render and the
    # stale-branded GitLab MR screenshots, so they are no longer asserted here
    # (those files may still linger in docs/images/ but are not referenced).
    referenced_assets = [
        "docs/images/bubo-avatar-preview.png",
        "docs/examples/README.md",
        "assets/bubo.png",
    ]
    for asset in referenced_assets:
        assert (ROOT / asset).is_file(), f"referenced asset missing on disk: {asset}"
        assert asset in readme, f"README no longer links to asserted asset: {asset}"
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
    assert "api.scorecard.dev/projects/github.com/mountainowl/bubo/badge" in readme
    assert "Run it as a poller beside your" in readme


def test_readme_config_table_groups_sections() -> None:
    config_doc = (ROOT / "docs" / "configuration.md").read_text()

    assert "| Section | Setting | Default | Purpose / impact |" not in config_doc
    assert "<th>Setting</th>" in config_doc
    assert "<th>Default</th>" in config_doc
    assert "<th>Purpose / impact</th>" in config_doc
    assert "<code>[secrets]</code>" not in config_doc
    assert "<code>[agent]</code>" not in config_doc
    for section in (
        "[scm]",
        "[gitlab]",
        "[github]",
        "[review]",
        "[poller]",
        "[agents]",
        "[telemetry]",
        "[[projects]]",
    ):
        assert f'<tr><th colspan="3"><code>{section}</code></th></tr>' in config_doc
    assert "<code>[telemetry.pricing.default]</code>" not in config_doc


def test_readme_links_to_split_docs() -> None:
    readme = (ROOT / "README.md").read_text()
    for doc in (
        "docs/prerequisites.md",
        "docs/install-and-configure.md",
        "docs/run.md",
        "docs/configuration.md",
        "docs/operate.md",
        "docs/telemetry.md",
    ):
        assert (ROOT / doc).is_file(), f"missing split doc: {doc}"
        assert doc in readme, f"README does not link to {doc}"


def test_meta_prompt_includes_concise_review_style_example() -> None:
    prompt = (ROOT / "prompts" / "00-meta.md").read_text()

    assert "<style_examples>" in prompt
    assert "trim is not applied downstream" in prompt
    assert "Remove filler such as" in prompt
    assert "Actual output must still be JSON" in prompt
    assert "Issue (nit" not in prompt
