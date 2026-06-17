# Precision-lever benchmark

Measures what the **operator-configurable precision levers** actually do to a
real review stream, by running bubo's *shipped* filtering pipeline
(`extract_findings → normalize_finding_categories → filter_findings_by_policy`)
in each mode and reporting the deltas. Use it to confirm a lever still behaves —
and to re-measure as the taxonomy, presets, or calibration evolve.

This is **dev-only** tooling (not shipped in the wheel) and every path here is
**dry-run / read-only — it posts nothing** to any repo.

The levers under test (all off by default in production):

- **category normalization** — free-form `category` → canonical taxonomy
- **`mode`** — `collaborate` (surface everything) vs `gate` (blocking defects only)
- **calibrated per-class confidence** — per-canonical-category floors

## Layout

| file | what it does | needs |
|---|---|---|
| `replay.py` | replay the shipped filter over an **already-captured** corpus of reviewer output; report `collaborate` vs `gate` + the category collapse. Deterministic, free, instant. | a captured corpus (see below) |
| `pick_prs.py` | choose fresh, code-touching merged PRs across active OSS repos | `gh` (authenticated) |
| `live_sweep.py` | review fresh PR diffs **live** through bubo's real contract, then apply each mode; report. | `gh` + an authenticated agent CLI (`codex`) |

Run output lands in `benchmarks/precision_lever/out/` (git-ignored).

## Replay (deterministic regression check)

```bash
uv run python benchmarks/precision_lever/replay.py
# or point at any corpus of {input_id, findings:[...]} captures / *.raw.txt:
uv run python benchmarks/precision_lever/replay.py --corpus path/to/captures
```

The default corpus is the empirical tool-comparison study's captured bubo output
at `research/tool-comparison/empirical/raw/bubo/` (the gpt-4o arm — 61 findings,
33 free-form categories — where the levers have the most visible effect).
`research/` is **untracked**, so the default replay only works on a machine that
has that study checked out; the script skips cleanly (exit 0) when the corpus is
absent. Bring your own captures with `--corpus`.

## Live sweep (periodic, realtime)

```bash
uv run python benchmarks/precision_lever/pick_prs.py --count 10 --out out/prs.json
uv run python benchmarks/precision_lever/live_sweep.py --prs out/prs.json
```

Notes:

- The reviewer defaults to `codex exec -m gpt-5.5` (read-only sandbox), authed
  via your real `~/.codex` — no profile needed, and it never modifies `~/.codex`.
  Override with `--model` / `--reviewer`.
- **A strong model (gpt-5.5/claude) is defect-converged**, so its `gate`-vs-
  `collaborate` delta is small *by design* — the noise-cutting effect is most
  visible on a weaker model. `gpt-4o` is **not** available on a ChatGPT-account
  codex auth (it needs an OpenAI API key); use `replay.py` over the gpt-4o
  corpus to see the dramatic cut.
- Each review is a full agent run (tokens + ~30–60s). On a memory-constrained
  machine, wrap the sweep in your own RSS watchdog — it spawns one agent
  subprocess at a time.
