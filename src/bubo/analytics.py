"""Pseudonymous usage analytics — "help improve Bubo".

Bubo is free and open source. The only way the project learns what real
installs actually use — which SCM, which models, how many reviews, how much
gets reviewed — is usage signal. This module ships that signal to
PostHog through its Product Analytics batch API. It is **on by default**; see
:mod:`bubo.analytics_config` for the three opt-outs.

Design — privacy is the whole point, so it is enforced structurally:

* **Default-deny allowlist.** Every attribute that may leave the machine is
  named in :data:`_ALLOWED_ATTRS`. Anything not on the list is dropped by
  :func:`_clean` before an event is built. There is no code path that sends
  an un-allowlisted field. Names require verified provider metadata: organization
  namespaces for any visibility, repository names only for public OSS projects.
  File paths, SHAs, finding text, review bodies, errors and credentials stay out.
* **No stdlib logging at all.** Product Analytics events are assembled here
  and sent directly over HTTPS. The Python stdlib :mod:`logging` package is
  never involved, so there is structurally no way for the rest of bubo's logs
  (which *do* contain repo names and paths) to reach PostHog. We only ever send
  what we explicitly hand to :func:`_emit`.
* **Best-effort, never fatal.** Every public function swallows all
  exceptions. Events are buffered until :func:`flush`, which uses one request
  per configured destination with a short timeout.
* **Pseudonymous identity.** Random installation and project IDs count usage
  without personal account IDs. Approved organization metadata can identify
  organizations, so these events are not described as fully anonymous.
"""

from __future__ import annotations

import atexit
import fcntl
import json
import os
import platform
import tempfile
import threading
import uuid
from collections.abc import Iterable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from bubo import paths
from bubo.analytics_config import AnalyticsConfig

# Bump when the event field set changes in a way PostHog dashboards care about.
SCHEMA_VERSION = 3

# Every key that is permitted to leave the machine. Default-deny: anything not
# here is dropped by `_clean`. Organization/repo names have additional gates.
# Deliberately absent (and must stay absent): project, repo, iid,
# sha, file, line, body, report, error, discussion_id, note_id, fingerprint,
# matched_rule, reason, sensitive_paths, tokens/credentials, any URL.
_ALLOWED_ATTRS = frozenset(
    {
        # install context (anonymous)
        "distinct_id",
        "install_id",
        "bubo_version",
        "python_version",
        "os",
        "arch",
        "schema_version",
        # fixed PostHog privacy controls
        "$geoip_disable",
        "$ip",
        "$process_person_profile",
        "scm_provider",
        "agent",
        "model",
        "projects_count",
        # per-review event
        "status",
        "dry_run",
        "review_mode",
        "tone",
        "duration_seconds",
        "tokens_input",
        "tokens_output",
        "tokens_cached",
        "tokens_total",
        "cost_usd",
        "findings_posted",
        "findings_planned",
        "findings_skipped",
        "files_changed",
        "lines_changed",
        "circuit_state",
        "language",
        "language_files",
        "queue_seconds",
        "project_id",
        "domain",
        "project_type",
        "profile_source",
        "stack_source",
        "stack_coverage",
        "metadata_status",
        "visibility",
        "namespace_kind",
        "org",
        "repo_name",
        "license",
        "technology_kind",
        "technology",
        # per-outcome engagement event — one per finding-outcome transition
        # (see `record_finding_outcome`). The value is the outcome name only.
        "outcome",
    }
)

# Known SCM provider / agent enums. A value outside the set is normalized to
# "other" so a custom command can never leak a path or arbitrary string.
_KNOWN_PROVIDERS = frozenset({"gitlab", "github"})
_KNOWN_AGENTS = frozenset({"codex", "claude"})
_KNOWN_EVENTS = frozenset(
    {
        "session_start",
        "review_completed",
        "review_language",
        "project_profile",
        "project_technology",
        "finding_outcome",
        "subscription_circuit",
    }
)
# Developer-engagement outcome dimensions. Mirrors the per-finding flags the
# poller's outcome sync writes to SQLite; a value outside the set normalizes to
# "other" so a future column can never leak as an arbitrary string.
_KNOWN_OUTCOMES = frozenset(
    {"resolved", "deleted", "developer_replied", "disputed", "false_positive", "duplicate"}
)

