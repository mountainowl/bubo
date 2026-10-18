"""What one real ``worker()`` review emits to product analytics and OTel.

Runs the real worker and the real GitHub provider. Only the HTTP boundary,
the git checkout and the reviewer agent subprocess are faked. PostHog events
are read from Bubo's own outbound queue (``conftest`` blocks the network), and
OTel points from a real SDK ``MeterProvider`` with an in-memory reader.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.trace import NoOpTracerProvider
from review_output import review_output

from bubo import analytics, db, paths, poller
from bubo.analytics_config import AnalyticsConfig
from bubo.review_config import ReviewConfig
from bubo.scm.github import GitHubProvider
from bubo.telemetry.config import TelemetryConfig
from bubo.telemetry.metrics import ReviewTelemetry

SHA = "b" * 40
PATCH = "@@ -0,0 +1,3 @@\n+a\n+b\n+c"
CONSENT = AnalyticsConfig(enabled=True, endpoint="https://posthog.invalid/batch", api_key="k")
FINDING = {
    "file": "a.py",
    "line": 2,
    "title": "t",
    "body": "b",
    "severity": "blocking",
    "category": "correctness",
    "confidence": 0.95,
    "kind": "bug",
    "fix": "f",
}


def _route(method: str, url: str, _body: Any) -> Any:
    path = urlparse(url).path
    if path.endswith("/pulls/1/files"):
        return [{"filename": "a.py", "patch": PATCH}]
    if path.endswith("/pulls/1/commits"):
        return [{"sha": "c1", "commit": {"message": "feat: x", "author": {"name": "J"}}}]
    if path.endswith("/pulls/1"):
        return {"number": 1, "state": "open", "head": {"sha": SHA}}
    if method == "POST" and path.endswith("/pulls/1/comments"):
        return {"id": 77, "node_id": "PRRC_1"}
    return [] if method == "GET" else {}


@contextmanager
def _state() -> Iterator[Path]:
    old = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "state" / "reviewer.sqlite"
            paths.DB.parent.mkdir()
            db.init_db()
            yield Path(tmp)
    finally:
        paths.DB = old


def run_worker(
    tmp: Path, cfg: ReviewConfig, *, findings: list[dict[str, Any]] | None = None, **patches: Any
) -> SimpleNamespace:
    """Run one review; return its exit code, queued PostHog events and OTel points."""
    reader = InMemoryMetricReader()
    telemetry = ReviewTelemetry(
        cfg.telemetry_config,
        providers=(
            NoOpTracerProvider().get_tracer("bubo"),
            MeterProvider(metric_readers=[reader]).get_meter("bubo"),
        ),
    )
    stdout = review_output([FINDING] if findings is None else findings)
    prompt = tmp / "prompt.md"
    prompt.write_text("rendered prompt", encoding="utf-8")
    job = tmp / "job.json"
    job.write_text(json.dumps({"project": "o/r", "mr": {"number": 1, "head": {"sha": SHA}}}))

    def http(url: str, *, method: str, body: Any = None, **_: Any) -> tuple[Any, dict[str, str]]:
        return _route(method, url, body), {}

    with (
        patch("bubo._http.request_json", side_effect=http),
        patch.object(GitHubProvider, "token", return_value="tok"),
        patch.object(GitHubProvider, "checkout", return_value=None),
        patch.object(poller.paths, "REPORTS", tmp / "reports"),
        patch.object(poller.paths, "WORK", tmp / "work"),
        patch("bubo.poller.read_config", return_value=cfg),
        patch("bubo.poller.write_rendered_meta_prompt", return_value=prompt),
        patch(
            "bubo.poller.run",
            return_value=SimpleNamespace(stdout=stdout, stderr="", returncode=0),
        ),
        patch("bubo.poller.cleanup_worktree"),
        patch("bubo.poller.analytics.flush"),
        patch("bubo.poller.ReviewTelemetry.from_config", return_value=telemetry),
        patch("bubo.project_analytics.collect_profile", return_value={}),
    ):
        with _extra(patches):
            rc = poller.worker(job)
    events = [
        (event["event"], dict(event["properties"])) for _, _, event in analytics._pending_events
    ]
    analytics._pending_events.clear()
    return SimpleNamespace(rc=rc, events=events, points=_points(reader))


@contextmanager
def _extra(patches: dict[str, Any]) -> Iterator[None]:
    managers = [patch(target, value) for target, value in patches.items()]
    for manager in managers:
        manager.start()
    try:
        yield
    finally:
        for manager in reversed(managers):
            manager.stop()


def _points(reader: InMemoryMetricReader) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    data = reader.get_metrics_data()
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                out.setdefault(metric.name, []).extend(
                    dict(point.attributes) for point in metric.data.data_points
                )
    return out


def _cfg(**overrides: Any) -> ReviewConfig:
    return ReviewConfig(
        provider="github",
        dry_run=False,
        analytics_config=CONSENT,
        telemetry_config=TelemetryConfig(analytics=CONSENT),
        **overrides,
    )


def _completed(result: SimpleNamespace) -> dict[str, Any]:
    [props] = [props for name, props in result.events if name == "review_completed"]
    return props


def test_review_run_carries_prompt_version_to_otel_and_posthog() -> None:
    with _state() as tmp:
        result = run_worker(tmp, _cfg())
        stored = db.connect_db().execute("select prompt_version from review_runs").fetchone()[0]
    assert result.rc == 0
    assert len(stored) == 12
    assert _completed(result)["prompt_version"] == stored
    assert {point.get("prompt_version") for point in result.points["llm_review.runs"]} == {stored}
