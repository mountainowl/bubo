from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from bubo import gitlab, paths, poller
from bubo.review_config import ReviewConfig
from bubo.telemetry.config import TelemetryConfig


class _FakeProvider:
    """Minimal provider stand-in for sync_outcomes tests."""

    name = "fake"

    def __init__(self, outcome=None, error=None):
        self._outcome = outcome
        self._error = error

    def token(self) -> str:
        return "token"

    def bot_username(self) -> str:
        return "bubo"

    def fetch_outcome(self, cfg, token, project, number, thread_id, bot_username):
        if self._error is not None:
            raise self._error
        return self._outcome


class _FakeGitLabProvider(_FakeProvider):
    name = "gitlab"

    def bot_username(self) -> str:
        return "lt-bubo"

    def head_sha(self, change):
        return change.get("sha") or "head-sha"


class _FakeGitHubProvider(_FakeProvider):
    name = "github"

    def bot_username(self) -> str:
        return "lt-bubo"


def test_classify_discussion_outcome_uses_explicit_manual_markers() -> None:
    discussion = {
        "id": "disc1",
        "resolved": True,
        "notes": [
            {"author": {"username": "bubo"}, "body": "finding"},
            {"author": {"username": "dev1"}, "body": "[llm-review:false-positive] not an issue"},
            {"author": {"username": "dev2"}, "body": "[llm-review:duplicate] already covered"},
        ],
    }

    outcome = gitlab.classify_discussion_outcome(discussion, bot_username="bubo", mr_state="merged")

    assert outcome["resolved"] is True
    assert outcome["developer_replied"] is True
    assert outcome["false_positive"] is True
    assert outcome["duplicate"] is True
    assert outcome["merged_unresolved"] is False


def test_classify_discussion_outcome_extracts_finding_and_reply_text() -> None:
    # GitLab path: the LLM reply classifier needs the bot's finding and the
    # developer's reply in original case.
    discussion = {
        "id": "disc1",
        "resolved": True,
        "notes": [
            {"author": {"username": "bubo"}, "body": "The Finding Body"},
            {"author": {"username": "dev1"}, "body": "Working As Intended"},
        ],
    }
    outcome = gitlab.classify_discussion_outcome(discussion, bot_username="bubo", mr_state="merged")
    assert outcome["_finding_text"] == "The Finding Body"
    assert "Working As Intended" in outcome["_reply_text"]


def test_classify_discussion_outcome_marks_unresolved_after_merge() -> None:
    discussion = {"id": "disc1", "resolved": False, "notes": []}

    outcome = gitlab.classify_discussion_outcome(discussion, bot_username="bubo", mr_state="merged")

    assert outcome["resolved"] is False
    assert outcome["merged_unresolved"] is True


def test_classify_discussion_outcome_uses_note_resolved_state() -> None:
    discussion = {
        "id": "disc1",
        "notes": [
            {
                "author": {"username": "bubo"},
                "body": "finding",
                "resolvable": True,
                "resolved": True,
            }
        ],
    }

    outcome = gitlab.classify_discussion_outcome(discussion, bot_username="bubo", mr_state="merged")

    assert outcome["resolved"] is True
    assert outcome["merged_unresolved"] is False