# Cap any string attribute (only model survives as a free-ish value) and reject
# anything with whitespace/control chars so a misconfigured model id cannot
# smuggle multi-line content.
_MAX_STR = 64

# Buffered per-process; workers are exec'd, not forked, so each gets a fresh
# queue. Keeping destination beside each event also handles callers that use
# more than one AnalyticsConfig in the same process.
_pending_events: list[tuple[str, str, dict[str, Any]]] = []
_pending_lock = threading.Lock()
_install_id: str | None = None
_install_path: Path | None = None
_identity_lock = threading.Lock()

# Only these labels may leave the machine; paths are used locally and discarded.
_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".r": "r",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".html": "html",
    ".css": "css",
    ".scss": "css",
    ".vue": "vue",
    ".svelte": "svelte",
}


def language_counts(files: Iterable[str]) -> dict[str, int]:
    """Count changed files by fixed extension categories, never returning paths."""
    counts: dict[str, int] = {}
    for filename in files:
        label = _LANGUAGES.get(Path(filename).suffix.lower(), "other")
        counts[label] = counts.get(label, 0) + 1
    return counts


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def analytics_enabled(cfg: AnalyticsConfig) -> bool:
    """Return whether analytics should send, honoring env kill-switches.

    Precedence: the cross-tool ``DO_NOT_TRACK`` opt-out wins, then an explicit
    ``BUBO_ANALYTICS``, then the config flag. A blank endpoint or api_key
    always disables sending (there is nowhere to send).
    """
    dnt = os.environ.get("DO_NOT_TRACK")
    if dnt is not None and _truthy(dnt):
        return False
    has_dest = bool(cfg.endpoint and cfg.api_key)
    override = os.environ.get("BUBO_ANALYTICS")
    if override is not None:
        return _truthy(override) and has_dest
    return cfg.enabled and has_dest


def install_id() -> str | None:
    """Return this install's anonymous id, creating it on first use.

    A random UUID persisted next to the state DB. Not tied to user, host, or
    repository. Concurrent workers share a lock and atomic publication. Failure
    returns None: dropping analytics is preferable to inventing extra installs.
    """
    global _install_id, _install_path
    path = paths.DB.parent / "install_id"
    with _identity_lock:
        if _install_id is not None and _install_path == path:
            return _install_id
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with (path.parent / "install_id.lock").open("a") as lock:
                # Never stall a review behind another process's telemetry.
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    existing = path.read_text(encoding="utf-8").strip()
                    parsed = uuid.UUID(existing)
                    if parsed.version != 4:
                        raise ValueError("Expected a random UUID")
                except FileNotFoundError, ValueError, UnicodeError:
                    existing = uuid.uuid4().hex
                    temporary: str | None = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            mode="w", dir=path.parent, delete=False, encoding="utf-8"
                        ) as output:
                            temporary = output.name
                            output.write(existing + "\n")
                            output.flush()
                            os.fsync(output.fileno())
                        os.replace(temporary, path)
                    finally:
                        if temporary is not None:
                            Path(temporary).unlink(missing_ok=True)
                _install_id, _install_path = existing, path
                return existing
        except OSError:
            return None


def _bubo_version() -> str:
    try:
        return version("bubo")
    except PackageNotFoundError:
        return "unknown"


def agent_label(reviewer_command: list[str]) -> str:
    """Map a reviewer command to a low-cardinality agent label.

    Reads only the first argv token's basename (e.g. ``codex``/``claude``);
    anything else becomes ``"other"`` so a custom command path never leaks.
    """
    if not reviewer_command:
        return "other"
    name = os.path.basename(str(reviewer_command[0])).strip().lower()
    return name if name in _KNOWN_AGENTS else "other"


