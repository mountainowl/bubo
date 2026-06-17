#!/usr/bin/env python3
"""Review fresh PR diffs LIVE through bubo's real contract, then apply each mode.

For each PR in the input list: fetch the diff (``gh pr diff``), wrap it in bubo's
rendered review contract, run the agent reviewer, then run the shipped
``filter_findings_by_policy`` in ``collaborate`` and ``gate`` and report what each
surfaces. The realtime companion to ``replay.py``.

Dry-run / read-only: it reviews and reports — it **posts nothing**. The reviewer
defaults to ``codex exec -m gpt-5.5`` (read-only sandbox) authed via your real
``~/.codex`` (no profile, never modified). A strong model is defect-converged, so
its gate-vs-collaborate delta is small by design — see README.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from bubo.findings import (
    DEFECT_CATEGORIES,
    extract_findings,
    filter_findings_by_policy,
    normalize_finding_categories,
    surface_predicate_for_mode,
)
from bubo.prompt import render_meta_prompt
from bubo.review_config import DEFAULT_MIN_CONFIDENCE

ROOT = Path(__file__).resolve().parents[2]


def slim(f: dict) -> dict:
    return {
        k: f.get(k)
        for k in (
            "type",
            "severity",
            "category",
            "category_canonical",
            "confidence",
            "title",
            "file",
        )
    }


def reviewer_argv(reviewer: str, model: str, prompt: str) -> list[str]:
    if reviewer == "codex":
        return [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "-m",
            model,
            "-s",
            "read-only",
            "--skip-git-repo-check",
            prompt,
        ]
    if reviewer == "claude":
        return ["claude", "-p", prompt]
    raise SystemExit(f"unknown --reviewer {reviewer!r} (expected codex|claude)")


def review_pr(pr: dict, meta: str, args: argparse.Namespace, out: Path) -> dict:
    diff = subprocess.run(
        ["gh", "pr", "diff", "-R", pr["repo"], str(pr["number"])],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    ).stdout[: args.max_diff_bytes]
    prompt = (
        f"{meta}\n\n---\nThe change to review is the unified diff below for PR "
        f'{pr["repo"]}#{pr["number"]} ("{pr["title"]}"). Review ONLY this diff; use only '
        f"what is visible here as evidence. Return only the JSON findings array per the "
        f"contract above.\n\n```diff\n{diff}\n```\n"
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "CODEX_HOME": os.path.expanduser("~/.codex"),
    }
    t0 = time.monotonic()
    proc = subprocess.run(
        reviewer_argv(args.reviewer, args.model, prompt),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        check=False,
        timeout=args.timeout,
    )
    secs = round(time.monotonic() - t0, 1)
    (out / f"{pr['id']}.raw.txt").write_text((proc.stdout or "")[-60000:])
    try:
        findings = normalize_finding_categories(
            extract_findings(proc.stdout or "", max_findings=args.max_findings)
        )
    except ValueError:
        findings = []
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
    res = {
        **pr,
        "secs": secs,
        "exit": proc.returncode,
        "n_raw": len(findings),
        "collab": [slim(f) for f in collab],
        "gate": [slim(f) for f in gate],
        "gate_all_defect": all(f.get("category_canonical") in DEFECT_CATEGORIES for f in gate),
    }
    (out / f"{pr['id']}.json").write_text(json.dumps(res, indent=2))
    return res


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prs", type=Path, default=ROOT / "benchmarks/precision_lever/out/prs.json"
    )
    parser.add_argument("--reviewer", default="codex", choices=["codex", "claude"])
    parser.add_argument("--model", default="gpt-5.5")
    parser.add_argument("--max-findings", type=int, default=8)
    parser.add_argument("--max-diff-bytes", type=int, default=45000)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--out", type=Path, default=ROOT / "benchmarks/precision_lever/out")
    args = parser.parse_args()

    if not args.prs.exists():
        print(f"no PR list at {args.prs} — run pick_prs.py first")
        return 1
    prs = json.loads(args.prs.read_text())
    args.out.mkdir(parents=True, exist_ok=True)
    meta = render_meta_prompt((ROOT / "prompts/00-meta.md").read_text(), args.max_findings)

    summary = []
    for i, pr in enumerate(prs, 1):
        print(f"[{i}/{len(prs)}] {pr['repo']}#{pr['number']} reviewing...", flush=True)
        try:
            r = review_pr(pr, meta, args, args.out)
            print(
                f"    raw={r['n_raw']} collab={len(r['collab'])} gate={len(r['gate'])} "
                f"({r['secs']}s, exit={r['exit']})",
                flush=True,
            )
            summary.append(r)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"    ERROR: {exc}", flush=True)
            summary.append({**pr, "error": str(exc)})

    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    raw = sum(s.get("n_raw", 0) for s in summary)
    collab = sum(len(s.get("collab", [])) for s in summary)
    gate = sum(len(s.get("gate", [])) for s in summary)
    print(f"\nDONE. PRs={len(summary)} raw={raw} collaborate={collab} gate={gate} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
