"""JSON salvage scanners stay linear-ish on big transcripts with identical output (#252).

``extract_findings`` used to try ``raw_decode`` at every ``[`` in the agent
transcript and the three verdict parsers at every ``{``, slicing the rest of
the string each time, so a large Codex transcript cost quadratic time. Each
test here compares the real parser against the original forward scan (kept
below as the oracle) and adds a timing guard on a multi-megabyte transcript.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bubo import findings, outcome_classifier, verification
from bubo.types import JsonObject

_FUZZ = settings(max_examples=300, deadline=None)

_FINDING = {
    "file": "a.py",
    "line": 3,
    "title": "Off by one",
    "body": "xs[i+1] reads past the end",
    "severity": "major",
    "category": "correctness",
    "confidence": 0.9,
    "kind": "bug",
    "fix": "use xs[i]",
}
_CODE = "for i in range(len(xs)):\n    ys[i] = xs[i][0] + m[i][j] + {k: v[0]}  # arr[k]\n"


def _transcript(kilobytes: int, answer: str) -> str:
    body = _CODE * (kilobytes * 1024 // len(_CODE))
    return "exec\n" + body + "\ncodex\n" + answer + "\ntokens used: 1234\n"


# Oracles: the pre-#252 implementations, verbatim in behaviour.
_ORIGINAL_ARRAY_START = re.compile(r"\[")


def _extract_with_original_scan(raw: str, monkeypatch: pytest.MonkeyPatch) -> object:
    with monkeypatch.context() as patch:
        patch.setattr(findings, "_JSON_ARRAY_START", _ORIGINAL_ARRAY_START)
        return _outcome(lambda: findings.extract_findings(raw))


def _forward_last_object(stdout: str, key: str) -> JsonObject | None:
    decoder = json.JSONDecoder()
    found: JsonObject | None = None
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and key in candidate:
            found = candidate
    return found


def _outcome(call: Callable[[], object]) -> object:
    try:
        return call()
    except ValueError as exc:
        return ("ValueError", str(exc))


_PIECES = [
    "[]",
    "[ ]",
    "[\n]",
    '[{"file":"a.py","line":1}]',
    '[ {"x": 1} ]',
    '{"findings": []}',
    '{"findings": [{"file": "b.py"}]}',
    "[1,2]",
    "[{]",
    "[[{}]]",
    '{"verdict": "accepted"}',
    '{"verdict": "fixed", "confidence": 0.5}',
    '{"real": true, "confidence": 0.9}',
    '{"a": {"verdict": "x"}}',
    "\ncodex\n",
    "\N{NO-BREAK SPACE}",
    "\x0c",
]
_BRACKETY = st.lists(
    st.one_of(st.sampled_from(list('[]{}" ,:abc\n\t0')), st.sampled_from(_PIECES)),
    max_size=80,
).map("".join)


@_FUZZ
@given(_BRACKETY)
def test_extract_findings_matches_original_scan(text: str) -> None:
    with pytest.MonkeyPatch.context() as monkeypatch:
        expected = _extract_with_original_scan(text, monkeypatch)
    assert _outcome(lambda: findings.extract_findings(text)) == expected


@_FUZZ
@given(_BRACKETY)
def test_verdict_parsers_match_original_forward_scan(text: str) -> None:
    classifier = _forward_last_object(text, "verdict")
    assert outcome_classifier.parse_verdict(text) == outcome_classifier.parse_verdict(
        json.dumps(classifier) if classifier is not None else ""
    )
    real = _forward_last_object(text, "real")
    assert verification.parse_verdict(text, lens="x") == (
        verification.parse_verdict(json.dumps(real), lens="x") if real is not None else None
    )
    reconcile = _forward_last_object(text, "verdict")
    assert verification.parse_reconciliation_result(text) == (
        verification.parse_reconciliation_result(json.dumps(reconcile))
        if reconcile is not None
        else None
    )


def test_extract_findings_large_transcript_is_fast_and_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _transcript(2048, json.dumps([_FINDING]))
    assert text.count("[") > 150_000
    started = time.perf_counter()
    result = findings.extract_findings(text)
    elapsed = time.perf_counter() - started
    assert [item["title"] for item in result] == ["Off by one"]
    # The original scan takes ~11 s here; the anchored scan takes ~0.02 s.
    assert elapsed < 0.5, f"extract_findings took {elapsed:.2f}s on a 2 MB transcript"


@pytest.mark.parametrize(
    ("parse", "answer", "check"),
    [
        pytest.param(
            outcome_classifier.parse_verdict,
            '{"verdict": "rejected", "false_positive": true}',
            lambda out: out == {"verdict": "rejected", "false_positive": True},
            id="outcome_classifier",
        ),
        pytest.param(
            lambda s: verification.parse_verdict(s, lens="x"),
            '{"real": true, "confidence": 0.9}',
            lambda out: out is not None and out.real is True,
            id="verification",
        ),
        pytest.param(
            verification.parse_reconciliation_result,
            '{"verdict": "fixed", "confidence": 0.8}',
            lambda out: out is not None and out.verdict == "fixed",
            id="reconciliation",
        ),
    ],
)
def test_verdict_parsers_large_transcript_are_fast(
    parse: Callable[[str], object], answer: str, check: Callable[[object], bool]
) -> None:
    text = _transcript(2048, answer)
    started = time.perf_counter()
    out = parse(text)
    elapsed = time.perf_counter() - started
    assert check(out), out
    assert elapsed < 0.5, f"parser took {elapsed:.2f}s on a 2 MB transcript"
