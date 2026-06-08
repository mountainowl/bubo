"""Runtime configuration for the review pipeline.

This module owns the :class:`ReviewConfig` dataclass — the single typed view
of ``config/env.toml`` the rest of the codebase passes around. Loading is
split into two functions so tests can build a config from an in-memory dict
without touching the filesystem:

* :func:`load_review_config` — read ``env.toml`` from disk, apply runtime
  environment-variable exports, and parse the result.
* :func:`review_config_from_dict` — parse an already-loaded mapping.

Defaults live on the dataclass so a partial TOML file (or no TOML at all)
still yields a usable config. Anything that requires an operator decision
(GitLab token, project list) stays empty/None and is enforced at the call
site.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bubo.config_values import (
    ConfigError,
    bool_value,
    confidence_threshold,
    lower_string_list,
    positive_int,
    section,
    text_value,
)
from bubo.env_config import apply_runtime_env, read_config_file
from bubo.paths import DEFAULT_REVIEWER, ROOT
from bubo.telemetry import TelemetryConfig, telemetry_config_from_dict

# Default minimum confidence for posting a finding. Findings with a numeric
# ``confidence`` field below this score are dropped before posting/planning.
# 0.85 means "post when the model is at least 85% confident".
DEFAULT_MIN_CONFIDENCE = 0.85

# Supported source-control providers. Selected via ``[scm].provider``.
SUPPORTED_PROVIDERS = ("gitlab", "github")
DEFAULT_PROVIDER = "gitlab"

# Default body of the change-level comment posted when a review finds nothing.
# Operators override via ``[agents].no_findings_comment_body``. Kept
# short on purpose — the value of the signal is "reviewer ran and was happy",
# not a verbose summary.
DEFAULT_NO_FINDINGS_COMMENT = "Automated review ran — no issues found."


@dataclass(frozen=True, slots=True)
class ReviewConfig:
    """Immutable, fully-typed view of ``config/env.toml`` for the runtime.

    All fields are required by the codebase; defaults are chosen so a fresh
    install with a stub TOML still parses. Mutability is forbidden so a
    partially-populated config cannot drift across threads or fork boundaries
    after :func:`load_review_config` returns.

    Attributes
    ----------
    provider:
        Source-control provider — ``"gitlab"`` (default) or ``"github"``.
        Selects which :mod:`bubo.provider` implementation the
        poller dispatches to. The same ``projects`` list, caps, and
        policy filters apply regardless of provider.
    gitlab_url:
        Web host the poller reads MRs from (GitLab provider). Used as the
        base for REST API calls.
    github_api_url:
        REST API base for the GitHub provider (``https://api.github.com``
        for github.com; ``https://<host>/api/v3`` for GitHub Enterprise).
    dry_run:
        When ``True``, planned findings are stored in SQLite but **no GitLab
        comments are posted**. Safe-by-default for a new install.
    max_merge_requests_per_poll:
        Cap on MRs queued per single poll cycle. Higher values fan out more
        workers in parallel. (Field name matches the TOML key exactly.)
    max_findings_per_merge_request:
        Cap on findings accepted from a single review. Also substituted into
        the rendered meta prompt as ``{{MAX_FINDINGS_PER_REVIEW}}`` so the
        agent itself stops once the cap is hit. (Field name matches the
        TOML key exactly.)
    timeout_seconds:
        Per-MR worker timeout. The worker subprocess is killed if it exceeds
        this. (Field name matches the TOML key exactly.)
    target_merge_request_iid:
        If set, the poller only reviews this single MR IID. Intended for
        manual debugging. (Field name matches the TOML key exactly.)
    reviewer_command:
        Argv prefix for the review subprocess (typically
        ``["bin/bubo-codex"]``).
    model:
        Model identifier used for cost-attribution metric labels.
    post_summary:
        Reserved for a future "post one summary comment per MR" path.
    telemetry_config:
        Parsed :class:`TelemetryConfig` block.
    projects:
        List of GitLab project paths the poller should scan, with disabled
        entries already filtered out.
    min_confidence:
        Confidence threshold (0.0-1.0). Findings with ``finding.confidence``
        below this value are dropped before posting/planning. Defaults to
        :data:`DEFAULT_MIN_CONFIDENCE`.
    allowed_kinds:
        Lowercase whitelist of finding kinds that are allowed through. A
        finding is kept if **any** of its ``severity``, ``category``, or
        ``type`` fields appear in this list. An empty list means
        "post everything that meets ``min_confidence``" — the common default.
    post_no_findings_comment:
        When ``True`` (the default), the poller posts a single change-level
        comment after a review completes with zero actionable findings, so
        authors and approvers can tell "reviewer ran and was happy" apart
        from "reviewer never ran." Set ``False`` to restore the previous
        silent-on-no-findings behavior. Respects ``dry_run`` — no comment
        is posted while ``dry_run`` is ``True``.
    no_findings_comment_body:
        Body of the no-findings comment, posted verbatim. Operators can
        customize it for localization or branding. Do NOT embed per-run
        values (URLs, timestamps, model names) — the body must be byte-
        identical across re-reviews of the same MR/PR for the idempotent
        dedup to work; a per-run-varying body would stack a new comment
        every poll. Falls back to :data:`DEFAULT_NO_FINDINGS_COMMENT` when
        unset; an empty or whitespace-only value disables the post even
        when ``post_no_findings_comment`` is ``True``.
    """

    provider: str = DEFAULT_PROVIDER
    gitlab_url: str = "https://gitlab.com"
    github_api_url: str = "https://api.github.com"
    dry_run: bool = True
    max_merge_requests_per_poll: int = 5
    max_findings_per_merge_request: int = 5
    timeout_seconds: int = 1800
    target_merge_request_iid: int | None = None
    reviewer_command: list[str] = field(default_factory=lambda: [str(DEFAULT_REVIEWER)])
    model: str | None = None
    post_summary: bool = False
    telemetry_config: TelemetryConfig = field(default_factory=TelemetryConfig)
    projects: list[str] = field(default_factory=list)
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    allowed_kinds: list[str] = field(default_factory=list)
    post_no_findings_comment: bool = True
    no_findings_comment_body: str = DEFAULT_NO_FINDINGS_COMMENT


def load_review_config(
    config_path: Path, log_event: Callable[..., None] | None = None
) -> ReviewConfig:
    """Read ``config_path``, export runtime env vars, return a :class:`ReviewConfig`.

    ``log_event`` is the structured-logging callable from
    :mod:`bubo.poller`; passing it lets configuration warnings
    (currently only "telemetry disabled because parse failed") show up in the
    same JSON-line stream as the rest of the runtime events.

    The ``BUBO_PROVIDER`` environment variable overrides
    ``[scm].provider`` from the file. This is how the ``bubo-gh-poller``
    entry point forces ``provider = "github"`` without requiring the operator
    to edit ``env.toml`` — the override is applied to the parsed mapping
    before :func:`review_config_from_dict` validates it.

    Raises :class:`ConfigError` if the file is missing or any value is out of
    range.
    """
    if not config_path.exists():
        raise ConfigError(f"missing config: {config_path}")
    raw = read_config_file(config_path)
    apply_runtime_env(ROOT, raw)
    override = os.environ.get("BUBO_PROVIDER")
    if override:
        raw.setdefault("scm", {})["provider"] = override
    return review_config_from_dict(raw, log_event=log_event)


def review_config_from_dict(
    raw: dict[str, Any], log_event: Callable[..., None] | None = None
) -> ReviewConfig:
    """Parse an already-loaded TOML mapping into :class:`ReviewConfig`.

    Telemetry parsing is wrapped in a try/except because a malformed
    ``[telemetry]`` block should *not* prevent the rest of the poller from
    running — it just falls back to disabled telemetry and logs a warning
    when ``log_event`` is provided.
    """
    scm = section(raw, "scm")
    gitlab = section(raw, "gitlab")
    github = section(raw, "github")
    review = section(raw, "review")
    poller = section(raw, "poller")
    agent = section(raw, "agents")

    provider = str(scm.get("provider", DEFAULT_PROVIDER)).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ConfigError(f"[scm].provider must be one of {SUPPORTED_PROVIDERS}, got {provider!r}")

    try:
        telemetry_config = telemetry_config_from_dict(raw)
    except (TypeError, ValueError) as exc:
        telemetry_config = TelemetryConfig(enabled=False)
        if log_event is not None:
            log_event("telemetry_config_disabled", error=str(exc))

    target_iid = poller.get("target_merge_request_iid")
    return ReviewConfig(
        provider=provider,
        gitlab_url=str(gitlab.get("url", "https://gitlab.com")),
        github_api_url=str(github.get("api_url", "https://api.github.com")),
        dry_run=bool(review.get("dry_run", True)),
        # Field names below match the TOML keys exactly — single vocabulary
        # for operators (TOML) and programmers (Python).
        max_merge_requests_per_poll=positive_int(
            review.get("max_merge_requests_per_poll", 5),
            "max_merge_requests_per_poll",
        ),
        max_findings_per_merge_request=positive_int(
            review.get("max_findings_per_merge_request", 5),
            "max_findings_per_merge_request",
        ),
        timeout_seconds=positive_int(review.get("timeout_seconds", 1800), "timeout_seconds"),
        target_merge_request_iid=int(target_iid) if target_iid is not None else None,
        reviewer_command=[
            str(item) for item in agent.get("reviewer_command", [str(DEFAULT_REVIEWER)])
        ],
        model=str(agent["llm_model"]) if agent.get("llm_model") else None,
        telemetry_config=telemetry_config,
        projects=[
            item["path"]
            for item in raw.get("projects", [])
            if isinstance(item, dict) and item.get("enabled", True) and item.get("path")
        ],
        min_confidence=confidence_threshold(
            review.get("min_confidence", DEFAULT_MIN_CONFIDENCE),
            "min_confidence",
        ),
        allowed_kinds=lower_string_list(review.get("allowed_kinds", []), "allowed_kinds"),
        post_no_findings_comment=bool_value(
            agent.get("post_no_findings_comment"),
            "post_no_findings_comment",
            default=True,
        ),
        no_findings_comment_body=text_value(
            agent.get("no_findings_comment_body"),
            "no_findings_comment_body",
            default=DEFAULT_NO_FINDINGS_COMMENT,
        ),
    )


__all__ = [
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_NO_FINDINGS_COMMENT",
    "DEFAULT_PROVIDER",
    "SUPPORTED_PROVIDERS",
    "ConfigError",
    "ReviewConfig",
    "load_review_config",
    "review_config_from_dict",
]
