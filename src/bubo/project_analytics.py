"""Local project classification and fail-closed SCM metadata for product analytics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bubo import paths

DOMAINS = frozenset(
    {
        "unknown",
        "travel",
        "fintech",
        "healthcare",
        "education",
        "retail",
        "gaming",
        "media",
        "logistics",
        "government",
        "developer_tools",
        "other",
    }
)
PROJECT_TYPES = frozenset(
    {
        "unknown",
        "backend_api",
        "frontend",
        "full_stack",
        "mobile",
        "library",
        "cli",
        "data_pipeline",
        "infrastructure",
        "other",
    }
)
# Conservative subset: unidentified, proprietary and source-available licenses are excluded.
OSS_LICENSES = frozenset(
    {
        "mit",
        "apache-2.0",
        "bsd-2-clause",
        "bsd-3-clause",
        "isc",
        "mpl-2.0",
        "epl-1.0",
        "epl-2.0",
        "artistic-2.0",
        "zlib",
        "unlicense",
        "gpl-2.0",
        "gpl-2.0-only",
        "gpl-2.0-or-later",
        "gpl-3.0",
        "gpl-3.0-only",
        "gpl-3.0-or-later",
        "lgpl-2.1",
        "lgpl-2.1-only",
        "lgpl-2.1-or-later",
        "lgpl-3.0",
        "lgpl-3.0-only",
        "lgpl-3.0-or-later",
        "agpl-3.0",
        "agpl-3.0-only",
        "agpl-3.0-or-later",
    }
)
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
_DEPENDENCIES = {
    "django": ("framework", "django"),
    "flask": ("framework", "flask"),
    "fastapi": ("framework", "fastapi"),
    "react": ("framework", "react"),
    "next": ("framework", "nextjs"),
    "express": ("framework", "express"),
    "vue": ("framework", "vue"),
    "svelte": ("framework", "svelte"),
    "spring-boot": ("framework", "spring"),
    "rails": ("framework", "rails"),
    "psycopg": ("database", "postgresql"),
    "psycopg2": ("database", "postgresql"),
    "pg": ("database", "postgresql"),
    "postgresql": ("database", "postgresql"),
    "mysqlclient": ("database", "mysql"),
    "pymysql": ("database", "mysql"),
    "mysql2": ("database", "mysql"),
    "mysql-connector": ("database", "mysql"),
    "oracledb": ("database", "oracle"),
    "cx-oracle": ("database", "oracle"),
    "ojdbc": ("database", "oracle"),
    "clickhouse-connect": ("database", "clickhouse"),
    "clickhouse-driver": ("database", "clickhouse"),
    "clickhouse": ("database", "clickhouse"),
    "pymongo": ("database", "mongodb"),
    "mongodb": ("database", "mongodb"),
    "redis": ("database", "redis"),
    "sqlite": ("database", "sqlite"),
    "better-sqlite3": ("database", "sqlite"),
    "duckdb": ("database", "duckdb"),
    "mssql": ("database", "sqlserver"),
    "tedious": ("database", "sqlserver"),
}
_MANIFESTS = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "Pipfile",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Gemfile",
        "go.mod",
        "Cargo.toml",
        "composer.json",
        "pubspec.yaml",
    }
)


def project_id(installation: str, provider: str, host: str, project: str) -> str | None:
    """Map a local project key to a random ID. Raw names never enter the payload."""
    key = hashlib.sha256(json.dumps([installation, provider, host, project]).encode()).hexdigest()
    path = paths.DB.parent / "analytics_projects.sqlite"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with sqlite3.connect(path, timeout=0.2) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS projects (key TEXT PRIMARY KEY, id TEXT NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO projects VALUES (?, ?)", (key, uuid.uuid4().hex))
            row = db.execute("SELECT id FROM projects WHERE key = ?", (key,)).fetchone()
            return str(row[0]) if row else None
    except OSError, sqlite3.Error:
        return None


def allowed_metadata(provider: str, data: dict[str, Any]) -> dict[str, str]:
    """Only verified org namespaces; repository names require public AND OSS."""
    result = {"metadata_status": "verified", "visibility": "unknown", "namespace_kind": "unknown"}
    public = False
    license_data = data.get("license")
    license_data = license_data if isinstance(license_data, dict) else {}
    if provider == "github":
        owner = data.get("owner") or {}
        result["namespace_kind"] = (
            "organization"
            if owner.get("type") == "Organization"
            else "user"
            if owner.get("type") == "User"
            else "unknown"
        )
        org = owner.get("login") if owner.get("type") == "Organization" else None
        public = data.get("private") is False and data.get("visibility", "public") == "public"
        result["visibility"] = (
            "public" if public else "private" if data.get("private") is True else "unknown"
        )
        license_id = license_data.get("spdx_id", "")
    elif provider == "gitlab":
        namespace = data.get("namespace") or {}
        result["namespace_kind"] = (
            "group"
            if namespace.get("kind") == "group"
            else "user"
            if namespace.get("kind") == "user"
            else "unknown"
        )
        org = (
            namespace.get("full_path", "").split("/")[0]
            if namespace.get("kind") == "group"
            else None
        )
        public = data.get("visibility") == "public"
        result["visibility"] = (
            str(data.get("visibility"))
            if data.get("visibility") in {"public", "private", "internal"}
            else "unknown"
        )
        license_id = license_data.get("key", "")
    else:
        return {"metadata_status": "unavailable", "visibility": "unknown"}
    if isinstance(org, str) and _SLUG.fullmatch(org):
        result["org"] = org
    name = data.get("name") if provider == "github" else data.get("path")
    if (
        public
        and str(license_id).lower() in OSS_LICENSES
        and isinstance(name, str)
        and _SLUG.fullmatch(name)
    ):
        result["repo_name"] = name
        result["license"] = str(license_id).lower()
    return result


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None  # Never forward credentials or trust metadata from another origin.


def fetch_metadata(provider: str, base: str, token: str, project: str) -> dict[str, str]:
    """One bounded SCM read; errors never authorize transmitting a repository name."""
    try:
        if urlsplit(base).scheme != "https":
            return {"metadata_status": "unavailable", "visibility": "unknown"}
        if provider == "github":
            url = base.rstrip("/") + "/repos/" + quote(project, safe="/")
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        elif provider == "gitlab":
            url = base.rstrip("/") + "/api/v4/projects/" + quote(project, safe="") + "?license=true"
            headers = {"PRIVATE-TOKEN": token}
        else:
            return {"metadata_status": "unavailable", "visibility": "unknown"}
        with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=2) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("Metadata too large")
        data = json.loads(raw)
        return allowed_metadata(provider, data)
    except Exception:
        return {"metadata_status": "unavailable", "visibility": "unknown"}


def detect_stack(repo: Path) -> tuple[list[tuple[str, str]], str]:
    """Inspect bounded tracked paths/manifests locally, never execute project code."""
    from bubo.analytics import language_counts

    found: set[tuple[str, str]] = set()
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-rz", "--name-only", "HEAD"],
            cwd=repo,
            capture_output=True,
            check=True,
            timeout=2,
        )
        names = result.stdout.decode("utf-8", errors="replace").split("\0")
        bounded = len(result.stdout) <= 2_000_000 and len(names) <= 10001
        names = names[:10000]
        found.update(("language", label) for label in language_counts(names) if label != "other")
        manifest_count = 0
        for name in names:
            path = repo / name
            if path.name not in _MANIFESTS and not (
                path.name.startswith("requirements") and path.suffix == ".txt"
            ):
                continue
            manifest_count += 1
            if manifest_count > 100:
                bounded = False
                break
            # Symlinks may target secrets or files outside this checkout.
            if path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
                continue
            with path.open("rb") as source:
                raw = source.read(262145)
            if len(raw) > 262144:
                bounded = False
                continue
            content = raw.decode("utf-8", errors="replace").lower().replace("_", "-")
            for dependency, tag in _DEPENDENCIES.items():
                if re.search(r"(?<![a-z0-9-])" + re.escape(dependency) + r"(?![a-z0-9])", content):
                    found.add(tag)
        return sorted(found), "bounded" if bounded else "partial"
    except Exception:
        return sorted(found), "unavailable"


def collect_profile(cfg: Any, token: str, project: str, repo: Path) -> dict[str, Any]:
    """Return only classified labels and deliberately approved SCM metadata."""
    from bubo import analytics

    if not analytics.analytics_enabled(cfg.analytics_config):
        return {}
    try:
        installation = analytics.install_id()
        if installation is None:
            return {}
        base = cfg.github_api_url if cfg.provider == "github" else cfg.gitlab_url
        identifier = project_id(installation, cfg.provider, base, project)
        if identifier is None:
            return {}
        configured = cfg.analytics_config.profiles.get(f"{cfg.provider}:{project}", {})
        domain = configured.get("domain", "unknown")
        project_type = configured.get("project_type", "unknown")
        technologies, coverage = detect_stack(repo)
        return {
            "project_id": identifier,
            "domain": domain if domain in DOMAINS else "unknown",
            "project_type": project_type if project_type in PROJECT_TYPES else "unknown",
            "profile_source": "declared" if configured else "unknown",
            "stack_source": "tracked_paths_and_manifests",
            "stack_coverage": coverage,
            "technologies": technologies,
            **fetch_metadata(cfg.provider, base, token, project),
        }
    except Exception:
        return {}
