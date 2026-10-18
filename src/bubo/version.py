"""The installed version of this process, never the version staged by an updater."""

from __future__ import annotations

import json
import re
import subprocess
from importlib.metadata import PackageNotFoundError, distribution, version
from urllib.parse import urlparse
from urllib.request import url2pathname


def _source_qualifier() -> str | None:
    try:
        direct = json.loads(distribution("bubo").read_text("direct_url.json") or "{}")
        if not direct.get("dir_info", {}).get("editable"):
            return None
        location = urlparse(direct.get("url", ""))
        if location.scheme != "file" or location.netloc not in {"", "localhost"}:
            return "source-unverified"
        command = ["git", "-C", url2pathname(location.path)]
        revision = subprocess.run(
            [*command, "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        sha = revision.stdout.strip()
        if revision.returncode or not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            return "source-unverified"
        dirty = subprocess.run(
            [*command, "status", "--porcelain"],
            capture_output=True,
            timeout=1,
            check=False,
        )
        if dirty.returncode:
            return "source-unverified"
        return f"g{sha}" + (".dirty" if dirty.stdout else "")
    except OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired:
        return "source-unverified"
    except PackageNotFoundError:
        return None


def _resolve() -> str | None:
    try:
        value = version("bubo")
    except PackageNotFoundError:
        return None
    # Metadata is not an arbitrary telemetry string or an executable argument.
    if not re.fullmatch(r"[0-9][A-Za-z0-9.!+_-]{0,63}", value):
        return None
    qualifier = _source_qualifier()
    if qualifier:
        value += ("." if "+" in value else "+") + qualifier
    return value if len(value) <= 64 else None


INSTALLED_VERSION = _resolve()


def installed_version() -> str | None:
    return INSTALLED_VERSION
