"""Local dashboard aggregates. No content, credentials, or external transport."""

from __future__ import annotations

import math
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from bubo.findings import normalize_category

TABLES = (
    "reviewed_mrs",
    "review_runs",
    "review_findings",
    "finding_outcomes",
    "finding_reconciliations",
    "governance_decisions",
)


# Shared across overlapping window cohorts: ~50,000 compact records per request.
# Exceeding the budget invalidates the entire metrics result, never denominators.
MAX_METRIC_ROWS = 50_000
COLUMNS = {
    "review_runs": "run_id,started_at,finished_at,status,dry_run,model,tokens_input,tokens_output,"
    "tokens_cached,tokens_total,cost_usd,lines_reviewed,provenance_band,"
    "provenance_source,provenance_confidence,prompt_version,review_mode,tone",
    "review_findings": "category,severity,type,status,verified,confidence",
    "governance_decisions": "triggered,action,mode,matched_rule,rigor_injected",
    "finding_reconciliations": "state,verdict",
}
OUTCOME_COLUMNS = (
    "resolved,deleted,developer_replied,disputed,false_positive,duplicate,"
    "merged_unresolved,reply_classified,developer_disposition,resolution_source,"
    "resolution_evidence,disposition_evidence"
)


class _MetricBudgetExceeded(Exception):
    pass


def ratio(n: float, d: float) -> float | None:
    return n / d if d else None


def stats(values: list[float]) -> dict:
    """Linear interpolation at (n-1)*p, with population standard deviation."""
    values = sorted(values)
    if not values:
        return dict(count=0, mean=None, p50=None, p95=None, min=None, max=None, stddev=None)

    def percentile(p: float) -> float:
        index = (len(values) - 1) * p
        low = math.floor(index)
        return values[low] + (values[math.ceil(index)] - values[low]) * (index - low)

    mean = sum(values) / len(values)
    return dict(
        count=len(values),
        mean=mean,
        p50=percentile(0.5),
        p95=percentile(0.95),
        min=values[0],
        max=values[-1],
        stddev=math.sqrt(sum((v - mean) ** 2 for v in values) / len(values)),
    )


def model_family(model: str | None) -> str:
    name = (model or "").lower()
    for marker, family in [
        ("claude", "Claude"),
        ("gpt", "GPT"),
        ("gemini", "Gemini"),
        ("deepseek", "DeepSeek"),
        ("llama", "Llama"),
        ("qwen", "Qwen"),
    ]:
        if marker in name:
            return family
    if any(part.startswith(("o1", "o3", "o4")) for part in name.split("/")):
        return "OpenAI reasoning"
    return "Other" if name and name != "unknown" else "Unknown"


def _duration(row: dict) -> float | None:
    try:
        seconds = (
            datetime.fromisoformat(row["finished_at"]) - datetime.fromisoformat(row["started_at"])
        ).total_seconds()
        return seconds if seconds >= 0 and math.isfinite(seconds) else None
    except TypeError, ValueError, KeyError:
        return None


def _usage(rows: list[dict]) -> dict:
    completed = [r for r in rows if r.get("finished_at")]
    durations = [v for r in completed if (v := _duration(r)) is not None]
    result: dict[str, Any] = dict(
        runs=len(rows),
        completed=len(completed),
        unfinished=len(rows) - len(completed),
        latency=stats(durations),
        statuses=dict(Counter(r["status"] for r in rows)),
        dry_runs=sum(bool(r["dry_run"]) for r in rows),
    )
    for key in (
        "tokens_input",
        "tokens_output",
        "tokens_cached",
        "tokens_total",
        "cost_usd",
        "lines_reviewed",
    ):
        values = [
            r[key]
            for r in rows
            if isinstance(r.get(key), (int, float)) and r[key] >= 0 and math.isfinite(r[key])
        ]
        result[key] = dict(total=sum(values), recorded=len(values), missing=len(rows) - len(values))
    result["cost_per_recorded_run"] = ratio(
        result["cost_usd"]["total"], result["cost_usd"]["recorded"]
    )
    return result


def _production(rows: list[dict], *, dry_run: bool = False) -> dict:
    production = [row for row in rows if bool(row["dry_run"]) == dry_run]
    terminal = [
        row
        for row in production
        if row.get("finished_at") and row["status"] in {"success", "no_findings", "failed"}
    ]
    successful = [row for row in terminal if row["status"] in {"success", "no_findings"}]
    failed = sum(row["status"] == "failed" for row in terminal)
    return dict(
        **_usage(production),
        terminal=len(terminal),
        successful=len(successful),
        failed=failed,
        success_rate=ratio(len(successful), len(terminal)),
        failure_rate=ratio(failed, len(terminal)),
        successful_usage=_usage(successful),
        failed_usage=_usage([row for row in terminal if row["status"] == "failed"]),
    )


