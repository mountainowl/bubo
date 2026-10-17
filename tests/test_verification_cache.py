import json
import sqlite3

import pytest

from bubo import db, paths

POLICY = '{"contract_version":1,"min_votes":1}'


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DB", tmp_path / "reviewer.sqlite")
    with sqlite3.connect(paths.DB) as connection:
        connection.execute(
            "create table review_findings "
            "(project text, iid integer, sha text, fingerprint text, verified integer, verify_votes text, status text)"
        )

    def put(verified, votes, status=None):
        with sqlite3.connect(paths.DB) as connection:
            connection.execute("delete from review_findings")
            connection.execute(
                "insert into review_findings values (?,?,?,?,?,?,?)",
                ("owner/repo", 7, "head", "fingerprint", verified, votes,
                 status or ("refuted" if verified == 0 else "posted")),
            )

    return put


def vote(real=True, **changes):
    return {
        "contract_version": 1, "policy": POLICY, "lens": "correctness", "real": real,
        "confidence": 0.95, "ok": True, "reason": "Visible deciding evidence", **changes,
    }


@pytest.mark.parametrize("decision", [True, False])
def test_current_decisive_verification_is_reusable(cache, decision):
    cache(int(decision), json.dumps([vote(decision)]))
    assert db.prior_verification("owner/repo", 7, "head", "fingerprint", expected_policy=POLICY) is decision


@pytest.mark.parametrize("key", [
    ("other/repo", 7, "head", "fingerprint"),
    ("owner/repo", 8, "head", "fingerprint"),
    ("owner/repo", 7, "other-head", "fingerprint"),
    ("owner/repo", 7, "head", "other-fingerprint"),
])
def test_cache_is_scoped_to_exact_finding_revision(cache, key):
    cache(1, json.dumps([vote()]))
    assert db.prior_verification(*key, expected_policy=POLICY) is None


@pytest.mark.parametrize("votes", [
    None, "invalid", "null", "{}", "[]", "[null]",
    json.dumps([vote(contract_version=True)]),
    json.dumps([vote(contract_version=2)]),
    json.dumps([vote(policy=None)]),
    json.dumps([vote(), vote(policy="changed-policy")]),
    json.dumps([vote(), {k: v for k, v in vote().items() if k != "contract_version"}]),
    json.dumps([vote(real="false")]),
    json.dumps([vote(confidence=float("nan"))]),
    json.dumps([vote(confidence=10**400)]),
    json.dumps([vote(confidence=True)]),
    json.dumps([vote(ok="true")]),
    json.dumps([vote(reason=None)]),
    json.dumps([vote(lens=None)]),
    json.dumps([vote(real=False)]),
    json.dumps([vote(ok=False)]),
    json.dumps([vote(real=None)]),
])
def test_malformed_legacy_or_non_supporting_votes_are_not_reused(cache, votes):
    cache(1, votes)
    assert db.prior_verification("owner/repo", 7, "head", "fingerprint", expected_policy=POLICY) is None


@pytest.mark.parametrize("verified", [None, 2])
def test_indeterminate_or_invalid_stored_decision_is_not_reused(cache, verified):
    cache(verified, json.dumps([vote()]))
    assert db.prior_verification("owner/repo", 7, "head", "fingerprint", expected_policy=POLICY) is None


def test_missing_finding_has_no_cached_decision(cache):
    assert db.prior_verification("owner/repo", 7, "head", "fingerprint", expected_policy=POLICY) is None


@pytest.mark.parametrize("policy", [None, "", "changed-policy"])
def test_missing_or_changed_policy_prevents_reuse(cache, policy):
    cache(1, json.dumps([vote()]))
    assert db.prior_verification("owner/repo", 7, "head", "fingerprint", expected_policy=policy) is None


@pytest.mark.parametrize(("decision", "status"), [
    (False, "verify_capped"), (True, "verify_capped"), (True, "skipped"),
    (False, "planned"), (True, "refuted"), (True, "unknown"),
])
def test_nondecisive_or_conflicting_status_cannot_reuse_stale_boolean(cache, decision, status):
    cache(int(decision), json.dumps([vote(True), vote(False), vote(None)]), status)
    assert db.prior_verification("owner/repo", 7, "head", "fingerprint", expected_policy=POLICY) is None