def test_outcome_sync_flag_suppresses_outcome_metric_emission() -> None:
    class FakeTelemetry:
        config = TelemetryConfig(enabled=True, emit_outcome_sync=False)

        def __init__(self) -> None:
            self.finding_metrics = 0

        def record_finding(self, **_: object) -> None:
            self.finding_metrics += 1

        def record_failure(self, **_: object) -> None:
            raise AssertionError("unexpected failure metric")

    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            with sqlite3.connect(paths.DB) as db:
                db.execute(
                    """
                    insert into review_findings(project,iid,sha,fingerprint,file,line,status,discussion_id,body,updated_at)
                    values(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "group/repo",
                        1,
                        "sha",
                        "fp",
                        "src/A.py",
                        1,
                        "posted",
                        "disc",
                        "body",
                        poller.now(),
                    ),
                )
            fake = FakeTelemetry()
            cfg = ReviewConfig(gitlab_url="https://gitlab.com", telemetry_config=fake.config)
            provider = _FakeProvider(
                outcome={
                    "resolved": True,
                    "deleted": False,
                    "developer_replied": False,
                    "disputed": False,
                    "false_positive": False,
                    "duplicate": False,
                    "resolved_at": None,
                    "merged_unresolved": False,
                }
            )

            with patch("bubo.poller.read_config", return_value=cfg):
                with patch("bubo.poller.get_provider", return_value=provider):
                    with patch("bubo.poller.ReviewTelemetry.from_config", return_value=fake):
                        assert poller.sync_outcomes(limit=1) == 1

            assert fake.finding_metrics == 0
    finally:
        paths.DB = original_db


def _seed_posted_finding(db_path: Path) -> None:
    with sqlite3.connect(db_path) as db:
        db.execute(
            """
            insert into review_findings(project,iid,sha,fingerprint,file,line,status,discussion_id,body,updated_at)
            values(?,?,?,?,?,?,?,?,?,?)
            """,
            ("group/repo", 1, "sha", "fp", "src/A.py", 1, "posted", "disc", "body", poller.now()),
        )


def _replied_unresolved_outcome() -> dict[str, object]:
    return {
        "resolved": True,
        "deleted": False,
        "developer_replied": True,
        "disputed": False,
        "false_positive": False,
        "duplicate": False,
        "resolved_at": None,
        "merged_unresolved": False,
        "_finding_text": "the finding",
        "_reply_text": "this is working as intended",
    }


class _SilentTelemetry:
    config = TelemetryConfig(enabled=True, emit_outcome_sync=False)

    def record_finding(self, **_: object) -> None:
        pass

    def record_failure(self, **_: object) -> None:
        raise AssertionError("unexpected failure metric")


def test_outcome_sync_classifies_rejecting_reply_as_disputed() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            _seed_posted_finding(paths.DB)
            fake = _SilentTelemetry()
            cfg = ReviewConfig(gitlab_url="https://gitlab.com", telemetry_config=fake.config)
            provider = _FakeProvider(outcome=_replied_unresolved_outcome())
            rejected = {"verdict": "rejected", "false_positive": False}
            with patch("bubo.poller.read_config", return_value=cfg):
                with patch("bubo.poller.get_provider", return_value=provider):
                    with patch("bubo.poller.ReviewTelemetry.from_config", return_value=fake):
                        with patch(
                            "bubo.poller.classify_developer_reply", return_value=rejected
                        ) as mocked:
                            assert poller.sync_outcomes(limit=1) == 1
            mocked.assert_called_once()
            with sqlite3.connect(paths.DB) as db:
                row = db.execute(
                    "select disputed, reply_classified from finding_outcomes"
                ).fetchone()
            assert row == (1, 1)
    finally:
        paths.DB = original_db


def test_outcome_sync_classifies_each_finding_only_once() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            _seed_posted_finding(paths.DB)
            fake = _SilentTelemetry()
            cfg = ReviewConfig(gitlab_url="https://gitlab.com", telemetry_config=fake.config)
            provider = _FakeProvider(outcome=_replied_unresolved_outcome())
            accepted = {"verdict": "accepted", "false_positive": False}
            with patch("bubo.poller.read_config", return_value=cfg):
                with patch("bubo.poller.get_provider", return_value=provider):
                    with patch("bubo.poller.ReviewTelemetry.from_config", return_value=fake):
                        with patch(
                            "bubo.poller.classify_developer_reply", return_value=accepted
                        ) as mocked:
                            poller.sync_outcomes(limit=1)
                            # reply_classified is now set; a second pass must
                            # not re-invoke the (paid) LLM classifier.
                            poller.sync_outcomes(limit=1)
            assert mocked.call_count == 1
    finally:
        paths.DB = original_db


def test_outcome_sync_retries_after_transient_classifier_error() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            _seed_posted_finding(paths.DB)
            fake = _SilentTelemetry()
            cfg = ReviewConfig(gitlab_url="https://gitlab.com", telemetry_config=fake.config)
            provider = _FakeProvider(outcome=_replied_unresolved_outcome())
            error = {"verdict": "error", "false_positive": False}
            with patch("bubo.poller.read_config", return_value=cfg):
                with patch("bubo.poller.get_provider", return_value=provider):
                    with patch("bubo.poller.ReviewTelemetry.from_config", return_value=fake):
                        with patch(
                            "bubo.poller.classify_developer_reply", return_value=error
                        ) as mocked:
                            poller.sync_outcomes(limit=1)
                            # A transient error must NOT mark the finding
                            # classified, so the next sync retries it.
                            poller.sync_outcomes(limit=1)
            assert mocked.call_count == 2
            with sqlite3.connect(paths.DB) as db:
                row = db.execute(
                    "select disputed, reply_classified from finding_outcomes"
                ).fetchone()
            assert row == (0, 0)
    finally:
        paths.DB = original_db


def test_outcome_sync_caps_reply_classifications_per_run() -> None:
    original_db = paths.DB
    original_cap = poller.MAX_REPLY_CLASSIFICATIONS_PER_SYNC
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            with sqlite3.connect(paths.DB) as db:
                for i in range(3):
                    db.execute(
                        """
                        insert into review_findings(project,iid,sha,fingerprint,file,line,status,discussion_id,body,updated_at)
                        values(?,?,?,?,?,?,?,?,?,?)
                        """,
                        ("group/repo", 1, "sha", f"fp{i}", "src/A.py", 1, "posted",
                         f"disc{i}", "body", poller.now()),
                    )
            poller.MAX_REPLY_CLASSIFICATIONS_PER_SYNC = 2
            fake = _SilentTelemetry()
            cfg = ReviewConfig(gitlab_url="https://gitlab.com", telemetry_config=fake.config)
            provider = _FakeProvider(outcome=_replied_unresolved_outcome())
            accepted = {"verdict": "accepted", "false_positive": False}
            with patch("bubo.poller.read_config", return_value=cfg):
                with patch("bubo.poller.get_provider", return_value=provider):
                    with patch("bubo.poller.ReviewTelemetry.from_config", return_value=fake):
                        with patch(
                            "bubo.poller.classify_developer_reply", return_value=accepted
                        ) as mocked:
                            # Three eligible findings, cap of two -> only two
                            # LLM calls this run; the third drains next time.
                            poller.sync_outcomes(limit=10)
            assert mocked.call_count == 2
    finally:
        poller.MAX_REPLY_CLASSIFICATIONS_PER_SYNC = original_cap
        paths.DB = original_db


def test_outcome_sync_failure_advances_last_checked_at() -> None:
    class FakeTelemetry:
        config = TelemetryConfig(enabled=True, emit_outcome_sync=True)

        def record_finding(self, **_: object) -> None:
            raise AssertionError("unexpected finding metric")

        def record_failure(self, **_: object) -> None:
            return None

    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            with sqlite3.connect(paths.DB) as db:
                db.execute(
                    """
                    insert into review_findings(project,iid,sha,fingerprint,file,line,status,discussion_id,body,updated_at)
                    values(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "group/repo",
                        1,
                        "sha",
                        "fp",
                        "src/A.py",
                        1,
                        "posted",
                        "missing-disc",
                        "body",
                        poller.now(),
                    ),
                )
            fake = FakeTelemetry()
            cfg = ReviewConfig(gitlab_url="https://gitlab.com", telemetry_config=fake.config)
            provider = _FakeProvider(error=RuntimeError("404"))

            with patch("bubo.poller.read_config", return_value=cfg):
                with patch("bubo.poller.get_provider", return_value=provider):
                    with patch("bubo.poller.ReviewTelemetry.from_config", return_value=fake):
                        assert poller.sync_outcomes(limit=1) == 0

            with sqlite3.connect(paths.DB) as db:
                row = db.execute(
                    "select discussion_id,last_checked_at from finding_outcomes where fingerprint='fp'"
                ).fetchone()

            assert row[0] == "missing-disc"
            assert row[1]
    finally:
        paths.DB = original_db


