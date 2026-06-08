"""Tests for the ``bubo`` CLI introduced in #22.

Covers:

* ``plan_init`` produces the expected workspace + env-seed + runtime-copy +
  agent-config actions for both fresh and existing roots.
* ``cmd_init --dry-run`` exits 0 without touching the filesystem.
* A real ``cmd_init`` against an empty tmpdir writes env.toml, copies
  prompts/skills/plugins, initializes the SQLite DB, and is idempotent
  on re-run.
* ``cmd_doctor`` returns 0 on a successful install and non-zero on
  missing env.toml / missing DB / missing Codex profile.
* ``_asset`` resolves both via packaged ``bubo._assets`` (when
  installed as a wheel) and via the editable-install fallback (this
  test suite hits the fallback path because ``uv sync --dev`` is
  editable).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bubo import cli, paths


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Re-target paths so cli/init_db writes under tmp_path, not $HOME."""
    monkeypatch.setenv("BUBO_ROOT", str(tmp_path))
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    state = tmp_path / "var"
    monkeypatch.setattr(paths, "CONFIG", tmp_path / "config" / "env.toml")
    monkeypatch.setattr(paths, "DB", state / "state" / "reviewer.sqlite")
    monkeypatch.setattr(paths, "WORK", state / "work")
    monkeypatch.setattr(paths, "REPORTS", state / "reports")
    monkeypatch.setattr(paths, "JOBS", state / "jobs")
    monkeypatch.setattr(paths, "LOGS", state / "log")
    monkeypatch.setattr(paths, "RENDERED_PROMPTS", state / "rendered-prompts")
    return tmp_path


# ---------------------------------------------------------------------------
# plan_init shape
# ---------------------------------------------------------------------------


def test_plan_init_includes_workspace_env_runtime_and_db(tmp_path: Path) -> None:
    actions = cli.plan_init(tmp_path, force=False, install_agent_config=False)
    kinds = [a.kind for a in actions]

    # All the workspace dirs.
    assert kinds.count("mkdir") >= 6
    # env.toml seed (fresh root has no existing env.toml).
    assert any(a.target.name == "env.toml" and a.kind == "write_file" for a in actions)
    # The three runtime copies.
    copy_targets = {a.target.name for a in actions if a.kind == "copy_tree"}
    assert copy_targets == {"prompts", "skills", "plugins"}
    # DB init runs last.
    assert actions[-1].kind == "init_db"


def test_plan_init_skips_existing_env_toml_without_force(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "env.toml").write_text("# operator's hand-edited config\n")

    actions = cli.plan_init(tmp_path, force=False, install_agent_config=False)
    env_actions = [a for a in actions if a.target.name == "env.toml"]

    assert len(env_actions) == 1
    assert env_actions[0].kind == "skip"


def test_plan_init_overwrites_env_toml_with_force(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "env.toml").write_text("# operator's hand-edited config\n")

    actions = cli.plan_init(tmp_path, force=True, install_agent_config=False)
    env_actions = [a for a in actions if a.target.name == "env.toml"]

    assert len(env_actions) == 1
    assert env_actions[0].kind == "write_file"


