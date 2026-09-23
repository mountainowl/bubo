"""Measure Bubo DB query-shape latency on a synthetic 10k-review state store.

Run with:
    .venv/bin/python benchmarks/benchmark_db_query_latency.py --iterations 8

The script uses the production timing boundary, warms every exercised shape
once, then fails when any measured SQL execution exceeds 30ms. It is kept out
of ordinary pytest because host scheduling and storage cache make latency
assertions unsuitable for CI.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sqlite3
import statistics
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from time import perf_counter
from typing import cast

from bubo import db, db_reporting, paths, report, ui_export
from bubo.db_schema import SLOW_QUERY_MS, observe_queries, query_label
from bubo.governance_policy import GovernanceDecision
from bubo.provenance import ProvenanceSignal
from bubo.statuses import FindingStatus, ReviewStatus
from bubo.telemetry import TokenUsage


@contextmanager
def benchmark_state() -> Iterator[None]:
    """Point Bubo's state paths at an isolated, populated temporary database."""
    original = {
        name: getattr(paths, name)
        for name in (
            "DB",
            "WORK",
            "REPORTS",
            "JOBS",
            "LOGS",
            "RENDERED_PROMPTS",
            "CONFIG",
        )
    }
    with tempfile.TemporaryDirectory(prefix="bubo-db-latency-") as raw:
        root = Path(raw)
        paths.DB = root / "state.sqlite"
        paths.WORK = root / "work"
        paths.REPORTS = root / "reports"
        paths.JOBS = root / "jobs"
        paths.LOGS = root / "logs"
        paths.RENDERED_PROMPTS = root / "prompts"
        paths.CONFIG = root / "env.toml"
        try:
            db.init_db()
            _seed_10k()
            yield
        finally:
            for name, value in original.items():
                setattr(paths, name, value)


def _seed_10k() -> None:
    """Seed outside measurement so bulk fixture loading is not a production result."""
    stamp = "2026-09-23T12:00:00+00:00"
    with sqlite3.connect(paths.DB) as connection:
        connection.executemany(
            "insert into reviewed_mrs(project,iid,sha,status,updated_at) values(?,?,?,?,?)",
            [
                (f"g/r{index % 20}", index, f"sha{index}", "success", stamp)
                for index in range(10_000)
            ],
        )
        connection.executemany(
            "insert into review_runs("
            "run_id,project,iid,sha,status,model,prompt_version,review_mode,"
            "dry_run,started_at,finished_at,tokens_total,cost_usd"
            ") values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"run{index}",
                    f"g/r{index % 20}",
                    index,
                    f"sha{index}",
                    "success",
                    "model",
                    "v1",
                    "diff",
                    0,
                    stamp,
                    stamp,
                    100,
                    0.01,
                )
                for index in range(10_000)
            ],
        )
        connection.executemany(
            "insert into review_findings("
            "project,iid,sha,fingerprint,file,line,status,discussion_id,body,updated_at"
            ") values(?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    f"g/r{index % 20}",
                    index,
                    f"sha{index}",
                    f"f{index}",
                    "file.py",
                    1,
                    "posted",
                    f"d{index}",
                    "body",
                    stamp,
                )
                for index in range(10_000)
            ],
        )
        connection.executemany(
            "insert into finding_outcomes("
            "finding_id,project,iid,sha,fingerprint,resolved,last_checked_at"
            ") values(?,?,?,?,?,?,?)",
            [
                (
                    f"g/r{index % 20}:{index}:sha{index}:f{index}",
                    f"g/r{index % 20}",
                    index,
                    f"sha{index}",
                    f"f{index}",
                    index % 2,
                    stamp,
                )
                for index in range(10_000)
            ],
        )
        connection.executemany(
            "insert into governance_decisions("
            "run_id,project,iid,sha,mode,action,triggered,created_at"
            ") values(?,?,?,?,?,?,?,?)",
            [
                (f"run{index}", f"g/r{index % 20}", index, f"sha{index}", "off", "clear", 0, stamp)
                for index in range(10_000)
            ],
        )


def _exercise_once() -> dict[str, object]:
    """Exercise schema, runtime writes/readers, and report/UI query shapes."""
    project, iid, sha, run_id, fingerprint = "g/r0", 0, "sha0", "benchmark", "benchmark"
    finding = {"file": "file.py", "line": 1, "category": "correctness", "confidence": 0.9}
    outcome = {
        "resolved": False,
        "deleted": False,
        "developer_replied": False,
        "disputed": False,
        "false_positive": False,
        "duplicate": False,
        "merged_unresolved": False,
    }
    db.init_db()
    db.record_review_run_start(
        run_id=run_id,
        project=project,
        iid=iid,
        sha=sha,
        model="model",
        prompt_version="v1",
        review_mode="diff",
        dry_run=True,
    )
    db.record_review_run_finish(
        run_id=run_id,
        status=ReviewStatus.SUCCESS,
        tokens=TokenUsage(input=1, output=1, cached=0, total=2),
        cost_usd=0.01,
        error=None,
    )
    db.record_provenance(run_id, ProvenanceSignal())
    db.provenance_for(run_id)
    db.record_governance_decision(
        run_id,
        project=project,
        iid=iid,
        sha=sha,
        decision=GovernanceDecision(
            mode="off",
            action="clear",
            triggered=False,
            matched_rule="",
            band="unknown",
            sensitive_paths=[],
            reason="benchmark",
        ),
    )
    db.governance_decision_for(run_id)
    db.governance_decisions_for(project, iid, sha)
    db.governance_decisions_for(project, iid)
    db.record(project, iid, sha, ReviewStatus.SUCCESS)
    db.already_seen(project, iid, sha)
    db.count_inflight_workers()
    db.latest_reviewed_row()
    db.finding_seen(project, iid, sha, fingerprint)
    db.record_finding(
        project=project,
        iid=iid,
        sha=sha,
        fingerprint=fingerprint,
        finding=finding,
        status=FindingStatus.POSTED,
        body="body",
        discussion_id="benchmark",
    )
    db.record_finding_outcome(
        project=project,
        iid=iid,
        sha=sha,
        fingerprint=fingerprint,
        discussion_id="benchmark",
        outcome=outcome,
    )
    db.record_finding_outcome_sync_attempt(
        project=project,
        iid=iid,
        sha=sha,
        fingerprint=fingerprint,
        discussion_id="benchmark",
    )
    db.posted_findings_for_outcome_sync()
    db.disputed_finding_classes(project, min_samples=1, threshold=0.5)
    db.disputed_class_stats(project, min_samples=1)
    db.list_recent_reviews(limit=50)
    db.list_recent_reviews(limit=50, status=ReviewStatus.SUCCESS, project=project)
    db.metrics_summary(since_hours=720, project=project)
    db.get_review_row(project, iid)
    db.get_review_row(project, iid, sha)
    db.findings_for(project, iid)
    db.findings_for(project, iid, sha)
    db.outcomes_for(project, iid)
    db.outcomes_for(project, iid, sha)
    with db.connect_db(readonly=True) as connection:
        db.review_details_for([(project, iid, sha)], connection=connection)
        report.build_report(since_hours=720, limit=50, connection=connection)
        db.audit_rows(since_hours=720, project=project, limit=50, connection=connection)
        with suppress(sqlite3.OperationalError):
            connection.execute("select ? from", ("benchmark-private",))
    return cast(dict[str, object], ui_export.build_data())


