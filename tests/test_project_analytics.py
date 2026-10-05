"""Profile metadata disclosure boundary and local classification regressions."""

import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from bubo import analytics
from bubo import project_analytics as profiles
from bubo.analytics_config import AnalyticsConfig, analytics_config_from_dict
from bubo.config_values import ConfigError
from bubo.review_config import ReviewConfig


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles.paths, "DB", tmp_path / "state" / "reviewer.sqlite")
    monkeypatch.setattr(analytics, "_install_id", None)
    monkeypatch.setattr(analytics, "_install_path", None)
    monkeypatch.setattr(analytics, "_pending_events", [])
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("BUBO_ANALYTICS", raising=False)


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("visibility", ["public", "private", "internal", "unknown"])
@pytest.mark.parametrize("license_id", ["MIT", "NOASSERTION", "BUSL-1.1", "", "proprietary"])
def test_repo_name_requires_both_public_and_known_oss(provider, visibility, license_id):
    data = {
        "name": "sensitive-project",
        "path": "sensitive-project",
        "visibility": visibility,
        "license": {"spdx_id": license_id, "key": license_id},
        "namespace": {"kind": "group", "full_path": "acme/private-subgroup"},
        "owner": {"type": "Organization", "login": "acme"},
    }
    if provider == "github" and visibility in {"public", "private"}:
        data["private"] = visibility == "private"
    result = profiles.allowed_metadata(provider, data)
    assert result["org"] == "acme"
    assert ("repo_name" in result) == (visibility == "public" and license_id == "MIT")
    assert "private-subgroup" not in json.dumps(result)


@pytest.mark.parametrize("provider", ["github", "gitlab"])
def test_personal_namespaces_are_not_organizations(provider):
    result = profiles.allowed_metadata(
        provider,
        {
            "owner": {"type": "User", "login": "personal-username"},
            "namespace": {"kind": "user", "full_path": "personal-username"},
            "private": True,
            "visibility": "private",
            "name": "secret",
            "path": "secret",
        },
    )
    assert "org" not in result
    assert "repo_name" not in result
    assert "personal-username" not in json.dumps(result)


def test_chokepoint_rejects_unverified_names():
    assert analytics._clean({"org": "acme", "repo_name": "secret"}) == {}
    attrs = {
        "org": "acme",
        "repo_name": "secret",
        "metadata_status": "verified",
        "visibility": "private",
        "license": "mit",
        "scm_provider": "github",
        "namespace_kind": "organization",
    }
    assert analytics._clean(attrs).get("org") == "acme"
    assert "repo_name" not in analytics._clean(attrs)
    attrs["visibility"] = "public"
    assert analytics._clean(attrs)["repo_name"] == "secret"
    attrs["namespace_kind"] = "user"
    assert "org" not in analytics._clean(attrs)


def test_project_identity_is_stable_and_scoped():
    args = ("install-a", "github", "https://api.github.com", "acme/private")
    first = profiles.project_id(*args)
    assert uuid.UUID(first).version == 4
    assert profiles.project_id(*args) == first
    assert profiles.project_id("install-b", *args[1:]) != first
    assert profiles.project_id(*args[:3], "acme/other") != first
    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(lambda _: profiles.project_id(*args), range(16)))
    assert {value for value in identities if value is not None} == {first}
    assert (
        b"acme/private" not in (profiles.paths.DB.parent / "analytics_projects.sqlite").read_bytes()
    )


def test_metadata_failure_redacts_everything(monkeypatch):
    opener = Mock()
    opener.open.side_effect = OSError("credentials and repository name")
    monkeypatch.setattr(profiles, "build_opener", lambda *args: opener)
    assert profiles.fetch_metadata(
        "github", "https://api.github.com", "secret", "acme/private"
    ) == {
        "metadata_status": "unavailable",
        "visibility": "unknown",
    }
    assert opener.open.call_args.kwargs["timeout"] == 2
    assert (
        profiles._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test")
        is None
    )


def test_opt_out_does_no_scanning_or_requests(tmp_path, monkeypatch):
    fetch = Mock(side_effect=AssertionError("must not run"))
    monkeypatch.setattr(profiles, "fetch_metadata", fetch)
    monkeypatch.setattr(profiles, "detect_stack", fetch)
    cfg = ReviewConfig(analytics_config=AnalyticsConfig(enabled=False))
    assert profiles.collect_profile(cfg, "token", "secret", tmp_path) == {}
    fetch.assert_not_called()
    assert not profiles.paths.DB.parent.exists()


def tracked_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"react": "1", "mysql2": "1"}})
    )
    (tmp_path / "requirements.txt").write_text("fastapi\noracledb\nclickhouse-connect\n")
    for name in ("a.py", "b.js", "c.java", "d.c", "e.r"):
        (tmp_path / name).write_text("private code not transmitted")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return tmp_path