def _base_attrs() -> dict[str, Any]:
    py = platform.python_version_tuple()
    iid = install_id()
    if iid is None:
        raise OSError("Persistent analytics identity unavailable")
    return {
        # PostHog keys events on `distinct_id`; setting it to the anonymous
        # install id is what lets "count distinct installs" actually work
        # (without it, every install collapses into one anonymous actor).
        "distinct_id": iid,
        "install_id": iid,
        "bubo_version": _bubo_version(),
        "python_version": f"{py[0]}.{py[1]}",
        "os": platform.system() or "unknown",
        "arch": platform.machine() or "unknown",
        "schema_version": SCHEMA_VERSION,
        # The capture API otherwise derives and stores location from the
        # request IP and creates a person profile for the anonymous install.
        "$geoip_disable": True,
        "$ip": "0.0.0.0",
        "$process_person_profile": False,
    }


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    """Default-deny filter: keep only allowlisted, scalar, sanitized values.

    This chokepoint enforces the approved disclosure boundary.
    Unknown keys, ``None`` values, and unsupported types
    are dropped; strings are stripped, rejected if they contain whitespace or
    control characters, and truncated.
    """
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if key not in _ALLOWED_ATTRS or value is None:
            continue
        if key in {"org", "repo_name"}:
            from bubo.project_analytics import _SLUG, OSS_LICENSES

            if not isinstance(value, str) or not _SLUG.fullmatch(value):
                continue
            if attrs.get("metadata_status") != "verified":
                continue
            if key == "repo_name" and (
                attrs.get("visibility") != "public" or attrs.get("license") not in OSS_LICENSES
            ):
                continue
            if key == "org" and (attrs.get("scm_provider"), attrs.get("namespace_kind")) not in {
                ("github", "organization"),
                ("gitlab", "group"),
            }:
                continue
        # bool is a subclass of int, so `bool | int | float` covers it.
        if isinstance(value, bool | int | float):
            out[key] = value
        elif isinstance(value, str):
            cleaned = value.strip()
            if not cleaned or len(cleaned) > _MAX_STR or any(c.isspace() for c in cleaned):
                continue
            out[key] = cleaned
    return out


def _emit(cfg: AnalyticsConfig, event: str, attrs: dict[str, Any]) -> None:
    """Queue one anonymized Product Analytics event. Never raises."""
    try:
        if not analytics_enabled(cfg) or event not in _KNOWN_EVENTS:
            return
        payload = _clean({**attrs, **_base_attrs()})
        # These controls are fixed after cleaning so no caller can override
        # them while adding event-specific attributes.
        payload["$geoip_disable"] = True
        payload["$ip"] = "0.0.0.0"
        payload["$process_person_profile"] = False
        with _pending_lock:
            _pending_events.append(
                (cfg.endpoint, cfg.api_key, {"event": event, "properties": payload})
            )
    except Exception:
        return


def record_session_start(cfg: AnalyticsConfig, *, scm_provider: str, projects_count: int) -> None:
    """One event per poll cycle start — liveness + install context."""
    _emit(
        cfg,
        "session_start",
        {"scm_provider": _provider(scm_provider), "projects_count": projects_count},
    )


def record_subscription_circuit(cfg: AnalyticsConfig, *, event: str) -> None:
    """Emit only safe, transition-only circuit state analytics."""
    state = "opened" if event == "subscription_circuit_opened" else "recovered"
    _emit(cfg, "subscription_circuit", {"circuit_state": state})


