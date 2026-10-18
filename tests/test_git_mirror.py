"""Per-project bare mirror + per-review worktree checkout (#254).

Everything here runs real ``git`` against a local bare repository. The
provider's ``https://`` clone URL is redirected to that repository with a
test-only ``url.<file>.insteadOf`` rule in an isolated global git config, so
the full ``GitHubProvider.checkout`` -> ``git_checkout_change`` ->
``cleanup_worktree`` path runs unmodified.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from bubo import paths, poller
from bubo.review_config import ReviewConfig
from bubo.scm import base
from bubo.scm.github import GitHubProvider

PROJECT = "o/r"
CLONE_URL = "https://github.com/o/r.git"


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    bare = tmp_path / "origin.git"
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = T\n\temail = t@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        f'[url "file://{bare}"]\n\tinsteadOf = {CLONE_URL}\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-SECRET")
    src = tmp_path / "src"
    src.mkdir()
    git("init", "-q", "-b", "main", str(src))
    (src / "a.py").write_text("".join(f"line{i}\n" for i in range(1, 200)))
    git("add", ".", cwd=src)
    git("commit", "-q", "-m", "base", cwd=src)
    git("clone", "-q", "--bare", str(src), str(bare))
    git("checkout", "-q", "-b", "feature", cwd=src)
    return SimpleNamespace(src=src, bare=bare)


def push_commit(
    origin: SimpleNamespace, text: str, *, number: int = 1, force_from: str = ""
) -> str:
    """Commit on the feature branch and publish it as the PR head ref."""
    if force_from:
        git("reset", "-q", "--hard", force_from, cwd=origin.src)
    (origin.src / "b.py").write_text(text)
    git("add", ".", cwd=origin.src)
    git("commit", "-q", "-m", text[:20], cwd=origin.src)
    sha = git("rev-parse", "HEAD", cwd=origin.src)
    git("push", "-q", "--force", str(origin.bare), f"HEAD:refs/pull/{number}/head", cwd=origin.src)
    return sha


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = base.run_bounded

    def record(args, **kwargs):
        calls.append(list(args))
        return real(args, **kwargs)

    monkeypatch.setattr(base, "run_bounded", record)
    return calls


def subcommands(calls: list[list[str]]) -> list[str]:
    """Reduce git argv to its subcommand (``worktree add`` keeps its verb)."""
    out = []
    for argv in calls:
        rest = argv[1:]
        while rest and rest[0] in {"-c", "-C", "--git-dir"}:
            rest = rest[2:]
        if rest[:1] == ["worktree"]:
            out.append(" ".join(rest[:2]))
        else:
            out.append(rest[0])
    return out


def checkout(sha: str, number: int = 1) -> Path:
    dest = paths.WORK / poller.slug(PROJECT) / str(number) / sha[:12]
    change = {"number": number, "head": {"sha": sha}, "base": {"ref": "main"}}
    GitHubProvider().checkout(ReviewConfig(provider="github"), PROJECT, change, dest)
    return dest


def mirror_dir() -> Path:
    mirrors = list((paths.WORK / "_mirrors").glob("*.git"))
    assert len(mirrors) == 1, mirrors
    return mirrors[0]


def test_first_review_creates_mirror_and_worktree_at_head(origin, spy) -> None:
    sha = push_commit(origin, "one\n")
    dest = checkout(sha)

    assert git("rev-parse", "HEAD", cwd=dest) == sha
    # The agent can diff against the target branch, as with a full clone.
    assert "b.py" in git("diff", "--stat", "origin/main...HEAD", cwd=dest)
    assert "b.py" in git("diff", "--stat", "main...HEAD", cwd=dest)
    mirror = mirror_dir()
    assert git("rev-parse", "--is-bare-repository", cwd=mirror) == "true"
    assert subcommands(spy) == ["clone", "fetch", "worktree add"]
    # No credential is ever persisted into the mirror's config.
    config = (mirror / "config").read_text()
    assert "SECRET" not in config
    assert "extraheader" not in config.lower()
    assert "Authorization" not in config
    # Remote calls carry the per-invocation header; local ones do not.
    for argv in spy:
        has_auth = any(p.startswith("http.extraHeader=Authorization: Basic ") for p in argv)
        assert has_auth == (subcommands([argv])[0] in {"clone", "fetch"})


def test_second_commit_runs_only_fetch_and_worktree_add(origin, spy) -> None:
    first = push_commit(origin, "one\n")
    poller.cleanup_worktree(checkout(first))
    spy.clear()

    second = push_commit(origin, "two\n")
    dest = checkout(second)

    assert subcommands(spy) == ["fetch", "worktree add"]
    assert not any("clone" in argv for argv in spy)
    assert git("rev-parse", "HEAD", cwd=dest) == second
    # The previously reviewed commit is still available locally.
    assert git("cat-file", "-t", first, cwd=dest) == "commit"


def test_fetch_is_limited_to_target_branch_and_change_ref(origin, spy) -> None:
    sha = push_commit(origin, "one\n")
    checkout(sha)

    fetch = next(argv for argv in spy if "fetch" in argv)
    assert "--prune" not in fetch
    refspecs = [p for p in fetch if ":" in p and not p.startswith("http.")]
    assert sorted(refspecs) == sorted(
        [
            "+refs/heads/main:refs/heads/main",
            "+refs/heads/main:refs/remotes/origin/main",
            "+refs/pull/1/head:refs/remotes/origin/pr-1",
        ]
    )


def test_force_pushed_change_ref_still_checks_out(origin, spy) -> None:
    first = push_commit(origin, "one\n")
    poller.cleanup_worktree(checkout(first))
    base_sha = git("rev-parse", "main", cwd=origin.src)

    rewritten = push_commit(origin, "rewritten\n", force_from=base_sha)
    dest = checkout(rewritten)

    assert git("rev-parse", "HEAD", cwd=dest) == rewritten


def test_cleanup_removes_worktree_and_keeps_mirror(origin, spy) -> None:
    sha = push_commit(origin, "one\n")
    dest = checkout(sha)
    mirror = mirror_dir()

    poller.cleanup_worktree(dest)

    assert not dest.exists()
    assert mirror.is_dir()
    listing = git("worktree", "list", "--porcelain", cwd=mirror)
    assert str(dest) not in listing
    assert not list((mirror / "worktrees").glob("*")) if (mirror / "worktrees").exists() else True


def test_corrupt_mirror_is_deleted_and_review_falls_back_to_full_clone(origin, spy) -> None:
    first = push_commit(origin, "one\n")
    poller.cleanup_worktree(checkout(first))
    mirror = mirror_dir()
    (mirror / "HEAD").write_text("garbage\n")
    for pack in (mirror / "objects").rglob("*"):
        if pack.is_file():
            pack.chmod(0o600)
            pack.write_bytes(b"corrupt")
    spy.clear()

    second = push_commit(origin, "two\n")
    dest = checkout(second)

    assert git("rev-parse", "HEAD", cwd=dest) == second
    assert (dest / ".git").is_dir()  # a real clone, not a worktree
    assert "clone" in subcommands(spy)
    # The redundant all-branch `fetch --prune` after a fresh clone is gone.
    assert not any("--prune" in argv for argv in spy)
    assert not mirror.exists() or git("rev-parse", "--is-bare-repository", cwd=mirror) == "true"
    poller.cleanup_worktree(dest)
    assert not dest.exists()


def test_concurrent_reviews_share_one_mirror(origin, spy) -> None:
    first = push_commit(origin, "one\n", number=1)
    git("checkout", "-q", "-b", "other", "main", cwd=origin.src)
    second = push_commit(origin, "two\n", number=2)
    results: dict[int, str] = {}
    errors: list[BaseException] = []

    def run(number: int, sha: str) -> None:
        try:
            results[number] = git("rev-parse", "HEAD", cwd=checkout(sha, number))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=a) for a in ((1, first), (2, second))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert results == {1: first, 2: second}
    assert len(list((paths.WORK / "_mirrors").glob("*.git"))) == 1
    assert sum(1 for argv in spy if "clone" in argv) == 1


def test_mirrors_for_unconfigured_projects_are_pruned(origin, spy) -> None:
    sha = push_commit(origin, "one\n")
    poller.cleanup_worktree(checkout(sha))
    mirror = mirror_dir()

    assert base.prune_mirrors([PROJECT]) == []
    assert mirror.is_dir()
    assert base.prune_mirrors(["someone/else"]) == [mirror]
    assert not mirror.exists()
