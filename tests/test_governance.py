"""Governance wiring tests (Rec ②a, Phase 1).

Covers config parsing, the write-once provenance DB layer, provider
``list_commits`` mapping, and the off-by-default poller glue
(:func:`bubo.poller.capture_provenance`).
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from bubo import db, paths
from bubo.governance_config import DEFAULT_AI_TRAILER_PATTERNS
from bubo.provenance import ProvenanceSignal
from bubo.review_config import ReviewConfig, review_config_from_dict
from bubo.statuses import ReviewMode


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


def _seed_run(run_id: str = "rid", project: str = "g/r") -> None:
    db.record_review_run_start(
        run_id=run_id,
        project=project,
        iid=1,
        sha="sha",
        model="m",
        prompt_version="pv",
        review_mode=ReviewMode.DIFF,
        dry_run=True,
    )


# --- config -----------------------------------------------------------------


def test_governance_config_defaults_off() -> None:
    gov = review_config_from_dict({}).governance_config
    assert gov.capture_provenance is False
    assert gov.ai_trailer_patterns == list(DEFAULT_AI_TRAILER_PATTERNS)
    assert gov.sensitive_path_globs == []


def test_governance_config_parses_block_preserving_case() -> None:
    gov = review_config_from_dict(
        {
            "governance": {
                "capture_provenance": True,
                "ai_trailer_patterns": [r"^X-AI:"],
                "sensitive_path_globs": ["Payments/**", "*.PEM"],
            }
        }
    ).governance_config
    assert gov.capture_provenance is True
    assert gov.ai_trailer_patterns == [r"^X-AI:"]
    # Case must be preserved (paths are case-sensitive on Linux).
    assert gov.sensitive_path_globs == ["Payments/**", "*.PEM"]


# --- db: write-once provenance ---------------------------------------------


def test_record_and_read_provenance_round_trips() -> None:
    with _temp_db():
        _seed_run()
        db.record_provenance(
            "rid",
            ProvenanceSignal(
                band="likely_ai",
                source="trailer",
                confidence="declared",
                ai_signals=["Generated-by: GPT-4"],
                sensitive_paths=["payments/charge.py"],
            ),
        )
        got = db.provenance_for("rid")

    assert got == {
        "band": "likely_ai",
        "source": "trailer",
        "confidence": "declared",
        "ai_signals": ["Generated-by: GPT-4"],
        "sensitive_paths": ["payments/charge.py"],
    }


def test_record_provenance_is_write_once() -> None:
    with _temp_db():
        _seed_run()
        db.record_provenance("rid", ProvenanceSignal(band="likely_ai", source="trailer"))
        # A second write must NOT overwrite — audit integrity.
        db.record_provenance("rid", ProvenanceSignal(band="collaborative", source="trailer"))
        got = db.provenance_for("rid")

    assert got is not None
    assert got["band"] == "likely_ai"


def test_provenance_for_absent_run_is_none() -> None:
    with _temp_db():
        # No run row at all, and a seeded run with no provenance, both -> None.
        assert db.provenance_for("missing") is None
        _seed_run()
        assert db.provenance_for("rid") is None


# --- providers: list_commits mapping ---------------------------------------


def test_gitlab_list_commits_maps_fields() -> None:
    from bubo.scm.gitlab import GitLabProvider

    raw = [{"id": "abc", "title": "t", "message": "feat: x", "author_name": "Jane"}]
    with patch("bubo.gitlab.get_mr_commits", return_value=raw):
        out = GitLabProvider().list_commits(ReviewConfig(), "tok", "g/r", 1)
    assert out == [{"sha": "abc", "message": "feat: x", "author": "Jane"}]


def test_github_list_commits_maps_nested_fields() -> None:
    from bubo.scm.github import GitHubProvider

    raw = [{"sha": "abc", "commit": {"message": "feat: x", "author": {"name": "Jane"}}}]
    with patch("bubo.github.get_pr_commits", return_value=raw):
        out = GitHubProvider().list_commits(ReviewConfig(), "tok", "o/r", 1)
    assert out == [{"sha": "abc", "message": "feat: x", "author": "Jane"}]


# --- poller glue: off by default -------------------------------------------


class _ExplodingProvider:
    """Any data fetch is a failure — used to prove the disabled path is inert."""

    def list_commits(self, *a: object, **k: object) -> list:
        raise AssertionError("list_commits must not be consulted when capture is off")

    def changed_lines(self, *a: object, **k: object) -> dict:
        raise AssertionError("changed_lines must not be consulted when capture is off")


class _FakeProvider:
    def __init__(self, commits: list[dict], paths_: list[str]) -> None:
        self._commits = commits
        self._paths = paths_

    def list_commits(self, *a: object, **k: object) -> list[dict]:
        return self._commits

    def changed_lines(self, *a: object, **k: object) -> dict[str, dict]:
        return {p: {} for p in self._paths}


def test_capture_provenance_disabled_consults_nothing() -> None:
    from bubo import poller

    cfg = ReviewConfig()  # capture_provenance defaults False
    with _temp_db():
        _seed_run()
        poller.capture_provenance(
            cfg,
            token="t",
            project="g/r",
            number=1,
            run_id="rid",
            provider=_ExplodingProvider(),  # type: ignore[arg-type]
        )
        # Nothing recorded.
        assert db.provenance_for("rid") is None


def test_capture_provenance_enabled_records_and_logs() -> None:
    from bubo import poller
    from bubo.governance_config import GovernanceConfig

    cfg = ReviewConfig(
        governance_config=GovernanceConfig(
            capture_provenance=True,
            sensitive_path_globs=["payments/**"],
        )
    )
    provider = _FakeProvider(
        commits=[{"sha": "a", "message": "feat: x\n\nGenerated-by: GPT-4", "author": "Jane"}],
        paths_=["payments/charge.py", "src/util.py"],
    )
    events: list[tuple[str, dict]] = []

    def _capture(event: str, **fields: object) -> None:
        events.append((event, fields))

    with _temp_db():
        _seed_run()
        with patch.object(poller, "log", _capture):
            poller.capture_provenance(
                cfg,
                token="t",
                project="g/r",
                number=1,
                run_id="rid",
                provider=provider,  # type: ignore[arg-type]
            )
        got = db.provenance_for("rid")

    assert got is not None
    assert got["band"] == "likely_ai"
    assert got["source"] == "trailer"
    assert got["sensitive_paths"] == ["payments/charge.py"]
    captured = [f for name, f in events if name == "provenance_captured"]
    assert len(captured) == 1
    assert captured[0]["band"] == "likely_ai"
    assert captured[0]["sensitive_paths"] == ["payments/charge.py"]


def test_capture_provenance_failure_is_soft() -> None:
    from bubo import poller
    from bubo.governance_config import GovernanceConfig

    cfg = ReviewConfig(governance_config=GovernanceConfig(capture_provenance=True))

    class _Boom:
        def list_commits(self, *a: object, **k: object) -> list:
            raise RuntimeError("api down")

        def changed_lines(self, *a: object, **k: object) -> dict:
            return {}

    events: list[tuple[str, dict]] = []
    with _temp_db():
        _seed_run()
        with patch.object(poller, "log", lambda e, **f: events.append((e, f))):
            # Must NOT raise — provenance is additive.
            poller.capture_provenance(
                cfg,
                token="t",
                project="g/r",
                number=1,
                run_id="rid",
                provider=_Boom(),  # type: ignore[arg-type]
            )
        assert db.provenance_for("rid") is None
    assert any(name == "provenance_capture_failed" for name, _ in events)
