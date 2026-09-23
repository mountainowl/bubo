"""GitLab MR review poller — the daemon-like CLI entry point.

This file is the orchestrator. It sequences the review pipeline; the
heavy lifting lives in dedicated sibling modules:

* :mod:`bubo.db` — SQLite schema and all writers.
* :mod:`bubo.gitlab` — REST client.
* :mod:`bubo.findings` — finding extraction, policy filter,
  diff-position mapping.
* :mod:`bubo.subproc` — bounded subprocess execution with
  process-group cleanup.
* :mod:`bubo.secrets` — credential redaction.
* :mod:`bubo.signals` — cooperative SIGTERM/SIGINT shutdown.
* :mod:`bubo.events` — structured JSON-line logging.

What stays here:

* :func:`poll` — one poll cycle, plus the SIGTERM-aware loop and the
  in-flight backpressure check.
* :func:`worker` — one MR review end-to-end (checkout → agent → parse
  → policy filter → post/plan → record).
* :func:`sync_outcomes` — periodic GitLab-side state refresh.
* :func:`check_health` — liveness probe for cron/systemd.
* :func:`main` — argparse dispatch.

Selected symbols from the extracted modules are re-exported here so the
existing test suite (`tests/test_*.py`) can keep using ``poller.X``
without churn. New code should import from the canonical module
instead.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from bubo import analytics, github, gitlab, paths
from bubo.backfill import (
    BackfilledComment as _BackfilledComment,
)
from bubo.backfill import (
    finding_from_comment as _finding_from_backfilled_comment,
)
from bubo.backfill import (
    first_bot_note as _first_bot_note,
)
from bubo.backfill import (
    record_backfilled_comment as _record_backfilled_comment,
)
from bubo.config_values import ConfigError
from bubo.db import (
    already_seen,
    connect_db,
    count_inflight_workers,
    disputed_class_stats,
    disputed_finding_classes,
    finding_seen,
    init_db,
    latest_reviewed_row,
    posted_findings_for_outcome_sync,
    prompt_version,
    record_finding_outcome,
    record_finding_outcome_sync_attempt,
    record_governance_decision,
    record_provenance,
    record_review_run_finish,
    record_review_run_start,
    review_health,
    review_run_id,
    status_age_seconds,
)
from bubo.db import record as _db_record
from bubo.db import record_finding as _db_record_finding
from bubo.errors import describe
from bubo.events import log, now
from bubo.findings import (
    calibrated_category_floors,
    count_added_lines,
    dispute_stats_by_canonical,
    extract_findings,
    filter_findings_by_policy,
    finding_body,
    finding_comment_body,
    finding_fingerprint,
    normalize_finding_categories,
    surface_predicate_for_mode,
)
from bubo.governance_policy import (
    POLICY_OFF,
    evaluate_policy,
    heightened_scrutiny_directive,
    is_escalated,
)
from bubo.hash_utils import stable_hash
from bubo.outcome_classifier import classify_developer_reply
from bubo.paths import CONFIG, ROOT
from bubo.prompt import render_meta_prompt as _render_meta_prompt
from bubo.prompt import write_rendered_meta_prompt as write_rendered_prompt_file
from bubo.provenance import ProvenanceSignal, compile_patterns, compute_provenance
from bubo.review_config import ReviewConfig, load_review_config, review_config_from_dict
from bubo.scm import ScmProvider, get_provider
from bubo.scm.base import native_changed_lines
from bubo.secrets import redact_secrets
from bubo.signals import (
    install_signal_handlers as _install_signal_handlers,
)
from bubo.signals import (
    shutdown_requested as _shutdown_requested,
)
from bubo.statuses import FindingStatus, ReviewMode, ReviewStatus
from bubo.subproc import kill_process_group
from bubo.subproc import run_bounded as run
from bubo.telemetry import (
    ReviewTelemetry,
    TokenUsage,
    estimate_cost_usd,
    parse_codex_token_usage,
)
from bubo.types import JsonObject
from bubo.verification import (
    Verdict,
    build_verification_prompt,
    parse_verdict,
)

# In-flight backpressure: a cycle backs off when running+queued already
# exceeds ``max_merge_requests_per_poll * INFLIGHT_WORKER_MULTIPLIER``.
# 2x leaves room for normal cycle-time jitter while preventing a stuck
# cycle from silently doubling GitLab/LLM load.
INFLIGHT_WORKER_MULTIPLIER = 2
_NOTE_HEADER = re.compile(
    r"^\*\*(Issue|Suggestion|Question) \((blocking|non-blocking), ([^)]+)\):\*\* (.+)$",
    re.IGNORECASE,
)
_NOTE_CONFIDENCE = re.compile(r"\*\*Confidence:\*\*\s*([0-9.]+)", re.IGNORECASE)

# Allowlist of env-var names forwarded into the agent subprocess. Anything
# not on this list (including every credential the wrapper exported into
# our own environment) is stripped. This is the primary defense against
# prompt-injection exfiltration of secrets.
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


# ---------------------------------------------------------------------------
# Subprocess env + config glue
# ---------------------------------------------------------------------------


def reviewer_env(source: Mapping[str, str], cfg: ReviewConfig | None = None) -> dict[str, str]:
    """Build the env dict for the agent subprocess.

    Filters ``source`` through :data:`REVIEWER_ENV_ALLOWLIST` — dropping
    every credential the wrapper exported into our own environment (the
    primary anti-exfiltration defense) — and injects ``BUBO_ROOT`` so any
    MCP server the agent spawns via ``bin/bubo`` resolves the install
    root. The review prompt (with its contract + findings cap) is passed to
    the agent as a command argument, not via the environment.

    **base_url exception:** a custom OpenAI-compatible endpoint
    (``cfg.llm_base_url``) reads the API key from the environment at request
    time — there is no login flow to stash it in ``auth.json``. So, and *only*
    when a base URL is configured, exactly ``LLM_API_KEY`` and ``LLM_BASE_URL``
    are let through the allowlist. This deliberately re-exposes one credential
    to the agent, which is why it is gated on the operator opting into a base
    URL rather than widening the static allowlist for everyone.
    """
    env = {key: value for key, value in source.items() if key in REVIEWER_ENV_ALLOWLIST}
    env["BUBO_ROOT"] = str(ROOT)
    if cfg is not None and cfg.llm_base_url:
        for name in ("LLM_API_KEY", "LLM_BASE_URL"):
            if source.get(name):
                env[name] = source[name]
    return env


def run_verification(finding: JsonObject, repo: Path | None, cfg: ReviewConfig) -> list[Verdict]:
    """Run the opt-in verification lenses for one finding (the IO seam).

    For each lens in ``cfg.verify_lenses`` this spawns one verifier exec
    (``cfg.verify_command`` falling back to ``cfg.reviewer_command``) with
    ``cwd=repo`` and the secret-stripped :func:`reviewer_env`, so the
    verifier can inspect the checked-out code. Each check gets its own
    ``cfg.verify_timeout_seconds`` budget.

    A check that fails to run — spawn error, timeout, non-zero exit, or
    unparseable output — yields a verdict with ``ok=False`` rather than a
    refutation, so a verifier outage cannot silently drop every finding (the
    decision core ignores ``ok=False`` votes; the caller posts unverified
    when no lens succeeded). This is the only function in the verification
    path that touches the filesystem/subprocess, so tests monkeypatch it.
    """
    command = list(cfg.verify_command or cfg.reviewer_command)
    verdicts: list[Verdict] = []
    if not command:
        return verdicts
    env = reviewer_env(os.environ, cfg)
    for lens in cfg.verify_lenses:
        prompt = build_verification_prompt(finding, lens=lens)
        try:
            result = run(
                [*command, prompt],
                cwd=repo,
                env=env,
                timeout=cfg.verify_timeout_seconds,
            )
        except Exception as exc:  # spawn / timeout / OS error — failed to run
            log("verify_check_failed", lens=lens, error=type(exc).__name__)
            verdicts.append(Verdict(lens=lens, real=False, confidence=0.0, reason="", ok=False))
            continue
        if result.returncode:
            log("verify_check_nonzero", lens=lens, returncode=result.returncode)
            verdicts.append(Verdict(lens=lens, real=False, confidence=0.0, reason="", ok=False))
            continue
        verdict = parse_verdict(result.stdout or "", lens=lens)
        if verdict is None:
            log("verify_check_unparsed", lens=lens)
            verdicts.append(Verdict(lens=lens, real=False, confidence=0.0, reason="", ok=False))
            continue
        verdicts.append(verdict)
    return verdicts


def read_config() -> ReviewConfig:
    """Load and apply ``config/env.toml``."""
    return load_review_config(CONFIG, log_event=log)


def normalize_config(cfg: JsonObject) -> ReviewConfig:
    """Build a :class:`ReviewConfig` from an in-memory TOML mapping."""
    return review_config_from_dict(cfg, log_event=log)


def analytics_identity(
    cfg: ReviewConfig, provider: ScmProvider, token: str
) -> analytics.AnalyticsIdentity | None:
    """Resolve one SCM pseudonym without affecting review execution.

    Subject lookup is analytics-only: an unavailable method, malformed API
    response, or provider error leaves the install identity in place.
    """
    if not analytics.analytics_enabled(cfg.analytics_config):
        return None
    try:
        return analytics.scm_identity(provider.name, provider.authenticated_subject(cfg, token))
    except Exception:
        return None


def reviewer_model(cfg: ReviewConfig) -> str:
    """Return the configured model label for telemetry, or ``"unknown"``."""
    return cfg.model or "unknown"


# ---------------------------------------------------------------------------
# Re-exports / thin wrappers that the test suite touches via ``poller.X``
# ---------------------------------------------------------------------------


def record(
    project: str,
    iid: int,
    sha: str,
    status: ReviewStatus,
    report: str | None = None,
    error: str | None = None,
) -> None:
    """Thin wrapper around :func:`db.record` for the existing API surface."""
    _db_record(project, iid, sha, status, report, error)


def record_finding(
    *,
    project: str,
    iid: int,
    sha: str,
    fingerprint: str,
    finding: JsonObject,
    status: FindingStatus,
    discussion_id: str | None = None,
    run_id: str | None = None,
    note_id: str | None = None,
    verified: bool | None = None,
    verify_votes: str | None = None,
) -> None:
    """Persist a finding with its rendered body.

    Records the *canonical* body via :func:`findings.finding_body` (mood-neutral,
    matching the fingerprint), NOT the tone-aware posted body. So under a
    non-default ``[review].tone`` the stored body stays stable while the comment
    developers see (via :func:`findings.finding_comment_body`) carries the voice
    — keeping the audit dataset comparable across tones. The DB layer takes
    ``body`` as a parameter so it does not need to know about finding-formatting
    rules.

    Note: the in-voice ``comment`` itself is not stored — ``review_findings``
    has no raw-finding column, so the canonical body is the durable record and
    the voiced prose lives only on the posted SCM comment. ``verified`` /
    ``verify_votes`` carry the opt-in verification verdict (``None`` when the
    pass did not run).
    """
    _db_record_finding(
        project=project,
        iid=iid,
        sha=sha,
        fingerprint=fingerprint,
        finding=finding,
        status=status,
        body=finding_body(finding),
        discussion_id=discussion_id,
        run_id=run_id,
        note_id=note_id,
        verified=verified,
        verify_votes=verify_votes,
    )


# ---------------------------------------------------------------------------
# Prompt rendering glue
# ---------------------------------------------------------------------------


def write_rendered_meta_prompt(cfg: ReviewConfig) -> Path:
    """Render and cache the meta prompt for a single review.

    ``BUBO_PROMPT_SOURCE`` lets tests point at a different
    source file without touching the install directory.
    """
    source = Path(os.environ.get("BUBO_PROMPT_SOURCE", paths.ROOT / "prompts" / "00-meta.md"))
    if not source.is_file():
        raise RuntimeError(
            describe(
                f"meta prompt is not readable: {source}",
                reason="the rendered meta-prompt file is missing/unreadable",
                fix="ensure the prompt template renders and the path is readable.",
            )
        )
    return write_rendered_prompt_file(
        source, paths.RENDERED_PROMPTS, cfg.max_findings_per_merge_request
    )


def render_meta_prompt(prompt_text: str, max_findings: int) -> str:
    """Pure in-memory render of the meta prompt template."""
    return _render_meta_prompt(prompt_text, max_findings)


# ---------------------------------------------------------------------------
# Worker fork + per-change job files
# ---------------------------------------------------------------------------


def slug(value: str) -> str:
    """Make ``value`` safe for use as a path or filename component."""
    return "".join(c if c.isalnum() else "-" for c in value).strip("-").lower()


def sha_for(change: JsonObject) -> str:
    """Return the head SHA from a change payload, GitLab or GitHub shaped.

    Used for dedup keys, job filenames, and report paths — provider-neutral
    so a single helper serves both. GitLab exposes ``sha`` /
    ``diff_refs.head_sha``; GitHub exposes ``head.sha``.
    """
    return (
        change.get("sha")
        or (change.get("head") or {}).get("sha")
        or change.get("diff_refs", {}).get("head_sha")
        or ""
    )


def change_number_of(change: JsonObject) -> int:
    """Return the change number from a payload, GitLab ``iid`` or GitHub ``number``.

    Provider-neutral helper for the pre-flight bookkeeping (dedup key, job
    filename, report path) that runs before the provider is resolved.
    """
    value = change.get("iid")
    if value is None:
        value = change.get("number")
    if value is None:
        raise KeyError(
            describe(
                "change payload has neither 'iid' nor 'number'",
                reason="the SCM webhook/poll payload lacked an MR/PR id",
                fix="verify the provider and payload shape.",
            )
        )
    return int(value)


def write_job(project: str, change: JsonObject) -> Path:
    """Serialize one review job to disk for the forked worker.

    The job filename embeds the change number and head sha. ``change`` is
    stored verbatim so the worker re-reads the exact payload the poller saw.
    """
    number = change_number_of(change)
    sha = sha_for(change)
    path = paths.JOBS / f"{slug(project)}-{number}-{sha[:12]}.json"
    path.write_text(
        json.dumps({"project": project, "mr": change, "queued_at": now()}, indent=2),
        encoding="utf-8",
    )
    return path


def queue_latency_seconds(job_data: JsonObject) -> float | None:
    """Compute fork-to-pickup latency for the queue-latency metric."""
    raw = job_data.get("queued_at")
    if not raw:
        return None
    try:
        queued_at = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if queued_at.tzinfo is None:
        queued_at = queued_at.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - queued_at).total_seconds())


def fork_worker(job: Path) -> int:
    """Spawn a detached worker subprocess for one MR.

    Uses ``start_new_session=True`` so the worker is in its own process
    group — the parent's eventual exit does not take the worker with it.
    The log file handle is opened in the parent only long enough for
    ``Popen`` to dup it, then closed; the child holds its own copy.
    """
    log_file = paths.LOGS / f"{job.stem}.log"
    out = log_file.open("ab", buffering=0)
    configured = os.environ.get("BUBO_WORKER_COMMAND")
    command = shlex.split(configured) if configured else [sys.executable, "-m", "bubo.poller"]
    try:
        proc = subprocess.Popen(
            [*command, "--worker", str(job)],
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        out.close()
    log("worker_forked", pid=proc.pid, job=str(job), log=str(log_file))
    return proc.pid


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


def poll() -> int:
    """Run one poll cycle: scan projects, queue eligible changes, fork workers.

    Provider-agnostic — obtains the configured provider via
    :func:`bubo.scm.get_provider` and drives it. Returns the number
    of changes queued. Each cycle is bounded by:

    * ``cfg.max_merge_requests_per_poll`` — per-cycle cap on newly queued
      changes.
    * ``count_inflight_workers`` x ``INFLIGHT_WORKER_MULTIPLIER`` —
      back-pressure when cron fires faster than workers drain.
    * SIGTERM/SIGINT (cooperative) — the loop checks
      :func:`signals.shutdown_requested` between changes and exits cleanly.

    Every emitted log line carries ``poll_run_id`` so events from the
    same cycle correlate across the JSON-line stream.
    """
    init_db()
    cfg = read_config()
    provider = get_provider(cfg)
    token = provider.token()
    identity = analytics_identity(cfg, provider, token)
    queued = 0
    target_number = cfg.target_merge_request_iid
    poll_run_id = stable_hash({"poll": now()})[:12]
    inflight_cap = cfg.max_merge_requests_per_poll * INFLIGHT_WORKER_MULTIPLIER
    inflight = count_inflight_workers()
    log(
        "poll_start",
        poll_run_id=poll_run_id,
        provider=provider.name,
        projects=len(cfg.projects),
        inflight=inflight,
        inflight_cap=inflight_cap,
        max_merge_requests_per_poll=cfg.max_merge_requests_per_poll,
    )
    analytics.record_session_start(
        cfg.analytics_config,
        scm_provider=cfg.provider,
        projects_count=len(cfg.projects),
        identity=identity,
    )
    if inflight >= inflight_cap:
        log(
            "poll_throttled_inflight",
            poll_run_id=poll_run_id,
            inflight=inflight,
            inflight_cap=inflight_cap,
        )
        return 0
    for project in cfg.projects:
        if _shutdown_requested():
            log("poll_interrupted", poll_run_id=poll_run_id, reason="shutdown", queued=queued)
            return queued
        log("poll_project", poll_run_id=poll_run_id, project=project)
        for change in provider.list_open_changes(cfg, project, token):
            if _shutdown_requested():
                log("poll_interrupted", poll_run_id=poll_run_id, reason="shutdown", queued=queued)
                return queued
            number = provider.change_number(change)
            if target_number is not None and number != int(target_number):
                continue
            sha = sha_for(change)
            if not sha or already_seen(
                project,
                number,
                sha,
                queued_ttl_seconds=cfg.timeout_seconds * 2,
                failed_ttl_seconds=cfg.timeout_seconds,
            ):
                continue
            record(project, number, sha, ReviewStatus.QUEUED)
            job = write_job(project, change)
            fork_worker(job)
            queued += 1
            inflight += 1
            if queued >= cfg.max_merge_requests_per_poll or inflight >= inflight_cap:
                log(
                    "poll_capped",
                    poll_run_id=poll_run_id,
                    queued=queued,
                    inflight=inflight,
                    inflight_cap=inflight_cap,
                )
                return queued
    if queued == 0:
        log("no_pending_reviews", poll_run_id=poll_run_id)
    analytics.flush()
    log("poll_done", poll_run_id=poll_run_id, queued=queued)
    return queued


# ---------------------------------------------------------------------------
# Worker — one change review end-to-end
# ---------------------------------------------------------------------------


def cleanup_worktree(repo: Path) -> None:
    """Best-effort remove of a per-change worktree.

    Guarded with a path-containment check so a misconfigured ``repo``
    path cannot ``rm -rf`` an arbitrary location. Resolves ``paths.WORK``
    via the module so test monkey-patches reach this code.
    """
    try:
        repo.resolve().relative_to(paths.WORK.resolve())
    except ValueError:
        return
    shutil.rmtree(repo, ignore_errors=True)


def review_prompt(project: str, change: JsonObject, cfg: ReviewConfig | None = None) -> str:
    """Build the per-change review task prompt via the configured provider.

    ``cfg`` is optional for backwards compatibility; when omitted a default
    GitLab config is used (preserves the historical signature the tests
    exercise).
    """
    cfg = cfg or ReviewConfig()
    return get_provider(cfg).review_prompt(project, change, cfg)


def emit_finding_metric(
    telemetry: ReviewTelemetry | None,
    *,
    repo: str,
    status: FindingStatus | str,
    finding: JsonObject,
    dry_run: bool,
) -> None:
    """Forward one finding-lifecycle event to OTel if telemetry is enabled."""
    if telemetry and telemetry.config.emit_finding_events:
        telemetry.record_finding(repo=repo, status=status, finding=finding, dry_run=dry_run)


def changed_loc(
    provider: ScmProvider,
    cfg: ReviewConfig,
    token: str,
    project: str,
    number: int,
    changed: dict[str, JsonObject] | None = None,
) -> tuple[int | None, int | None]:
    """Best-effort ``(files_changed, lines_changed)`` for anonymous analytics.

    Sums the provider's added-line counts (the same changed-line map the
    poster uses for position mapping). Returns ``(None, None)`` on any failure
    — analytics must never break a review, and an unknown count must not
    masquerade as zero.
    """
    if changed is None:
        try:
            changed = provider.changed_lines(cfg, token, project, number)
        except Exception:
            return None, None
    files = len(changed)
    lines = sum(len(entry.get("new_lines") or ()) for entry in changed.values())
    return files, lines


def _position_file(position: JsonObject) -> Any:
    """File path from a provider position dict, provider-agnostic.

    GitLab positions carry ``new_path``; GitHub positions carry ``path``.
    Used only for log fields, so a falsy ``new_path`` falls through to
    ``path`` rather than emitting an empty string.
    """
    return position.get("new_path") or position.get("path")


def _position_line(position: JsonObject) -> Any:
    """Line number from a provider position dict, provider-agnostic.

    GitLab positions carry ``new_line``; GitHub positions carry ``line``.
    """
    return position.get("new_line") or position.get("line")


NoFindingsCommentVerdict = Literal[
    "posted", "posted_pending_id", "skipped_dry_run", "disabled", "errored"
]


def post_no_findings_comment(
    *,
    cfg: ReviewConfig,
    token: str,
    project: str,
    number: int,
    provider: ScmProvider,
) -> tuple[NoFindingsCommentVerdict, str]:
    """Post the change-level "no issues found" comment.

    Called only when the review finished with status
    :attr:`ReviewStatus.NO_FINDINGS`. Returns ``(verdict, detail)`` where
    ``verdict`` is one of:

    * ``"posted"`` — a new comment was created or an existing identical
      one was matched; ``detail`` is the provider comment ID.
    * ``"posted_pending_id"`` — the provider call succeeded but returned
      no ID (rare 2xx without ``id``); ``detail`` is empty. Surfaced so
      the structured log distinguishes a healthy post from a partial one.
    * ``"skipped_dry_run"`` — ``cfg.dry_run`` is set; ``detail`` is empty.
    * ``"disabled"`` — either ``post_no_findings_comment`` is ``False``
      or ``no_findings_comment_body`` is empty/whitespace-only; ``detail``
      is empty.
    * ``"errored"`` — the provider raised. ``detail`` is the (redacted)
      error string. The caller treats this as a soft failure: the review
      itself succeeded, only the cosmetic acknowledgement failed.

    The provider call is idempotent on exact body match scoped to the
    bot's author: a re-review of the same MR/PR will reuse the existing
    comment instead of stacking duplicates on rebases or repeated polls.
    The submitted body is stripped to match the gate check exactly, so
    a trailing newline in the operator's config cannot defeat dedup on
    platforms that normalize stored bodies.
    """
    body = cfg.no_findings_comment_body.strip()
    if not cfg.post_no_findings_comment or not body:
        return ("disabled", "")
    if cfg.dry_run:
        return ("skipped_dry_run", "")
    try:
        comment_id = provider.post_change_comment(cfg, token, project, number, body)
    except (RuntimeError, OSError, urllib.error.URLError) as exc:
        # Soft failure: a comment-post error must NEVER flip a clean review
        # to FAILED. The inline-comment path treats individual post failures
        # as PENDING_EXTERNAL_ID; this cosmetic acknowledgement is at least
        # as forgiving.
        return ("errored", redact_secrets(str(exc)))
    if not comment_id:
        return ("posted_pending_id", "")
    return ("posted", comment_id)


def capture_provenance(
    cfg: ReviewConfig,
    *,
    token: str,
    project: str,
    number: int,
    sha: str,
    run_id: str,
    provider: ScmProvider,
    telemetry: ReviewTelemetry | None = None,
    changed: dict[str, JsonObject] | None = None,
) -> tuple[ProvenanceSignal, str] | None:
    """Capture provenance + evaluate governance policy (opt-in, off by default).

    Computes the change's provenance signal once and, per the enabled
    capabilities, (1) persists it write-once for audit (``capture_provenance``),
    (2) returns a heightened-scrutiny directive to inject into the review prompt
    when the change escalates (``rigor_modulation``), and (3) records an
    advisory, write-once governance decision (``policy_mode != off``). All are
    **advisory** — bubo never blocks a merge.

    Returns ``(signal, directive)`` where ``directive`` is the prompt suffix
    (``""`` when rigor is off or the change did not escalate), or ``None`` when
    governance is disabled or any step soft-fails — a governance hiccup must
    never fail a review. The commit/diff fetch happens only when governance is
    enabled, so a disabled install makes zero extra API calls.
    """
    governance = cfg.governance_config
    if not governance.enabled:
        return None
    try:
        commits = provider.list_commits(cfg, token, project, number)
        messages = [str(commit.get("message") or "") for commit in commits]
        if changed is None:
            changed = provider.changed_lines(cfg, token, project, number)
        signal = compute_provenance(
            messages,
            changed.keys(),
            trailer_patterns=compile_patterns(governance.ai_trailer_patterns),
            sensitive_globs=governance.sensitive_path_globs,
        )
        if governance.capture_provenance:
            record_provenance(run_id, signal)
            log(
                "provenance_captured",
                project=project,
                iid=number,
                run_id=run_id,
                band=signal.band,
                source=signal.source,
                confidence=signal.confidence,
                ai_signals=len(signal.ai_signals),
                sensitive_paths=signal.sensitive_paths,
            )
            if telemetry is not None:
                telemetry.record_provenance(repo=project, band=signal.band, source=signal.source)
        directive = ""
        if governance.rigor_modulation and is_escalated(
            signal,
            escalate_bands=governance.escalate_bands,
            require_sensitive=governance.rigor_require_sensitive,
        ):
            directive = heightened_scrutiny_directive()
        if governance.policy_mode != POLICY_OFF:
            decision = evaluate_policy(
                signal,
                mode=governance.policy_mode,
                escalate_bands=governance.escalate_bands,
                require_sensitive=governance.rigor_require_sensitive,
                rigor_injected=bool(directive),
            )
            record_governance_decision(
                run_id, project=project, iid=number, sha=sha, decision=decision
            )
            log(
                "governance_decision",
                project=project,
                iid=number,
                run_id=run_id,
                mode=decision.mode,
                action=decision.action,
                triggered=decision.triggered,
                matched_rule=decision.matched_rule,
                rigor_injected=decision.rigor_injected,
                band=decision.band,
                sensitive_paths=decision.sensitive_paths,
            )
            if telemetry is not None:
                telemetry.record_governance(
                    repo=project, mode=decision.mode, action=decision.action
                )
        return (signal, directive)
    except Exception as exc:  # governance is advisory — never fail the review
        log(
            "provenance_capture_failed",
            project=project,
            iid=number,
            run_id=run_id,
            error=redact_secrets(str(exc)),
        )
        return None


def post_or_plan_findings(
    *,
    cfg: ReviewConfig,
    token: str,
    project: str,
    mr: JsonObject,
    raw_review: str,
    run_id: str | None = None,
    telemetry: ReviewTelemetry | None = None,
    provider: ScmProvider | None = None,
    repo: Path | None = None,
    changed: dict[str, JsonObject] | None = None,
) -> tuple[int, int, int]:
    """Compatibility facade for the dedicated finding posting pipeline."""
    from bubo.finding_pipeline import FindingPipelineDeps
    from bubo.finding_pipeline import post_or_plan_findings as run_pipeline

    resolved_provider = provider or get_provider(cfg)
    deps = FindingPipelineDeps(
        extract_findings=lambda review: extract_findings(
            review, max_findings=cfg.max_findings_per_merge_request
        ),
        prepare_findings=_prepare_findings_for_posting,
        sha_for=sha_for,
        fingerprint=finding_fingerprint,
        finding_seen=finding_seen,
        record_finding=record_finding,
        emit_metric=emit_finding_metric,
        log=log,
        run_verification=run_verification,
        position_file=_position_file,
        position_line=_position_line,
        comment_body=finding_comment_body,
    )
    return run_pipeline(
        deps,
        cfg=cfg,
        token=token,
        project=project,
        mr=mr,
        raw_review=raw_review,
        run_id=run_id,
        telemetry=telemetry,
        provider=resolved_provider,
        repo=repo,
        changed=changed,
    )


def _prepare_findings_for_posting(
    cfg: ReviewConfig, project: str, number: int, findings: list[JsonObject]
) -> list[JsonObject]:
    """Normalize and filter findings before the provider diff calls."""
    # Map each free-form `category` onto the canonical taxonomy, stored in a
    # separate `category_canonical` field; the original label is preserved for
    # the body, fingerprint, and audit row. The surface-mode filter below reads
    # the canonical field, so `gate` matches deterministically across the 30+
    # free-form labels models actually emit. (The MCP `review` tool has its own
    # parse path and returns raw findings to its client without the policy
    # filter, so normalization is intentionally a poster-path concern only.)
    findings = normalize_finding_categories(findings)
    # Opt-in, off by default: drop categories this repo has repeatedly
    # rejected, using the accept/dispute signal in finding_outcomes. The set
    # is empty (and the DB never queried) unless the operator enabled it.
    suppressed_categories: tuple[str, ...] = ()
    if cfg.suppress_disputed_classes:
        suppressed_categories = tuple(
            disputed_finding_classes(
                project,
                min_samples=cfg.dispute_suppress_min_samples,
                threshold=cfg.dispute_suppress_threshold,
            )
        )
    # Per-category confidence floors (the calibrated-confidence lever, off by
    # default). Manual operator overrides always apply; when calibration is on,
    # derive additional floors from this repo's dispute history aggregated onto
    # the CANONICAL category (raw labels fragment the signal across synonyms).
    # Manual entries win over a derived floor for the same category.
    category_floors: dict[str, float] = dict(cfg.category_min_confidence)
    if cfg.calibrate_confidence:
        calibrated = calibrated_category_floors(
            dispute_stats_by_canonical(disputed_class_stats(project, min_samples=1)),
            base=cfg.min_confidence,
            max_floor=cfg.calibrate_max_confidence,
            min_samples=cfg.dispute_suppress_min_samples,
        )
        for category, floor in calibrated.items():
            category_floors.setdefault(category, floor)
        if calibrated:
            log("confidence_calibrated", project=project, iid=number, floors=calibrated)
    findings, dropped = filter_findings_by_policy(
        findings,
        min_confidence=cfg.min_confidence,
        allowed_kinds=cfg.allowed_kinds,
        category_floors=category_floors or None,
        surface_predicate=surface_predicate_for_mode(cfg.mode),
        suppressed_categories=suppressed_categories,
    )
    for finding, reason in dropped:
        log(
            "finding_filtered",
            project=project,
            iid=number,
            file=finding.get("file") or finding.get("path"),
            line=finding.get("line") or finding.get("new_line"),
            reason=reason,
            confidence=finding.get("confidence"),
            severity=finding.get("severity"),
            category=finding.get("category"),
            category_canonical=finding.get("category_canonical"),
            type=finding.get("type"),
        )
    return findings


def _finalize_worker(
    *,
    cfg: ReviewConfig | None,
    telemetry: ReviewTelemetry | None,
    run_id: str,
    project: str,
    model: str,
    status: ReviewStatus,
    started: float,
    tokens: TokenUsage,
    cost_usd: float,
    lines_reviewed: int,
    files_changed: int | None,
    lines_changed: int | None,
    identity: analytics.AnalyticsIdentity | None,
    error: str | None,
    error_type: str | None,
    posted: int,
    planned: int,
    skipped: int,
) -> None:
    """Write the shared terminal DB, telemetry, and analytics state."""
    duration_seconds = round(time.monotonic() - started, 2)
    if cfg is not None:
        record_review_run_finish(
            run_id=run_id,
            status=status,
            tokens=tokens,
            cost_usd=cost_usd,
            error=error,
            lines_reviewed=lines_reviewed,
        )
    if telemetry is not None:
        if error is not None:
            telemetry.record_failure(
                repo=project, error_type=error_type or "UnknownError", operation="review"
            )
        telemetry.record_review_done(
            repo=project,
            model=model,
            status=status,
            review_mode=ReviewMode.DIFF,
            dry_run=cfg.dry_run if cfg is not None else True,
            duration_seconds=duration_seconds,
            tokens=tokens,
            cost_usd=cost_usd,
            tone=cfg.tone if cfg is not None else None,
            lines_reviewed=lines_reviewed,
        )
    if cfg is not None:
        analytics.record_review_completed(
            cfg.analytics_config,
            scm_provider=cfg.provider,
            agent=analytics.agent_label(cfg.reviewer_command),
            model=model,
            status=str(status),
            dry_run=cfg.dry_run,
            review_mode=str(ReviewMode.DIFF),
            tone=cfg.tone,
            duration_seconds=duration_seconds,
            tokens_input=tokens.input,
            tokens_output=tokens.output,
            tokens_cached=tokens.cached,
            tokens_total=tokens.total,
            cost_usd=cost_usd,
            findings_posted=posted,
            findings_planned=planned,
            findings_skipped=skipped,
            files_changed=files_changed,
            lines_changed=lines_changed,
            identity=identity,
        )


def worker(job: Path) -> int:
    """Run one MR review end-to-end. Exit code is the value the worker prints.

    The shape is: try the happy path, record success; catch any exception,
    write a redacted error transcript, record ``FAILED``. The ``finally``
    block always cleans up the per-MR worktree so a failed review does
    not leak disk.
    """
    init_db()
    data = json.loads(job.read_text())
    project = data["project"]
    mr = data["mr"]
    iid = change_number_of(mr)
    sha = sha_for(mr)
    run_id = review_run_id(project, iid, sha)
    model = "unknown"
    cfg: ReviewConfig | None = None
    telemetry: ReviewTelemetry | None = None
    report = paths.REPORTS / slug(project) / str(iid) / sha[:12] / "review.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    tokens = TokenUsage()
    cost_usd = 0.0
    lines_reviewed = 0
    repo: Path | None = None
    files_changed: int | None = None
    lines_changed: int | None = None
    changed: dict[str, JsonObject] | None = None
    identity: analytics.AnalyticsIdentity | None = None
    try:
        cfg = read_config()
        provider = get_provider(cfg)
        token = provider.token()
        identity = analytics_identity(cfg, provider, token)
        telemetry = ReviewTelemetry.from_config(cfg.telemetry_config)
        queued_seconds = queue_latency_seconds(data)
        if queued_seconds is not None:
            telemetry.record_queue_latency(repo=project, seconds=queued_seconds)
        model = reviewer_model(cfg)
        log(
            "review_start",
            project=project,
            iid=iid,
            sha=sha,
            reviewer=cfg.reviewer_command,
            dry_run=cfg.dry_run,
            run_id=run_id,
        )
        record(project, iid, sha, ReviewStatus.RUNNING, str(report))
        rendered_prompt = write_rendered_meta_prompt(cfg)
        record_review_run_start(
            run_id=run_id,
            project=project,
            iid=iid,
            sha=sha,
            model=model,
            prompt_version=prompt_version(rendered_prompt),
            review_mode=ReviewMode.DIFF,
            dry_run=cfg.dry_run,
            tone=cfg.tone,
        )
        with telemetry.span("llm_review.run", repo=project, mr_iid=iid, sha=sha, run_id=run_id):
            repo = paths.WORK / slug(project) / str(iid) / sha[:12]
            with telemetry.span("llm_review.checkout", repo=project, sha=sha):
                provider.checkout(cfg, project, mr, repo)
            changed = native_changed_lines(mr, repo)
            if changed is None:
                try:
                    changed = provider.changed_lines(cfg, token, project, iid)
                except Exception as exc:
                    log("lines_reviewed_failed", project=project, iid=iid, error=type(exc).__name__)
            # Lines of code reviewed: added lines across the change's diff,
            # captured for EVERY review (independent of whether findings exist,
            # so a clean review still records its size). Soft-fail — a metric
            # must never break a review (mirrors capture_provenance).
            if changed is not None:
                lines_reviewed = count_added_lines(changed)
            # Anonymous LoC for analytics — computed ONLY when analytics is
            # enabled, so an opted-out user pays no extra API round-trip.
            if analytics.analytics_enabled(cfg.analytics_config):
                files_changed, lines_changed = changed_loc(
                    provider, cfg, token, project, iid, changed
                )
            # Opt-in governance (off by default). Captures provenance and
            # evaluates the policy gate; returns a heightened-scrutiny directive
            # to inject into the prompt when the change escalates. No-op + no API
            # calls unless enabled; soft-fails so it never breaks a review.
            with telemetry.span("llm_review.provenance", repo=project):
                governance = capture_provenance(
                    cfg,
                    token=token,
                    project=project,
                    number=iid,
                    sha=sha,
                    run_id=run_id,
                    provider=provider,
                    telemetry=telemetry,
                    changed=changed,
                )
            extra_directive = governance[1] if governance else ""
            env = reviewer_env(os.environ, cfg)
            with telemetry.span("llm_review.agent", repo=project, model=model) as agent_span:
                result = run(
                    [
                        *cfg.reviewer_command,
                        provider.review_prompt(project, mr, cfg, extra_directive=extra_directive),
                    ],
                    cwd=repo,
                    timeout=cfg.timeout_seconds,
                    env=env,
                )
                safe_stdout = redact_secrets(result.stdout)
                report.write_text(safe_stdout, encoding="utf-8")
                tokens = parse_codex_token_usage(result.stdout)
                cost_usd = estimate_cost_usd(tokens, cfg.telemetry_config.price_for(model))
                telemetry.set_span_attrs(
                    agent_span,
                    tokens_input=tokens.input,
                    tokens_output=tokens.output,
                    tokens_total=tokens.total,
                    cost_usd=cost_usd,
                    exit_code=result.returncode,
                )
                if result.returncode:
                    raise RuntimeError(
                        describe(
                            f"code review subprocess exited {result.returncode}",
                            reason="the reviewer command returned a non-zero status",
                            fix=(
                                "inspect the agent transcript saved to the run's report "
                                "file for the underlying error; ensure the reviewer command, "
                                "model, and credentials are configured and reachable."
                            ),
                        )
                    )
            with telemetry.span("llm_review.post", repo=project, dry_run=cfg.dry_run) as post_span:
                posted, planned, skipped = post_or_plan_findings(
                    cfg=cfg,
                    token=token,
                    project=project,
                    mr=mr,
                    raw_review=safe_stdout,
                    run_id=run_id,
                    telemetry=telemetry,
                    provider=provider,
                    repo=repo,
                    changed=changed,
                )
                telemetry.set_span_attrs(
                    post_span,
                    findings_posted=posted,
                    findings_planned=planned,
                    findings_skipped=skipped,
                )
            status = (
                ReviewStatus.NO_FINDINGS
                if (posted, planned, skipped) == (0, 0, 0)
                else ReviewStatus.SUCCESS
            )
            if status == ReviewStatus.NO_FINDINGS:
                # This is another real provider write, so it needs the same
                # last-moment head check as inline findings.
                should_post_no_findings = (
                    cfg.post_no_findings_comment
                    and bool(cfg.no_findings_comment_body.strip())
                    and not cfg.dry_run
                )
                current = (
                    provider.get_change(cfg, token, project, iid)
                    if should_post_no_findings
                    else None
                )
                current_sha = sha_for(current) if current is not None else sha
                if current_sha and current_sha != sha:
                    no_findings_verdict, no_findings_detail = "skipped_superseded", current_sha
                else:
                    no_findings_verdict, no_findings_detail = post_no_findings_comment(
                        cfg=cfg,
                        token=token,
                        project=project,
                        number=iid,
                        provider=provider,
                    )
                log(
                    "no_findings_comment",
                    project=project,
                    iid=iid,
                    sha=sha,
                    verdict=no_findings_verdict,
                    detail=no_findings_detail,
                    run_id=run_id,
                )
            record(project, iid, sha, status, str(report))
            _finalize_worker(
                cfg=cfg,
                telemetry=telemetry,
                run_id=run_id,
                project=project,
                model=model,
                status=status,
                started=started,
                tokens=tokens,
                cost_usd=cost_usd,
                lines_reviewed=lines_reviewed,
                posted=posted,
                planned=planned,
                skipped=skipped,
                files_changed=files_changed,
                lines_changed=lines_changed,
                identity=identity,
                error=None,
                error_type=None,
            )
            log(
                "review_done",
                project=project,
                iid=iid,
                sha=sha,
                status=status,
                posted=posted,
                planned=planned,
                skipped=skipped,
                seconds=round(time.monotonic() - started, 2),
                tokens_total=tokens.total,
                cost_usd=cost_usd,
                lines_reviewed=lines_reviewed,
                report=str(report),
                run_id=run_id,
            )
            return 0
    except Exception as exc:
        error = redact_secrets(str(exc))
        error_type = type(exc).__name__
        # Preserve the agent transcript if we already wrote one; put the
        # error in a sibling `.error` file so debug info survives a
        # failure mid-write.
        if not report.exists() or not report.read_text(encoding="utf-8").strip():
            report.write_text(error, encoding="utf-8")
        else:
            report.with_suffix(report.suffix + ".error").write_text(error, encoding="utf-8")
        record(project, iid, sha, ReviewStatus.FAILED, str(report), error)
        _finalize_worker(
            cfg=cfg,
            telemetry=telemetry,
            run_id=run_id,
            project=project,
            model=model,
            status=ReviewStatus.FAILED,
            started=started,
            tokens=tokens,
            cost_usd=cost_usd,
            lines_reviewed=lines_reviewed,
            files_changed=files_changed,
            lines_changed=lines_changed,
            identity=identity,
            error=error,
            error_type=error_type,
            posted=0,
            planned=0,
            skipped=0,
        )
        log(
            "review_failed",
            project=project,
            iid=iid,
            sha=sha,
            error=error,
            report=str(report),
            run_id=run_id,
        )
        return 1
    finally:
        if repo is not None:
            cleanup_worktree(repo)
        analytics.flush()


# ---------------------------------------------------------------------------
# Outcome sync + health check
# ---------------------------------------------------------------------------


def check_health() -> int:
    """Report liveness based on freshness of the latest ``reviewed_mrs`` row.

    Exit code is ``0`` if the most recent row's ``updated_at`` is within
    ``timeout_seconds x 3`` of now (room for one regular cycle plus jitter),
    ``1`` otherwise. ``2`` if the DB is missing or the config does not load.

    Emits one ``health_check`` JSON-line event on stdout so the operator
    can pipe ``bubo-poller --health`` straight into a monitor.
    """
    try:
        init_db()
        cfg = read_config()
    except ConfigError as exc:
        log("health_check", verdict="config_error", error=str(exc))
        return 2
    health = review_health(cfg.timeout_seconds)
    if health["status"] == "empty":
        log(
            "health_check",
            verdict="empty",
            threshold_seconds=health["threshold_seconds"],
            note=health["message"],
        )
        # Empty state is not failure on a fresh install; cron will create
        # rows on the first cycle.
        return 0
    verdict = "ok" if health["fresh"] else "stale"
    log(
        "health_check",
        verdict=verdict,
        last_status=health["last_status"],
        last_updated_at=health["last_updated_at"],
        age_seconds=health["age_seconds"],
        threshold_seconds=health["threshold_seconds"],
    )
    return 0 if verdict == "ok" else 1


# Cap LLM reply classifications per sync run so the first sync after this
# ships — when every historical replied-resolved finding is unclassified —
# cannot fire hundreds of sequential agent subprocesses in one invocation.
# Skipped findings still get their outcome recorded and ``last_checked_at``
# advanced, so they rotate in and drain over subsequent runs.
MAX_REPLY_CLASSIFICATIONS_PER_SYNC = 20


def sync_outcomes(limit: int = 200) -> int:
    """Refresh provider-side outcomes for up to ``limit`` posted findings.

    Provider-agnostic — fetches each posted comment's current state via
    ``provider.fetch_outcome``. Records per-finding state (resolved /
    disputed / deleted / etc.) and emits one telemetry event per
    non-default outcome. Touches ``last_checked_at`` even on failure so a
    persistently-broken finding cannot head-of-line block the queue.

    At most :data:`MAX_REPLY_CLASSIFICATIONS_PER_SYNC` LLM reply
    classifications run per invocation; the rest are deferred to later runs.
    """
    init_db()
    cfg = read_config()
    provider = get_provider(cfg)
    token = provider.token()
    identity = analytics_identity(cfg, provider, token)
    telemetry = ReviewTelemetry.from_config(cfg.telemetry_config)
    bot_username = provider.bot_username()
    synced = 0
    classifications = 0
    for finding in posted_findings_for_outcome_sync(limit):
        project = finding["project"]
        iid = int(finding["iid"])
        try:
            outcome = provider.fetch_outcome(
                cfg, token, project, iid, finding["discussion_id"], bot_username
            )
            # When the developer replied but no explicit dispute marker was
            # found, ask the configured agent whether the reply accepts or
            # rejects the finding — so a thread resolved after a rebuttal is
            # not miscounted as a success. Classify once per finding (the LLM
            # verdict is cached via reply_classified) to bound cost.
            already_classified = bool(finding.get("reply_classified"))
            outcome["reply_classified"] = already_classified
            reply_text = str(outcome.get("_reply_text") or "")
            if (
                outcome.get("developer_replied")
                and not outcome.get("disputed")
                and not already_classified
                and reply_text.strip()
                and classifications < MAX_REPLY_CLASSIFICATIONS_PER_SYNC
            ):
                classifications += 1
                verdict = classify_developer_reply(
                    cfg,
                    str(outcome.get("_finding_text") or ""),
                    reply_text,
                )
                # "error" is a transient classifier failure — leave the
                # finding unclassified so a later sync retries it.
                if verdict["verdict"] != "error":
                    if verdict["verdict"] == "rejected" or verdict["false_positive"]:
                        outcome["disputed"] = True
                    if verdict["false_positive"]:
                        outcome["false_positive"] = True
                    outcome["reply_classified"] = True
                log(
                    "reply_classified",
                    project=project,
                    iid=iid,
                    verdict=verdict["verdict"],
                    false_positive=verdict["false_positive"],
                )
            record_finding_outcome(
                project=project,
                iid=iid,
                sha=finding["sha"],
                fingerprint=finding["fingerprint"],
                discussion_id=finding["discussion_id"],
                outcome=outcome,
            )
            prior_outcome = finding.get("prior_outcome") or {}
            analytics_on = analytics.analytics_enabled(cfg.analytics_config)
            for name in (
                "resolved",
                "deleted",
                "developer_replied",
                "disputed",
                "false_positive",
                "duplicate",
            ):
                if not outcome[name]:
                    continue
                if telemetry.config.emit_outcome_sync:
                    telemetry.record_finding(
                        repo=project,
                        status=name,
                        finding={"type": "unknown", "severity": "unknown", "category": "unknown"},
                        dry_run=False,
                    )
                # Anonymous analytics fan-out, beside the DB upsert above. Emit
                # only on the false->true transition: the same posted finding is
                # re-checked every cycle and PostHog has no per-finding key to
                # dedupe on (the fingerprint is never sent), so an every-sync
                # emit would multiply the count.
                if analytics_on and not prior_outcome.get(name):
                    analytics.record_finding_outcome(
                        cfg.analytics_config,
                        scm_provider=cfg.provider,
                        outcome=name,
                        identity=identity,
                    )
            synced += 1
        except Exception as exc:
            record_finding_outcome_sync_attempt(
                project=project,
                iid=iid,
                sha=finding["sha"],
                fingerprint=finding["fingerprint"],
                discussion_id=finding["discussion_id"],
            )
            telemetry.record_failure(
                repo=project, error_type=type(exc).__name__, operation="outcome_sync"
            )
            log(
                "outcome_sync_failed",
                project=project,
                iid=iid,
                error=redact_secrets(str(exc)),
            )
    analytics.flush()
    log("outcome_sync_done", synced=synced, classified=classifications)
    return synced


def backfill_gitlab_bot_comments(updated_after: str, limit: int = 500) -> int:
    """Import already-posted GitLab bot discussions into local metrics state.

    Analytics boundary: outcomes written here are DB-only. This imports
    pre-existing history, so it deliberately does not emit anonymous
    ``finding_outcome`` events (those would land in PostHog at backfill time,
    corrupting the by-day breakdown). Only the live ``sync_outcomes`` path
    emits — see :func:`bubo.analytics.record_finding_outcome`.
    """
    init_db()
    cfg = read_config()
    provider = get_provider(cfg)
    if provider.name != "gitlab":
        log("backfill_unsupported_provider", provider=provider.name)
        return 0
    token = provider.token()
    bot_username = provider.bot_username()
    imported = 0
    for project in cfg.projects:
        for mr in gitlab.merge_requests_updated_after(cfg, project, token, updated_after):
            iid = int(mr["iid"])
            for discussion in gitlab.get_mr_discussions(cfg, token, project, iid):
                if imported >= limit:
                    log("backfill_done", imported=imported)
                    return imported
                note = _first_bot_note(discussion, bot_username)
                if note is None or str(note.get("created_at") or "") < updated_after:
                    continue
                outcome = gitlab.classify_discussion_outcome(
                    discussion, bot_username=bot_username, mr_state=str(mr.get("state") or "")
                )
                position = note.get("position") or {}
                comment = _BackfilledComment(
                    discussion_id=str(discussion.get("id") or ""),
                    note_id=str(note.get("id") or ""),
                    sha=str(position.get("head_sha") or provider.head_sha(mr)),
                    body=str(note.get("body") or ""),
                    file=str(position.get("new_path") or position.get("old_path") or ""),
                    line=position.get("new_line") or position.get("old_line"),
                )
                imported += _record_backfilled_comment(project, iid, comment, outcome)
    log("backfill_done", imported=imported)
    return imported


def _finding_from_bot_note(note: JsonObject, position: JsonObject) -> JsonObject:
    return _finding_from_backfilled_comment(
        _BackfilledComment(
            discussion_id="",
            note_id="",
            sha="",
            body=str(note.get("body") or ""),
            file=str(position.get("new_path") or position.get("old_path") or ""),
            line=position.get("new_line") or position.get("old_line"),
        )
    )


def backfill_github_bot_comments(updated_after: str, limit: int = 500) -> int:
    """Import already-posted GitHub bot review threads into local metrics state.

    The GitHub analogue of :func:`backfill_gitlab_bot_comments`. Walks PRs
    updated at/after ``updated_after``, then each PR's review threads via
    GraphQL (so resolution state is real), records the bot's root comment as
    a POSTED finding, and upserts its outcome. Correlates to any existing
    row by the stored comment id so a re-run is idempotent.

    Analytics boundary: like its GitLab twin, outcomes written here are
    DB-only — backfilled history is not emitted to anonymous analytics; only
    the live ``sync_outcomes`` path emits ``finding_outcome`` events.
    """
    init_db()
    cfg = read_config()
    provider = get_provider(cfg)
    if provider.name != "github":
        log("backfill_unsupported_provider", provider=provider.name)
        return 0
    token = provider.token()
    bot_username = provider.bot_username()
    imported = 0
    for project in cfg.projects:
        for pr in github.pulls_updated_after(cfg, project, token, updated_after):
            number = int(pr["number"])
            head_sha = str((pr.get("head") or {}).get("sha") or "")
            merged = bool(pr.get("merged") or pr.get("merged_at"))
            pr_state = "merged" if merged else str(pr.get("state") or "")
            for thread in github.get_pr_review_threads(cfg, token, project, number):
                if imported >= limit:
                    log("backfill_done", imported=imported)
                    return imported
                root = github.first_bot_comment(thread, bot_username)
                if root is None:
                    continue
                discussion_id = str(root.get("database_id") or root.get("node_id") or "")
                if not discussion_id:
                    continue
                outcome = github.classify_graphql_thread_outcome(thread, bot_username, pr_state)
                comment = _BackfilledComment(
                    discussion_id=discussion_id,
                    note_id=str(root.get("node_id") or ""),
                    sha=head_sha,
                    body=str(root.get("body") or ""),
                    file=str(root.get("path") or ""),
                    line=root.get("line"),
                )
                imported += _record_backfilled_comment(project, number, comment, outcome)
    log("backfill_done", imported=imported)
    return imported


def _finding_from_github_comment(comment: JsonObject) -> JsonObject:
    """Reconstruct a finding dict from a backfilled GitHub bot comment.

    Mirrors :func:`_finding_from_bot_note` for GitHub's review-comment shape
    (``path``/``line`` instead of GitLab's ``position``).
    """
    return _finding_from_backfilled_comment(
        _BackfilledComment(
            discussion_id="",
            note_id="",
            sha="",
            body=str(comment.get("body") or ""),
            file=str(comment.get("path") or ""),
            line=comment.get("line"),
        )
    )


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def main() -> int:
    """CLI entry point for ``bubo-poller``.

    Modes (mutually exclusive):

    * ``--init-db`` — create or migrate the SQLite schema and exit.
    * ``--health`` — report liveness based on the freshness of the latest
      review row. Exit ``0`` healthy, ``1`` stale, ``2`` config error.
    * ``--sync-outcomes [--sync-limit N]`` — check GitLab state for up to
      N already-posted findings and record outcomes.
    * ``--backfill-gitlab-bot-comments-since ISO_TS`` — import GitLab bot
      discussions that predate local SQLite state, then record outcomes.
    * ``--backfill-github-bot-comments-since ISO_TS`` — the GitHub analogue:
      import bot review threads (with real GraphQL resolution state) that
      predate local SQLite state, then record outcomes.
    * ``--worker PATH`` — run as a single-MR worker from a queued job
      file. Used internally by :func:`fork_worker`; operators do not
      invoke this directly.
    * (default) — run one poll cycle.

    Exit codes:

    * ``0`` — success.
    * ``1`` — worker failed (``--worker`` mode) or health check stale
      (``--health`` mode).
    * ``2`` — configuration error (missing or malformed ``env.toml``).
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--init-db", action="store_true")
    parser.add_argument(
        "--health",
        action="store_true",
        help="Report liveness based on freshness of last reviewed MR row.",
    )
    parser.add_argument("--sync-outcomes", action="store_true")
    parser.add_argument("--sync-limit", type=int, default=200)
    parser.add_argument("--backfill-gitlab-bot-comments-since")
    parser.add_argument("--backfill-github-bot-comments-since")
    parser.add_argument("--backfill-limit", type=int, default=500)
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    _install_signal_handlers()
    try:
        if args.init_db:
            init_db()
            log("db_ready", path=str(paths.DB))
            return 0
        if args.health:
            return check_health()
        if args.sync_outcomes:
            sync_outcomes(args.sync_limit)
            return 0
        if args.backfill_gitlab_bot_comments_since:
            backfill_gitlab_bot_comments(
                args.backfill_gitlab_bot_comments_since, args.backfill_limit
            )
            return 0
        if args.backfill_github_bot_comments_since:
            backfill_github_bot_comments(
                args.backfill_github_bot_comments_since, args.backfill_limit
            )
            return 0
        if args.worker:
            return worker(args.worker)
        poll()
        return 0
    except ConfigError as exc:
        log("config_error", error=str(exc))
        return 2


# Public surface, including symbols re-exported from sibling modules that
# the test suite and external callers reach via ``poller.X``. Listing them
# here documents the intent and tells the linter the re-exports are
# deliberate, not dead imports.
__all__ = [
    # re-exports (canonical home in sibling modules)
    "already_seen",
    "backfill_gitlab_bot_comments",
    # pipeline helpers
    "change_number_of",
    # orchestration
    "check_health",
    "cleanup_worktree",
    "connect_db",
    "count_inflight_workers",
    "emit_finding_metric",
    "finding_seen",
    "fork_worker",
    # config glue
    "get_provider",
    "init_db",
    "kill_process_group",
    "latest_reviewed_row",
    "log",
    "main",
    "normalize_config",
    "now",
    "poll",
    "post_or_plan_findings",
    "posted_findings_for_outcome_sync",
    "prompt_version",
    "read_config",
    "record",
    "record_finding",
    "record_finding_outcome",
    "record_finding_outcome_sync_attempt",
    "record_review_run_finish",
    "record_review_run_start",
    "redact_secrets",
    "render_meta_prompt",
    "review_prompt",
    "review_run_id",
    "reviewer_env",
    "reviewer_model",
    "run",
    "sha_for",
    "slug",
    "status_age_seconds",
    "sync_outcomes",
    "worker",
    "write_job",
    "write_rendered_meta_prompt",
]


if __name__ == "__main__":
    sys.exit(main())
