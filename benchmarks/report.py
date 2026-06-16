"""Render benchmark results as a deterministic Markdown + JSON report. Stdlib only."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict

from score import CaseScore


def to_json(rows: Sequence[dict[str, object]], generated_at: str) -> str:
    """rows: [{"reviewer", "aggregate": {...}, "cases": [CaseScore, ...]}]."""
    out = {
        "schema_version": 1,
        "generated_at": generated_at,
        "reviewers": [
            {
                "reviewer": r["reviewer"],
                "aggregate": r["aggregate"],
                "cases": [asdict(c) for c in r["cases"]],  # type: ignore[union-attr]
            }
            for r in rows
        ],
    }
    return json.dumps(out, indent=2, sort_keys=False) + "\n"


def _num(v: object) -> str:
    return "—" if v is None else str(v)


def to_markdown(rows: Sequence[dict[str, object]], generated_at: str) -> str:
    """Headline comparison table (one row per reviewer) + a per-case appendix."""
    lines = [
        "# Bubo review benchmark",
        "",
        f"_generated {generated_at}_",
        "",
        "Apples-to-apples on the same corpus: quality (vs labeled ground truth) and",
        "efficiency. Higher precision/recall/F1 is better; lower FP-rate and fewer",
        "tokens/$ per true positive is better.",
        "",
        "| reviewer | cases | precision | recall | F1 | FP-rate | tokens | $ | tokens/TP |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        a = r["aggregate"]  # type: ignore[index]
        lines.append(
            f"| {r['reviewer']} | {a['cases']} | {a['precision']} | {a['recall']} | "
            f"{a['f1']} | {a['fp_rate']} | {a['tokens_total']} | {a['cost_usd']} | "
            f"{_num(a['tokens_per_true_positive'])} |"
        )
    lines += ["", "## Per case", ""]
    for r in rows:
        lines.append(f"### {r['reviewer']}")
        lines.append("")
        lines.append("| case | TP | FP | FN | precision | recall | tokens | $ |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for c in r["cases"]:  # type: ignore[union-attr]
            assert isinstance(c, CaseScore)
            lines.append(
                f"| {c.case_id} | {c.tp} | {c.fp} | {c.fn} | {c.precision} | "
                f"{c.recall} | {c.efficiency.tokens_total} | {c.efficiency.cost_usd} |"
            )
        lines.append("")
    return "\n".join(lines)
