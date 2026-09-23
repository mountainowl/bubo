"""Posting pipeline for reviewed findings.

The adapter keeps orchestration-specific seams in :mod:`bubo.poller` while
allowing this substantial lifecycle to stay independently readable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bubo.review_config import ReviewConfig
from bubo.scm import ScmProvider
from bubo.statuses import FindingStatus
from bubo.telemetry import ReviewTelemetry
from bubo.types import JsonObject
from bubo.verification import Verdict, decide, votes_summary


@dataclass(frozen=True)
class FindingPipelineDeps:
    """Small, explicit adapter for poller-owned side effects and seams."""

    extract_findings: Callable[[str], list[JsonObject]]
    prepare_findings: Callable[[ReviewConfig, str, int, list[JsonObject]], list[JsonObject]]
    sha_for: Callable[[JsonObject], str]
    fingerprint: Callable[[str, int, str, JsonObject], str]
    finding_seen: Callable[[str, int, str, str], bool]
    record_finding: Callable[..., None]
    emit_metric: Callable[..., None]
    log: Callable[..., None]
    run_verification: Callable[[JsonObject, Path | None, ReviewConfig], list[Verdict]]
    position_file: Callable[[JsonObject], Any]
    position_line: Callable[[JsonObject], Any]
    comment_body: Callable[[JsonObject, str], str]


def post_or_plan_findings(
    deps: FindingPipelineDeps,
    *,
    cfg: ReviewConfig,
    token: str,
    project: str,
    mr: JsonObject,
    raw_review: str,
    run_id: str | None = None,
    telemetry: ReviewTelemetry | None = None,
    provider: ScmProvider,
    repo: Path | None = None,
    changed: dict[str, JsonObject] | None = None,
) -> tuple[int, int, int]:
    """Parse, filter, verify, and post (or plan) findings for one change."""
    number = provider.change_number(mr)
    sha = deps.sha_for(mr)
    findings = deps.extract_findings(raw_review)
    if not findings:
        return (0, 0, 0)
    findings = deps.prepare_findings(cfg, project, number, findings)
    if not findings:
        return (0, 0, 0)
    change = provider.get_change(cfg, token, project, number)
    # The agent and its checkout reviewed ``mr``.  Never anchor that review
    # onto a newer head that arrived while the agent was running.
    current_sha = deps.sha_for(change)
    if current_sha and current_sha != sha:
        deps.log(
            "finding_posting_superseded",
            project=project,
            iid=number,
            reviewed_sha=sha,
            current_sha=current_sha,
        )
        return (0, 0, len(findings))
    if changed is None:
        changed = provider.changed_lines(cfg, token, project, number)
    posted = planned = skipped = 0
    verified_count = refuted_count = capped_count = verified_attempts = 0
    for finding_index, finding in enumerate(findings):
        fingerprint = deps.fingerprint(project, number, sha, finding)
        if deps.finding_seen(project, number, sha, fingerprint):
            skipped += 1
            continue
        position = provider.build_position(change, changed, finding)
        if not position:
            _record(deps, project, number, sha, fingerprint, finding, FindingStatus.SKIPPED, run_id)
            _emit(deps, telemetry, project, FindingStatus.SKIPPED, finding, cfg.dry_run)
            deps.log(
                "finding_skipped",
                project=project,
                iid=number,
                file=finding.get("file") or finding.get("path"),
                line=finding.get("line") or finding.get("new_line"),
                reason="line_not_in_diff",
            )
            skipped += 1
            continue
        verified, votes, refuted, capped = _verify(
            deps,
            cfg,
            finding,
            repo,
            telemetry,
            project,
            number,
            sha,
            fingerprint,
            position,
            run_id,
            verified_attempts,
        )
        if cfg.verify_findings and verified_attempts < cfg.verify_max_findings:
            verified_attempts += 1
        if verified:
            verified_count += 1
        if refuted:
            refuted_count += 1
            skipped += 1
            continue
        if capped:
            capped_count += 1
        body = deps.comment_body(finding, cfg.tone)
        if cfg.dry_run:
            _record(
                deps,
                project,
                number,
                sha,
                fingerprint,
                finding,
                FindingStatus.PLANNED,
                run_id,
                verified,
                votes,
            )
            _emit(deps, telemetry, project, FindingStatus.PLANNED, finding, True)
            deps.log(
                "finding_planned",
                project=project,
                iid=number,
                file=deps.position_file(position),
                line=deps.position_line(position),
            )
            planned += 1
            continue
        # Verification can take several minutes. Re-read the head directly
        # before its irreversible side effect so a later push never receives
        # a comment anchored to the reviewed commit.
        current_sha = deps.sha_for(provider.get_change(cfg, token, project, number))
        if current_sha and current_sha != sha:
            remaining = len(findings) - finding_index
            deps.log(
                "finding_posting_superseded",
                project=project,
                iid=number,
                reviewed_sha=sha,
                current_sha=current_sha,
                remaining_findings=remaining,
            )
            skipped += remaining
            break
        comment_id = provider.post_inline_comment(cfg, token, project, number, body, position)
        if not comment_id:
            _record(
                deps,
                project,
                number,
                sha,
                fingerprint,
                finding,
                FindingStatus.PENDING_EXTERNAL_ID,
                run_id,
                verified,
                votes,
            )
            _emit(deps, telemetry, project, FindingStatus.PENDING_EXTERNAL_ID, finding, False)
            deps.log(
                "finding_pending_external_id",
                project=project,
                iid=number,
                file=deps.position_file(position),
                line=deps.position_line(position),
            )
            skipped += 1
            continue
        _record(
            deps,
            project,
            number,
            sha,
            fingerprint,
            finding,
            FindingStatus.POSTED,
            run_id,
            verified,
            votes,
            comment_id,
        )
        _emit(deps, telemetry, project, FindingStatus.POSTED, finding, False)
        deps.log(
            "finding_posted",
            project=project,
            iid=number,
            file=deps.position_file(position),
            line=deps.position_line(position),
            discussion_id=comment_id,
        )
        posted += 1
    if cfg.verify_findings:
        deps.log(
            "verification_summary",
            project=project,
            iid=number,
            verified=verified_count,
            refuted=refuted_count,
            capped=capped_count,
            max_findings=cfg.verify_max_findings,
            lenses=len(cfg.verify_lenses),
        )
    return (posted, planned, skipped)


def _verify(
    deps: FindingPipelineDeps,
    cfg: ReviewConfig,
    finding: JsonObject,
    repo: Path | None,
    telemetry: ReviewTelemetry | None,
    project: str,
    number: int,
    sha: str,
    fingerprint: str,
    position: JsonObject,
    run_id: str | None,
    attempts: int,
) -> tuple[bool | None, str | None, bool, bool]:
    """Return verified flag, votes, whether refuted, and whether unavailable/capped."""
    if not cfg.verify_findings:
        return None, None, False, False
    if attempts >= cfg.verify_max_findings:
        deps.log(
            "finding_verify_capped",
            project=project,
            iid=number,
            file=deps.position_file(position),
            line=deps.position_line(position),
            cap=cfg.verify_max_findings,
        )
        return None, None, False, True
    verdicts = deps.run_verification(finding, repo, cfg)
    outcome = decide(
        verdicts, min_votes=cfg.verify_min_votes, confidence_floor=cfg.verify_confidence_floor
    )
    votes = votes_summary(verdicts)
    ran = sum(verdict.ok for verdict in verdicts)
    if outcome.survives:
        if telemetry is not None:
            telemetry.record_verification(repo=project, outcome="verified")
        deps.log(
            "finding_verified",
            project=project,
            iid=number,
            file=deps.position_file(position),
            line=deps.position_line(position),
            votes=f"{outcome.real_votes}/{outcome.total}",
        )
        return True, votes, False, False
    if ran >= cfg.verify_min_votes:
        _record(
            deps,
            project,
            number,
            sha,
            fingerprint,
            finding,
            FindingStatus.REFUTED,
            run_id,
            False,
            votes,
        )
        _emit(deps, telemetry, project, FindingStatus.REFUTED, finding, cfg.dry_run)
        if telemetry is not None:
            telemetry.record_verification(repo=project, outcome="refuted")
        deps.log(
            "finding_refuted",
            project=project,
            iid=number,
            file=deps.position_file(position),
            line=deps.position_line(position),
            votes=f"{outcome.real_votes}/{outcome.total}",
            min_votes=cfg.verify_min_votes,
        )
        return False, votes, True, False
    deps.log(
        "finding_verify_unavailable",
        project=project,
        iid=number,
        file=deps.position_file(position),
        line=deps.position_line(position),
        ran=ran,
        min_votes=cfg.verify_min_votes,
    )
    return None, None, False, False


def _record(
    deps: FindingPipelineDeps,
    project: str,
    number: int,
    sha: str,
    fingerprint: str,
    finding: JsonObject,
    status: FindingStatus,
    run_id: str | None,
    verified: bool | None = None,
    votes: str | None = None,
    discussion_id: str | None = None,
) -> None:
    deps.record_finding(
        project=project,
        iid=number,
        sha=sha,
        fingerprint=fingerprint,
        finding=finding,
        status=status,
        discussion_id=discussion_id,
        run_id=run_id,
        verified=verified,
        verify_votes=votes,
    )


def _emit(
    deps: FindingPipelineDeps,
    telemetry: ReviewTelemetry | None,
    project: str,
    status: FindingStatus,
    finding: JsonObject,
    dry_run: bool,
) -> None:
    deps.emit_metric(telemetry, repo=project, status=status, finding=finding, dry_run=dry_run)
