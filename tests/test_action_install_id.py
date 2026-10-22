"""The GitHub Action keeps one anonymous install id across runs.

Each Action run starts from an empty ``runner.temp``. The Action restores
Bubo's random install id with actions/cache, so a repository's runs are one
install, not one install per run. Runs the real ``bubo init`` and identity
code in two fresh roots, carrying over only the paths the Action caches.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _steps() -> list[dict]:
    return yaml.safe_load((ROOT / "action.yml").read_text())["runs"]["steps"]


def _cache_step() -> dict:
    return next(step for step in _steps() if str(step.get("uses", "")).startswith("actions/cache@"))


def _cached_paths(runner_temp: Path) -> list[Path]:
    raw = _cache_step()["with"]["path"].strip().splitlines()
    return [Path(p.replace("${{ runner.temp }}", str(runner_temp))) for p in raw]


def _run(runner_temp: Path) -> str:
    """One Action run: init a fresh root, then read the analytics install id."""
    root = runner_temp / "bubo"
    env = {k: v for k, v in os.environ.items() if not k.startswith("BUBO_")}
    env.update(BUBO_ROOT=str(root), HOME=str(runner_temp / "home"))
    bubo = Path(sys.executable).with_name("bubo")
    subprocess.run(
        [str(bubo), "init", "--root", str(root), "--no-agent-config"],
        env=env,
        check=True,
        capture_output=True,
    )
    probe = "from bubo import analytics; print(analytics.install_id())"
    out = subprocess.run(
        [sys.executable, "-c", probe], env=env, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def test_cache_is_restored_before_bubo_starts_and_keyed_per_repository() -> None:
    names = [step.get("name") for step in _steps()]
    cache = _cache_step()
    assert names.index(cache["name"]) < names.index("Initialize Bubo workspace")
    assert "${{ github.repository_id }}" in cache["with"]["key"]
    # Pinned by commit like every other action in this repository.
    assert len(cache["uses"].split("@", 1)[1]) == 40


def test_two_action_runs_share_one_install_id(tmp_path: Path) -> None:
    first = tmp_path / "run1"
    first_id = _run(first)
    assert first_id
    assert first_id != "None"

    # The next run gets a new, empty runner.temp; actions/cache restores
    # exactly the cached paths into it before Bubo runs.
    second = tmp_path / "run2"
    cached = _cached_paths(first)
    assert first / "bubo" / "var" / "state" / "install_id" in cached
    for source in (path for path in cached if path.exists()):
        target = second / source.relative_to(first)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    assert _run(second) == first_id


def test_without_the_cache_each_run_is_a_new_install(tmp_path: Path) -> None:
    assert _run(tmp_path / "run1") != _run(tmp_path / "run2")
