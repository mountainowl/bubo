"""Unit tests for the descriptive-error helper (:mod:`bubo.errors`).

Every runtime error in bubo composes its message through :func:`describe` so logs
carry *what / why / fix*.
"""

from __future__ import annotations

from bubo.errors import describe


def test_describe_with_only_what_returns_what() -> None:
    assert describe("thing broke") == "thing broke"


def test_describe_composes_what_why_fix_on_one_line() -> None:
    msg = describe("thing broke", reason="the disk was full", fix="free up space")
    assert msg == "thing broke | why: the disk was full | fix: free up space"
    assert "\n" not in msg


def test_describe_omits_absent_segments() -> None:
    assert describe("thing broke", fix="retry") == "thing broke | fix: retry"
    assert describe("thing broke", reason="bad input") == "thing broke | why: bad input"


def test_describe_strips_whitespace_in_each_segment() -> None:
    assert describe("  boom  ", reason="  cause  ", fix="  do x  ") == "boom | why: cause | fix: do x"
