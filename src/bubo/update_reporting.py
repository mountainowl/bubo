"""Local issue reports and durable, consent-respecting update failure delivery.

This store is deliberately separate from reviewer.sqlite and its rollback snapshot.
Only fixed categories, validated versions and UUIDs enter reports or PostHog.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bubo.analytics import analytics_enabled
from bubo.analytics_config import AnalyticsConfig
from bubo.update_identity import guard_path, guarded_identity, read_identity
from bubo.version import installed_version

PROTOCOL_VERSION = 1
PHASES = frozenset(
    {
        "discovery",
        "staging",
        "admission",
        "quiescing",
        "readiness",
        "backup",
        "install",
        "migration",
        "activation",
        "restart",
        "validation",
        "recovery",
        "delivery",
        "bootstrap",
        "cleanup",
        "other",
    }
)
CODES = frozenset(
    {
        "cleanup_failed",
        "network_unavailable",
        "invalid_artifact",
        "incompatible_runtime",
        "permission_denied",
        "unsafe_installation",
        "local_changes",
        "identity_changed",
        "version_unavailable",
        "storage_unavailable",
        "migration_failed",
        "health_failed",
        "restart_failed",
        "service_restart_required",
        "mcp_reconnect_required",
        "interrupted",
        "mutation_not_quiescent",
        "unsupported_protocol",
        "unexpected_error",
        "configuration_invalid",
        "controller_unavailable",
        "install_failed",
        "other",
    }
)
INSTALL_TYPES = frozenset({"uv", "pipx", "pip", "editable", "docker", "other"})
RECOVERY = frozenset({"not_needed", "pending", "restored", "committed", "blocked", "other"})
_VERSION = re.compile(r"[0-9][A-Za-z0-9.!+_-]{0,127}\Z")
_ACTIONS = {
    "service_restart_required": (
        "Restart the same Bubo service or HTTP MCP server with its original launch command, "
        "configuration and credentials; "
        "the retained helper will resume recovery."
    ),
    "mcp_reconnect_required": (
        "Reconnect the same MCP client/server configuration; recovery requires a fresh connection."
    ),
    "network_unavailable": "Check connectivity; the next attempt will retry safely.",
    "permission_denied": "Use the owning installation manager with the required permissions.",
    "unsafe_installation": "Use a dedicated environment or the owning deployment manager.",
    "local_changes": "Preserve and commit local changes before retrying the update.",
    "identity_changed": "Restore the original installation identity before recovery.",
    "storage_unavailable": "Restore writable free space in the persistent state directory.",
    "controller_unavailable": "Configure the retained update helper before unattended updates.",
    "mutation_not_quiescent": "Ensure the previous installer has exited before retrying recovery.",
    "version_unavailable": "Repair installation metadata using the owning package manager.",
}


def _category(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else "other"


def _version(value: str | None) -> str | None:
    return value if value and _VERSION.fullmatch(value) else None


class DiagnosticsUnavailable(OSError):
    """A new update must not begin without durable diagnostics."""


class FailureStore:
    def __init__(
        self, state: Path, *, capacity: int = 100 * 1024 * 1024, reserve: int = 10 * 1024 * 1024
    ):
        self.state = state
        self.directory = state / "update-diagnostics"
        self.path = self.directory / "failures.sqlite"
        self.capacity = capacity
        self.reserve = reserve

    @contextmanager
    def _db(self, *, write: bool = False):
        if write:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.is_symlink() or self.directory.is_symlink():
                raise DiagnosticsUnavailable("Unsafe diagnostic path")
            # Exclusive creation prevents accidentally exposing newly stored records.
            self.path.touch(mode=0o600, exist_ok=True)
            db = sqlite3.connect(self.path, timeout=3)
        else:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("PRAGMA synchronous=FULL")
                schema = db.execute("PRAGMA user_version").fetchone()[0]
                if schema not in (0, PROTOCOL_VERSION):
                    raise DiagnosticsUnavailable(
                        "Unsupported diagnostic schema; retain compatible helper"
                    )
                db.execute("""CREATE TABLE IF NOT EXISTS failures (
                    failure_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
                    occurred_at REAL NOT NULL, record TEXT NOT NULL, payload TEXT,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0, transport_code INTEGER
                )""")
                db.execute("PRAGMA user_version=1")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def ensure_capacity(self, *, recovery: bool = False) -> None:
        with self._db(write=True) as db:
            db.execute(
                "DELETE FROM failures WHERE status IN ('acknowledged','suppressed','local') "
                "AND occurred_at < ?",
                (time.time() - 30 * 86400,),
            )
            pages = db.execute("PRAGMA page_count").fetchone()[0]
            free = db.execute("PRAGMA freelist_count").fetchone()[0]
            size = db.execute("PRAGMA page_size").fetchone()[0]
            limit = self.capacity - (0 if recovery else self.reserve)
            if (pages - free) * size + 65536 > limit:
                raise DiagnosticsUnavailable("Diagnostic capacity exhausted; updates deferred")

    def record(
        self,
        *,
        attempt_id: str,
        phase: str,
        error_code: str,
        cfg: AnalyticsConfig,
        install_type: str = "other",
        from_version: str | None = None,
        target_version: str | None = None,
        recovery_status: str = "not_needed",
        failure_id: str | None = None,
        producer_version: str | None = None,
        consent: bool | None = None,
    ) -> str:
        attempt_id = str(uuid.UUID(attempt_id))
        failure_id = str(uuid.UUID(failure_id)) if failure_id else str(uuid.uuid4())
        phase = _category(phase, PHASES)
        producer = _version(producer_version or installed_version())
        now = time.time()
        record = {
            "protocol_version": PROTOCOL_VERSION,
            "failure_id": failure_id,
            "attempt_id": attempt_id,
            "occurred_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "phase": phase,
            "error_code": _category(error_code, CODES),
            "install_type": _category(install_type, INSTALL_TYPES),
            "bubo_version": producer,
            "from_version": _version(from_version),
            "target_version": _version(target_version),
            "recovery_status": _category(recovery_status, RECOVERY),
        }
        payload = None
        try:
            identity = (
                guarded_identity(self.state)
                if guard_path(self.state).exists()
                else read_identity(self.state)
            )
        except OSError, ValueError, UnicodeError, AttributeError, TypeError:
            identity = None
        allowed = analytics_enabled(cfg) if consent is None else consent
        if identity and producer and allowed:
            properties = {
                k: v
                for k, v in record.items()
                if k not in {"occurred_at", "failure_id"} and v is not None
            }
            properties.update(
                {
                    "distinct_id": identity,
                    "install_id": identity,
                    "$insert_id": failure_id,
                    "$geoip_disable": True,
                    "$ip": "0.0.0.0",
                    "$process_person_profile": False,
                }
            )
            payload = json.dumps(
                {
                    "event": "update_failed",
                    "properties": properties,
                    "timestamp": record["occurred_at"],
                },
                sort_keys=True,
            )
        self.ensure_capacity(recovery=phase == "recovery")
        with self._db(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO failures "
                "(failure_id,attempt_id,occurred_at,record,payload,status) VALUES (?,?,?,?,?,?)",
                (
                    failure_id,
                    attempt_id,
                    now,
                    json.dumps(record, sort_keys=True),
                    payload,
                    "pending" if payload else "local",
                ),
            )
        return failure_id

    def status(self, attempt_id: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        query = "SELECT record,status,attempts,transport_code FROM failures"
        params = ()
        if attempt_id:
            query += " WHERE attempt_id=?"
            params = (str(uuid.UUID(attempt_id)),)
        with self._db() as db:
            return [
                {
                    **json.loads(row["record"]),
                    "delivery": row["status"],
                    "delivery_attempts": row["attempts"],
                    "transport_code": row["transport_code"],
                }
                for row in db.execute(query + " ORDER BY occurred_at, failure_id", params)
            ]

    def report(self, attempt_id: str | None = None) -> str:
        rows = self.status(attempt_id)
        if not rows:
            return "No update failures recorded.\n"
        if attempt_id is None:
            latest = rows[-1]["attempt_id"]
            rows = [row for row in rows if row["attempt_id"] == latest]
        sections = []
        for row in rows:
            if row.get("protocol_version") != PROTOCOL_VERSION:
                sections.append(
                    "# Bubo update report unavailable\nUse the retained compatible helper."
                )
                continue
            lines = [f"# Bubo update failed: {row['phase']}"]
            for key in (
                "occurred_at",
                "attempt_id",
                "failure_id",
                "install_type",
                "bubo_version",
                "from_version",
                "target_version",
                "error_code",
                "recovery_status",
                "delivery",
            ):
                lines.append(f"- {key}: {row.get(key) or 'unavailable'}")
            lines.append(
                "\nNext action: "
                + (
                    "Recovery completed; no restart is required."
                    if row["error_code"] in {"service_restart_required", "mcp_reconnect_required"}
                    and row["recovery_status"] in {"committed", "restored"}
                    else _ACTIONS.get(
                        row["error_code"],
                        "Inspect update status and retain this report "
                        "when requesting Bubo support.",
                    )
                )
            )
            sections.append("\n".join(lines))
        return "\n\n".join(sections) + "\n"

    def suppress(self) -> None:
        if self.path.exists():
            with self._db(write=True) as db:
                db.execute(
                    "UPDATE failures SET status='suppressed',payload=NULL "
                    "WHERE status IN ('pending','quarantined')"
                )

    def finish_recovery(self, attempt_id: str, outcome: str) -> None:
        """Update local diagnostics; never rewrite captured external payloads."""
        outcome = _category(outcome, RECOVERY)
        with self._db(write=True) as db:
            rows = db.execute(
                "SELECT failure_id,record FROM failures WHERE attempt_id=?",
                (str(uuid.UUID(attempt_id)),),
            ).fetchall()
            for row in rows:
                record = json.loads(row["record"])
                record["recovery_status"] = outcome
                db.execute(
                    "UPDATE failures SET record=? WHERE failure_id=?",
                    (json.dumps(record, sort_keys=True), row["failure_id"]),
                )

    def retry(self, attempt_id: str, cfg: AnalyticsConfig) -> None:
        if not analytics_enabled(cfg):
            self.suppress()
            return
        with self._db(write=True) as db:
            db.execute(
                "UPDATE failures SET status='pending',next_attempt_at=0 "
                "WHERE attempt_id=? AND status='quarantined' AND payload IS NOT NULL",
                (str(uuid.UUID(attempt_id)),),
            )

    def drain(
        self,
        cfg: AnalyticsConfig,
        *,
        permitted: Callable[[], bool] = lambda: True,
        consent: Callable[[], bool | None] | None = None,
        now: float | None = None,
        sender=urlopen,
        limit: int = 20,
    ) -> int:
        """One bounded delivery pass. Callers supply authoritative helper consent.

        Holding the store write transaction serializes send with suppression and
        concurrent fallback senders. A response lost after acceptance is retried
        with the same $insert_id; this is at-least-once, not exactly-once delivery.
        """
        if not self.path.exists():
            return 0
        allowed = analytics_enabled(cfg) if consent is None else consent()
        if allowed is False:
            self.suppress()
            return 0
        if allowed is None:
            return 0
        now = time.time() if now is None else now
        delivered = 0
        for _ in range(min(limit, 20)):
            with self._db(write=True) as db:
                db.execute("BEGIN IMMEDIATE")
                allowed = analytics_enabled(cfg) if consent is None else consent()
                if allowed is False:
                    db.execute(
                        "UPDATE failures SET status='suppressed',payload=NULL "
                        "WHERE status IN ('pending','quarantined')"
                    )
                    return delivered
                if allowed is None or not permitted():
                    return delivered
                row = db.execute(
                    "SELECT * FROM failures WHERE status='pending' AND next_attempt_at<=? "
                    "ORDER BY occurred_at LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    return delivered
                event = json.loads(row["payload"])
                if event.get("properties", {}).get("protocol_version") != PROTOCOL_VERSION:
                    db.execute(
                        "UPDATE failures SET status='quarantined' WHERE failure_id=?",
                        (row["failure_id"],),
                    )
                    continue
                request = Request(
                    cfg.endpoint,
                    data=json.dumps({"api_key": cfg.api_key, "batch": [event]}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                status, code = "pending", None
                try:
                    with sender(request, timeout=3) as response:
                        code = response.status
                    if 200 <= code < 300:
                        status = "acknowledged"
                        delivered += 1
                    elif 400 <= code < 500 and code not in (408, 429):
                        status = "quarantined"
                except HTTPError as exc:
                    code = exc.code
                    if 400 <= code < 500 and code not in (408, 429):
                        status = "quarantined"
                except OSError, ValueError:
                    # Transport errors leave the row pending so the next flush retries it.
                    pass
                delay = min(3600, 30 * 2 ** min(row["attempts"], 7)) + random.uniform(0, 10)
                db.execute(
                    "UPDATE failures SET status=?,attempts=attempts+1,"
                    "next_attempt_at=?,transport_code=? "
                    "WHERE failure_id=?",
                    (status, now + delay, code, row["failure_id"]),
                )
        return delivered
