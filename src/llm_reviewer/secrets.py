"""Best-effort secret redaction for persisted review output.

Three categories of secret get scrubbed before any text touches a durable
sink (report file, log line, SQLite ``error`` column):

1. **Named env-var assignments** — anything matching ``<NAME>=<value>`` for
   the credential env names we know about. Catches accidental ``echo`` of
   the environment.
2. **Token-shape literals** — ``glpat-...`` (GitLab PATs), ``sk-...``
   (OpenAI keys). Caught regardless of context.
3. **Credentialed URLs** — ``https://user:secret@host/...`` patterns that
   `glab` and `git` emit verbatim in error messages.

This is defense-in-depth, not a security boundary: the canonical fix is
to avoid printing secrets in the first place via the
:data:`REVIEWER_ENV_ALLOWLIST` in :mod:`llm_reviewer.poller`. The redactor
exists for the cases where a tool insists on logging its environment or
emits an error string we did not anticipate.
"""

from __future__ import annotations

import re

from llm_reviewer.env_config import GITLAB_TOKEN_ENV_NAMES, LLM_API_KEY_ENV_KEYS

# All credential env-var names whose ``NAME=value`` form should be redacted
# wherever it appears. Sourced from the canonical lists in env_config so
# the redactor cannot drift out of sync with the exporter.
SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    *LLM_API_KEY_ENV_KEYS,
    *GITLAB_TOKEN_ENV_NAMES,
    # Static bearer token for the HTTP MCP transport. The codex subprocess
    # spawned by `review_change` inherits this env var; redact in case any
    # sub-tool prints its environment on the way to the MCP response.
    "LLM_REVIEWER_MCP_BEARER_TOKEN",
)

_CRED_URL = re.compile(r"(https?://[^:/@\s]+:)[^@\s/]+(@)")
_GLPAT = re.compile(r"\bglpat-[A-Za-z0-9_-]+\b")
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]+\b")
_REDACTED = "<redacted>"


def redact_secrets(text: str) -> str:
    """Return ``text`` with credential-shaped substrings replaced.

    Idempotent: running the redactor on already-redacted text is a no-op.
    """
    redacted = _CRED_URL.sub(rf"\1{_REDACTED}\2", text)
    for name in SECRET_ENV_NAMES:
        redacted = re.sub(rf"({re.escape(name)}=)[^\s]+", rf"\1{_REDACTED}", redacted)
    redacted = _GLPAT.sub(_REDACTED, redacted)
    redacted = _OPENAI_KEY.sub(_REDACTED, redacted)
    return redacted


__all__ = ["SECRET_ENV_NAMES", "redact_secrets"]
