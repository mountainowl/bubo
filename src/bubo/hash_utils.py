"""Stable hashing helpers used for fingerprints and content-addressed IDs.

The codebase needs a hash that is:

* Deterministic across processes and Python versions (so a forked worker
  computes the same fingerprint as the parent).
* Stable across reorderings of dict keys (so semantically equal payloads
  hash equal).
* Safe for use as an SQLite primary key.

SHA-256 over the JSON encoding of the payload with ``sort_keys=True`` and
``ensure_ascii=False`` satisfies all three. The hash is **not** a security
boundary — it is a content identifier, not a credential.
"""

from __future__ import annotations

import hashlib
import json


def stable_hash(value: object, length: int | None = None) -> str:
    """Return the SHA-256 hex digest of ``value``'s canonical JSON encoding.

    Parameters
    ----------
    value:
        Any JSON-serializable object. Nested dicts have their keys sorted
        before hashing so ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` produce
        the same digest.
    length:
        Optional truncation. ``None`` returns the full 64-char hex digest;
        an integer truncates to that many leading characters. Use shorter
        values (e.g. 12) for human-visible IDs where collision risk is
        acceptable.

    Raises
    ------
    TypeError
        If ``value`` contains a type that :func:`json.dumps` cannot handle.
    """
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return stable_digest(payload, length=length)


def stable_digest(payload: bytes, length: int | None = None) -> str:
    """Return the SHA-256 hex digest of an arbitrary byte payload.

    Use this when the input is already bytes — for example, hashing the
    contents of a file without round-tripping through JSON. ``length``
    behaves the same as in :func:`stable_hash`.
    """
    digest = hashlib.sha256(payload).hexdigest()
    return digest[:length] if length is not None else digest
