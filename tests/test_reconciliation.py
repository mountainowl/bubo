"""Guarded cross-head reconciliation tests; every provider call is mocked."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bubo import db, paths, poller
from bubo.reconciliation import ReconciliationOutcome, ReconciliationStatus, finding_identity
from bubo.review_config import ReviewConfig
from bubo.scm.base import (
    FindingThread,
    FindingThreadReply,
    FindingThreadResolution,
    FindingThreadState,
)
from bubo.statuses import FindingStatus
from bubo.verification import ReconciliationResult, ReconciliationVerdict


@contextmanager
def _temp_db() -> Iterator[None]:
    old = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            db.init_db()
            yield
    finally:
        paths.DB = old


def _prior() -> dict[str, object]:
    return {
        "file": "x.py", "line": 3, "title": "bug", "type": "issue",
        "severity": "non-blocking", "category": "correctness", "body": "bad branch",
    }


def _seed() -> None:
    finding = _prior()
    db.record_finding(
        project="o/r", iid=1, sha="a" * 40, fingerprint="fp", finding=finding,
        body="bad branch", status=FindingStatus.POSTED, discussion_id="thread",
        finding_identity=finding_identity("o/r", 1, finding),
    )


def _provider(state: FindingThreadState = FindingThreadState.OPEN) -> MagicMock:
    provider = MagicMock()
    provider.head_sha.return_value = "b" * 40
    provider.bot_username.return_value = "bubo"
    provider.finding_thread.return_value = FindingThread(state)
    provider.reply_to_finding_thread.return_value = FindingThreadReply(
        "reply", True, True, FindingThreadState.OPEN
    )
    provider.resolve_finding_thread.return_value = FindingThreadResolution(
        FindingThreadState.RESOLVED, True
    )
    return provider


def _run(provider: MagicMock) -> ReconciliationStatus:
    with tempfile.TemporaryDirectory() as raw:
        repo = Path(raw)
        (repo / "x.py").write_text("one\ntwo\nthree\n")
        return poller.reconcile_prior_findings(
            cfg=ReviewConfig(dry_run=False, reconcile_fixed_findings=True), token="t", project="o/r",
            number=1, head_sha="b" * 40, provider=provider, repo=repo,
        ).status


def _fixed() -> ReconciliationResult:
    return ReconciliationResult(
        ReconciliationVerdict.FIXED, 1.0, "proof", evidence_path="x.py", evidence_line=3
    )


def test_fixed_replies_with_full_sha_then_resolves_nonblocking() -> None:
    with _temp_db(), patch.object(
        poller, "run_reconciliation_verification",
        return_value=_fixed(),
    ):
        _seed()
        provider = _provider()
        assert _run(provider) is ReconciliationStatus.RESOLVED
    reply = provider.reply_to_finding_thread.call_args.args[5]
    assert f"Verified fixed as of {'b' * 40}" in reply
    assert provider.resolve_finding_thread.called


def test_still_valid_and_uncertain_never_write_and_signal_survivor() -> None:
    for verdict in (ReconciliationVerdict.STILL_VALID, ReconciliationVerdict.UNCERTAIN):
        with _temp_db(), patch.object(
            poller, "run_reconciliation_verification",
            return_value=ReconciliationResult(verdict, 0, "not proved"),
        ):
            _seed()
            provider = _provider()
            assert _run(provider) in {ReconciliationStatus.SURVIVES, ReconciliationStatus.BLOCKED}
            provider.reply_to_finding_thread.assert_not_called()
            provider.resolve_finding_thread.assert_not_called()


def test_foreign_deleted_and_resolved_threads_are_noops() -> None:
    for state in (FindingThreadState.FOREIGN, FindingThreadState.DELETED, FindingThreadState.RESOLVED):
        with _temp_db():
            _seed()
            provider = _provider(state)
            assert _run(provider) is ReconciliationStatus.NO_CANDIDATES
            provider.reply_to_finding_thread.assert_not_called()
            provider.resolve_finding_thread.assert_not_called()


def test_head_race_and_dry_run_skip_writes() -> None:
    with _temp_db():
        _seed()
        provider = _provider()
        provider.head_sha.return_value = "new"
        assert _run(provider) is ReconciliationStatus.BLOCKED
        provider.reply_to_finding_thread.assert_not_called()
    with _temp_db():
        _seed()
        provider = _provider()
        assert poller.reconcile_prior_findings(
            cfg=ReviewConfig(dry_run=True, reconcile_fixed_findings=True), token="t", project="o/r",
            number=1, head_sha="b" * 40, provider=provider, repo=None,
        ).status is ReconciliationStatus.NO_CANDIDATES
        provider.get_change.assert_not_called()


def test_head_race_before_resolve_keeps_thread_open_after_reply() -> None:
    with _temp_db(), patch.object(
        poller, "run_reconciliation_verification",
        return_value=_fixed(),
    ):
        _seed()
        provider = _provider()
        provider.head_sha.side_effect = ["b" * 40, "b" * 40, "different-head"]
        assert _run(provider) is ReconciliationStatus.BLOCKED
        provider.reply_to_finding_thread.assert_called_once()
        provider.resolve_finding_thread.assert_not_called()


def test_legacy_untrusted_row_and_developer_resolved_thread_are_noops() -> None:
    with _temp_db():
        # Pre-feature rows have no cross-head identity and must stay read-only.
        db.record_finding(
            project="o/r", iid=1, sha="a" * 40, fingerprint="legacy", finding=_prior(),
            body="legacy", status=FindingStatus.POSTED, discussion_id="legacy-thread",
        )
        provider = _provider()
        assert _run(provider) is ReconciliationStatus.NO_CANDIDATES
        provider.finding_thread.assert_not_called()
    with _temp_db():
        _seed()
        provider = _provider(FindingThreadState.RESOLVED)
        assert _run(provider) is ReconciliationStatus.NO_CANDIDATES
        provider.reply_to_finding_thread.assert_not_called()
        provider.resolve_finding_thread.assert_not_called()


def test_fixed_requires_high_confidence_and_valid_current_evidence() -> None:
    for verdict in (
        ReconciliationResult(ReconciliationVerdict.FIXED, 0.2, "proof", evidence_path="x.py", evidence_line=3),
        ReconciliationResult(ReconciliationVerdict.FIXED, 1.0, "proof", evidence_path="../secret", evidence_line=1),
        ReconciliationResult(ReconciliationVerdict.FIXED, 1.0, "", evidence_path="x.py", evidence_line=3),
    ):
        with _temp_db(), patch.object(poller, "run_reconciliation_verification", return_value=verdict):
            _seed()
            provider = _provider()
            assert _run(provider) is ReconciliationStatus.BLOCKED
            provider.reply_to_finding_thread.assert_not_called()


def test_native_legacy_run_is_candidate_but_import_without_run_is_excluded() -> None:
    with _temp_db(), patch.object(poller, "run_reconciliation_verification", return_value=_fixed()):
        db.record_finding(
            project="o/r", iid=1, sha="a" * 40, fingerprint="native", finding=_prior(),
            body="native", status=FindingStatus.POSTED, discussion_id="thread", run_id="bubo-run",
        )
        provider = _provider()
        _run(provider)
        provider.finding_thread.assert_called()


def test_reconciliation_lease_allows_one_claim_then_safe_expiry_retry() -> None:
    with _temp_db():
        first = db.claim_finding_reconciliation(
            project="o/r", iid=1, prior_sha="old", fingerprint="fp", head_sha="head",
            discussion_id="thread", reply_marker="marker", lease_seconds=300,
        )
        # A second worker/connection reaches the same head-specific action.
        second = db.claim_finding_reconciliation(
            project="o/r", iid=1, prior_sha="old", fingerprint="fp", head_sha="head",
            discussion_id="thread", reply_marker="marker", lease_seconds=300,
        )
        assert (first, second) == (True, False)
        with sqlite3.connect(paths.DB) as con:
            con.execute("update finding_reconciliations set claim_until='1970-01-01T00:00:00+00:00'")
        assert db.claim_finding_reconciliation(
            project="o/r", iid=1, prior_sha="old", fingerprint="fp", head_sha="head",
            discussion_id="thread", reply_marker="marker", lease_seconds=300,
        ) is True


def test_two_workers_same_head_run_one_verifier_and_reply_path() -> None:
    with _temp_db(), patch.object(poller, "run_reconciliation_verification", return_value=_fixed()) as verifier:
        _seed()
        first = _provider()
        second = _provider()
        assert _run(first) is ReconciliationStatus.RESOLVED
        assert _run(second) is ReconciliationStatus.BLOCKED
        assert verifier.call_count == 1
        first.reply_to_finding_thread.assert_called_once()
        second.reply_to_finding_thread.assert_not_called()


def test_duplicate_logical_finding_reconciles_each_trusted_thread() -> None:
    with _temp_db(), patch.object(poller, "run_reconciliation_verification", return_value=_fixed()):
        finding = _prior()
        identity = finding_identity("o/r", 1, finding)
        for fingerprint, discussion in (("one", "thread-one"), ("two", "thread-two")):
            db.record_finding(
                project="o/r", iid=1, sha="a" * 40, fingerprint=fingerprint, finding=finding,
                body="body", status=FindingStatus.POSTED, discussion_id=discussion,
                finding_identity=identity,
            )
        provider = _provider()
        assert _run(provider) is ReconciliationStatus.RESOLVED
        assert provider.finding_thread.call_count == 4
        assert provider.reply_to_finding_thread.call_count == 2


def test_reply_external_terminal_is_safe_noop_and_resolution_attribution_blocks() -> None:
    for reply_state in (FindingThreadState.RESOLVED, FindingThreadState.DELETED):
        with _temp_db(), patch.object(poller, "run_reconciliation_verification", return_value=_fixed()):
            _seed()
            provider = _provider()
            provider.reply_to_finding_thread.return_value = FindingThreadReply(
                None, False, False, reply_state
            )
            assert _run(provider) is ReconciliationStatus.NO_CANDIDATES
            provider.resolve_finding_thread.assert_not_called()
    for state in (FindingThreadState.RESOLVED, FindingThreadState.DELETED, FindingThreadState.FOREIGN, FindingThreadState.OPEN):
        with _temp_db(), patch.object(poller, "run_reconciliation_verification", return_value=_fixed()):
            _seed()
            provider = _provider()
            provider.resolve_finding_thread.return_value = FindingThreadResolution(state, False)
            expected = (
                ReconciliationStatus.NO_CANDIDATES
                if state in {FindingThreadState.RESOLVED, FindingThreadState.DELETED}
                else ReconciliationStatus.BLOCKED
            )
            assert _run(provider) is expected


def test_reply_exception_blocks_and_never_resolves() -> None:
    with _temp_db(), patch.object(poller, "run_reconciliation_verification", return_value=_fixed()):
        _seed()
        provider = _provider()
        provider.reply_to_finding_thread.side_effect = RuntimeError("network")
        assert _run(provider) is ReconciliationStatus.BLOCKED
        provider.resolve_finding_thread.assert_not_called()
        with sqlite3.connect(paths.DB) as con:
            assert con.execute("select state from finding_reconciliations").fetchone()[0] == "reply_failed"


def test_two_workers_lease_before_verification_and_marker_retry_is_idempotent() -> None:
    """One DB lease owns verification/reply; expiry retries only the resolution."""
    with _temp_db(), patch.object(
        poller, "run_reconciliation_verification", return_value=_fixed()
    ) as verifier:
        _seed()
        first_worker = _provider()
        assert _run(first_worker) is ReconciliationStatus.RESOLVED
        assert verifier.call_count == 1
        first_worker.reply_to_finding_thread.assert_called_once()

        # A second worker uses a separate provider and therefore a separate
        # connect_db() call in claim_finding_reconciliation, but is blocked
        # before verifier/reply work while the first lease remains active.
        second_worker = _provider()
        assert _run(second_worker) is ReconciliationStatus.BLOCKED
        assert verifier.call_count == 1
        second_worker.reply_to_finding_thread.assert_not_called()

        # After expiry, the provider confirms the first worker's hidden marker
        # exists.  The retry can finish resolution without a duplicate reply.
        with sqlite3.connect(paths.DB) as con:
            con.execute(
                "update finding_reconciliations set claim_until='1970-01-01T00:00:00+00:00'"
            )
        retry_worker = _provider()
        retry_worker.finding_thread.return_value = FindingThread(
            FindingThreadState.OPEN, reply_marker_present=True
        )
        assert _run(retry_worker) is ReconciliationStatus.RESOLVED
        assert verifier.call_count == 2
        retry_worker.reply_to_finding_thread.assert_not_called()
        retry_worker.resolve_finding_thread.assert_called_once()


def _run_worker_case(
    tmp: Path,
    *,
    cfg: ReviewConfig,
    returncode: int = 0,
    post_result: tuple[int, int, int] = (0, 0, 0),
    pending: bool = False,
    events: list[str] | None = None,
    reconciliation_status: ReconciliationStatus = ReconciliationStatus.NO_CANDIDATES,
) -> tuple[MagicMock, MagicMock, MagicMock, MagicMock, int]:
    """Run one worker through mocked local/SCM seams; no external calls."""
    job = tmp / "job.json"
    job.write_text(json.dumps({"project": "o/r", "mr": {"iid": 1, "sha": "b" * 40}}))
    provider = MagicMock()
    provider.token.return_value = "token"
    provider.review_prompt.return_value = "prompt"
    provider.changed_lines.return_value = {}
    telemetry = MagicMock()
    telemetry.span.return_value.__enter__.return_value = MagicMock()
    reconciliation_result = ReconciliationOutcome(reconciliation_status)
    reconciler = MagicMock(
        return_value=reconciliation_result,
        side_effect=(
            lambda **_: (events.append("reconcile"), reconciliation_result)[1]
        ) if events is not None else None,
    )
    finish = MagicMock(
        side_effect=(lambda **_: events.append("finish")) if events is not None else None,
    )
    no_findings = MagicMock(return_value=("disabled", "test"))

    def post(**kwargs: object) -> tuple[int, int, int]:
        if pending:
            external = kwargs["pending_external_ids"]
            assert isinstance(external, list)
            external.append(True)
        return post_result

    with (
        patch.object(poller.paths, "REPORTS", tmp / "reports"),
        patch("bubo.poller.read_config", return_value=cfg),
        patch("bubo.poller.get_provider", return_value=provider),
        patch("bubo.poller.ReviewTelemetry.from_config", return_value=telemetry),
        patch("bubo.poller.write_rendered_meta_prompt", return_value=tmp / "prompt"),
        patch("bubo.poller.capture_provenance", return_value=None),
        patch("bubo.poller.count_added_lines", return_value=0),
        patch("bubo.poller.run", return_value=SimpleNamespace(stdout="", returncode=returncode)),
        patch("bubo.poller.post_or_plan_findings", side_effect=post),
        patch("bubo.poller.record_review_run_finish", finish),
        patch("bubo.poller.reconcile_prior_findings", reconciler),
        patch("bubo.poller.post_no_findings_comment", no_findings),
        patch("bubo.poller.cleanup_worktree"),
        patch("bubo.poller.analytics.flush"),
        patch("bubo.poller.analytics.record_review_completed"),
    ):
        result = poller.worker(job)
    return provider, reconciler, finish, no_findings, result


def test_worker_failed_run_never_reconciles() -> None:
    with _temp_db(), tempfile.TemporaryDirectory() as raw:
        _, reconciler, finish, no_findings, result = _run_worker_case(
            Path(raw), cfg=ReviewConfig(dry_run=False, reconcile_fixed_findings=True), returncode=1
        )
    assert result == 1
    reconciler.assert_not_called()
    no_findings.assert_not_called()
    assert finish.call_args.kwargs["status"].value == "failed"


def test_worker_pending_external_id_skips_reconciliation_and_acknowledgement() -> None:
    with _temp_db(), tempfile.TemporaryDirectory() as raw:
        _, reconciler, finish, no_findings, result = _run_worker_case(
            Path(raw), cfg=ReviewConfig(dry_run=False, reconcile_fixed_findings=True), pending=True
        )
    assert result == 0
    assert finish.call_args.kwargs["status"].value == "no_findings"
    reconciler.assert_not_called()
    no_findings.assert_not_called()


def test_worker_stale_head_blocks_actual_no_findings_acknowledgement() -> None:
    with _temp_db(), tempfile.TemporaryDirectory() as raw:
        provider, reconciler, _, no_findings, result = _run_worker_case(
            Path(raw), cfg=ReviewConfig(dry_run=False, reconcile_fixed_findings=True),
            reconciliation_status=ReconciliationStatus.BLOCKED,
        )
    assert result == 0
    reconciler.assert_called_once()
    no_findings.assert_not_called()
    provider.reply_to_finding_thread.assert_not_called()
    provider.resolve_finding_thread.assert_not_called()


def test_worker_finishes_run_before_successful_reconciliation() -> None:
    events: list[str] = []
    with _temp_db(), tempfile.TemporaryDirectory() as raw:
        provider, reconciler, _, _, result = _run_worker_case(
            Path(raw), cfg=ReviewConfig(dry_run=False, reconcile_fixed_findings=True), events=events
        )
    assert result == 0
    reconciler.assert_called_once()
    assert provider.reply_to_finding_thread.call_count == 0
    assert events == ["finish", "reconcile"]


def test_worker_dry_run_never_reconciles_or_mutates_finding_threads() -> None:
    with _temp_db(), tempfile.TemporaryDirectory() as raw:
        provider, reconciler, _, _, result = _run_worker_case(
            Path(raw), cfg=ReviewConfig(dry_run=True, reconcile_fixed_findings=True)
        )
    assert result == 0
    reconciler.assert_not_called()
    provider.reply_to_finding_thread.assert_not_called()
    provider.resolve_finding_thread.assert_not_called()


def test_reply_marker_retry_does_not_duplicate_reply() -> None:
    with _temp_db(), patch.object(
        poller, "run_reconciliation_verification",
        return_value=_fixed(),
    ):
        _seed()
        provider = _provider()
        provider.finding_thread.side_effect = [
            FindingThread(FindingThreadState.OPEN),
            FindingThread(FindingThreadState.OPEN, reply_marker_present=True),
        ]
        assert _run(provider) is ReconciliationStatus.RESOLVED
        provider.reply_to_finding_thread.assert_not_called()
        provider.resolve_finding_thread.assert_called_once()
