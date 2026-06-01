"""Tiny JSON-RPC client for the GitLab MCP server.

The poster path uses MCP to create inline review threads on GitLab. The
MCP server is a separate process (typically ``mcp-gitlab`` or
``gitlab-mcp`` on ``PATH``, wrapped by ``bin/mcp-upstream-gitlab``); this
module speaks the minimal subset of the protocol we need:

* ``initialize`` handshake.
* ``notifications/initialized``.
* one ``tools/call``.
* read the matching response by ``id``.

The whole exchange runs as a single ``Popen`` lifetime under a bounded
timeout — no persistent MCP client, no connection pool. Each posted
finding spins up its own MCP subprocess and tears it down. That keeps
this module stateless and the failure mode obvious: if MCP misbehaves,
exactly one finding fails.
"""

from __future__ import annotations

import json
import subprocess
import urllib.parse

from llm_reviewer.paths import ROOT
from llm_reviewer.subproc import kill_process_group
from llm_reviewer.types import JsonObject

# Wall-clock timeout for the MCP exchange. Tight on purpose — anything
# that takes longer than a minute to post a comment is wedged.
MCP_TIMEOUT_SECONDS = 60

# Default MCP server wrapper — the GitLab one, for backwards compatibility
# with the original single-provider call sites.
DEFAULT_MCP_SERVER = ROOT / "bin" / "mcp-upstream-gitlab"


def thread_args(project: str, iid: int, body: str, position: JsonObject) -> JsonObject:
    """Build the ``arguments`` payload for ``create_merge_request_thread``."""
    return {
        "project_id": urllib.parse.quote(project, safe=""),
        "merge_request_iid": str(iid),
        "body": body,
        "position": position,
    }


def call_tool(name: str, arguments: JsonObject, *, server: str | None = None) -> JsonObject:
    """Invoke a tool on an MCP server wrapper and return its result.

    ``server`` is the path to the server wrapper script (e.g.
    ``bin/mcp-upstream-gitlab`` or ``bin/mcp-upstream-github``); defaults to
    :data:`DEFAULT_MCP_SERVER` (GitLab) so existing call sites are
    unchanged.

    Raises
    ------
    TimeoutError
        If the MCP exchange exceeds :data:`MCP_TIMEOUT_SECONDS`.
    RuntimeError
        If the server returns a JSON-RPC ``error`` or never sends a
        response with the expected ``id``.
    """
    server_bin = str(server) if server is not None else str(DEFAULT_MCP_SERVER)
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "llm-code-review-poller", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    ]
    payload = "".join(json.dumps(message) + "\n" for message in messages)
    with subprocess.Popen(
        [server_bin],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    ) as proc:
        try:
            stdout, _ = proc.communicate(input=payload, timeout=MCP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            kill_process_group(proc)
            raise TimeoutError(f"MCP tool timed out: {name}") from exc
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        response = json.loads(line)
        if response.get("id") != 2:
            continue
        if response.get("error"):
            raise RuntimeError(json.dumps(response["error"]))
        return response.get("result") or {}
    raise RuntimeError(f"MCP tool did not return: {name}")


def discussion_id(result: JsonObject) -> str:
    """Extract the discussion ID from an MCP ``tools/call`` result.

    GitLab MCP servers vary in how they wrap the result — sometimes a
    flat ``id`` field, sometimes nested in ``content[*].text`` as a JSON
    string. Returns ``""`` if no ID can be located so the caller can fall
    back to the REST path.
    """
    if result.get("id"):
        return str(result["id"])
    for item in result.get("content") or []:
        text = item.get("text") if isinstance(item, dict) else None
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
    return ""


def discussion_id_from_response(response: JsonObject) -> str:
    """Extract the discussion ID from a direct REST POST response.

    Companion to :func:`discussion_id` for the fallback REST path that
    fires when MCP returns no ID.
    """
    return str(response.get("id") or "")


__all__ = [
    "DEFAULT_MCP_SERVER",
    "MCP_TIMEOUT_SECONDS",
    "call_tool",
    "discussion_id",
    "discussion_id_from_response",
    "thread_args",
]