def test_plan_init_with_agent_config_adds_codex_and_claude(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()

    actions = cli.plan_init(tmp_path, force=False, install_agent_config=True, home=home)
    targets = {str(a.target) for a in actions}

    assert str(home / ".codex" / "config.toml") in targets
    assert str(home / ".claude" / "settings.json") in targets
    assert str(home / ".codex" / "skills" / "code-reviewer") in targets


# ---------------------------------------------------------------------------
# cmd_init — dry run is a true no-op
# ---------------------------------------------------------------------------


def test_cmd_init_dry_run_does_not_touch_filesystem(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = cli.build_parser().parse_args(
        ["init", "--root", str(tmp_path), "--dry-run", "--no-agent-config"]
    )

    rc = cli.cmd_init(args)
    captured = capsys.readouterr()

    assert rc == 0
    # No directories or files were created.
    assert list(tmp_path.iterdir()) == []
    # Every planned action was printed.
    assert "mkdir" in captured.out
    assert "write_file" in captured.out
    assert "copy_tree" in captured.out
    assert "init_db" in captured.out


# ---------------------------------------------------------------------------
# cmd_init — real run is idempotent
# ---------------------------------------------------------------------------


def test_cmd_init_real_run_creates_full_workspace(isolated_root: Path) -> None:
    args = cli.build_parser().parse_args(
        ["init", "--root", str(isolated_root), "--no-agent-config"]
    )

    rc = cli.cmd_init(args)

    assert rc == 0
    assert (isolated_root / "config" / "env.toml").is_file()
    assert (isolated_root / "var" / "state").is_dir()
    assert (isolated_root / "var" / "state" / "reviewer.sqlite").is_file()
    assert (isolated_root / "prompts" / "00-meta.md").is_file()
    assert (isolated_root / "skills" / "code-reviewer" / "SKILL.md").is_file()
    assert (isolated_root / "plugins" / "superpowers").is_dir()


def test_cmd_init_renders_deploy_templates_with_root_substituted(
    isolated_root: Path,
) -> None:
    # docs/operate.md tells operators to `sudo install` the cron + systemd
    # files from $ROOT/deploy/templates/. The CLI must materialize all
    # three templates and substitute {{ROOT}} wherever it appears, so
    # the rendered files are ready for `sudo install` / `systemctl
    # enable` without further hand-editing.
    args = cli.build_parser().parse_args(
        ["init", "--root", str(isolated_root), "--no-agent-config"]
    )

    cli.cmd_init(args)

    for name in ("bubo.cron", "bubo.service", "bubo.timer"):
        rendered = (isolated_root / "deploy" / "templates" / name).read_text()
        assert "{{ROOT}}" not in rendered, f"{name} kept unrendered placeholder"

    # cron + service reference ROOT (paths to bin/log dirs); timer
    # references only the systemd unit name and has no ROOT to render.
    cron = (isolated_root / "deploy" / "templates" / "bubo.cron").read_text()
    service = (isolated_root / "deploy" / "templates" / "bubo.service").read_text()
    assert str(isolated_root) in cron
    assert str(isolated_root) in service


def test_cmd_init_is_idempotent_on_rerun(isolated_root: Path) -> None:
    args = cli.build_parser().parse_args(
        ["init", "--root", str(isolated_root), "--no-agent-config"]
    )

    cli.cmd_init(args)
    # Mutate env.toml as an operator would; second run must NOT clobber it
    # without --force.
    operator_marker = "# OPERATOR EDIT — do not clobber\n"
    env_toml = isolated_root / "config" / "env.toml"
    env_toml.write_text(operator_marker + env_toml.read_text())

    rc2 = cli.cmd_init(args)

    assert rc2 == 0
    assert env_toml.read_text().startswith(operator_marker)


def test_cmd_init_force_overwrites_operator_edits(isolated_root: Path) -> None:
    cli.cmd_init(
        cli.build_parser().parse_args(["init", "--root", str(isolated_root), "--no-agent-config"])
    )
    env_toml = isolated_root / "config" / "env.toml"
    env_toml.write_text("# operator edit\n")

    cli.cmd_init(
        cli.build_parser().parse_args(
            ["init", "--root", str(isolated_root), "--no-agent-config", "--force"]
        )
    )

    assert "# operator edit" not in env_toml.read_text()
    # The packaged example always contains [agents] (sanity check the
    # template flowed through).
    assert "[agents]" in env_toml.read_text()


# ---------------------------------------------------------------------------
# Agent config — substitutes ROOT and points at the right skills dir
# ---------------------------------------------------------------------------


def test_cmd_init_agent_config_writes_codex_with_profile(
    isolated_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cli.cmd_init(cli.build_parser().parse_args(["init", "--root", str(isolated_root)]))

    codex_config = (fake_home / ".codex" / "config.toml").read_text()
    # The load-bearing block — the v0.5.0 incident's root cause was this
    # being absent.
    assert "[profiles.bubo]" in codex_config
    # ROOT placeholder substituted with the actual root.
    assert "{{ROOT}}" not in codex_config
    assert str(isolated_root) in codex_config


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def test_cmd_doctor_passes_after_init(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.cmd_init(
        cli.build_parser().parse_args(["init", "--root", str(isolated_root), "--no-agent-config"])
    )
    capsys.readouterr()  # drain init output

    rc = cli.cmd_doctor(
        cli.build_parser().parse_args(["doctor", "--root", str(isolated_root), "--no-agent-config"])
    )

    assert rc == 0


def test_cmd_doctor_fails_when_env_toml_missing(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Fresh root — no init run.
    (isolated_root / "config").mkdir()
    (isolated_root / "var" / "state").mkdir(parents=True)

    rc = cli.cmd_doctor(
        cli.build_parser().parse_args(["doctor", "--root", str(isolated_root), "--no-agent-config"])
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL" in captured.out


def test_cmd_doctor_flags_missing_codex_profile_block(
    isolated_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    (fake_home / ".codex").mkdir(parents=True)
    # Codex config exists but DOES NOT contain [profiles.bubo]
    # — the exact shape that caused the v0.5.0 incident.
    (fake_home / ".codex" / "config.toml").write_text("[history]\npersistence = 'none'\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cli.cmd_init(
        cli.build_parser().parse_args(["init", "--root", str(isolated_root), "--no-agent-config"])
    )

    rc = cli.cmd_doctor(cli.build_parser().parse_args(["doctor", "--root", str(isolated_root)]))

    assert rc == 1  # Codex profile block missing must fail doctor.


# ---------------------------------------------------------------------------
# Asset resolution — editable fallback
# ---------------------------------------------------------------------------


def test_asset_resolves_packaged_template() -> None:
    # In the editable install the fallback resolves to deploy/templates/codex-config.toml.
    # In a wheel install it resolves to bubo/_assets/codex-config.toml.
    # Either way the content must contain the [profiles.bubo] block
    # — the bug that motivated #20 was this block being absent.
    src = cli._asset("codex-config.toml")
    assert "[profiles.bubo]" in src.read_text()


def test_asset_fallback_finds_prompts_tree() -> None:
    src = cli._asset("prompts")
    children = {entry.name for entry in src.iterdir()}
    assert "00-meta.md" in children


def test_asset_raises_on_unknown_part() -> None:
    with pytest.raises(FileNotFoundError):
        cli._asset("does-not-exist")


# ---------------------------------------------------------------------------
# Bridge to existing DB
# ---------------------------------------------------------------------------


def test_cmd_init_db_has_review_findings_table(isolated_root: Path) -> None:
    cli.cmd_init(
        cli.build_parser().parse_args(["init", "--root", str(isolated_root), "--no-agent-config"])
    )
    db_path = isolated_root / "var" / "state" / "reviewer.sqlite"

    conn = sqlite3.connect(db_path)
    try:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()

    # init_db is the same call bubo-poller --init-db makes; expect
    # the canonical reviewed_mrs / review_findings tables to exist.
    assert "reviewed_mrs" in names
    assert "review_findings" in names
