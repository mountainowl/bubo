#!/usr/bin/env python3
"""Pick fresh, code-touching merged PRs across active OSS repos for a live sweep.

Excludes dependency bumps / docs / release PRs and prefers moderate diffs that
actually touch source, so the reviewer has something to find. Writes a JSON list
``[{id, repo, number, title, additions, deletions, files, mergedAt}]`` consumed
by ``live_sweep.py``. Read-only: only lists PRs via ``gh``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPOS = [
    "fastapi/fastapi",
    "pallets/flask",
    "pydantic/pydantic",
    "expressjs/express",
    "gin-gonic/gin",
    "tokio-rs/tokio",
    "fmtlib/fmt",
    "sharkdp/bat",
    "stretchr/testify",
    "psf/requests",
    "sqlalchemy/sqlalchemy",
    "encode/httpx",
]
CODE_EXT = (".py", ".go", ".rs", ".ts", ".js", ".cpp", ".cc", ".h", ".hpp", ".java", ".rb")
SKIP_TITLE = ("bump", "⬆", "release", "changelog", "merge ", "revert", "translation", "docs:")


def recent_prs(repo: str, limit: int) -> list[dict]:
    out = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "merged",
            "-L",
            str(limit),
            "--json",
            "number,title,additions,deletions,changedFiles,files,mergedAt",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(out.stdout) if out.stdout.strip() else []


def is_code_pr(pr: dict) -> bool:
    title = (pr.get("title") or "").lower()
    if any(s in title for s in SKIP_TITLE):
        return False
    if not (2 <= pr["changedFiles"] <= 8 and 30 <= pr["additions"] <= 350):
        return False
    paths = [f.get("path", "") for f in (pr.get("files") or [])]
    return any(p.endswith(CODE_EXT) for p in paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repos", nargs="*", default=DEFAULT_REPOS)
    parser.add_argument("--count", type=int, default=11)
    parser.add_argument("--scan", type=int, default=25, help="PRs to scan per repo")
    parser.add_argument(
        "--out", type=Path, default=ROOT / "benchmarks/precision_lever/out/prs.json"
    )
    args = parser.parse_args()

    picked: list[dict] = []
    for repo in args.repos:
        if len(picked) >= args.count:
            break
        for pr in recent_prs(repo, args.scan):
            if is_code_pr(pr):
                picked.append(
                    {
                        "id": f"{repo.split('/')[1]}-{pr['number']}",
                        "repo": repo,
                        "number": pr["number"],
                        "title": pr["title"],
                        "additions": pr["additions"],
                        "deletions": pr["deletions"],
                        "files": pr["changedFiles"],
                        "mergedAt": pr["mergedAt"],
                    }
                )
                break  # one per repo

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(picked, indent=2))
    print(f"picked {len(picked)} PRs -> {args.out}")
    for p in picked:
        print(
            f"  {p['repo']}#{p['number']}  +{p['additions']}/-{p['deletions']} "
            f"{p['files']}f  {p['mergedAt'][:10]}  {p['title'][:50]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