def _rss_bytes() -> int:
    """Return current resident memory, excluding seed-time high-water usage."""
    output = subprocess.run(
        ["ps", "-o", "rss=", "-p", str(os.getpid())],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return int(output.strip()) * 1024


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.9999) - 1))]


def _summarize(timings: dict[str, list[float]]) -> dict[str, dict[str, float | int]]:
    return {
        label: {
            "count": len(values),
            "p50_ms": round(statistics.median(values), 3),
            "p95_ms": round(_percentile(values, 0.95), 3),
            "max_ms": round(max(values), 3),
        }
        for label, values in sorted(timings.items())
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--audit-page-size", type=int)
    args = parser.parse_args()
    if args.audit_page_size is not None:
        db_reporting._AUDIT_PAGE_SIZE = args.audit_page_size
    cold_timings: dict[str, list[float]] = defaultdict(list)
    timings: dict[str, list[float]] = defaultdict(list)
    cold_operation_ms = 0.0
    operation_times: list[float] = []
    data: dict[str, object] = {}
    rss_after_warm = 0

    with benchmark_state():
        with observe_queries() as collector:
            started = perf_counter()
            data = _exercise_once()
            cold_operation_ms = (perf_counter() - started) * 1000
        for timing in collector.records:
            cold_timings[timing.label].append(timing.duration_ms)
        del data
        _exercise_once()  # warm SQLite page/cache and create the benchmark rows
        gc.collect()
        rss_after_warm = _rss_bytes()
        with observe_queries() as collector:
            for _ in range(args.iterations):
                started = perf_counter()
                data = _exercise_once()
                operation_times.append((perf_counter() - started) * 1000)
        for timing in collector.records:
            timings[timing.label].append(timing.duration_ms)

    cold_summary = _summarize(cold_timings)
    summary = _summarize(timings)
    cold_failures = {
        label: item for label, item in cold_summary.items() if item["max_ms"] > SLOW_QUERY_MS
    }
    failures = {label: item for label, item in summary.items() if item["max_ms"] > SLOW_QUERY_MS}
    audit_shapes = {label: item for label, item in summary.items() if label.startswith("with:")}
    audit_events = sum(
        len(values) for label, values in timings.items() if label.startswith("with:")
    )
    ui_json_bytes = len(json.dumps(data, sort_keys=True).encode())
    required_shapes = {
        "failed_execute": query_label("select ? from"),
        "limited_audit_count": query_label(
            "select count(*) from review_runs "
            "where started_at >= ? and started_at <= ? "
            "and (? is null or project = ?)"
        ),
    }
    rss_with_data = _rss_bytes()
    del data
    gc.collect()
    rss_after_release = _rss_bytes()
    print(
        json.dumps(
            {
                "cold": {
                    "operation_ms": round(cold_operation_ms, 3),
                    "query_observation_count": sum(len(values) for values in cold_timings.values()),
                    "query_shape_count": len(cold_summary),
                    "query_shapes": cold_summary,
                    "slow_failures": cold_failures,
                },
                "audit_pagination": {
                    "page_size": db_reporting._AUDIT_PAGE_SIZE,
                    "statement_count": audit_events,
                },
                "audit_page_shapes": audit_shapes,
                "operation": {
                    "p50_ms": round(statistics.median(operation_times), 3),
                    "p95_ms": round(_percentile(operation_times, 0.95), 3),
                    "max_ms": round(max(operation_times), 3),
                    "rss_after_release_bytes": rss_after_release,
                    "rss_after_warm_bytes": rss_after_warm,
                    "rss_with_data_bytes": rss_with_data,
                    "ui_json_bytes": ui_json_bytes,
                },
                "output_equality": "covered by test_audit_rows_preserve_correlated_count_semantics",
                "query_observation_count": sum(len(values) for values in timings.values()),
                "query_shape_count": len(summary),
                "query_shapes": summary,
                "required_shapes": {
                    name: summary.get(label) for name, label in required_shapes.items()
                },
                "slow_failures": {"cold": cold_failures, "warm": failures},
            },
            sort_keys=True,
        )
    )
    return 1 if cold_failures or failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
