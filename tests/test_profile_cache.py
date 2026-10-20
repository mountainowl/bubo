"""The project profile is computed once per project per day, not per review.

Each review runs in its own worker process, so the cache must outlive the
process. Drives the real ``collect_profile`` against a real git checkout and
counts the stack scans and SCM metadata reads it does.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bubo import analytics
from bubo import project_analytics as profiles
from bubo.analytics_config import AnalyticsConfig
from bubo.review_config import ReviewConfig


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles.paths, "DB", tmp_path / "state" / "reviewer.sqlite")
    monkeypatch.setattr(analytics, "_install_id", None)
    monkeypatch.setattr(analytics, "_install_path", None)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("import fastapi\n")
    (repo / "requirements.txt").write_text("fastapi\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    return repo


CFG = ReviewConfig(provider="github", analytics_config=AnalyticsConfig(enabled=True))
METADATA = {"metadata_status": "verified", "visibility": "public", "namespace_kind": "org"}


def _collect(repo: Path, days: list[str], metadata=METADATA, project="acme/app"):
    profiles_seen = []
    with (
        patch.object(profiles, "detect_stack", wraps=profiles.detect_stack) as scan,
        patch.object(profiles, "fetch_metadata", return_value=metadata) as meta,
        patch.object(profiles, "_utc_day", side_effect=days),
    ):
        for _ in days:
            profiles_seen.append(profiles.collect_profile(CFG, "tok", project, repo))
    return profiles_seen, scan.call_count, meta.call_count


def test_reviews_of_one_project_on_one_day_scan_once(tmp_path):
    seen, scans, reads = _collect(_repo(tmp_path), ["2026-10-20"] * 3)
    assert (scans, reads) == (1, 1)
    assert seen[0] == seen[1] == seen[2]
    assert ("framework", "fastapi") in seen[0]["technologies"]


def test_profile_is_refreshed_the_next_day(tmp_path):
    _, scans, reads = _collect(_repo(tmp_path), ["2026-10-20", "2026-10-20", "2026-10-21"])
    assert (scans, reads) == (2, 2)


def test_each_project_has_its_own_profile(tmp_path):
    repo = _repo(tmp_path)
    _collect(repo, ["2026-10-20"], project="acme/app")
    _, scans, _ = _collect(repo, ["2026-10-20"], project="acme/other")
    assert scans == 1


def test_unavailable_metadata_is_retried_next_review(tmp_path):
    down = {"metadata_status": "unavailable", "visibility": "unknown"}
    _, _, reads = _collect(_repo(tmp_path), ["2026-10-20"] * 2, metadata=down)
    assert reads == 2


def test_declared_profile_changes_apply_without_waiting_a_day(tmp_path):
    repo = _repo(tmp_path)
    _collect(repo, ["2026-10-20"])
    declared = ReviewConfig(
        provider="github",
        analytics_config=AnalyticsConfig(
            enabled=True, profiles={"github:acme/app": {"domain": "fintech"}}
        ),
    )
    with (
        patch.object(profiles, "fetch_metadata", return_value=METADATA),
        patch.object(profiles, "_utc_day", return_value="2026-10-20"),
    ):
        assert profiles.collect_profile(declared, "tok", "acme/app", repo)["domain"] == "fintech"
