"""Product telemetry says why a finding was not posted.

Drives the real ``post_or_plan_findings`` with a real ``ReviewTelemetry`` on
an in-memory OTel reader, then passes each point through the PostHog
``safe_context`` filter, so a label only counts if it survives export.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.trace import NoOpTracerProvider
from review_output import review_output

from bubo import db, paths, poller
from bubo.analytics_config import AnalyticsConfig
from bubo.review_config import ReviewConfig
from bubo.telemetry.config import TelemetryConfig
from bubo.telemetry.metrics import ReviewTelemetry
from bubo.telemetry.posthog import safe_context
from bubo.verification import Verdict


@contextmanager
def _temp_db() -> Iterator[None]:
    original = paths.DB
    try:
        with tempfile.TemporaryDirectory() as tmp:
            paths.DB = Path(tmp) / "reviewer.sqlite"
            db.init_db()
            yield
    finally:
        paths.DB = original


def _finding(line: int = 1, confidence: float = 0.99) -> dict[str, Any]:
    return {
        "category": "correctness",
        "file": "f.py",
        "line": line,
        "confidence": confidence,
        "title": f"bug on {line}",
        "type": "issue",
        "severity": "blocking",
        "impact": "x",
        "evidence": "y",
        "fix": "z",
    }


class _Provider:
    name = "gitlab"

    def change_number(self, mr):
        return mr["iid"]

    def get_change(self, *a, **k):
        return {}

    def head_sha(self, change):
        return "sha"

    def changed_lines(self, *a, **k):
        return {}

    def build_position(self, change, changed, finding):
        # Line 99 is outside the diff; everything else maps.
        if finding.get("line") == 99:
            return None
        return {"new_path": "f.py", "new_line": finding.get("line")}


def _run(cfg: ReviewConfig, findings: list[dict[str, Any]], verdicts=None) -> dict[str, list]:
    reader = InMemoryMetricReader()
    telemetry = ReviewTelemetry(
        TelemetryConfig(analytics=AnalyticsConfig(enabled=True)),
        providers=(
            NoOpTracerProvider().get_tracer("bubo"),
            MeterProvider(metric_readers=[reader]).get_meter("bubo"),
        ),
    )
    with _temp_db(), patch.object(poller, "run_verification", return_value=verdicts or []):
        poller.post_or_plan_findings(
            cfg=cfg,
            token="t",
            project="g/r",
            mr={"iid": 1, "sha": "sha"},
            raw_review=review_output(findings),
            telemetry=telemetry,
            provider=_Provider(),
        )
    out: dict[str, list] = {}
    data = reader.get_metrics_data()
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    exported = safe_context(dict(point.attributes))
                    out.setdefault(metric.name, []).extend([exported] * int(point.value))
    return out


def _reasons(points: dict[str, list]) -> list[tuple[str | None, str | None]]:
    return sorted(
        (p.get("status"), p.get("skip_reason")) for p in points.get("llm_review.findings", [])
    )


def test_policy_filtered_finding_is_counted_with_its_reason() -> None:
    points = _run(ReviewConfig(dry_run=True, min_confidence=0.5), [_finding(confidence=0.1)])
    assert _reasons(points) == [("skipped", "confidence_below_threshold")]


def test_out_of_diff_finding_says_line_not_in_diff() -> None:
    points = _run(ReviewConfig(dry_run=True), [_finding(line=99), _finding(line=2)])
    assert _reasons(points) == [("planned", None), ("skipped", "line_not_in_diff")]


def test_verifier_outage_is_counted_as_unresolved() -> None:
    failed = [
        Verdict(lens="correctness", real=False, confidence=0.0, reason="", ok=False),
        Verdict(lens="in_diff", real=False, confidence=0.0, reason="", ok=False),
    ]
    cfg = ReviewConfig(dry_run=True, verify_findings=True, verify_min_votes=2)
    points = _run(cfg, [_finding()], verdicts=failed)
    assert _reasons(points) == [("verify_capped", "verification_unavailable")]
    assert [p.get("outcome") for p in points["llm_review.verifications"]] == ["unresolved"]


def test_verification_cap_is_counted_once_per_finding() -> None:
    real = [
        Verdict(lens="correctness", real=True, confidence=0.9, reason="ok"),
        Verdict(lens="in_diff", real=True, confidence=0.9, reason="ok"),
    ]
    cfg = ReviewConfig(
        dry_run=True, verify_findings=True, verify_min_votes=2, verify_max_findings=1
    )
    points = _run(cfg, [_finding(line=1), _finding(line=2)], verdicts=real)
    assert _reasons(points) == [("planned", None), ("verify_capped", "verification_capped")]
    outcomes = sorted(p.get("outcome") for p in points["llm_review.verifications"])
    assert outcomes == ["capped", "verified"]


def test_skip_reason_outside_the_known_set_is_not_exported() -> None:
    assert "skip_reason" not in safe_context({"skip_reason": "src/secret/path.py"})
    assert json.dumps(safe_context({"skip_reason": "line_not_in_diff"})).count("line_not") == 1