def _priority_security(row: dict, prefix: str = "") -> bool:
    return (
        normalize_category(row.get(prefix + "category")) == "security"
        and (row.get(prefix + "severity") or "").lower() in {"blocking", "high", "critical"}
        and row.get(prefix + "status") == "posted"
        and row.get(prefix + "verified") != 0
    )


def build_metrics(path: Path, *, generated_at: datetime | None = None) -> dict:
    """Return complete metrics or an explicit unavailable state, never partial stats."""
    try:
        return _build_metrics(path, generated_at=generated_at)
    except _MetricBudgetExceeded:
        return {
            "inventory": {},
            "windows": [],
            "active": [],
            "error": (
                f"Metrics unavailable: the dashboard's {MAX_METRIC_ROWS:,}-row "
                "processing budget was exceeded across the window cohorts. "
                "No partial metrics are shown."
            ),
        }


def _build_metrics(path: Path, *, generated_at: datetime | None = None) -> dict:
    """Aggregate every canonical table, with one consistent window per cohort.

    Findings use updated_at (latest state), runs use started_at, and governance
    uses created_at. Outcomes join the selected finding cohort, not their latest
    polling timestamp. These deliberately different cohorts are labelled in UI.
    """
    instant = generated_at or datetime.now(UTC)
    result: dict[str, Any] = {"inventory": {}, "windows": [], "active": []}
    if not path.exists():
        return result
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as con:
        deadline = time.monotonic() + 5
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        con.row_factory = sqlite3.Row
        present = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        for table in TABLES:
            result["inventory"][table] = (
                con.execute(f"select count(*) from {table}").fetchone()[0]
                if table in present
                else None
            )

        processed = 0

        def bounded(query: str, parameters: tuple) -> list[dict]:
            nonlocal processed
            remaining = MAX_METRIC_ROWS - processed
            records = con.execute(query + " limit ?", (*parameters, remaining + 1)).fetchall()
            if len(records) > remaining:
                raise _MetricBudgetExceeded
            processed += len(records)
            return [dict(record) for record in records]

        def rows(table: str, column: str, cutoff: str) -> list[dict]:
            if table not in present:
                return []
            return bounded(
                f"select {COLUMNS[table]} from {table} where julianday({column}) >= julianday(?) "
                f"and julianday({column}) <= julianday(?)",
                (cutoff, instant.isoformat()),
            )

        if "reviewed_mrs" in present:
            result["active"] = [
                dict(r)
                for r in con.execute(
                    "select project,iid,sha,status,updated_at from reviewed_mrs "
                    "where status in ('queued','running','reviewing') "
                    "order by updated_at desc limit 100"
                )
            ]
        processed += len(result["active"])
        if processed > MAX_METRIC_ROWS:
            raise _MetricBudgetExceeded
        for label, hours in [("today", 24), ("7d", 168), ("30d", 720)]:
            cutoff = (instant - timedelta(hours=hours)).isoformat()
            runs = rows("review_runs", "started_at", cutoff)
            findings = rows("review_findings", "updated_at", cutoff)
            policies = rows("governance_decisions", "created_at", cutoff)
            reconciliations = rows("finding_reconciliations", "updated_at", cutoff)
            grouped: dict[str, list] = defaultdict(list)
            for run in runs:
                grouped[run.get("model") or "Unknown"].append(run)
            categories = Counter(normalize_category(f.get("category")) for f in findings)
            outcomes = []
            if "finding_outcomes" in present and "review_findings" in present:
                projection = ",".join(f"o.{column}" for column in OUTCOME_COLUMNS.split(","))
                projection += ",f.category as finding_category,f.severity as finding_severity,"
                projection += "f.status as finding_status,f.verified as finding_verified"
                outcomes = bounded(
                    f"""select {projection} from finding_outcomes o join review_findings f
                    on (o.project,o.iid,o.sha,o.fingerprint)=(f.project,f.iid,f.sha,f.fingerprint)
                    where julianday(f.updated_at)>=julianday(?)
                    and julianday(f.updated_at)<=julianday(?)""",
                    (cutoff, instant.isoformat()),
                )

            production_runs = [r for r in runs if not r["dry_run"]]
            policy_runs = []
            policy_available = "governance_decisions" in present
            if policy_available and "review_runs" in present:
                # Match the selected run cohort, regardless of decision timestamp.
                # DISTINCT prevents repeated decisions inflating coverage; this
                # additional projection shares the same bounded processing budget.
                policy_runs = bounded(
                    """select distinct g.run_id from governance_decisions g
                    join review_runs r on r.run_id=g.run_id
                    where r.dry_run=0 and julianday(r.started_at)>=julianday(?)
                    and julianday(r.started_at)<=julianday(?)""",
                    (cutoff, instant.isoformat()),
                )
            known_provenance = sum(
                r.get("provenance_band") in {"likely_ai", "collaborative"} for r in production_runs
            )
            resolved_without_rejection = [
                o
                for o in outcomes
                if o.get("resolved") and not o.get("disputed") and not o.get("false_positive")
            ]
            priority = [f for f in findings if _priority_security(f)]
            observed_priority = [o for o in outcomes if _priority_security(o, "finding_")]
            attention = [
                o
                for o in observed_priority
                if not any(
                    o.get(flag)
                    for flag in ("resolved", "deleted", "disputed", "false_positive", "duplicate")
                )
            ]
            outcome_counts = {
                k: sum(bool(o.get(k)) for o in outcomes)
                for k in (
                    "resolved",
                    "deleted",
                    "developer_replied",
                    "disputed",
                    "false_positive",
                    "duplicate",
                    "merged_unresolved",
                    "reply_classified",
                )
            }
            outcome_counts["resolved_without_rejection"] = len(resolved_without_rejection)
            confidence = [
                f["confidence"]
                for f in findings
                if isinstance(f.get("confidence"), (int, float)) and 0 <= f["confidence"] <= 1
            ]
            result["windows"].append(
                dict(
                    label=label,
                    since=cutoff,
                    until=instant.isoformat(),
                    usage=_usage(runs),
                    production=_production(runs),
                    validation=_production(runs, dry_run=True),
                    audit_evidence=dict(
                        available=policy_available,
                        production_runs=len(production_runs),
                        policy_recorded=len(policy_runs) if policy_available else None,
                        policy_missing=(len(production_runs) - len(policy_runs))
                        if policy_available
                        else None,
                        policy_coverage=ratio(len(policy_runs), len(production_runs))
                        if policy_available
                        else None,
                        provenance_known=known_provenance,
                        provenance_unknown=len(production_runs) - known_provenance,
                        provenance_coverage=ratio(known_provenance, len(production_runs)),
                    ),
                    security_attention=dict(
                        total=len(priority),
                        observed=len(observed_priority),
                        unobserved=len(priority) - len(observed_priority),
                        open=len(attention),
                    ),
                    families=dict(Counter(model_family(r.get("model")) for r in runs)),
                    prompt_versions=dict(
                        Counter(r.get("prompt_version") or "unknown" for r in runs)
                    ),
                    models=[
                        dict(
                            model=model,
                            family=model_family(model),
                            production=_production(group),
                            **_usage(group),
                        )
                        for model, group in sorted(grouped.items())
                    ],
                    findings=dict(
                        total=len(findings),
                        severity=dict(Counter(f.get("severity") or "unknown" for f in findings)),
                        categories=dict(categories),
                        raw_categories=dict(
                            Counter(f.get("category") or "unknown" for f in findings)
                        ),
                        types=dict(Counter(f.get("type") or "unknown" for f in findings)),
                        statuses=dict(Counter(f["status"] for f in findings)),
                        security=categories["security"],
                        verified=sum(f.get("verified") == 1 for f in findings),
                        refuted=sum(f.get("verified") == 0 for f in findings),
                        confidence=stats(confidence),
                    ),
                    outcomes=dict(
                        blocking_resolved_without_rejection=sum(
                            (o.get("finding_severity") or "").lower() == "blocking"
                            for o in resolved_without_rejection
                        ),
                        observed=len(outcomes),
                        unobserved=len(findings) - len(outcomes),
                        **outcome_counts,
                        rates={k: ratio(v, len(outcomes)) for k, v in outcome_counts.items()},
                        dispositions=dict(
                            Counter(o.get("developer_disposition") or "unknown" for o in outcomes)
                        ),
                        resolution_evidence=dict(
                            Counter(o.get("resolution_evidence") or "unknown" for o in outcomes)
                        ),
                        disposition_evidence=dict(
                            Counter(o.get("disposition_evidence") or "unknown" for o in outcomes)
                        ),
                        resolution_sources=dict(
                            Counter(o.get("resolution_source") or "unknown" for o in outcomes)
                        ),
                    ),
                    governance=dict(
                        total=len(policies),
                        triggered=sum(bool(p["triggered"]) for p in policies),
                        actions=dict(Counter(p["action"] for p in policies)),
                        modes=dict(Counter(p["mode"] for p in policies)),
                        rules=dict(Counter(p.get("matched_rule") or "none" for p in policies)),
                        rigor_injected=sum(bool(p.get("rigor_injected")) for p in policies),
                    ),
                    provenance=dict(
                        recorded=sum(bool(r.get("provenance_band")) for r in runs),
                        bands=dict(Counter(r.get("provenance_band") or "unknown" for r in runs)),
                        confidence=dict(
                            Counter(r.get("provenance_confidence") or "unknown" for r in runs)
                        ),
                        sources=dict(
                            Counter(r.get("provenance_source") or "unknown" for r in runs)
                        ),
                    ),
                    reconciliations=dict(
                        total=len(reconciliations),
                        states=dict(Counter(r["state"] for r in reconciliations)),
                        verdicts=dict(
                            Counter(r.get("verdict") or "unknown" for r in reconciliations)
                        ),
                    ),
                    review_modes=dict(Counter(r.get("review_mode") or "unknown" for r in runs)),
                    tones=dict(Counter(r.get("tone") or "unknown" for r in runs)),
                )
            )
    return result