def test_backfill_gitlab_bot_comments_imports_resolved_discussions() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            cfg = ReviewConfig(gitlab_url="https://gitlab.com", projects=["group/repo"])
            discussion = {
                "id": "disc1",
                "resolved": True,
                "notes": [
                    {
                        "id": 99,
                        "author": {"username": "lt-bubo"},
                        "created_at": "2026-05-29T00:00:00Z",
                        "body": "**Issue (blocking, correctness):** bad path\n\n**Confidence:** 0.91",
                        "position": {
                            "head_sha": "sha",
                            "new_path": "src/A.java",
                            "new_line": 12,
                        },
                        "resolvable": True,
                        "resolved": True,
                    }
                ],
            }

            with patch("bubo.poller.read_config", return_value=cfg):
                with patch("bubo.poller.get_provider", return_value=_FakeGitLabProvider()):
                    with patch(
                        "bubo.poller.gitlab.merge_requests_updated_after",
                        return_value=[{"iid": 7, "state": "opened", "sha": "sha"}],
                    ):
                        with patch(
                            "bubo.poller.gitlab.get_mr_discussions",
                            return_value=[discussion],
                        ):
                            assert poller.backfill_gitlab_bot_comments("2026-05-25T00:00:00Z") == 1

            with sqlite3.connect(paths.DB) as db:
                finding = db.execute(
                    "select file,line,status,discussion_id,note_id,type,severity,category,confidence from review_findings"
                ).fetchone()
                outcome = db.execute(
                    "select resolved,developer_replied from finding_outcomes"
                ).fetchone()

            assert finding == (
                "src/A.java",
                12,
                "posted",
                "disc1",
                "99",
                "issue",
                "blocking",
                "correctness",
                0.91,
            )
            assert outcome == (1, 0)
    finally:
        paths.DB = original_db


