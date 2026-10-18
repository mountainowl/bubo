"""Salvage a JSON object from agent output that mixes transcript and answer."""

from __future__ import annotations

import json

from bubo.types import JsonObject


def last_json_object_with_key(text: str, key: str) -> JsonObject | None:
    """Return the JSON object that starts last in ``text`` and carries ``key``.

    Agent CLIs interleave reasoning and tool transcript with the answer, and
    the real answer comes last. Scanning ``{`` positions from the end returns
    the same object a forward scan keeping the last match would, but usually
    touches only the tail of a large transcript instead of decoding from
    every brace.
    """
    decoder = json.JSONDecoder()
    index = text.rfind("{")
    while index != -1:
        try:
            candidate, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(candidate, dict) and key in candidate:
                return candidate
        index = text.rfind("{", 0, index)
    return None
