"""Anonymous usage analytics — "help improve Bubo".

Bubo is free and open source. The only way the project learns what real
installs actually use — which SCM, which models, how many reviews, how much
gets reviewed — is anonymous usage signal. This module ships that signal to
PostHog over OTLP logs. It is **on by default**; see
:mod:`bubo.analytics_config` for the three opt-outs.

Design — privacy is the whole point, so it is enforced structurally:

* **Default-deny allowlist.** Every attribute that may leave the machine is
  named in :data:`_ALLOWED_ATTRS`. Anything not on the list is dropped by
  :func:`_clean` before an event is built. There is no code path that sends
  an un-allowlisted field. The list is *numbers and low-cardinality enums
  only* — never project/repo names, file paths, SHAs, finding text, review
  bodies, error strings, or credentials.
* **No stdlib logging at all.** OTLP egress goes through the OpenTelemetry
  logs API directly (``LoggerProvider.get_logger(...).emit(...)``). The Python
  stdlib :mod:`logging` package is never involved, so there is structurally no
  way for the rest of bubo's logs (which *do* contain repo names and paths) to
  reach PostHog. We only ever emit what we explicitly hand to :func:`_emit`.
* **Best-effort, never fatal.** Every public function swallows all
  exceptions. Analytics must never slow, block, or break a review. The OTLP
  exporter uses a short timeout and a background batch processor.
* **Anonymous, not identified.** A random install id (see :func:`install_id`)
  lets us count distinct installs without identifying anyone; it is a UUID
  with no link to user, host, or repo.
"""

from __future__ import annotations

import atexit
import os
import platform
import uuid
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from bubo import paths
from bubo.analytics_config import AnalyticsConfig

# Bump when the event field set changes in a way PostHog dashboards care about.
SCHEMA_VERSION = 1

# Every key that is permitted to leave the machine. Default-deny: anything not
# here is dropped by `_clean`. Keep this list to NUMBERS and low-cardinality
# ENUMS only. Deliberately absent (and must stay absent): project, repo, iid,
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
        # per-outcome engagement event — one per finding-outcome transition
        # (see `record_finding_outcome`). The value is the outcome name only.
        "outcome",
    }
)

# Known SCM provider / agent enums. A value outside the set is normalized to
# "other" so a custom command can never leak a path or arbitrary string.
_KNOWN_PROVIDERS = frozenset({"gitlab", "github"})
_KNOWN_AGENTS = frozenset({"codex", "claude"})
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

_INSTRUMENTATION_NAME = "bubo.analytics"

# Module singletons (one OTLP pipeline per process; workers are exec'd, not
# forked, so each gets a fresh one). `_logger_failed` marks "tried and
# failed/disabled" so we don't retry setup on every event. Typed `Any` so the
# OTel SDK is imported lazily inside `_build_pipeline` (a missing/broken SDK
# degrades to "no analytics" rather than breaking import of the poller).
_otlp_provider: Any = None
_logger: Any = None
_logger_failed: bool = False
_install_id: str | None = None


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


def install_id() -> str:
    """Return this install's anonymous id, creating it on first use.

    A random UUID persisted next to the state DB. Not tied to user, host, or
    repository — purely a counter so distinct installs can be told apart. If
    the file cannot be read or written, a process-local ephemeral id is used
    so analytics still works (it just won't be stable across runs).
    """
    global _install_id
    if _install_id is not None:
        return _install_id
    path = paths.DB.parent / "install_id"
    try:
        if path.exists():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                _install_id = existing
                return _install_id
        new_id = uuid.uuid4().hex
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id + "\n", encoding="utf-8")
        _install_id = new_id
    except OSError:
        # Read-only state dir, race, etc. — fall back to an ephemeral id.
        _install_id = uuid.uuid4().hex
    return _install_id


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
    }


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    """Default-deny filter: keep only allowlisted, scalar, sanitized values.

    This is the single chokepoint that guarantees no identifying content
    leaves the machine. Unknown keys, ``None`` values, and unsupported types
    are dropped; strings are stripped, rejected if they contain whitespace or
    control characters, and truncated.
    """
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if key not in _ALLOWED_ATTRS or value is None:
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


def _resource() -> Any:
    """Build the OTel resource with ONLY a fixed service name.

    Uses the raw ``Resource(attributes=...)`` constructor, NOT
    ``Resource.create()`` — the latter merges ``OTEL_RESOURCE_ATTRIBUTES`` /
    ``OTEL_SERVICE_NAME`` from the environment, which bubo operators commonly
    set (they run a ``[telemetry]`` OTLP exporter). Those ride alongside every
    log record and would bypass the `_clean` allowlist, leaking host/env names
    to PostHog. The raw constructor performs no env merge.
    """
    from opentelemetry.sdk.resources import Resource

    return Resource(attributes={"service.name": "bubo"})


def _build_pipeline(cfg: AnalyticsConfig) -> tuple[Any, Any] | None:
    """Build ``(provider, logger)`` for OTLP log egress, or ``None`` on failure.

    Imports are local so a missing/broken OTel SDK degrades to "no analytics"
    rather than breaking import of this module (and the poller).
    """
    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        provider = LoggerProvider(resource=_resource())
        exporter = OTLPLogExporter(
            endpoint=cfg.endpoint,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
            timeout=3,
        )
        provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
        logger = provider.get_logger(_INSTRUMENTATION_NAME)
        # Backstop: flush on interpreter exit even if the caller forgets, so a
        # process that dies between emit and an explicit flush still ships.
        atexit.register(flush)
        return provider, logger
    except Exception:
        return None


def _get_logger(cfg: AnalyticsConfig) -> Any:
    global _otlp_provider, _logger, _logger_failed
    if _logger is None and not _logger_failed:
        built = _build_pipeline(cfg)
        if built is None:
            _logger_failed = True
        else:
            _otlp_provider, _logger = built
    return _logger


def _emit(cfg: AnalyticsConfig, event: str, attrs: dict[str, Any]) -> None:
    """Emit one anonymized event. Best-effort: never raises."""
    try:
        if not analytics_enabled(cfg):
            return
        logger = _get_logger(cfg)
        if logger is None:
            return
        from opentelemetry._logs import SeverityNumber

        payload = _clean({**_base_attrs(), **attrs})
        # The event name is both the log body and event_name; PostHog maps it.
        logger.emit(
            body=event,
            event_name=event,
            severity_number=SeverityNumber.INFO,
            severity_text="INFO",
            attributes=payload,
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
) -> None:
    """The primary signal: one anonymized event per completed review."""
    _emit(
        cfg,
        "review_completed",
        {
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
        provider = _otlp_provider
        if provider is not None:
            # Bounded so a worker/poll never hangs long on exit when PostHog is
            # unreachable — dropping an event beats blocking a review.
            provider.force_flush(2000)
    except Exception:
        return


__all__ = [
    "AnalyticsConfig",
    "agent_label",
    "analytics_enabled",
    "flush",
    "install_id",
    "record_finding_outcome",
    "record_review_completed",
    "record_session_start",
]