def test_backfill_gitlab_bot_comments_reuses_existing_discussion_row() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            with sqlite3.connect(paths.DB) as db:
                db.execute(
                    """
                    insert into review_findings(project,iid,sha,fingerprint,file,line,status,discussion_id,body,updated_at,run_id)
                    values(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "group/repo",
                        7,
                        "sha",
                        "existing-fp",
                        "src/A.java",
                        12,
                        "posted",
                        "disc1",
                        "body",
                        poller.now(),
                        "run-1",
                    ),
                )
            cfg = ReviewConfig(gitlab_url="https://gitlab.com", projects=["group/repo"])
            discussion = {
                "id": "disc1",
                "resolved": True,
                "notes": [
                    {
                        "id": 99,
                        "author": {"username": "lt-bubo"},
                        "created_at": "2026-05-29T00:00:00Z",
                        "body": "**Issue (blocking, correctness):** bad path\n\n**Confidence:** 0.91",
                        "position": {"head_sha": "sha", "new_path": "src/A.java", "new_line": 12},
                        "resolvable": True,
                        "resolved": True,
                    }
                ],
            }

            with patch("bubo.poller.read_config", return_value=cfg):
                with patch("bubo.poller.get_provider", return_value=_FakeGitLabProvider()):
                    with patch(
                        "bubo.poller.gitlab.merge_requests_updated_after",
                        return_value=[{"iid": 7, "state": "opened", "sha": "sha"}],
                    ):
                        with patch(
                            "bubo.poller.gitlab.get_mr_discussions",
                            return_value=[discussion],
                        ):
                            assert poller.backfill_gitlab_bot_comments("2026-05-25T00:00:00Z") == 0

            with sqlite3.connect(paths.DB) as db:
                finding_count = db.execute("select count(*) from review_findings").fetchone()[0]
                outcome = db.execute("select fingerprint,resolved from finding_outcomes").fetchone()

            assert finding_count == 1
            assert outcome == ("existing-fp", 1)
    finally:
        paths.DB = original_db


def test_backfill_github_bot_comments_imports_resolved_threads() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            cfg = ReviewConfig(provider="github", projects=["o/r"])
            pr = {
                "number": 42,
                "state": "open",
                "updated_at": "2026-05-29T00:00:00Z",
                "head": {"sha": "sha"},
            }
            thread = {
                "is_resolved": True,
                "comments": [
                    {
                        "database_id": 555,
                        "node_id": "PRRC_x",
                        "login": "lt-bubo",
                        "body": (
                            "**Issue (blocking, correctness):** bad path\n\n**Confidence:** 0.91"
                        ),
                        "path": "src/A.java",
                        "line": 12,
                    },
                    {"database_id": 556, "node_id": "PRRC_y", "login": "dev1", "body": "thanks"},
                ],
            }

            with patch("bubo.poller.read_config", return_value=cfg):
                with patch("bubo.poller.get_provider", return_value=_FakeGitHubProvider()):
                    with patch("bubo.poller.github.pulls_updated_after", return_value=[pr]):
                        with patch(
                            "bubo.poller.github.get_pr_review_threads",
                            return_value=[thread],
                        ):
                            imported = poller.backfill_github_bot_comments("2026-05-25T00:00:00Z")
                            assert imported == 1

            with sqlite3.connect(paths.DB) as db:
                finding = db.execute(
                    "select file,line,status,discussion_id,note_id,type,severity,category,confidence"
                    " from review_findings"
                ).fetchone()
                outcome = db.execute(
                    "select resolved,developer_replied from finding_outcomes"
                ).fetchone()

            assert finding == (
                "src/A.java",
                12,
                "posted",
                "555",
                "PRRC_x",
                "issue",
                "blocking",
                "correctness",
                0.91,
            )
            # is_resolved=True and a non-bot reply present.
            assert outcome == (1, 1)
    finally:
        paths.DB = original_db


def test_backfill_github_bot_comments_reuses_existing_discussion_row() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            with sqlite3.connect(paths.DB) as db:
                db.execute(
                    """
                    insert into review_findings(project,iid,sha,fingerprint,file,line,status,discussion_id,body,updated_at,run_id)
                    values(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        "o/r",
                        42,
                        "sha",
                        "existing-fp",
                        "src/A.java",
                        12,
                        "posted",
                        "555",
                        "body",
                        poller.now(),
                        "run-1",
                    ),
                )
            cfg = ReviewConfig(provider="github", projects=["o/r"])
            pr = {
                "number": 42,
                "state": "closed",
                "merged": True,
                "updated_at": "2026-05-29T00:00:00Z",
                "head": {"sha": "sha"},
            }
            thread = {
                "is_resolved": True,
                "comments": [
                    {
                        "database_id": 555,
                        "node_id": "PRRC_x",
                        "login": "lt-bubo",
                        "body": "**Issue (blocking, correctness):** bad path",
                        "path": "src/A.java",
                        "line": 12,
                    }
                ],
            }

            with patch("bubo.poller.read_config", return_value=cfg):
                with patch("bubo.poller.get_provider", return_value=_FakeGitHubProvider()):
                    with patch("bubo.poller.github.pulls_updated_after", return_value=[pr]):
                        with patch(
                            "bubo.poller.github.get_pr_review_threads",
                            return_value=[thread],
                        ):
                            assert poller.backfill_github_bot_comments("2026-05-25T00:00:00Z") == 0

            with sqlite3.connect(paths.DB) as db:
                finding_count = db.execute("select count(*) from review_findings").fetchone()[0]
                outcome = db.execute("select fingerprint,resolved from finding_outcomes").fetchone()

            assert finding_count == 1
            assert outcome == ("existing-fp", 1)
    finally:
        paths.DB = original_db


def test_backfill_github_bot_comments_noops_on_gitlab_provider() -> None:
    original_db = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            poller.init_db()
            cfg = ReviewConfig(provider="gitlab", projects=["group/repo"])
            with patch("bubo.poller.read_config", return_value=cfg):
                with patch("bubo.poller.get_provider", return_value=_FakeGitLabProvider()):
                    assert poller.backfill_github_bot_comments("2026-05-25T00:00:00Z") == 0
    finally:
        paths.DB = original_db
