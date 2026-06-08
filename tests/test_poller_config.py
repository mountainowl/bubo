from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from bubo.poller import normalize_config
from bubo.review_config import load_review_config

ROOT = Path(__file__).resolve().parents[1]


def test_default_poller_uses_only_sample_repos() -> None:
    config = tomllib.loads((ROOT / "config" / "env.example.toml").read_text())
    projects = {item["path"]: item.get("enabled", True) for item in config["projects"]}

    assert projects == {"example/enabled-repo": True, "example/disabled-repo": False}
    assert all(path.startswith("example/") for path in projects)


def test_default_poller_review_limits() -> None:
    config = tomllib.loads((ROOT / "config" / "env.example.toml").read_text())

    assert config["review"]["max_merge_requests_per_poll"] == 8
    assert config["review"]["max_findings_per_merge_request"] == 8


def test_default_runtime_config_is_consolidated_in_env_toml() -> None:
    text = (ROOT / "config" / "env.example.toml").read_text()
    config = tomllib.loads(text)

    assert "runtime" not in config
    assert "secrets" not in config
    assert "agent" not in config
    assert "telemetry.pricing.default" not in text
    assert "post_summary" not in config
    assert config["agents"]["llm_model"] == "gpt-5.5"
    assert config["agents"]["reasoning_effort"] == "medium"
    assert config["poller"]["interval_seconds"] == 900


def test_new_config_names_normalize_to_internal_poller_keys() -> None:
    config = normalize_config(
        {
            "gitlab": {"url": "https://gitlab.example"},
            "review": {
                "dry_run": False,
                "max_merge_requests_per_poll": 11,
                "max_findings_per_merge_request": 7,
                "timeout_seconds": 1234,
            },
            "poller": {"target_merge_request_iid": 42},
            "agents": {"llm_model": "review-model", "reviewer_command": ["reviewer"]},
        }
    )

    assert config.gitlab_url == "https://gitlab.example"
    assert config.dry_run is False
    assert config.max_merge_requests_per_poll == 11
    assert config.max_findings_per_merge_request == 7
    assert config.timeout_seconds == 1234
    assert config.target_merge_request_iid == 42
    assert config.reviewer_command == ["reviewer"]
    assert config.model == "review-model"


def test_env_override_wins_over_toml_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # bubo-gh-poller exports BUBO_PROVIDER=github; it must override
    # whatever [scm].provider the on-disk env.toml declares.
    cfg_file = tmp_path / "env.toml"
    cfg_file.write_text('[scm]\nprovider = "gitlab"\n')
    monkeypatch.setenv("BUBO_PROVIDER", "github")

    assert load_review_config(cfg_file).provider == "github"


def test_toml_provider_used_when_env_override_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_file = tmp_path / "env.toml"
    cfg_file.write_text('[scm]\nprovider = "github"\n')
    monkeypatch.delenv("BUBO_PROVIDER", raising=False)

    assert load_review_config(cfg_file).provider == "github"