def test_local_stack_detection_and_declared_domain(tmp_path, monkeypatch):
    repo = tracked_repo(tmp_path)
    technologies, coverage = profiles.detect_stack(repo)
    assert coverage == "bounded"
    assert {
        ("database", "oracle"),
        ("database", "clickhouse"),
        ("database", "mysql"),
        ("framework", "react"),
        ("framework", "fastapi"),
        ("language", "python"),
        ("language", "java"),
        ("language", "c"),
        ("language", "r"),
    } <= set(technologies)
    monkeypatch.setattr(profiles, "fetch_metadata", lambda *args: {"visibility": "private"})
    cfg = ReviewConfig(
        provider="github",
        analytics_config=AnalyticsConfig(
            profiles={"github:acme/private": {"domain": "fintech", "project_type": "backend_api"}}
        ),
    )
    profile = profiles.collect_profile(cfg, "secret-token", "acme/private", repo)
    assert profile["domain"] == "fintech"
    assert profile["project_type"] == "backend_api"
    assert "private" not in json.dumps({k: v for k, v in profile.items() if k != "visibility"})
    assert "secret-token" not in json.dumps(profile)
    assert (
        profiles.collect_profile(replace(cfg, analytics_config=AnalyticsConfig()), "t", "p", repo)[
            "domain"
        ]
        == "unknown"
    )


def test_profile_config_rejects_free_text():
    with pytest.raises(ConfigError):
        analytics_config_from_dict(
            {"analytics": {"profiles": {"github:acme/p": {"domain": "private-company-name"}}}}
        )
    assert (
        analytics_config_from_dict(
            {"analytics": {"profiles": {"gitlab:acme/p": {"domain": "travel"}}}}
        ).profiles["gitlab:acme/p"]["domain"]
        == "travel"
    )


def test_symlink_manifest_cannot_read_external_file(tmp_path, monkeypatch):
    outside = tmp_path / "secret.txt"
    outside.write_text("oracledb")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").symlink_to(outside)
    monkeypatch.setattr(
        profiles.subprocess, "run", lambda *args, **kwargs: Mock(stdout=b"requirements.txt\0")
    )
    technologies, _ = profiles.detect_stack(repo)
    assert ("database", "oracle") not in technologies


@pytest.mark.parametrize("key", ["domain", "project_type"])
@pytest.mark.parametrize("value", [[], {}, 42, None])
def test_profile_config_rejects_non_string_categories(key, value):
    with pytest.raises(ConfigError):
        analytics_config_from_dict({"analytics": {"profiles": {"github:acme/p": {key: value}}}})


@pytest.mark.parametrize("provider", ["github", "gitlab"])
@pytest.mark.parametrize("visibility", ["public", "private"])
def test_emitted_profile_disclosure(provider, visibility):
    metadata = profiles.allowed_metadata(
        provider,
        {
            "private": visibility != "public",
            "visibility": visibility,
            "owner": {"type": "Organization", "login": "acme"},
            "namespace": {"kind": "group", "full_path": "acme/secret-subgroup"},
            "name": "sensitive-repo",
            "path": "sensitive-repo",
            "license": {"key": "mit", "spdx_id": "MIT"},
        },
    )
    analytics.record_review_completed(
        AnalyticsConfig(),
        scm_provider=provider,
        agent="codex",
        model=None,
        status="success",
        dry_run=False,
        review_mode="default",
        tone=None,
        duration_seconds=1,
        tokens_input=None,
        tokens_output=None,
        tokens_cached=None,
        tokens_total=None,
        cost_usd=0,
        findings_posted=0,
        findings_planned=0,
        findings_skipped=0,
        files_changed=1,
        lines_changed=1,
        languages={"python": 1},
        profile={
            **metadata,
            "project_id": uuid.uuid4().hex,
            "domain": "fintech",
            "technologies": [("database", "oracle"), ("database", "secret-db")],
            "project": "acme/secret",
            "token": "secret-token",
        },
    )
    events = [event for _, _, event in analytics._pending_events]
    assert {e["event"] for e in events} == {
        "review_completed",
        "review_language",
        "project_profile",
        "project_technology",
    }
    for event in events:
        props = event["properties"]
        assert props["org"] == "acme"
        assert ("repo_name" in props) == (visibility == "public")
        assert props["distinct_id"] == props["install_id"] == analytics.install_id()
    raw = json.dumps(events)
    for secret in ("secret-subgroup", "secret-db", "acme/secret", "secret-token"):
        assert secret not in raw
    if visibility == "private":
        assert "sensitive-repo" not in raw
