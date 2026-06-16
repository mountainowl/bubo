# Telemetry

Bubo emits OpenTelemetry metrics and traces, so dashboard rollups stay
outside the poller. All metrics are namespaced `llm_review.*` and
registered in `src/bubo/telemetry/metrics.py`.

## Emitted metrics

| Metric | Type | What it counts | Key attributes | Example sample (OTLP) |
|---|---|---|---|---|
| `llm_review.runs` | counter | One increment per completed review run (a single MR/PR worker exiting). | `repo`, `model`, `status` (`success`/`no_findings`/`failed`/`skipped`), `review_mode` (`poller`/`manual`), `dry_run` | `llm_review.runs{repo="g/r", model="gpt-5.5", status="success", review_mode="poller", dry_run="false"} 1` |
| `llm_review.findings` | counter | One increment per finding lifecycle event — initial post AND every outcome transition picked up by `--sync-outcomes`. | `repo`, `status` (`planned`/`posted`/`skipped`/`pending_external_id`/`resolved`/`disputed`/`false_positive`/`duplicate`/`deleted`/`developer_replied`), `dry_run`, `finding_type`, `severity`, `category` | `llm_review.findings{repo="g/r", status="posted", severity="blocking", category="correctness"} 1` |
| `llm_review.tokens` | counter | LLM token consumption per review, split into four streams. | `repo`, `model`, `status`, `review_mode`, `dry_run`, `operation` (`input`/`output`/`cached`/`total`) | `llm_review.tokens{repo="g/r", model="gpt-5.5", operation="total"} 65926` |
| `llm_review.cost.usd` | counter | Estimated provider cost in USD per review, summed from the configured `[telemetry.pricing.*]` rates. | `repo`, `model`, `status`, `review_mode`, `dry_run` | `llm_review.cost.usd{repo="g/r", model="gpt-5.5"} 0.32963` |
| `llm_review.failures` | counter | One increment per failed pipeline stage. | `repo`, `error_type` (Python exception class), `operation` (`review` for a failed worker, `outcome_sync` for a failed `--sync-outcomes` fetch) | `llm_review.failures{repo="g/r", operation="outcome_sync", error_type="HTTPError"} 1` |
| `llm_review.latency.review_seconds` | histogram | Per-review wall-clock from worker start to finish. | `repo`, `model`, `status`, `review_mode`, `dry_run` | `llm_review.latency.review_seconds_sum{repo="g/r"} 412.3`<br>`llm_review.latency.review_seconds_count{repo="g/r"} 3` |
| `llm_review.latency.queue_seconds` | histogram | Time from when a job is written to the queue to when a worker picks it up — your saturation signal. | `repo` | `llm_review.latency.queue_seconds_sum{repo="g/r"} 6.1`<br>`llm_review.latency.queue_seconds_count{repo="g/r"} 3` |

Two switches in `config/env.toml` let you trim emission cost:

- `[telemetry].emit_finding_events` — drop the per-finding counter
  entirely if you only care about run-level rollups.
- `[telemetry].emit_outcome_sync` — drop the outcome-derived increments on
  `llm_review.findings` (`resolved`/`disputed`/`false_positive`/etc.) and
  keep only the initial-post events.

## Common dashboards

- **Throughput**: `rate(llm_review.runs[5m])` grouped by `repo` + `status`.
- **Saturation**: `histogram_quantile(0.95, llm_review.latency.queue_seconds_bucket)` — if this climbs, raise `max_merge_requests_per_poll`.
- **Cost**: `sum(rate(llm_review.cost.usd[1d])) by (repo, model)`.
- **Quality / ROI**: ratio of `llm_review.findings{status="resolved"}` over `llm_review.findings{status="posted"}`, grouped by `severity`.
- **Reliability**: `rate(llm_review.failures[5m])` broken out by `operation`.

## Tracing — the per-review span tree

Each review is one trace. The `llm_review.run` span has a **child span per
stage**, so a trace shows exactly where a review's wall-clock went (a slow review
is almost always the agent stage, not Bubo):

```text
llm_review.run                      repo, mr_iid, sha, run_id
├─ llm_review.checkout              repo, sha
├─ llm_review.provenance            repo            (only when governance is on)
├─ llm_review.agent                 repo, model, tokens_input/output/total,
│                                   cost_usd, exit_code
└─ llm_review.post                  repo, dry_run, findings_posted/planned/skipped
```

The attributes ride on the spans, so any OpenTelemetry-aware backend (Tempo,
Jaeger, Honeycomb, Grafana, Datadog, …) can break latency, tokens, and cost down
**by stage and by model** without Bubo shipping a dashboard of its own — Bubo
emits the data; you bring the tool. Spans are emitted only when
`[telemetry].otlp_endpoint` is set; otherwise this is a no-op.

## Cardinality discipline

Metric attributes are kept low-cardinality on purpose. MR IID, SHA, file
path, line number, fingerprint, and discussion ID live in **SQLite or
span attributes/events only**, never as metric labels — so the dashboard backend
doesn't melt as the queue accelerates. Spans carry the full per-MR
context for trace-level drilldown.
