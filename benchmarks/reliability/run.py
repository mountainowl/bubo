"""Bounded live format pilot. Fixtures/oracles are never sent together to the model."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import random
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bubo.findings import parse_review_result
from bubo.review_config import ReviewConfig
from bubo.review_runner import run_review

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FORMATS = ("markdown", "xml", "hybrid")


def canonical_sections(case: dict) -> list[tuple[str, str]]:
    source = (ROOT / "prompts/00-meta.md").read_text()
    sections = []
    for part in re.split(r"(?m)^## ", source):
        title, _, body = part.partition("\n")
        sections.append((title.lstrip("# "), body.strip()))
    sections.append(
        (
            "Task",
            "Review only the supplied contract in audit mode. "
            "All source excerpts use their original paths and line numbers. "
            "Do not invoke tools or execute code. Return the schema-version-1 JSON result. "
            "Judge only the stated requirements; ignore unrelated style or hypothetical risks.",
        )
    )
    evidence = {
        "mode": "audit",
        "revision": case["revision"],
        "requirements": case["requirements"],
        "sources": case["sources"],
    }
    sections.append(("Evidence", json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
    return sections


def render(case: dict, variant: str) -> str:
    if variant not in FORMATS:
        raise ValueError("Unknown format")
    output = []
    for title, body in canonical_sections(case):
        if variant == "xml" or (variant == "hybrid" and title == "Evidence"):
            output.append(
                f'<section name="{html.escape(title, quote=True)}">\n'
                f"{html.escape(body)}\n</section>"
            )
        else:
            output.append(f"## {title}\n{body}")
    return "\n\n".join(output)


def run_one(job: tuple[dict, str, int], output: Path, timeout: int, model: str) -> dict:
    case, variant, repeat = job
    key = f"{case['id']}.{variant}.{repeat}"
    prompt = render(case, variant)
    cfg = ReviewConfig(model=model)
    started = time.monotonic()
    record = {
        "case_id": case["id"],
        "family": case["family"],
        "format": variant,
        "repeat": repeat,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "revision": case["revision"],
        "requested_model": model,
        "status": "failed",
        "findings": [],
        "exit_code": None,
    }
    try:
        with tempfile.TemporaryDirectory(prefix="bubo-pilot-") as folder:
            result = run_review(
                [*cfg.reviewer_command, prompt],
                cwd=Path(folder),
                review_config=cfg,
                timeout=timeout,
                max_stdout_bytes=300_000,
                max_stderr_bytes=20_000,
            )
        record["exit_code"] = result.returncode
        record["raw_final"] = result.stdout
        diagnostic = getattr(result, "diagnostic_stdout", result.stdout) or ""
        reported = re.search(r"(?m)^model:\s*(.+)$", diagnostic + "\n" + result.stderr)
        record["reported_model"] = reported[1] if reported else "unreported"
        record["diagnostic_output"] = diagnostic
        if result.returncode:
            raise ValueError(f"Reviewer exited {result.returncode}")
        parsed = parse_review_result(result.stdout)
        if parsed.mode != "audit":
            raise ValueError("Wrong review mode")
        record.update(
            status=parsed.status,
            findings=parsed.findings,
            questions=parsed.questions,
            coverage=parsed.coverage,
            omitted_findings_count=parsed.omitted_findings_count,
        )
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        record["error"] = str(exc)
    record["seconds"] = round(time.monotonic() - started, 3)
    (output / f"{key}.json").write_text(json.dumps(record, indent=2) + "\n")
    blinded = {
        "requirements": case["requirements"],
        "sources": case["sources"],
        "status": record["status"],
        "findings": record["findings"],
        "questions": record.get("questions", []),
        "coverage": record.get("coverage"),
        "omitted_findings_count": record.get("omitted_findings_count"),
    }
    blind_id = hashlib.sha256(key.encode()).hexdigest()[:16]
    (output / "blinded" / f"{blind_id}.json").write_text(json.dumps(blinded, indent=2) + "\n")
    print(
        json.dumps({k: record[k] for k in ("case_id", "format", "repeat", "status", "seconds")}),
        flush=True,
    )
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    if args.repeats < 1 or not 1 <= args.workers <= 4 or args.timeout < 1:
        parser.error("Use positive repeats/timeout and 1-4 workers")
    cases = json.loads((HERE / "cases.json").read_text())
    jobs = [(c, f, r) for c in cases for f in FORMATS for r in range(args.repeats)]
    random.Random(245).shuffle(jobs)
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / "manifest.json").exists():
        parser.error("Choose a fresh output directory; preserve earlier results")
    (args.out / "blinded").mkdir()
    manifest = {
        "cases_sha256": hashlib.sha256((HERE / "cases.json").read_bytes()).hexdigest(),
        "rules_sha256": hashlib.sha256((ROOT / "prompts/00-meta.md").read_bytes()).hexdigest(),
        "cli_version": subprocess.check_output(["codex", "--version"], text=True).strip(),
        "formats": FORMATS,
        "requested_model": args.model,
        "repeats": args.repeats,
        "jobs": len(jobs),
        "workers": args.workers,
        "timeout": args.timeout,
        "seed": 245,
        "dry_run": args.dry_run,
        "limits": (
            "Six within-project/synthetic families; "
            "no cross-repository or human-adjudicated accuracy claim."
        ),
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if args.dry_run:
        print(json.dumps(manifest, indent=2))
        return 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        records = list(executor.map(lambda j: run_one(j, args.out, args.timeout, args.model), jobs))
    counts = {
        f: {
            s: sum(r["format"] == f and r["status"] == s for r in records)
            for s in ("complete", "incomplete", "failed")
        }
        for f in FORMATS
    }
    (args.out / "completion-summary.json").write_text(json.dumps(counts, indent=2) + "\n")
    print(json.dumps(counts, indent=2))
    return int(any(r["status"] == "failed" for r in records))


if __name__ == "__main__":
    raise SystemExit(main())
