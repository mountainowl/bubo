"""A ``--root`` retarget reaches every module that reads Bubo's paths (#39)."""

from pathlib import Path

import pytest

from bubo import cli, outcome_classifier, paths, poller, review_config
from bubo.review_config import ReviewConfig


@pytest.fixture
def retargeted(tmp_path: Path):
    saved = {name: getattr(paths, name) for name in dir(paths) if name.isupper()}
    cli._retarget_paths(tmp_path)
    try:
        yield tmp_path
    finally:
        for name, value in saved.items():
            setattr(paths, name, value)


def test_poller_reads_config_from_retargeted_root(retargeted: Path) -> None:
    config = retargeted / "config" / "env.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[scm]\nprovider = "github"\n', encoding="utf-8")
    assert poller.read_config().provider == "github"


def test_reviewer_env_uses_retargeted_root(retargeted: Path) -> None:
    assert poller.reviewer_env({})["BUBO_ROOT"] == str(retargeted)


def test_classifier_env_uses_retargeted_root(retargeted: Path) -> None:
    env = outcome_classifier.classifier_env({}, ReviewConfig())
    assert env["BUBO_ROOT"] == str(retargeted)


def test_no_module_keeps_a_by_value_path_snapshot() -> None:
    for module in (poller, review_config, outcome_classifier):
        assert not hasattr(module, "ROOT"), module.__name__
        assert not hasattr(module, "CONFIG"), module.__name__
