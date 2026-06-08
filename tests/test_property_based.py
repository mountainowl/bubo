"""Property-based tests for parsers, hashers, and config expanders.

These use Hypothesis to generate many randomized inputs per run, which
catches edge cases the example-based unit tests miss (empty inputs,
boundary lengths, dict-key reorderings, weird-but-valid characters) and
also satisfies the OpenSSF Scorecard "Fuzzing" check — Scorecard's
fuzzing-detector recognizes a Hypothesis test suite as a fuzzer.

The properties asserted here are deliberately conservative: each one is a
universally-true invariant of the function under test, not a behavioral
spec. A regression here means a real semantic break, not a flaky example.
"""

from __future__ import annotations

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from bubo.config_values import ConfigError
from bubo.env_config import expand_env_placeholders
from bubo.findings import extract_findings
from bubo.hash_utils import stable_hash

# Keep run-time small in default `pytest` while still useful as a fuzzer.
_FUZZ_SETTINGS = settings(max_examples=50, deadline=None)

# Plain text that's guaranteed not to contain an env placeholder: no '$'.
_PLAIN_TEXT = st.text(
    alphabet=st.characters(blacklist_characters="$", blacklist_categories=("Cs",)),
    max_size=200,
)

# Identifier-shaped names matching the env-placeholder regex:
# `[A-Za-z_][A-Za-z0-9_]*`. The first character must NOT be a digit, or the
# regex won't recognize the construct as a placeholder and expansion is a
# no-op (which would falsify the property — and is the correct behavior).
_VAR_NAME = st.tuples(
    st.text(alphabet=string.ascii_letters + "_", min_size=1, max_size=1),
    st.text(alphabet=string.ascii_letters + string.digits + "_", max_size=31),
).map(lambda parts: parts[0] + parts[1])

# Default values that round-trip through `${VAR:-default}`. The placeholder
# grammar's default capture is `[^}]*`, so a default containing `}` would
# close the placeholder early (`${A:-}0}` parses as default="" then literal
# `0}`). Likewise, a default containing `${...}`-shaped substrings would
# nest a fresh placeholder that the parser then sees. Excluding `}` and `$`
# keeps the default strategy inside the grammar this property describes.
_DEFAULT_VALUE = st.text(
    alphabet=st.characters(blacklist_characters="}$", blacklist_categories=("Cs",)),
    max_size=50,
)


# ---------------------------------------------------------------------------
# expand_env_placeholders
# ---------------------------------------------------------------------------


@_FUZZ_SETTINGS
@given(_PLAIN_TEXT)
def test_expand_env_placeholders_is_identity_on_plain_text(text: str) -> None:
    # Without any ${...} placeholder, the expander must be a no-op.
    assert expand_env_placeholders(text, {}) == text


@settings(max_examples=200, deadline=None)
@given(_VAR_NAME, _DEFAULT_VALUE)
def test_expand_env_placeholders_uses_default_when_var_unset(name: str, default: str) -> None:
    # ${VAR:-default} with VAR unset must yield exactly the default. Bumped
    # to 200 examples after the original 50-example pass missed a `}`-in-
    # default case (see _DEFAULT_VALUE for the grammar constraint).
    template = f"${{{name}:-{default}}}"
    assert expand_env_placeholders(template, {}) == default


@_FUZZ_SETTINGS
@given(_VAR_NAME)
def test_expand_env_placeholders_required_raises_when_unset(name: str) -> None:
    # ${VAR} without a default and without the env var present must raise.
    template = f"${{{name}}}"
    try:
        expand_env_placeholders(template, {})
    except ConfigError:
        return
    raise AssertionError("expected ConfigError for unset required placeholder")


# ---------------------------------------------------------------------------
# stable_hash
# ---------------------------------------------------------------------------


_JSON_SCALAR = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**40), max_value=2**40),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(max_size=40),
)


@_FUZZ_SETTINGS
@given(_JSON_SCALAR)
def test_stable_hash_is_deterministic(value: object) -> None:
    assert stable_hash(value) == stable_hash(value)


@_FUZZ_SETTINGS
@given(st.dictionaries(_VAR_NAME, _JSON_SCALAR, min_size=2, max_size=8))
def test_stable_hash_is_dict_key_order_invariant(payload: dict[str, object]) -> None:
    # Build a same-content dict with reversed insertion order; hashes must match.
    reversed_payload = dict(reversed(list(payload.items())))
    assert stable_hash(payload) == stable_hash(reversed_payload)


@_FUZZ_SETTINGS
@given(_JSON_SCALAR, st.integers(min_value=1, max_value=64))
def test_stable_hash_truncation_length_is_honored(value: object, length: int) -> None:
    digest = stable_hash(value, length=length)
    assert len(digest) == length
    # Truncation is a prefix of the full digest.
    assert stable_hash(value).startswith(digest)


# ---------------------------------------------------------------------------
# extract_findings
# ---------------------------------------------------------------------------


@_FUZZ_SETTINGS
@given(st.text(max_size=500))
def test_extract_findings_returns_list_or_value_error(raw: str) -> None:
    # The agent's stdout is untrusted. Documented contract: either a list of
    # JSON objects, an empty list when no recognizable JSON is present, or a
    # ValueError when something JSON-like parses but isn't a findings array.
    # No other exception type is acceptable — anything else means the worker
    # crashes on hostile input instead of recording FAILED with a clean
    # transcript.
    try:
        result = extract_findings(raw)
    except ValueError:
        return
    assert isinstance(result, list)


@_FUZZ_SETTINGS
@given(st.text(max_size=500), st.integers(min_value=1, max_value=20))
def test_extract_findings_respects_max_findings_cap(raw: str, cap: int) -> None:
    try:
        result = extract_findings(raw, max_findings=cap)
    except ValueError:
        return
    assert len(result) <= cap
