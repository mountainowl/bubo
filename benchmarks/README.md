# Bubo review benchmark

Apples-to-apples evaluation of bubo — and any other reviewer — on the **same
corpus**, scoring both **quality** (vs labeled ground truth) and **efficiency**
(tokens / time / cost). Run it periodically to catch regressions, or run it
independently to compare tools.

## Metrics (standard practice)

| Metric | Meaning |
|---|---|
| **precision** | findings that hit a labeled issue ÷ all findings |
| **recall** | labeled issues found ÷ all labeled issues |
| **F1** | harmonic mean of the two — the headline number |
| **FP-rate** | findings that hit nothing ÷ all findings (= 1 − precision) |
| **tokens / seconds / cost** | efficiency per case, summed |
| **tokens·$/TP** | tokens (and cost) per true positive — the value-for-money axis |

A finding **hits** a ground-truth issue when it **localizes** it (same file, line
within a tolerance, optionally same category) and describes it. Matching is greedy
1:1, so piling on duplicate comments can't inflate true positives. The default
matcher is deterministic and dependency-free; swap in an LLM-as-judge matcher for
fuzzy description matching (see `score.py`). Aggregates are micro-averaged (pooled
TP/FP/FN) so bigger cases weigh proportionally.

## Run it (replay — no API key)

```sh
uv run python benchmarks/run.py
```

This scores the captured outputs in `benchmarks/results/sample/` against
`benchmarks/cases/` and writes `benchmarks/report.md` + `report.json`. The unit
tests (`benchmarks/test_score.py`) and this replay run are wired into CI
(`.github/workflows/benchmark.yml`, weekly + on demand) as a regression check —
neither needs an API key.

## Corpus — `benchmarks/cases/*.json`

```json
{
  "id": "py-divzero-001",
  "language": "python",
  "source": "synthetic",
  "description": "mean() divides by len(values) with no empty guard.",
  "diff": "optional snippet/diff for a live run",
  "ground_truth": [
    {"file": "stats.py", "line": 7, "category": "correctness", "severity": "blocking", "summary": "..."}
  ]
}
```

Include **clean** cases (empty `ground_truth`) to measure false positives. The two
shipped cases are synthetic — they exercise the harness. **For trustworthy
numbers, use real PRs with human-labeled issues**: synthetic, hand-injected bugs
are easier than real-world defects and overstate LLM performance.

## Add a reviewer (apples-to-apples) — `benchmarks/results/<run>/<reviewer>.json`

```json
{
  "reviewer": "bubo",
  "cases": {
    "py-divzero-001": {
      "findings": [{"file": "stats.py", "line": 7, "category": "correctness", "severity": "blocking", "summary": "..."}],
      "efficiency": {"tokens_total": 5400, "seconds": 41.2, "cost_usd": 0.27}
    }
  }
}
```

Then `uv run python benchmarks/run.py --results benchmarks/results/<run>`.

- **bubo:** review each case with `dry_run=true`, read findings from the
  `get_findings` MCP tool / SQLite and tokens+cost from `bubo report` (or the
  per-run telemetry), and write the result file. (A capture script is the natural
  next step.)
- **other tools** (CodeRabbit, Copilot, Qodo, …): export their PR comments into
  the same shape. Same corpus + same scoring + same line tolerance = a fair fight.

## Caveats — read before trusting a number

- **Look at F1 and both halves.** Many reviewers buy high precision with terrible
  recall (or the reverse); one number hides that.
- **Synthetic ≠ real.** Keep the bar honest with real, labeled PRs.
- **The matcher defines a "hit."** Keep `--line-tolerance` / `--require-category`
  (and the matcher) fixed across every reviewer in a comparison.

## References

- Qodo — [How we built a real-world benchmark for AI code review](https://www.qodo.ai/blog/how-we-built-a-real-world-benchmark-for-ai-code-review/).
- [Benchmarking and Studying the LLM-based Code Review](https://arxiv.org/pdf/2509.01494) (arXiv 2509.01494).
- Augment — [Deep code review: why recall beats precision for agents](https://www.augmentcode.com/guides/deep-code-review-recall-vs-precision).
- CodeAnt — [Why synthetic benchmarks fail for LLM code evaluation](https://www.codeant.ai/blogs/test-llm-performance-real-code).
