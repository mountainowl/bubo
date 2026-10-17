"""Credential policy shared by review orchestration and agent execution."""

from collections.abc import Mapping
from pathlib import Path

from bubo import paths
from bubo.review_config import ReviewConfig

REVIEWER_ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
}


def reviewer_env(
    source: Mapping[str, str], cfg: ReviewConfig | None = None, *, root: Path | None = None
) -> dict[str, str]:
    """Strip credentials except the explicitly configured custom-provider key.

    Provider login files are handled separately by the runner. A configured
    custom endpoint has no login-file flow, so only that opt-in permits
    LLM_API_KEY and LLM_BASE_URL. Source-control credentials are never forwarded.
    """
    env = {key: value for key, value in source.items() if key in REVIEWER_ENV_ALLOWLIST}
    env["BUBO_ROOT"] = str(paths.ROOT if root is None else root)
    if cfg is not None and cfg.llm_base_url:
        for name in ("LLM_API_KEY", "LLM_BASE_URL"):
            if source.get(name):
                env[name] = source[name]
    return env
