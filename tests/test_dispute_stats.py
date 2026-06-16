"""Tests for the read-only per-category dispute stats reader (Gap A surface).

Covers :func:`bubo.db.disputed_class_stats` — the raw, config-independent
truth behind :func:`bubo.db.disputed_finding_classes`. The two readers share
the same join/normalization via ``_dispute_class_rows``; these tests pin the
stats reader's rates, dilution semantics, the ``min_samples`` gate, ordering,
and the empty-project case, plus that it never mutates the schema.

Seeding mirrors ``tests/test_dispute_suppression.py`` so both readers are
exercised against the production schema (no stubbed DB layer).
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
    """A finding whose only outcome is a failed sync (disputed=0/fp=0).

    These dilute the denominator without ever counting as a dispute.
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


def test_rejected_vs_not_yields_raw_rate() -> None:
    with _temp_db():
        # 3 disputed + 2 accepted documentation findings → 3/5 = 0.6.
        for i in range(3):
            _seed_finding(project="g/r", category="documentation", index=i, disputed=True)
        for i in range(3, 5):
            _seed_finding(project="g/r", category="documentation", index=i)

        stats = db.disputed_class_stats("g/r", min_samples=1)

    assert len(stats) == 1
    row = stats[0]
    assert row["category"] == "documentation"
    assert row["total"] == 5
    assert row["rejected"] == 3
    assert row["dispute_rate"] == 0.6  # raw float, not rounded


def test_false_positive_counts_as_rejected() -> None:
    with _temp_db():
        for i in range(2):
            _seed_finding(project="g/r", category="style", index=i, false_positive=True)
        for i in range(2, 4):
            _seed_finding(project="g/r", category="style", index=i)

        stats = db.disputed_class_stats("g/r", min_samples=1)

    assert stats[0]["rejected"] == 2  # false_positive rows count as rejected
    assert stats[0]["dispute_rate"] == 0.5


def test_sync_failure_rows_dilute_the_denominator() -> None:
    with _temp_db():
        # 3 genuine disputes + 5 sync-failure rows → 3/8 = 0.375 (diluted).
        for i in range(3):
            _seed_finding(project="g/r", category="documentation", index=i, disputed=True)
        for i in range(5):
            _seed_sync_failure(project="g/r", category="documentation", index=i)

        stats = db.disputed_class_stats("g/r", min_samples=1)

    assert stats[0]["total"] == 8  # sync rows sit in the denominator
    assert stats[0]["rejected"] == 3
    assert stats[0]["dispute_rate"] == 3 / 8


def test_min_samples_gate_drops_thin_classes() -> None:
    with _temp_db():
        # documentation: 5 rows (kept); performance: 3 rows (gated out at 5).
        for i in range(5):
            _seed_finding(project="g/r", category="documentation", index=i, disputed=True)
        for i in range(3):
            _seed_finding(project="g/r", category="performance", index=i, disputed=True)

        gated = db.disputed_class_stats("g/r", min_samples=5)
        ungated = db.disputed_class_stats("g/r", min_samples=1)

    assert {r["category"] for r in gated} == {"documentation"}
    assert {r["category"] for r in ungated} == {"documentation", "performance"}


def test_ordering_is_rate_desc_then_category() -> None:
    with _temp_db():
        # security: 1/2 = 0.5 ; documentation: 3/3 = 1.0 ; style: 1/2 = 0.5.
        _seed_finding(project="g/r", category="security", index=0, disputed=True)
        _seed_finding(project="g/r", category="security", index=1)
        for i in range(3):
            _seed_finding(project="g/r", category="documentation", index=i, disputed=True)
        _seed_finding(project="g/r", category="style", index=0, disputed=True)
        _seed_finding(project="g/r", category="style", index=1)

        stats = db.disputed_class_stats("g/r", min_samples=1)

    # documentation (1.0) first, then the 0.5 pair tie-broken by category asc.
    assert [r["category"] for r in stats] == ["documentation", "security", "style"]


def test_empty_project_returns_empty_list() -> None:
    with _temp_db():
        # No findings/outcomes for this project at all.
        stats = db.disputed_class_stats("nobody/here", min_samples=1)
    assert stats == []


def test_scoped_per_project() -> None:
    with _temp_db():
        for i in range(3):
            _seed_finding(project="a/repo", category="documentation", index=i, disputed=True)
        for i in range(3):
            _seed_finding(project="b/repo", category="documentation", index=i)

        a_stats = db.disputed_class_stats("a/repo", min_samples=1)
        b_stats = db.disputed_class_stats("b/repo", min_samples=1)

    assert a_stats[0]["dispute_rate"] == 1.0
    assert b_stats[0]["dispute_rate"] == 0.0


def test_reader_does_not_mutate_schema() -> None:
    with _temp_db():
        _seed_finding(project="g/r", category="documentation", index=0, disputed=True)
        with sqlite3.connect(paths.DB) as con:
            before = {
                r[0]
                for r in con.execute(
                    "select name from sqlite_master where type='table'"
                ).fetchall()
            }
        db.disputed_class_stats("g/r", min_samples=1)
        with sqlite3.connect(paths.DB) as con:
            after = {
                r[0]
                for r in con.execute(
                    "select name from sqlite_master where type='table'"
                ).fetchall()
            }
    assert before == after


def test_matches_disputed_finding_classes_predicate() -> None:
    """The stats reader and the suppression-set reader must not drift.

    For the same data + thresholds, applying the suppression predicate to the
    raw stats must reproduce ``disputed_finding_classes`` exactly.
    """
    with _temp_db():
        # documentation 0.6 (suppressed at 0.5/5); security 0.4 (kept).
        for i in range(3):
            _seed_finding(project="g/r", category="documentation", index=i, disputed=True)
        for i in range(3, 5):
            _seed_finding(project="g/r", category="documentation", index=i)
        for i in range(2):
            _seed_finding(project="g/r", category="security", index=i, disputed=True)
        for i in range(2, 5):
            _seed_finding(project="g/r", category="security", index=i)

        stats = db.disputed_class_stats("g/r", min_samples=1)
        suppressed = db.disputed_finding_classes("g/r", min_samples=5, threshold=0.5)

    derived = {
        r["category"]
        for r in stats
        if r["total"] >= 5 and r["dispute_rate"] >= 0.5
    }
    assert derived == suppressed == {"documentation"}
