from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from bubo import codex_runner
from bubo.config_values import ConfigError
from bubo.env_config import expand_env_placeholders, read_config_file, runtime_env


def test_expand_env_placeholders_required_present() -> None:
    out = expand_env_placeholders("${MY_TOKEN}", {"MY_TOKEN": "secret"})
    assert out == "secret"


def test_expand_env_placeholders_required_missing_raises() -> None:
    with pytest.raises(ConfigError, match="MY_TOKEN"):
        expand_env_placeholders("${MY_TOKEN}", {})


def test_expand_env_placeholders_default_applies_when_missing() -> None:
    out = expand_env_placeholders("${MY_URL:-https://default.example}", {})
    assert out == "https://default.example"


def test_expand_env_placeholders_default_ignored_when_present() -> None:
    out = expand_env_placeholders(
        "${MY_URL:-https://default.example}", {"MY_URL": "https://override.example"}
    )
    assert out == "https://override.example"


def test_expand_env_placeholders_default_used_when_empty() -> None:
    out = expand_env_placeholders("${MY_URL:-fallback}", {"MY_URL": ""})
    assert out == "fallback"


def test_expand_env_placeholders_escapes_dollar() -> None:
    out = expand_env_placeholders("price: $$5", {})
    assert out == "price: $5"


def test_expand_env_placeholders_substring() -> None:
    out = expand_env_placeholders("Bearer ${API_KEY}", {"API_KEY": "abc123"})
    assert out == "Bearer abc123"


def test_read_config_file_interpolates_env_vars(tmp_path: Path) -> None:
    config_file = tmp_path / "env.toml"
    config_file.write_text(
        """
[gitlab]
token = "${GITLAB_TOKEN}"
url = "${GITLAB_URL:-https://gitlab.com}"

[agents]
llm_api_key = "${LLM_API_KEY:-}"
""",
        encoding="utf-8",
    )
    with patch.dict(os.environ, {"GITLAB_TOKEN": "glpat-xyz"}, clear=False):
        os.environ.pop("GITLAB_URL", None)
        os.environ.pop("LLM_API_KEY", None)
        cfg = read_config_file(config_file)

    assert cfg["gitlab"]["token"] == "glpat-xyz"
    assert cfg["gitlab"]["url"] == "https://gitlab.com"
    assert cfg["agents"]["llm_api_key"] == ""


def test_read_config_file_does_not_touch_non_strings(tmp_path: Path) -> None:
    """Numeric/boolean TOML scalars must not be coerced to string by the
    interpolator. Regression guard — the recursive walker must skip them.
    """
    config_file = tmp_path / "env.toml"
    config_file.write_text(
        """
[review]
dry_run = true
max_findings_per_merge_request = 9
""",
        encoding="utf-8",
    )
    cfg = read_config_file(config_file)
    assert cfg["review"]["dry_run"] is True
    assert cfg["review"]["max_findings_per_merge_request"] == 9


def test_runtime_env_exports_from_env_toml() -> None:
    root = Path("/opt/bubo")
    cfg = {
        "gitlab": {
            "api_url": "https://gitlab.example/api/v4",
            "bot_username": "review-bot",
            "denied_tools_regex": "^(delete_.*)$",
            "token": "gitlab-secret",
        },
        "poller": {
            "state_dir": "var",
            "interval_seconds": 123,
        },
        "agents": {
            "prompt_file": "prompts/00-meta.md",
            "llm_model": "gpt-test",
            "llm_api_key": "llm-secret",
            "llm_api_key_env": "OPENAI_API_KEY",
            "reasoning_effort": "high",
            "dry_run": False,
            "codex_profile": "reviewer",
            "codex_sandbox": "read-only",
        },
    }

    env = runtime_env(root, cfg)

    assert env["BUBO_ROOT"] == "/opt/bubo"
    # BUBO_HOME was removed — paths.py never read it and
    # BUBO_ROOT covers the same intent.
    assert "BUBO_HOME" not in env
    assert env["BUBO_BASE_DIR"] == "/opt/bubo/var"
    assert env["BUBO_PROMPT"] == "/opt/bubo/prompts/00-meta.md"
    assert env["REVIEW_MODEL"] == "gpt-test"
    assert env["REVIEW_REASONING_EFFORT"] == "high"
    assert env["REVIEW_DRY_RUN"] == "false"
    assert env["POLL_INTERVAL_SECONDS"] == "123"
    assert env["CODEX_REVIEW_PROFILE"] == "reviewer"
    assert env["CODEX_SANDBOX"] == "read-only"
    assert env["GITLAB_API_URL"] == "https://gitlab.example/api/v4"
    assert env["GITLAB_DENIED_TOOLS_REGEX"] == "^(delete_.*)$"
    assert env["BUBO_GITLAB_USERNAME"] == "review-bot"
    assert env["GITLAB_TOKEN"] == "gitlab-secret"
    assert env["GITLAB_PERSONAL_ACCESS_TOKEN"] == "gitlab-secret"
    assert env["GLAB_TOKEN"] == "gitlab-secret"
    # Generic LLM_API_KEY is always set; the operator-named var
    # (llm_api_key_env = "OPENAI_API_KEY") also gets it. No provider name
    # is inferred from the model, so the others are never exported.
    assert env["LLM_API_KEY"] == "llm-secret"
    assert env["OPENAI_API_KEY"] == "llm-secret"
    assert "ANTHROPIC_API_KEY" not in env
    assert "QWEN_API_KEY" not in env


