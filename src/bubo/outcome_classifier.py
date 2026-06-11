"""LLM classification of developer replies to review findings.

When a developer resolves or replies to a finding's discussion they might
**accept** it (agree / will fix / fixed it) or **reject** it (false
positive, "working as intended", disagreement). The naive
"resolved == success" signal cannot tell these apart, so it overcounts the
reviewer's precision — a thread resolved *after a rebuttal* looks identical
to one resolved *because the fix landed*.

This module asks an LLM to read the bot's finding plus the developer's
reply and return a verdict. It is model-agnostic: it drives the same agent
CLI the operator already configured for reviews. One wrinkle — the bundled
``bin/bubo-codex`` reviewer hardwires the code-reviewer meta-prompt (it
fetches an MR and emits findings JSON), so it cannot double as a free-form
classifier; when that bundled default is in use we invoke the raw
``codex exec`` profile path instead. A custom ``reviewer_command`` (e.g.
``claude -p``) already does free-form Q&A and is reused verbatim.

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

from bubo.codex_runner import codex_command
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

# Same PATH augmentation ``bin/bubo-codex`` applies so a directly-invoked
# ``codex`` resolves on hosts where it lives in a Homebrew / ~/.local bin.
_EXTRA_PATH = "/usr/local/bin:/opt/homebrew/bin"


def _unclear() -> JsonObject:
    return dict(_UNCLEAR)


def _error() -> JsonObject:
    return dict(_ERROR)


def _is_bundled_codex(command: list[str]) -> bool:
    """True when ``command`` is the bundled ``bin/bubo-codex`` reviewer."""
    return bool(command) and os.path.basename(command[0]) == "bubo-codex"


def classifier_command(cfg: ReviewConfig) -> list[str]:
    """Return the agent command used for reply classification.

    Smart default: the bundled ``bin/bubo-codex`` reviewer hardwires the
    code-reviewer meta-prompt, so it cannot classify; substitute the raw
    ``codex exec`` profile path. Any other configured ``reviewer_command``
    already does free-form Q&A and is reused verbatim.
    """
    if _is_bundled_codex(cfg.reviewer_command):
        return codex_command()
    return list(cfg.reviewer_command)


def classifier_env(source: Mapping[str, str]) -> dict[str, str]:
    """Build the (secret-stripped) env for the classifier subprocess."""
    env = {key: value for key, value in source.items() if key in _ENV_ALLOWLIST}
    path = _EXTRA_PATH + ":" + env.get("PATH", "")
    home = source.get("HOME")
    if home:
        path = path + ":" + os.path.join(home, ".local", "bin")
    env["PATH"] = path
    env["BUBO_ROOT"] = str(ROOT)
    # The bundled wrapper checks this to skip its agent-config env bootstrap;
    # harmless for non-bundled commands that ignore it.
    env["BUBO_SKIP_AGENT_CONFIG_ENV"] = "1"
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


def classify_developer_reply(cfg: ReviewConfig, finding_text: str, reply_text: str) -> JsonObject:
    """Classify a developer reply as accept/reject via the configured agent.

    Returns ``{"verdict": "accepted"|"rejected"|"unclear", "false_positive":
    bool}``. Degrades to ``unclear`` on any failure so it never raises into
    the outcome-sync loop.
    """
    if not reply_text.strip():
        return _unclear()
    command = classifier_command(cfg)
    if not command:
        return _unclear()
    prompt = build_prompt(finding_text, reply_text)
    try:
        result = run_bounded(
            [*command, prompt],
            env=classifier_env(os.environ),
            timeout=CLASSIFY_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # spawn / timeout / OS error — retry later
        log("reply_classify_failed", error=type(exc).__name__)
        return _error()
    if result.returncode:
        log("reply_classify_nonzero", returncode=result.returncode)
        return _error()
    return parse_verdict(result.stdout or "")
