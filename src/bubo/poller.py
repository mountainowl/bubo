"""GitLab MR review poller — the daemon-like CLI entry point.

This file is the orchestrator. It sequences the review pipeline; the
heavy lifting lives in dedicated sibling modules:

* :mod:`bubo.db` — SQLite schema and all writers.
* :mod:`bubo.mcp` — JSON-RPC client for the GitLab MCP server.
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
from typing import Any, Literal, cast

from bubo import github, gitlab, paths
from bubo.config_values import ConfigError, positive_int
from bubo.db import (
    already_seen,
    connect_db,
    count_inflight_workers,
    finding_seen,
    init_db,
    latest_reviewed_row,
    posted_findings_for_outcome_sync,
    prompt_version,
    record_finding_outcome,
    record_finding_outcome_sync_attempt,
    record_review_run_finish,
    record_review_run_start,
    review_run_id,
    status_age_seconds,
)
from bubo.db import record as _db_record
from bubo.db import record_finding as _db_record_finding
from bubo.events import log, now
from bubo.findings import (
    extract_findings,
    filter_findings_by_policy,
    finding_body,
    finding_fingerprint,
)
from bubo.hash_utils import stable_hash
from bubo.mcp import call_tool as mcp_call_tool
from bubo.paths import CONFIG, ROOT
from bubo.prompt import render_meta_prompt as _render_meta_prompt
from bubo.prompt import write_rendered_meta_prompt as write_rendered_prompt_file
from bubo.review_config import ReviewConfig, load_review_config, review_config_from_dict
from bubo.scm import ScmProvider, get_provider
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


def reviewer_env(source: Mapping[str, str], prompt: Path, max_findings: int) -> dict[str, str]:
    """Build the env dict for the agent subprocess.

    Filters ``source`` through :data:`REVIEWER_ENV_ALLOWLIST` and injects
    the variables the agent's CLI wrapper actually needs (root path,
    rendered prompt path, max-findings cap).
    """
    env = {key: value for key, value in source.items() if key in REVIEWER_ENV_ALLOWLIST}
    env["BUBO_ROOT"] = str(ROOT)
    env["BUBO_PROMPT"] = str(prompt)
    env["LLM_REVIEW_MAX_FINDINGS"] = str(
        positive_int(max_findings, "max_findings_per_merge_request")
    )
    env["BUBO_SKIP_AGENT_CONFIG_ENV"] = "1"
    return env


def read_config() -> ReviewConfig:
    """Load and apply ``config/env.toml``."""
    return load_review_config(CONFIG, log_event=log)


def normalize_config(cfg: JsonObject) -> ReviewConfig:
    """Build a :class:`ReviewConfig` from an in-memory TOML mapping."""
    return review_config_from_dict(cfg, log_event=log)


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
) -> None:
    """Persist a finding with its rendered body.

    Computes the body via :func:`findings.finding_body` and delegates to
    :func:`db.record_finding`. The DB layer takes ``body`` as a parameter
    so it does not need to know about finding-formatting rules.
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
        raise RuntimeError(f"meta prompt is not readable: {source}")
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
        raise KeyError("change payload has neither 'iid' nor 'number'")
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
) -> tuple[int, int, int]:
    """Parse, filter, and post (or plan) findings for one change.

    Provider-agnostic: change fetch, diff parsing, position mapping, and
    posting all go through ``provider`` (defaulting to the one configured
    in ``cfg``). Returns ``(posted, planned, skipped)``. Steps in order:

    1. Parse the agent's raw stdout into structured findings.
    2. Apply the operator policy filter (confidence threshold + kind
       whitelist) BEFORE any API call — dropped findings emit a
       ``finding_filtered`` log event with the reason.
    3. Map each finding's line to a diff position. Findings whose file/line
       isn't part of the change diff are recorded ``SKIPPED``.
    4. In dry-run, record ``PLANNED``; otherwise post and record
       ``POSTED`` (or ``PENDING_EXTERNAL_ID`` if the post returned no ID).

    ``mr`` is the change payload (kept named ``mr`` for call-site
    compatibility).
    """
    provider = provider or get_provider(cfg)
    number = provider.change_number(mr)
    sha = sha_for(mr)
    findings = extract_findings(raw_review, max_findings=cfg.max_findings_per_merge_request)
    if not findings:
        return (0, 0, 0)
    findings, dropped = filter_findings_by_policy(
        findings,
        min_confidence=cfg.min_confidence,
        allowed_kinds=cfg.allowed_kinds,
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
            type=finding.get("type"),
        )
    if not findings:
        return (0, 0, 0)
    change = provider.get_change(cfg, token, project, number)
    changed = provider.changed_lines(cfg, token, project, number)
    posted = planned = skipped = 0
    for finding in findings:
        fp = finding_fingerprint(project, number, sha, finding)
        if finding_seen(project, number, sha, fp):
            skipped += 1
            continue
        position = provider.build_position(change, changed, finding)
        if not position:
            record_finding(
                project=project,
                iid=number,
                sha=sha,
                fingerprint=fp,
                finding=finding,
                status=FindingStatus.SKIPPED,
                run_id=run_id,
            )
            emit_finding_metric(
                telemetry,
                repo=project,
                status=FindingStatus.SKIPPED,
                finding=finding,
                dry_run=cfg.dry_run,
            )
            log(
                "finding_skipped",
                project=project,
                iid=number,
                file=finding.get("file") or finding.get("path"),
                line=finding.get("line") or finding.get("new_line"),
                reason="line_not_in_diff",
            )
            skipped += 1
            continue
        body = finding_body(finding)
        if cfg.dry_run:
            record_finding(
                project=project,
                iid=number,
                sha=sha,
                fingerprint=fp,
                finding=finding,
                status=FindingStatus.PLANNED,
                run_id=run_id,
            )
            emit_finding_metric(
                telemetry,
                repo=project,
                status=FindingStatus.PLANNED,
                finding=finding,
                dry_run=True,
            )
            log(
                "finding_planned",
                project=project,
                iid=number,
                file=_position_file(position),
                line=_position_line(position),
            )
            planned += 1
        else:
            comment_id = provider.post_inline_comment(cfg, token, project, number, body, position)
            if not comment_id:
                record_finding(
                    project=project,
                    iid=number,
                    sha=sha,
                    fingerprint=fp,
                    finding=finding,
                    status=FindingStatus.PENDING_EXTERNAL_ID,
                    run_id=run_id,
                )
                emit_finding_metric(
                    telemetry,
                    repo=project,
                    status=FindingStatus.PENDING_EXTERNAL_ID,
                    finding=finding,
                    dry_run=False,
                )
                log(
                    "finding_pending_external_id",
                    project=project,
                    iid=number,
                    file=_position_file(position),
                    line=_position_line(position),
                )
                skipped += 1
                continue
            record_finding(
                project=project,
                iid=number,
                sha=sha,
                fingerprint=fp,
                finding=finding,
                status=FindingStatus.POSTED,
                discussion_id=comment_id,
                run_id=run_id,
            )
            emit_finding_metric(
                telemetry,
                repo=project,
                status=FindingStatus.POSTED,
                finding=finding,
                dry_run=False,
            )
            log(
                "finding_posted",
                project=project,
                iid=number,
                file=_position_file(position),
                line=_position_line(position),
                discussion_id=comment_id,
            )
            posted += 1
    return (posted, planned, skipped)


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
    repo: Path | None = None
    try:
        cfg = read_config()
        provider = get_provider(cfg)
        token = provider.token()
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
        )
        with telemetry.span("llm_review.run", repo=project, mr_iid=iid, sha=sha, run_id=run_id):
            repo = paths.WORK / slug(project) / str(iid) / sha[:12]
            provider.checkout(cfg, project, mr, repo)
            env = reviewer_env(os.environ, rendered_prompt, cfg.max_findings_per_merge_request)
            result = run(
                [*cfg.reviewer_command, provider.review_prompt(project, mr, cfg)],
                cwd=repo,
                timeout=cfg.timeout_seconds,
                env=env,
            )
            safe_stdout = redact_secrets(result.stdout)
            report.write_text(safe_stdout, encoding="utf-8")
            tokens = parse_codex_token_usage(result.stdout)
            cost_usd = estimate_cost_usd(tokens, cfg.telemetry_config.price_for(model))
            if result.returncode:
                raise RuntimeError(f"review exited {result.returncode}")
            posted, planned, skipped = post_or_plan_findings(
                cfg=cfg,
                token=token,
                project=project,
                mr=mr,
                raw_review=safe_stdout,
                run_id=run_id,
                telemetry=telemetry,
                provider=provider,
            )
            status = (
                ReviewStatus.NO_FINDINGS
                if (posted, planned, skipped) == (0, 0, 0)
                else ReviewStatus.SUCCESS
            )
            if status == ReviewStatus.NO_FINDINGS:
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
            record_review_run_finish(
                run_id=run_id,
                status=status,
                tokens=tokens,
                cost_usd=cost_usd,
                error=None,
            )
            telemetry.record_review_done(
                repo=project,
                model=model,
                status=status,
                review_mode=ReviewMode.DIFF,
                dry_run=cfg.dry_run,
                duration_seconds=round(time.monotonic() - started, 2),
                tokens=tokens,
                cost_usd=cost_usd,
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
                report=str(report),
                run_id=run_id,
            )
            return 0
    except Exception as exc:
        error = redact_secrets(str(exc))
        # Preserve the agent transcript if we already wrote one; put the
        # error in a sibling `.error` file so debug info survives a
        # failure mid-write.
        if not report.exists() or not report.read_text(encoding="utf-8").strip():
            report.write_text(error, encoding="utf-8")
        else:
            report.with_suffix(report.suffix + ".error").write_text(error, encoding="utf-8")
        record(project, iid, sha, ReviewStatus.FAILED, str(report), error)
        if cfg is not None:
            record_review_run_finish(
                run_id=run_id,
                status=ReviewStatus.FAILED,
                tokens=tokens,
                cost_usd=cost_usd,
                error=error,
            )
        if telemetry is not None:
            telemetry.record_failure(
                repo=project, error_type=type(exc).__name__, operation="review"
            )
            telemetry.record_review_done(
                repo=project,
                model=model,
                status=ReviewStatus.FAILED,
                review_mode=ReviewMode.DIFF,
                dry_run=cfg.dry_run if cfg is not None else True,
                duration_seconds=round(time.monotonic() - started, 2),
                tokens=tokens,
                cost_usd=cost_usd,
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
    threshold_seconds = cfg.timeout_seconds * 3
    latest = latest_reviewed_row()
    if latest is None:
        log(
            "health_check",
            verdict="empty",
            threshold_seconds=threshold_seconds,
            note="no rows yet — newly installed or never run",
        )
        # Empty state is not failure on a fresh install; cron will create
        # rows on the first cycle.
        return 0
    status, updated_at = latest
    from bubo.db import status_age_seconds as _status_age_seconds

    age = _status_age_seconds(updated_at)
    verdict = "ok" if age <= threshold_seconds else "stale"
    log(
        "health_check",
        verdict=verdict,
        last_status=status,
        last_updated_at=str(updated_at),
        age_seconds=age,
        threshold_seconds=threshold_seconds,
    )
    return 0 if verdict == "ok" else 1


def sync_outcomes(limit: int = 200) -> int:
    """Refresh provider-side outcomes for up to ``limit`` posted findings.

    Provider-agnostic — fetches each posted comment's current state via
    ``provider.fetch_outcome``. Records per-finding state (resolved /
    disputed / deleted / etc.) and emits one telemetry event per
    non-default outcome. Touches ``last_checked_at`` even on failure so a
    persistently-broken finding cannot head-of-line block the queue.
    """
    init_db()
    cfg = read_config()
    provider = get_provider(cfg)
    token = provider.token()
    telemetry = ReviewTelemetry.from_config(cfg.telemetry_config)
    bot_username = provider.bot_username()
    synced = 0
    for finding in posted_findings_for_outcome_sync(limit):
        project = finding["project"]
        iid = int(finding["iid"])
        try:
            outcome = provider.fetch_outcome(
                cfg, token, project, iid, finding["discussion_id"], bot_username
            )
            record_finding_outcome(
                project=project,
                iid=iid,
                sha=finding["sha"],
                fingerprint=finding["fingerprint"],
                discussion_id=finding["discussion_id"],
                outcome=outcome,
            )
            for name in (
                "resolved",
                "deleted",
                "developer_replied",
                "disputed",
                "false_positive",
                "duplicate",
            ):
                if outcome[name] and telemetry.config.emit_outcome_sync:
                    telemetry.record_finding(
                        repo=project,
                        status=name,
                        finding={"type": "unknown", "severity": "unknown", "category": "unknown"},
                        dry_run=False,
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
    log("outcome_sync_done", synced=synced)
    return synced


def backfill_gitlab_bot_comments(updated_after: str, limit: int = 500) -> int:
    """Import already-posted GitLab bot discussions into local metrics state."""
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
                discussion_id = str(discussion.get("id") or "")
                existing = _existing_finding_for_discussion(project, iid, discussion_id)
                outcome = gitlab.classify_discussion_outcome(
                    discussion, bot_username=bot_username, mr_state=str(mr.get("state") or "")
                )
                if existing is not None:
                    record_finding_outcome(
                        project=project,
                        iid=iid,
                        sha=existing["sha"],
                        fingerprint=existing["fingerprint"],
                        discussion_id=discussion_id,
                        outcome=outcome,
                    )
                    continue
                position = note.get("position") or {}
                finding = _finding_from_bot_note(note, position)
                sha = str(position.get("head_sha") or provider.head_sha(mr))
                fingerprint = stable_hash(
                    {
                        "project": project,
                        "iid": iid,
                        "sha": sha,
                        "discussion_id": discussion.get("id"),
                        "note_id": note.get("id"),
                    }
                )
                _db_record_finding(
                    project=project,
                    iid=iid,
                    sha=sha,
                    fingerprint=fingerprint,
                    finding=finding,
                    status=FindingStatus.POSTED,
                    body=str(note.get("body") or ""),
                    discussion_id=discussion_id,
                    note_id=str(note.get("id") or ""),
                )
                record_finding_outcome(
                    project=project,
                    iid=iid,
                    sha=sha,
                    fingerprint=fingerprint,
                    discussion_id=discussion_id,
                    outcome=outcome,
                )
                imported += 1
    log("backfill_done", imported=imported)
    return imported


def _existing_finding_for_discussion(
    project: str, iid: int, discussion_id: str
) -> JsonObject | None:
    with connect_db() as db:
        row = db.execute(
            """
            select sha,fingerprint from review_findings
            where project=? and iid=? and discussion_id=? and status=?
            order by case when run_id is null then 1 else 0 end
            limit 1
            """,
            (project, iid, discussion_id, FindingStatus.POSTED),
        ).fetchone()
    if row is None:
        return None
    return {"sha": str(row[0]), "fingerprint": str(row[1])}


def _first_bot_note(discussion: JsonObject, bot_username: str) -> JsonObject | None:
    for item in discussion.get("notes") or []:
        if not isinstance(item, dict):
            continue
        note = cast(JsonObject, item)
        if ((note.get("author") or {}).get("username") or "") == bot_username:
            return note
    return None


def _finding_from_bot_note(note: JsonObject, position: JsonObject) -> JsonObject:
    body = str(note.get("body") or "")
    first_line = body.splitlines()[0] if body else ""
    match = _NOTE_HEADER.match(first_line)
    confidence_match = _NOTE_CONFIDENCE.search(body)
    finding: JsonObject = {
        "file": position.get("new_path") or position.get("old_path") or "",
        "line": position.get("new_line") or position.get("old_line"),
        "body": body,
        "confidence": float(confidence_match.group(1)) if confidence_match else None,
    }
    if match:
        finding.update(
            {
                "type": match.group(1).lower(),
                "severity": match.group(2).lower(),
                "category": match.group(3).lower(),
                "title": match.group(4).strip(),
            }
        )
    return finding


def backfill_github_bot_comments(updated_after: str, limit: int = 500) -> int:
    """Import already-posted GitHub bot review threads into local metrics state.

    The GitHub analogue of :func:`backfill_gitlab_bot_comments`. Walks PRs
    updated at/after ``updated_after``, then each PR's review threads via
    GraphQL (so resolution state is real), records the bot's root comment as
    a POSTED finding, and upserts its outcome. Correlates to any existing
    row by the stored comment id so a re-run is idempotent.
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
                existing = _existing_finding_for_discussion(project, number, discussion_id)
                if existing is not None:
                    record_finding_outcome(
                        project=project,
                        iid=number,
                        sha=existing["sha"],
                        fingerprint=existing["fingerprint"],
                        discussion_id=discussion_id,
                        outcome=outcome,
                    )
                    continue
                node_id = str(root.get("node_id") or "")
                finding = _finding_from_github_comment(root)
                fingerprint = stable_hash(
                    {
                        "project": project,
                        "iid": number,
                        "sha": head_sha,
                        "discussion_id": discussion_id,
                        "note_id": node_id,
                    }
                )
                _db_record_finding(
                    project=project,
                    iid=number,
                    sha=head_sha,
                    fingerprint=fingerprint,
                    finding=finding,
                    status=FindingStatus.POSTED,
                    body=str(root.get("body") or ""),
                    discussion_id=discussion_id,
                    note_id=node_id,
                )
                record_finding_outcome(
                    project=project,
                    iid=number,
                    sha=head_sha,
                    fingerprint=fingerprint,
                    discussion_id=discussion_id,
                    outcome=outcome,
                )
                imported += 1
    log("backfill_done", imported=imported)
    return imported


def _finding_from_github_comment(comment: JsonObject) -> JsonObject:
    """Reconstruct a finding dict from a backfilled GitHub bot comment.

    Mirrors :func:`_finding_from_bot_note` for GitHub's review-comment shape
    (``path``/``line`` instead of GitLab's ``position``).
    """
    body = str(comment.get("body") or "")
    first_line = body.splitlines()[0] if body else ""
    match = _NOTE_HEADER.match(first_line)
    confidence_match = _NOTE_CONFIDENCE.search(body)
    finding: JsonObject = {
        "file": comment.get("path") or "",
        "line": comment.get("line"),
        "body": body,
        "confidence": float(confidence_match.group(1)) if confidence_match else None,
    }
    if match:
        finding.update(
            {
                "type": match.group(1).lower(),
                "severity": match.group(2).lower(),
                "category": match.group(3).lower(),
                "title": match.group(4).strip(),
            }
        )
    return finding


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
    "mcp_call_tool",
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
