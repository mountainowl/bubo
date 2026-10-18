"""Outcome sync re-reads a developer's reply when it changes (#127).

Runs the real ``sync_outcomes`` and SQLite writers; only the provider and the
paid LLM classifier are faked.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from test_outcome_sync import _FakeProvider, _seed_posted_finding, _SilentTelemetry

from bubo import db, github, gitlab, paths, poller
from bubo.review_config import ReviewConfig

FIRST = "Thanks, looking at it."
REBUTTAL = "This is fine since we do expect cabins to be correct even for old paths."


def _outcome(reply: str) -> dict[str, object]:
    return {
        "resolved": True,
        "deleted": False,
        "developer_replied": True,
        "disputed": False,
        "false_positive": False,
        "duplicate": False,
        "resolved_at": None,
        "merged_unresolved": False,
        "_finding_text": "Disabled cabin thresholds still require parsing the cabin",
        "_reply_text": reply,
    }


def _verdict(_cfg: object, _finding: str, reply: str) -> dict[str, object]:
    lowered = reply.lower()
    if "good catch" in lowered:
        return {"verdict": "agrees", "false_positive": False}
    if "this is fine" in lowered:
        return {"verdict": "rejected", "false_positive": False}
    return {"verdict": "accepted", "false_positive": False}


@pytest.fixture
def state(tmp_path: Path) -> Iterator[list[str]]:
    old = paths.DB
    paths.DB = tmp_path / "reviewer.sqlite"
    poller.init_db()
    _seed_posted_finding(paths.DB)
    with sqlite3.connect(paths.DB) as conn:
        conn.execute("update review_findings set category='correctness'")
    calls: list[str] = []
    try:
        yield calls
    finally:
        paths.DB = old


def _sync(calls: list[str], reply: str) -> tuple[object, ...]:
    fake = _SilentTelemetry()
    cfg = ReviewConfig(gitlab_url="https://gitlab.com", telemetry_config=fake.config)

    def classify(cfg: object, finding: str, text: str) -> dict[str, object]:
        calls.append(text)
        return _verdict(cfg, finding, text)

    with (
        patch("bubo.poller.read_config", return_value=cfg),
        patch("bubo.poller.get_provider", return_value=_FakeProvider(outcome=_outcome(reply))),
        patch("bubo.poller.ReviewTelemetry.from_config", return_value=fake),
        patch("bubo.poller.classify_developer_reply", side_effect=classify),
    ):
        poller.sync_outcomes(limit=1)
    with sqlite3.connect(paths.DB) as conn:
        return conn.execute(
            "select disputed, developer_disposition, reply_classified from finding_outcomes"
        ).fetchone()


def test_rebuttal_after_first_classification_is_disputed(state: list[str]) -> None:
    assert _sync(state, FIRST) == (0, "unknown", 1)
    assert _sync(state, FIRST + "\n\n" + REBUTTAL) == (1, "disagrees", 1)
    assert len(state) == 2
    [row] = db.disputed_class_stats("group/repo", min_samples=1)
    assert (row["total"], row["rejected"]) == (1, 1)


def test_classifier_dispute_survives_later_syncs_without_a_new_call(state: list[str]) -> None:
    assert _sync(state, REBUTTAL) == (1, "disagrees", 1)
    assert _sync(state, REBUTTAL) == (1, "disagrees", 1)
    assert _sync(state, REBUTTAL) == (1, "disagrees", 1)
    assert len(state) == 1


def test_later_agreement_replaces_classifier_dispute(state: list[str]) -> None:
    assert _sync(state, REBUTTAL) == (1, "disagrees", 1)
    assert _sync(state, REBUTTAL + "\n\nActually, good catch, fixed.") == (0, "agrees", 1)


def test_later_acknowledgement_clears_classifier_dispute(state: list[str]) -> None:
    assert _sync(state, REBUTTAL) == (1, "disagrees", 1)
    assert _sync(state, "Will look at it.") == (0, "unknown", 1)


def test_transient_error_keeps_previous_verdict_and_retries(state: list[str]) -> None:
    assert _sync(state, REBUTTAL) == (1, "disagrees", 1)
    with patch(
        "test_reply_reclassification._verdict",
        return_value={"verdict": "error", "false_positive": False},
    ):
        assert _sync(state, REBUTTAL + "\n\nmore") == (1, "disagrees", 1)
    assert _sync(state, REBUTTAL + "\n\nmore") == (1, "disagrees", 1)
    assert len(state) == 3  # the errored text was retried


def test_gitlab_finding_from_an_earlier_bot_identity_is_not_a_reply() -> None:
    discussion = {
        "resolved": True,
        "notes": [{"author": {"username": "v-user"}, "body": "Issue: x", "resolvable": True}],
    }
    out = gitlab.classify_discussion_outcome(discussion, "llm-reviewer", "opened")
    assert out["developer_replied"] is False
    assert out["_finding_text"] == "Issue: x"
    discussion["notes"].append({"author": {"username": "dev"}, "body": REBUTTAL})
    out = gitlab.classify_discussion_outcome(discussion, "llm-reviewer", "opened")
    assert out["developer_replied"] is True
    assert out["_reply_text"] == REBUTTAL


def test_github_root_from_an_earlier_bot_identity_is_not_a_reply() -> None:
    thread = {
        "is_resolved": True,
        "comments": [
            # A token-backed bot posts as a GitHub User, like a person does.
            {
                "login": "old-bot",
                "actor_type": "User",
                "body": "Issue: x",
                "in_reply_to_node_id": "",
                "node_id": "R",
            },
        ],
    }
    out = github.classify_graphql_thread_outcome(thread, "new-bot", "open")
    assert out["developer_replied"] is False
    assert out["_finding_text"] == "Issue: x"
    thread["comments"].append(
        {
            "login": "dev",
            "actor_type": "User",
            "body": REBUTTAL,
            "in_reply_to_node_id": "R",
            "node_id": "C",
        }
    )
    out = github.classify_graphql_thread_outcome(thread, "new-bot", "open")
    assert out["developer_replied"] is True
    assert out["_reply_text"] == REBUTTAL
