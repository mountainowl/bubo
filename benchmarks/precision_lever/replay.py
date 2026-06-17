#!/usr/bin/env python3
"""Replay the shipped precision filter over an already-captured corpus.

Runs bubo's real pipeline — ``extract_findings`` → ``normalize_finding_categories``
→ ``filter_findings_by_policy`` — over captured reviewer output and reports what
``collaborate`` (today's default) vs ``gate`` (blocking defects only) each
surface, plus the free-form → canonical category collapse. Deterministic, free,
and instant: a regression check you can re-run whenever the taxonomy, presets, or
filter change.

Corpus formats accepted (per file):
  * ``<id>.raw.txt`` — raw reviewer stdout (re-extracted for full fidelity), or
  * ``<id>.json``    — ``{"findings": [...]}`` or a bare ``[...]`` array.

Dry-run / read-only: posts nothing. See benchmarks/precision_lever/README.md.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bubo.findings import (
    DEFECT_CATEGORIES,
    extract_findings,
    filter_findings_by_policy,
    normalize_finding_categories,
    surface_predicate_for_mode,
)
from bubo.review_config import DEFAULT_MIN_CONFIDENCE

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = ROOT / "research" / "tool-comparison" / "empirical" / "raw" / "bubo"


def load_findings(path: Path) -> list[dict]:
    """Parse one capture file into a findings list via the shipped parser."""
    text = path.read_text()
    if path.suffixes[-2:] == [".raw", ".txt"] or path.suffix == ".txt":
        try:
            return extract_findings(text)
        except ValueError:
            return []
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("findings", [])
    return [f for f in data if isinstance(f, dict)]


def repo_labels(corpus: Path) -> dict[str, str]:
    """Best-effort ``id -> owner/repo#pr`` from a sibling study inputs.json."""
    for candidate in (corpus.parent.parent / "inputs" / "inputs.json",):
        if candidate.exists():
            try:
                rows = json.loads(candidate.read_text())
            except OSError, ValueError:
                return {}
            return {
                r["id"]: f"{r.get('repo', r['id'])}#{r.get('number', r.get('pr', ''))}"
                for r in rows
                if isinstance(r, dict) and r.get("id")
            }
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS,
        help=f"dir of capture files (default: {DEFAULT_CORPUS})",
    )
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    args = parser.parse_args()

    corpus: Path = args.corpus
    files = sorted([*corpus.glob("*.raw.txt")]) if corpus.is_dir() else []
    if not files and corpus.is_dir():
        files = sorted(p for p in corpus.glob("*.json") if not p.name.startswith("_"))
    if not files:
        print(
            f"no corpus at {corpus} — skipping replay "
            "(research/ is untracked; pass --corpus to point at your own captures)"
        )
        return 0

    labels = repo_labels(corpus)
    totals = {"findings": 0, "collab": 0, "gate": 0}
    collapse: dict[str, set[str]] = {}
    gate_types: dict[str, int] = {}
    rows: list[tuple[str, str, int, int, int]] = []

    for path in files:
        rid = path.name.split(".")[0]
        findings = normalize_finding_categories(load_findings(path))
        for f in findings:
            collapse.setdefault(f["category_canonical"], set()).add(str(f.get("category")).lower())
        collab, _ = filter_findings_by_policy(
            findings,
            min_confidence=args.min_confidence,
            surface_predicate=surface_predicate_for_mode("collaborate"),
        )
        gate, _ = filter_findings_by_policy(
            findings,
            min_confidence=args.min_confidence,
            surface_predicate=surface_predicate_for_mode("gate"),
        )
        for f in gate:
            t = str(f.get("type") or "issue").lower()
            gate_types[t] = gate_types.get(t, 0) + 1
        totals["findings"] += len(findings)
        totals["collab"] += len(collab)
        totals["gate"] += len(gate)
        rows.append((rid, labels.get(rid, rid), len(findings), len(collab), len(gate)))

    print("=" * 78)
    print(f"PRECISION-LEVER REPLAY — {len(files)} captures, min_confidence={args.min_confidence}")
    print("=" * 78)
    print(f"{'id':<7}{'repo#pr':<34}{'findings':>9}{'collab':>8}{'gate':>6}")
    print("-" * 78)
    for rid, repo, nf, nc, ng in rows:
        print(f"{rid:<7}{repo[:33]:<34}{nf:>9}{nc:>8}{ng:>6}")
    print("-" * 78)
    print(f"{'TOTAL':<41}{totals['findings']:>9}{totals['collab']:>8}{totals['gate']:>6}")
    print()
    print(f"collaborate (default) surfaces : {totals['collab']}/{totals['findings']}")
    print(
        f"gate surfaces                  : {totals['gate']}/{totals['findings']}  "
        f"(all type={sorted(gate_types)}, all DEFECT categories)"
    )
    print()
    print("category normalization (free-form → canonical):")
    for canon in sorted(collapse):
        tag = "DEFECT" if canon in DEFECT_CATEGORIES else "non-defect"
        print(f"  {canon:<14}[{tag:<10}] <- {', '.join(sorted(collapse[canon]))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
