"""Tests for dispute-driven finding-class suppression (Gap A).

Covers the DB-side aggregation (:func:`bubo.db.disputed_finding_classes`)
and the load-bearing "off by default" guarantee wired through the poller.
The pure filter behaviour lives in ``test_inline_review.py``.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from bubo import db, paths


@contextmanager
def _temp_db() -> Iterator[None]:
    original = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            db.init_db()
            yield
    finally:
        paths.DB = original


def _outcome(*, disputed: bool = False, false_positive: bool = False) -> dict[str, object]:
    """A minimal classify_discussion_outcome dict for record_finding_outcome."""
    return {
        "resolved": True,
        "deleted": False,
        "developer_replied": True,
        "disputed": disputed,
        "false_positive": false_positive,
        "duplicate": False,
        "merged_unresolved": False,
    }


def _seed_finding(
    *,
    project: str,
    category: str,
    index: int,
    disputed: bool = False,
    false_positive: bool = False,
) -> None:
    """Record one finding plus its outcome row under ``(project, category)``."""
    fingerprint = f"{category}-{index}"
    db.record_finding(
        project=project,
        iid=1,
        sha="sha",
        fingerprint=fingerprint,
        finding={"category": category, "file": "f.py", "line": 1, "confidence": 0.99},
        status=db.FindingStatus.POSTED,
        body="body",
        discussion_id=f"disc-{fingerprint}",
    )
    db.record_finding_outcome(
        project=project,
        iid=1,
        sha="sha",
        fingerprint=fingerprint,
        discussion_id=f"disc-{fingerprint}",
        outcome=_outcome(disputed=disputed, false_positive=false_positive),
    )


def _seed_sync_failure(*, project: str, category: str, index: int) -> None:
    """Record a finding whose only outcome row came from a failed sync.

    These rows carry disputed=0/false_positive=0, so they sit in the
    denominator without ever counting as a dispute — the deliberate
    dilution that biases the aggregate toward under-suppression.
    """
    fingerprint = f"{category}-sync-{index}"
    db.record_finding(
        project=project,
        iid=1,
        sha="sha",
        fingerprint=fingerprint,
        finding={"category": category, "file": "f.py", "line": 1, "confidence": 0.99},
        status=db.FindingStatus.POSTED,
        body="body",
        discussion_id=f"disc-{fingerprint}",
    )
    db.record_finding_outcome_sync_attempt(
        project=project,
        iid=1,
        sha="sha",
        fingerprint=fingerprint,
        discussion_id=f"disc-{fingerprint}",
    )


def test_disputed_class_crossing_threshold_is_suppressed() -> None:
    with _temp_db():
        # 5 documentation findings, 3 disputed → rate 0.6 ≥ 0.5.
        for i in range(3):
            _seed_finding(project="g/r", category="documentation", index=i, disputed=True)
        for i in range(3, 5):
            _seed_finding(project="g/r", category="documentation", index=i)

        result = db.disputed_finding_classes("g/r", min_samples=5, threshold=0.5)

    assert result == {"documentation"}


def test_false_positive_counts_as_a_dispute() -> None:
    with _temp_db():
        # 3 marked false_positive (without `disputed`) + 2 accepted → 0.6.
        for i in range(3):
            _seed_finding(project="g/r", category="style", index=i, false_positive=True)
        for i in range(3, 5):
            _seed_finding(project="g/r", category="style", index=i)

        result = db.disputed_finding_classes("g/r", min_samples=5, threshold=0.5)

    assert result == {"style"}


def test_class_below_threshold_is_kept() -> None:
    with _temp_db():
        # 5 security findings, 2 disputed → rate 0.4 < 0.5.
        for i in range(2):
            _seed_finding(project="g/r", category="security", index=i, disputed=True)
        for i in range(2, 5):
            _seed_finding(project="g/r", category="security", index=i)

        result = db.disputed_finding_classes("g/r", min_samples=5, threshold=0.5)

    assert result == set()


def test_class_below_min_samples_is_kept_even_at_full_dispute() -> None:
    with _temp_db():
        # 4 performance findings, all disputed → rate 1.0 but only 4 samples.
        for i in range(4):
            _seed_finding(project="g/r", category="performance", index=i, disputed=True)

        result = db.disputed_finding_classes("g/r", min_samples=5, threshold=0.5)

    assert result == set()


def test_sync_failure_rows_dilute_the_denominator() -> None:
    with _temp_db():
        # 3 genuine disputes (≥ min_samples) but 5 sync-failure rows drag the
        # rate to 3/8 = 0.375 < 0.5, so the class is NOT suppressed. This
        # pins the conservative, under-suppressing bias.
        for i in range(3):
            _seed_finding(project="g/r", category="documentation", index=i, disputed=True)
        for i in range(5):
            _seed_sync_failure(project="g/r", category="documentation", index=i)

        result = db.disputed_finding_classes("g/r", min_samples=3, threshold=0.5)

    assert result == set()


def test_suppression_is_scoped_per_project() -> None:
    with _temp_db():
        # Project A disputes "documentation" hard; project B never does.
        for i in range(5):
            _seed_finding(project="a/repo", category="documentation", index=i, disputed=True)
        for i in range(5):
            _seed_finding(project="b/repo", category="documentation", index=i)

        suppressed_a = db.disputed_finding_classes("a/repo", min_samples=5, threshold=0.5)
        suppressed_b = db.disputed_finding_classes("b/repo", min_samples=5, threshold=0.5)

    assert suppressed_a == {"documentation"}
    assert suppressed_b == set()


def test_post_or_plan_does_not_consult_db_when_disabled() -> None:
    """Off by default: the disputed-class query must not even run.

    The user's explicit constraint is "disabled by default". A category
    that would trip every threshold still posts everything, and
    ``disputed_finding_classes`` is never called.
    """
    from bubo import poller
    from bubo.review_config import ReviewConfig

    cfg = ReviewConfig(dry_run=True, suppress_disputed_classes=False)
    raw_review = (
        '[{"category": "documentation", "file": "f.py", "line": 1, '
        '"confidence": 0.99, "title": "doc nit", "type": "issue", '
        '"severity": "non-blocking", "impact": "x", "evidence": "y", "fix": "z"}]'
    )

    class _Provider:
        name = "gitlab"

        def change_number(self, mr):
            return mr["iid"]

        def get_change(self, *a, **k):
            return {}

        def changed_lines(self, *a, **k):
            return {}

        def build_position(self, *a, **k):
            return None  # forces SKIPPED, no real API calls

    # The poller imports the symbol by name, so patch it on the poller module.
    with _temp_db(), patch.object(
        poller, "disputed_finding_classes", side_effect=AssertionError("must not be consulted")
    ):
        posted, planned, skipped = poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project="g/r",
            mr={"iid": 1, "sha": "sha"},
            raw_review=raw_review,
            provider=_Provider(),
        )

    # The finding survived the (disabled) suppression filter and reached the
    # position-mapping stage, where it was skipped — never silently dropped.
    assert (posted, planned, skipped) == (0, 0, 1)


def test_post_or_plan_suppresses_a_disputed_class_when_enabled() -> None:
    """Enabled path: a finding in a repeatedly-disputed category is dropped.

    Mirror of the disabled-path test. With suppression on and a documentation
    class the repo has rejected, the same finding that otherwise reaches the
    position-mapping stage (and skips, 0/0/1) is now filtered out *before*
    any API call (0/0/0) and logged with reason `disputed_class_suppressed`.
    """
    from bubo import poller
    from bubo.review_config import ReviewConfig

    cfg = ReviewConfig(
        dry_run=True,
        suppress_disputed_classes=True,
        dispute_suppress_min_samples=5,
        dispute_suppress_threshold=0.5,
    )
    raw_review = (
        '[{"category": "documentation", "file": "f.py", "line": 1, '
        '"confidence": 0.99, "title": "doc nit", "type": "issue", '
        '"severity": "non-blocking", "impact": "x", "evidence": "y", "fix": "z"}]'
    )

    class _Provider:
        name = "gitlab"

        def change_number(self, mr):
            return mr["iid"]

        def get_change(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("suppressed finding must not reach change fetch")

        def changed_lines(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("suppressed finding must not reach diff fetch")

        def build_position(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("suppressed finding must not reach position mapping")

    events: list[tuple[str, dict[str, object]]] = []

    def _capture(event: str, **fields: object) -> None:
        events.append((event, fields))

    with _temp_db():
        # Build outcome history: 3 of 5 documentation findings disputed → 0.6.
        for i in range(3):
            _seed_finding(project="g/r", category="documentation", index=i, disputed=True)
        for i in range(3, 5):
            _seed_finding(project="g/r", category="documentation", index=i)

        with patch.object(poller, "log", _capture):
            posted, planned, skipped = poller.post_or_plan_findings(
                cfg=cfg,
                token="t",
                project="g/r",
                mr={"iid": 1, "sha": "sha"},
                raw_review=raw_review,
                provider=_Provider(),
            )

    # Dropped before any provider call (the _Provider methods all raise).
    assert (posted, planned, skipped) == (0, 0, 0)
    filtered = [
        fields for name, fields in events
        if name == "finding_filtered" and fields.get("reason") == "disputed_class_suppressed"
    ]
    assert len(filtered) == 1
    assert filtered[0].get("category") == "documentation"
