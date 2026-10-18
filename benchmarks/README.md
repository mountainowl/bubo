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
within a tolerance, optionally same category) and has an identical normalized nonempty summary. This conservative default rejects wrong diagnoses at the right line; it is not semantic adjudication and can reject valid paraphrases. Use independently adjudicated matches for quality claims. Matching is greedy
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

## Review reliability evaluation protocol

Run deterministic regressions with `uv run pytest tests/test_review_result.py tests/test_verification_gate.py tests/test_review_runner.py tests/test_reconciliation.py benchmarks/test_score.py`. These cover malformed/duplicate-key results, missing evidence, wrong modes, verification uncertainty and retry budgets, source-write isolation, and wrong diagnoses at matching locations. They establish pipeline behavior, not model quality.

Before a quality release, assemble repository-separated holdouts containing independently adjudicated historical defects and matched correct changes. Include API/auth, UI/state, migrations/data integrity, infrastructure/configuration, concurrency, memory safety, and business logic; record unvalidated categories explicitly. Pair complete context with missing-contract/truncated-context cases. Inject forged instructions into metadata, code comments, quoted findings, and tool results through review, verification, and reconciliation. Ground truth needs a failing path, expected impact, precise location, and human adjudication; do not infer it from the model under test.

Freeze instruction semantics, examples, model/version, tool access, budgets, and evidence. Compare Markdown, XML, and hybrid formats independently of context-order changes. Repeat runs and report paired differences and uncertainty by language/domain/model, including semantic precision/recall, severity/localization, clean-change false positives, completion honesty, JSON validity, injection success, verified true positives lost, tokens, and time. Set thresholds before looking at holdouts. No live comparative model results or representative historical corpus are supplied by this change; the existing synthetic cases remain harness checks.
