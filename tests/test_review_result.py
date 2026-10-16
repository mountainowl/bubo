import json

import pytest
from review_output import review_output

from bubo.findings import load_final_json, parse_review_result
from bubo.verification import Verdict, decide, parse_verdict


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "[]",
        '{"error":"no diff"}',
        "```json\n{}\n```",
        "tool output\ncodex\n" + review_output(),
        review_output() + "\nfailed",
        '{"schema_version":1,"status":"complete","status":"failed"}',
        '{"confidence":NaN}',
        '{"confidence":Infinity}',
    ],
)
def test_invalid_live_output_never_establishes_completion(raw):
    with pytest.raises(ValueError, match=r"Review failed|final JSON result"):
        parse_review_result(raw)


def test_explicit_empty_complete_result():
    result = parse_review_result(review_output())
    assert result.status == "complete"
    assert result.findings == []
    assert result.coverage["reviewed"]


def test_incomplete_retains_findings_and_blockers():
    result = parse_review_result(
        review_output(
            [{}],
            status="incomplete",
            questions=["What is the required retry contract?"],
            unreviewed=["retry handler"],
        )
    )
    assert result.status == "incomplete"
    assert len(result.findings) == 1
    assert result.questions


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": True},
        {"schema_version": 9},
        {"mode": "unknown"},
        {"questions": ["missing contract"]},
        {"omitted_findings_count": True},
        {"coverage": {"reviewed": [], "unreviewed": [], "limitations": []}},
        {"findings": [None]},
    ],
)
def test_schema_rejects_ambiguous_or_conflicting_fields(change):
    data = json.loads(review_output())
    data.update(change)
    with pytest.raises(ValueError, match=r"Review failed|final JSON result"):
        parse_review_result(json.dumps(data))


def test_split_or_unavailable_verification_is_not_refutation():
    result = decide(
        [Verdict("a", True, 0.99, "evidence"), Verdict("b", False, 0.99, "counterevidence")],
        min_votes=2,
        confidence_floor=0.85,
    )
    assert not result.survives
    assert not result.refuted
    result = decide(
        [Verdict("a", None, 0.99, "missing context")], min_votes=1, confidence_floor=0.85
    )
    assert not result.survives
    assert not result.refuted


def test_refutation_requires_explicit_quorum():
    result = decide(
        [Verdict("a", False, 0.99, "counterexample"), Verdict("b", False, 0.99, "contract")],
        min_votes=2,
        confidence_floor=0.85,
    )
    assert result.refuted
    assert not result.survives


def test_unknown_verdict_cannot_fall_back_to_legacy_boolean():
    assert parse_verdict('{"verdict":"bogus","real":false,"confidence":1,"reason":"x"}') is None
    assert (
        parse_verdict(
            '{"verdict":"insufficient_evidence","confidence":1,"reason":"missing base"}'
        ).real
        is None
    )


def test_duplicate_keys_nested_in_evidence_are_rejected():
    with pytest.raises(ValueError, match="final JSON result"):
        load_final_json('{"finding":{"file":"a","file":"b"}}')