def record_review_completed(
    cfg: AnalyticsConfig,
    *,
    scm_provider: str,
    agent: str,
    model: str | None,
    status: str,
    dry_run: bool,
    review_mode: str,
    tone: str | None,
    duration_seconds: float,
    tokens_input: int | None,
    tokens_output: int | None,
    tokens_cached: int | None,
    tokens_total: int | None,
    cost_usd: float,
    findings_posted: int,
    findings_planned: int,
    findings_skipped: int,
    files_changed: int | None,
    lines_changed: int | None,
    languages: dict[str, int] | None = None,
    queue_seconds: float | None = None,
    profile: dict[str, Any] | None = None,
) -> None:
    """The primary signal: one anonymized event per completed review."""
    context = {
        key: value
        for key, value in (profile or {}).items()
        if key
        in {
            "project_id",
            "domain",
            "project_type",
            "profile_source",
            "stack_source",
            "stack_coverage",
            "metadata_status",
            "visibility",
            "namespace_kind",
            "org",
            "repo_name",
            "license",
        }
    }
    _emit(
        cfg,
        "review_completed",
        {
            **context,
            "scm_provider": _provider(scm_provider),
            "agent": agent,
            "model": model,
            "status": status,
            "dry_run": dry_run,
            "review_mode": review_mode,
            "tone": tone,
            "duration_seconds": duration_seconds,
            "tokens_input": tokens_input,
            "tokens_output": tokens_output,
            "tokens_cached": tokens_cached,
            "tokens_total": tokens_total,
            "cost_usd": cost_usd,
            "findings_posted": findings_posted,
            "findings_planned": findings_planned,
            "findings_skipped": findings_skipped,
            "files_changed": files_changed,
            "lines_changed": lines_changed,
            "queue_seconds": queue_seconds,
        },
    )
    if context.get("project_id"):
        common = {
            **context,
            "scm_provider": _provider(scm_provider),
            "status": status,
            "dry_run": dry_run,
        }
        _emit(cfg, "project_profile", common)
        from bubo.project_analytics import _DEPENDENCIES

        known = set(_DEPENDENCIES.values()) | {("language", label) for label in _LANGUAGES.values()}
        for kind, technology in (profile or {}).get("technologies", []):
            if (kind, technology) in known:
                _emit(
                    cfg,
                    "project_technology",
                    {**common, "technology_kind": kind, "technology": technology},
                )
    for language, count in (languages or {}).items():
        if language not in {*_LANGUAGES.values(), "other"} or count <= 0:
            continue
        _emit(
            cfg,
            "review_language",
            {
                **context,
                "scm_provider": _provider(scm_provider),
                "language": language,
                "language_files": count,
                "status": status,
                "dry_run": dry_run,
            },
        )


def record_finding_outcome(cfg: AnalyticsConfig, *, scm_provider: str, outcome: str) -> None:
    """One event per developer-engagement outcome — emitted at sync time.

    The caller (``bubo.poller.sync_outcomes``) emits this only on the
    ``false -> true`` transition of a single outcome dimension, beside the
    SQLite ``finding_outcomes`` upsert. Transition-gating is load-bearing for
    correctness: a posted finding is re-checked every sync cycle, and PostHog
    has no per-finding key to dedupe on (the fingerprint is never sent), so an
    every-sync emit would multiply each outcome's count. Emitting once per
    transition makes a PostHog ``count`` of ``outcome=resolved`` match the
    distinct count of resolved findings in SQLite.
    """
    _emit(
        cfg,
        "finding_outcome",
        {"scm_provider": _provider(scm_provider), "outcome": _outcome(outcome)},
    )


def _provider(value: str) -> str:
    name = (value or "").strip().lower()
    return name if name in _KNOWN_PROVIDERS else "other"


def _outcome(value: str) -> str:
    name = (value or "").strip().lower()
    return name if name in _KNOWN_OUTCOMES else "other"


def flush() -> None:
    """Flush any buffered events. Best-effort, bounded, never raises.

    Called at the end of a worker / poll cycle so a short-lived process ships
    its events before exit.
    """
    try:
        with _pending_lock:
            queued = list(_pending_events)
            _pending_events.clear()
        batches: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for endpoint, api_key, event in queued:
            batches.setdefault((endpoint, api_key), []).append(event)
        for (endpoint, api_key), events in batches.items():
            body = json.dumps({"api_key": api_key, "batch": events}, separators=(",", ":")).encode(
                "utf-8"
            )
            request = Request(
                endpoint,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                # Bounded so a worker/poll never hangs long on exit when
                # PostHog is unreachable. Dropping analytics beats blocking a
                # review, and avoids ambiguous retries after response loss.
                with urlopen(request, timeout=3):
                    pass
            except Exception:
                continue
    except Exception:
        return


# Backstop for short-lived commands whose caller forgets an explicit flush.
atexit.register(flush)


__all__ = [
    "AnalyticsConfig",
    "agent_label",
    "analytics_enabled",
    "flush",
    "install_id",
    "record_finding_outcome",
    "record_review_completed",
    "record_session_start",
    "record_subscription_circuit",
]
