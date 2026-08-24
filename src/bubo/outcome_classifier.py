"""LLM classification of developer replies to review findings.

When a developer resolves or replies to a finding's discussion they might
**accept** it (agree / will fix / fixed it) or **reject** it (false
positive, "working as intended", disagreement). The naive
"resolved == success" signal cannot tell these apart, so it overcounts the
reviewer's precision — a thread resolved *after a rebuttal* looks identical
to one resolved *because the fix landed*.

This module asks an LLM to read the bot's finding plus the developer's
reply and return a verdict. It is model-agnostic: it runs the same
``[agents].reviewer_command`` the operator configured for reviews, directly
with the classification prompt. The review *contract* lives in the review
prompt (not the command), so the same command does free-form Q&A here.

A transient failure (spawn error, timeout, non-zero exit) returns the
``error`` sentinel so the caller leaves the finding unclassified and retries
on a later sync; genuine indecision and unparseable output return
``unclear`` (a terminal verdict). Either way the classifier never raises
into ``--sync-outcomes``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from bubo.events import log
from bubo.paths import ROOT
from bubo.review_config import ReviewConfig
from bubo.subproc import run_bounded
from bubo.types import JsonObject

# Classification is a tiny task; cap it well under the review timeout so a
# hung agent cannot stall outcome-sync.
CLASSIFY_TIMEOUT_SECONDS = 120

# Truncate the finding/reply text fed to the agent — a verdict does not need
# more, and it bounds token cost on long threads.
_MAX_TEXT_CHARS = 4000

_VERDICTS = frozenset({"accepted", "rejected", "unclear"})

# Terminal "we looked but can't decide" verdict — empty reply, no usable
# command, or output we could not parse. Marked classified; not retried.
_UNCLEAR: JsonObject = {"verdict": "unclear", "false_positive": False}

# Transient-failure sentinel — spawn error, timeout, or non-zero exit. The
# caller leaves the finding unclassified so a later sync retries it.
_ERROR: JsonObject = {"verdict": "error", "false_positive": False}

# Forwarded into the classifier subprocess. Mirrors the poller's
# ``reviewer_env`` philosophy: pass only non-secret operational vars plus
# the agent's own config-dir locators (the agent self-authenticates from
# CODEX_HOME / CLAUDE_CONFIG_DIR written by ``bubo init``), and strip raw
# credentials as an anti-exfiltration measure.
_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
    }
)

# PATH augmentation so a directly-invoked agent CLI (``codex``, ``claude``)
# resolves on hosts where it lives in a Homebrew / ~/.local bin.
_EXTRA_PATH = "/usr/local/bin:/opt/homebrew/bin"


def _unclear() -> JsonObject:
    return dict(_UNCLEAR)


def _error() -> JsonObject:
    return dict(_ERROR)


def classifier_env(
    source: Mapping[str, str], cfg: ReviewConfig | None = None
) -> dict[str, str]:
    """Build the env for the classifier subprocess.

    Credentials remain stripped by default. As with the main reviewer, a
    configured OpenAI-compatible endpoint needs exactly ``LLM_API_KEY`` and
    ``LLM_BASE_URL`` at request time, so forward only those two values when
    the operator explicitly configured ``llm_base_url``.
    """
    env = {key: value for key, value in source.items() if key in _ENV_ALLOWLIST}
    path = _EXTRA_PATH + ":" + env.get("PATH", "")
    home = source.get("HOME")
    if home:
        path = path + ":" + os.path.join(home, ".local", "bin")
    env["PATH"] = path
    env["BUBO_ROOT"] = str(ROOT)
    if cfg is not None and cfg.llm_base_url:
        for name in ("LLM_API_KEY", "LLM_BASE_URL"):
            if source.get(name):
                env[name] = source[name]
    return env


def build_prompt(finding_text: str, reply_text: str) -> str:
    """Render the fixed classification prompt."""
    finding = finding_text.strip()[:_MAX_TEXT_CHARS]
    reply = reply_text.strip()[:_MAX_TEXT_CHARS]
    return (
        "You are classifying whether a developer ACCEPTED or REJECTED an "
        "automated code-review finding, based solely on their reply. Do not "
        "review any code, fetch anything, or use any tools.\n\n"
        "AUTOMATED FINDING:\n"
        f"{finding}\n\n"
        "DEVELOPER REPLY:\n"
        f"{reply}\n\n"
        "Respond with ONLY a single JSON object, no prose and no code fence:\n"
        '{"verdict": "accepted" | "rejected" | "unclear", '
        '"false_positive": true | false}\n\n'
        '- "accepted": the developer agrees, will fix, or already fixed it.\n'
        '- "rejected": the developer disagrees — says it is working as '
        "intended, not a real problem, or a false positive.\n"
        '- "unclear": the reply neither clearly accepts nor rejects.\n'
        '- "false_positive": true only when the developer indicates the '
        "finding is factually wrong, not merely lower priority."
    )


def parse_verdict(stdout: str) -> JsonObject:
    """Extract the last JSON object carrying a ``verdict`` key from stdout.

    Agent CLIs interleave reasoning/tool transcript with the answer, and the
    real answer comes last — so we scan every ``{`` and keep the final
    object that has a ``verdict`` key. Unknown verdict strings collapse to
    ``unclear``.
    """
    decoder = json.JSONDecoder()
    found: JsonObject | None = None
    for index, char in enumerate(stdout):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "verdict" in candidate:
            found = candidate
    if found is None:
        return _unclear()
    verdict = str(found.get("verdict", "unclear")).strip().lower()
    if verdict not in _VERDICTS:
        verdict = "unclear"
    return {"verdict": verdict, "false_positive": bool(found.get("false_positive", False))}


def _failure_reason(output: str) -> str:
    """Map agent output to a safe, low-cardinality diagnostic label."""
    lowered = output.lower()
    if "missing environment variable" in lowered:
        return "missing_environment_variable"
    if "unauthorized" in lowered or "status 401" in lowered:
        return "unauthorized"
    if "rate limit" in lowered or "status 429" in lowered:
        return "rate_limited"
    return "agent_nonzero"


def classify_developer_reply(cfg: ReviewConfig, finding_text: str, reply_text: str) -> JsonObject:
    """Classify a developer reply as accept/reject via the configured agent.

    Returns ``{"verdict": "accepted"|"rejected"|"unclear", "false_positive":
    bool}``. Degrades to ``unclear`` on any failure so it never raises into
    the outcome-sync loop.
    """
    if not reply_text.strip():
        return _unclear()
    command = list(cfg.reviewer_command)
    if not command:
        return _unclear()
    prompt = build_prompt(finding_text, reply_text)
    try:
        result = run_bounded(
            [*command, prompt],
            env=classifier_env(os.environ, cfg),
            timeout=CLASSIFY_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # spawn / timeout / OS error — retry later
        log("reply_classify_failed", error=type(exc).__name__)
        return _error()
    if result.returncode:
        diagnostic = f"{result.stdout or ''}\n{result.stderr or ''}"
        log(
            "reply_classify_nonzero",
            returncode=result.returncode,
            reason=_failure_reason(diagnostic),
        )
        return _error()
    return parse_verdict(result.stdout or "")
