"""Score captured reviewer outputs against the labeled corpus (replay mode).

    python benchmarks/run.py \
        --cases benchmarks/cases \
        --results benchmarks/results/sample \
        --out benchmarks/report

Replay mode (the default) scores pre-captured findings JSON — no LLM, no API key —
so the suite runs in CI and is exactly how you plug in *other* reviewers: capture
each tool's findings into the result shape (see benchmarks/README.md) and drop
them in the results dir. Capturing bubo's own findings for a live run is also in
the README. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from report import to_json, to_markdown
from score import Efficiency, Issue, aggregate, score_case


def _issues(raw: list[dict]) -> list[Issue]:
    return [
        Issue(
            file=str(i.get("file", "")),
            line=int(i.get("line", 0)),
            category=str(i.get("category", "")),
            severity=str(i.get("severity", "")),
            summary=str(i.get("summary", "")),
        )
        for i in raw
    ]


def _efficiency(raw: dict) -> Efficiency:
    return Efficiency(
        tokens_total=int(raw.get("tokens_total", 0)),
        seconds=float(raw.get("seconds", 0.0)),
        cost_usd=float(raw.get("cost_usd", 0.0)),
    )


def load_cases(cases_dir: Path) -> dict[str, list[Issue]]:
    cases: dict[str, list[Issue]] = {}
    for path in sorted(cases_dir.glob("*.json")):
        data = json.loads(path.read_text())
        cases[str(data["id"])] = _issues(data.get("ground_truth", []))
    return cases


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="score reviewer outputs against the corpus")
    ap.add_argument("--cases", type=Path, default=Path(__file__).parent / "cases")
    ap.add_argument("--results", type=Path, default=Path(__file__).parent / "results" / "sample")
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "report")
    ap.add_argument("--line-tolerance", type=int, default=2)
    ap.add_argument("--require-category", action="store_true")
    ap.add_argument("--generated-at", default=None, help="fixed timestamp for reproducible output")
    args = ap.parse_args(argv)

    ground_truth = load_cases(args.cases)
    if not ground_truth:
        print(f"no cases in {args.cases}", file=sys.stderr)
        return 2

    rows: list[dict[str, object]] = []
    for result_path in sorted(args.results.glob("*.json")):
        data = json.loads(result_path.read_text())
        reviewer = str(data.get("reviewer", result_path.stem))
        case_scores = []
        for case_id, truth in ground_truth.items():
            entry = data.get("cases", {}).get(case_id)
            if entry is None:
                continue  # this reviewer didn't run this case
            case_scores.append(
                score_case(
                    case_id,
                    reviewer,
                    _issues(entry.get("findings", [])),
                    truth,
                    line_tolerance=args.line_tolerance,
                    require_category=args.require_category,
                    efficiency=_efficiency(entry.get("efficiency", {})),
                )
            )
        if case_scores:
            rows.append(
                {"reviewer": reviewer, "aggregate": aggregate(case_scores), "cases": case_scores}
            )

    if not rows:
        print(f"no reviewer results in {args.results}", file=sys.stderr)
        return 2

    generated_at = args.generated_at or datetime.now(UTC).isoformat(timespec="seconds")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.with_suffix(".md").write_text(to_markdown(rows, generated_at))
    args.out.with_suffix(".json").write_text(to_json(rows, generated_at))
    for r in rows:
        a = r["aggregate"]
        print(
            f"{r['reviewer']:>10}  P={a['precision']}  R={a['recall']}  "
            f"F1={a['f1']}  FP={a['fp_rate']}  "
            f"tok={a['tokens_total']}  ${a['cost_usd']}"
        )
    print(f"\nwrote {args.out.with_suffix('.md')} and {args.out.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
