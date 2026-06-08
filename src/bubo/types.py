"""Shared type aliases.

Kept deliberately small. Everything here exists because two or more modules
need the same name; one-off types stay private to their owning module.

:data:`JsonObject` is the canonical "opaque JSON dict" alias. When a finding,
GitLab MR, or MCP response is shaped enough to warrant a strict type, replace
it with a :class:`TypedDict` and rename the call sites — the alias here keeps
the boundary surface obvious until that happens.
"""

from __future__ import annotations

from typing import Any

JsonObject = dict[str, Any]

__all__ = ["JsonObject"]
