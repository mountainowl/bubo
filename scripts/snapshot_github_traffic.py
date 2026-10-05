#!/usr/bin/env python3
"""Maintainer-only GitHub acquisition snapshots; never identifies individual cloners."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="mountainowl/bubo")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # gh owns authentication; credentials are never copied into the snapshot.
    snapshot = {"repository": args.repo, "observed_at": dt.datetime.now(dt.UTC).isoformat()}
    try:
        for kind in ("clones", "views"):
            response = subprocess.run(
                ["gh", "api", f"repos/{args.repo}/traffic/{kind}"],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            data = json.loads(response.stdout)
            snapshot[kind] = {
                "count": data["count"],
                "uniques": data["uniques"],
                "days": data[kind],
            }
    except OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError:
        parser.exit(
            1,
            "Traffic snapshot failed; check gh authentication, repository access and network. "
            "No snapshot written.\n",
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("a", encoding="utf-8") as output:
        output.write(json.dumps(snapshot, sort_keys=True) + "\n")
    print(json.dumps({"saved": str(args.output), "observed_at": snapshot["observed_at"]}))


if __name__ == "__main__":
    main()