def test_llm_key_exports_under_operator_named_var() -> None:
    # The operator names the env var their LLM CLI reads — no model-based
    # guessing. Here Anthropic, despite no "claude" anywhere.
    env = runtime_env(
        Path("/opt/bubo"),
        {"agents": {"llm_api_key": "secret", "llm_api_key_env": "ANTHROPIC_API_KEY"}},
    )
    assert env["LLM_API_KEY"] == "secret"
    assert env["ANTHROPIC_API_KEY"] == "secret"
    assert "OPENAI_API_KEY" not in env


def test_llm_key_works_for_any_provider_name() -> None:
    # Provider-agnostic: a name the tool has never heard of still works.
    env = runtime_env(
        Path("/opt/bubo"),
        {"agents": {"llm_api_key": "secret", "llm_api_key_env": "GEMINI_API_KEY"}},
    )
    assert env["GEMINI_API_KEY"] == "secret"


def test_llm_key_without_env_name_only_sets_generic() -> None:
    # No llm_api_key_env -> only the generic LLM_API_KEY, no provider names.
    env = runtime_env(
        Path("/opt/bubo"),
        {"agents": {"llm_api_key": "secret", "llm_model": "custom-model-x"}},
    )
    assert env["LLM_API_KEY"] == "secret"
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_llm_key_blank_env_name_is_ignored() -> None:
    env = runtime_env(
        Path("/opt/bubo"),
        {"agents": {"llm_api_key": "secret", "llm_api_key_env": "  "}},
    )
    assert env["LLM_API_KEY"] == "secret"
    assert len([k for k in env if k.endswith("_API_KEY") and k != "LLM_API_KEY"]) == 0


def test_agents_dry_run_controls_review_dry_run_export() -> None:
    env = runtime_env(
        Path("/opt/bubo"),
        {"agents": {"dry_run": False}},
    )
    assert env["REVIEW_DRY_RUN"] == "false"

    env = runtime_env(
        Path("/opt/bubo"),
        {"agents": {"dry_run": True}},
    )
    assert env["REVIEW_DRY_RUN"] == "true"


def test_empty_tokens_are_not_exported() -> None:
    env = runtime_env(Path("/opt/bubo"), {"gitlab": {"token": ""}, "agents": {"llm_api_key": ""}})

    assert "GITLAB_TOKEN" not in env
    assert "GITLAB_PERSONAL_ACCESS_TOKEN" not in env
    assert "GLAB_TOKEN" not in env
    assert "LLM_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_read_config_file_reads_env_toml_without_overlay(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    env_file = config_dir / "env.toml"
    env_file.write_text(
        """
gitlab_url = "https://gitlab.com"

[[projects]]
path = "example/enabled-repo"
enabled = true
""",
        encoding="utf-8",
    )

    cfg = read_config_file(env_file)

    assert cfg["projects"] == [{"path": "example/enabled-repo", "enabled": True}]


def test_codex_runner_skip_agent_config_env_does_not_export_secrets(tmp_path: Path) -> None:
    env_file = tmp_path / "env.toml"
    env_file.write_text(
        """
[gitlab]
token = "gitlab-secret"

[agents]
llm_api_key = "llm-secret"
""",
        encoding="utf-8",
    )
    original_config = codex_runner.ENV_CONFIG
    try:
        codex_runner.ENV_CONFIG = env_file
        with patch.dict(os.environ, {"BUBO_SKIP_AGENT_CONFIG_ENV": "1"}, clear=True):
            codex_runner.load_runtime_config()

            assert "GITLAB_TOKEN" not in os.environ
            assert "OPENAI_API_KEY" not in os.environ
            assert "LLM_API_KEY" not in os.environ
    finally:
        codex_runner.ENV_CONFIG = original_config
