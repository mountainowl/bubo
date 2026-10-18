"""The ``ScmProvider`` protocol and shared provider helpers.

A provider encapsulates every operation that differs between GitLab and
GitHub: listing changes, checking one out, fetching diffs, mapping a
finding to an inline-comment anchor, posting the comment, and classifying
its outcome. The poller calls only these methods, so it never branches on
the provider name.

The terminology is normalized to "change" to cover both GitLab merge
requests and GitHub pull requests.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from bubo.errors import describe
from bubo.events import log
from bubo.hash_utils import stable_hash
from bubo.review_config import ReviewConfig
from bubo.secrets import redact_secrets
from bubo.subproc import run_bounded
from bubo.types import JsonObject
from bubo.update_locking import lock_file


class FindingThreadState(StrEnum):
    """Provider-normalized state for one persisted Bubo finding thread."""

    OPEN = "open"
    RESOLVED = "resolved"
    DELETED = "deleted"
    FOREIGN = "foreign"


@dataclass(frozen=True, slots=True)
class FindingThread:
    """Current provider view used to guard reconciliation writes.

    ``reply_marker_present`` lets a retry skip a duplicate Bubo verification
    reply when the first reply request succeeded but local persistence failed.
    """

    state: FindingThreadState
    reply_marker_present: bool = False


@dataclass(frozen=True, slots=True)
class FindingThreadReply:
    """Result of an idempotent reconciliation reply operation."""

    reply_id: str | None
    marker_confirmed: bool
    applied_by_bubo: bool
    state: FindingThreadState


@dataclass(frozen=True, slots=True)
class FindingThreadResolution:
    """Result of an idempotent resolution operation."""

    state: FindingThreadState
    applied_by_bubo: bool

# The finding-output contract shared by every provider's review prompt. Only
# the change-specific header differs per provider; the JSON shape the agent
# must return is identical. ``{max_findings}`` is filled in per review.
REVIEW_CONTRACT = """Use the `code-reviewer` skill through Superpowers for the review contract.
Review the diff only. Do not post comments yourself.
Return a JSON array only. Do not wrap it in markdown.
Return at most {max_findings} findings.
Each finding object must have:
- type
- severity
- category
- title
- file
- line
- impact
- evidence
- fix
- confidence
type must be one of: issue, suggestion, question.
severity must be one of: blocking, non-blocking.
confidence must be a number from 0 to 1.
Use line for the changed new-line where the inline comment should be placed.
Return [] when there are no actionable findings."""

# Per-tone voice directives appended to the review contract when the operator
# selects a non-default ``[review].tone``. Each asks the reviewer for ONE extra
# ``comment`` field — the finding rewritten in that voice — which bubo posts in
# place of the structured render. Two invariants every block enforces:
#   * the terse house style still governs title/impact/evidence/fix (those feed
#     the audit dataset + the mood-neutral dedup fingerprint), so only the
#     ``comment`` field changes voice;
#   * the example teaches register only (cross-domain, "do not reuse"), which is
#     the most reliable lever for tone and resists topic/phrasing overfit.
_VOICE_HEADER = """In addition to the structured fields, add a "comment" field to each finding:
the same finding rewritten as one short inline comment in the voice below. The
terse house style still governs title/impact/evidence/fix — the voice applies to
"comment" ONLY. Do not put a confidence number in "comment". The example shows
voice only; do not reuse its wording or topic."""

COMMENT_VOICES: dict[str, str] = {
    "collaborative": _VOICE_HEADER
    + """

Voice: a thoughtful senior engineer leaving an inline note. Acknowledge intent
when natural, show the bug with one concrete example, phrase the fix as a
suggestion. Hedges ("I think") and contractions are fine.
Example (style only):
  fix:     "Guard the divide; return 0 when count is 0."
  comment: "Heads up — if count comes in as 0 this throws, since we divide
           total/count with no guard. Probably worth returning 0 (or skipping)
           when the list is empty."
""",
    "socratic": _VOICE_HEADER
    + """

Voice: lead with a question that surfaces the gap, point at the mechanism, and
invite the author to confirm rather than asserting a verdict.
Example (style only):
  fix:     "Guard the divide; return 0 when count is 0."
  comment: "What happens here when count is 0? We divide total/count with no
           guard, so this looks like it'll throw on an empty list — should we
           return 0 (or skip) in that case?"
""",
    "formal": _VOICE_HEADER
    + """

Voice: measured and professional, complete sentences, no contractions or slang.
State the condition, the consequence, and the recommendation.
Example (style only):
  fix:     "Guard the divide; return 0 when count is 0."
  comment: "When count is 0, this computes total/count without a guard and will
           raise a ZeroDivisionError. Recommend returning 0, or skipping, for
           the empty case."
""",
    "casual": _VOICE_HEADER
    + """

Voice: relaxed, friendly, and brief. Contractions and light informality are
fine; stay precise about the actual bug.
Example (style only):
  fix:     "Guard the divide; return 0 when count is 0."
  comment: "Quick one — count being 0 will blow up here since we divide
           total/count with no guard. Just return 0 (or bail) when it's empty."
""",
}


def comment_voice_directive(tone: str) -> str:
    """Return the voice directive for ``tone``, or ``""`` for the default.

    ``terse`` (and any unrecognized value) returns the empty string, so the
    review prompt — and therefore the whole review — is byte-identical to its
    pre-tone behavior. Config validation (``review.tone`` via ``one_of``) keeps
    unrecognized values from reaching here in practice.
    """
    return COMMENT_VOICES.get(tone, "")


def build_review_contract(cfg: ReviewConfig) -> str:
    """Build the finding-output contract for a review.

    Fills the per-review ``max_findings`` cap and, when a non-default
    ``[review].tone`` is set, appends that tone's voice directive. Shared by
    both providers so their prompts cannot drift on the contract.
    """
    contract = REVIEW_CONTRACT.format(max_findings=cfg.max_findings_per_merge_request)
    voice = comment_voice_directive(cfg.tone)
    return f"{contract}\n\n{voice}" if voice else contract


def _git_auth_args(token: str, username: str) -> list[str]:
    """Per-invocation HTTP Basic auth, passed as a ``-c http.extraHeader`` so the
    token is sent on the wire but **never written to ``.git/config``** — the review
    agent later runs with read access to the worktree, so a persisted credential
    would be exfiltratable. Basic auth works for both GitLab (``oauth2:<token>``)
    and GitHub (``x-access-token:<token>``) git-over-HTTPS.
    """
    cred = base64.b64encode(f"{username}:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {cred}"]


def _run_git(args: list[str], *, cwd: Path | None, what: str) -> None:
    result = run_bounded(args, cwd=cwd, timeout=900)
    if result.returncode:
        raise RuntimeError(
            describe(
                what,
                reason=redact_secrets(result.stdout[-3000:]),
                fix=(
                    "verify the repo URL, the token's scope/permissions, and "
                    "network access to the host."
                ),
            )
        )


def path_slug(value: str) -> str:
    """Make ``value`` safe for use as a path or filename component."""
    return "".join(c if c.isalnum() else "-" for c in value).strip("-").lower()


# Bare per-project mirrors live beside the per-review worktrees. A project slug
# never starts with ``_`` (see :func:`path_slug`), so this cannot collide with
# a project's worktree directory.
MIRRORS_DIR = "_mirrors"
# Waiting for another worker's fetch on the same mirror is bounded by the same
# budget as one remote git call; past it the review falls back to a clone.
MIRROR_LOCK_TIMEOUT_SECONDS = 900
_MIRROR_NAME = re.compile(r"^(?P<slug>.+)-[0-9a-f]{8}\.git$")


class MirrorUnavailable(RuntimeError):
    """The mirror could not be locked; fall back without discarding it."""


def mirror_root() -> Path:
    """Directory holding every project's bare mirror (``WORK/_mirrors``).

    Resolved through the module attribute so test and CLI overrides of
    ``paths.WORK`` reach it.
    """
    from bubo import paths

    return paths.WORK / MIRRORS_DIR


def mirror_path(project: str, clone_url: str) -> Path:
    """Return the bare mirror path for ``project`` cloned from ``clone_url``.

    The name is the project slug plus a short hash of the clone URL, so two
    projects whose slugs collide (``a/b-c`` and ``a-b/c``) or a project that
    moved hosts never share, or fight over, one mirror.
    """
    return mirror_root() / f"{path_slug(project)}-{stable_hash(clone_url, 8)}.git"


@contextmanager
def _mirror_lock(mirror: Path, *, timeout: float = MIRROR_LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold an exclusive OS lock for ``mirror`` (a sibling ``.lock`` file).

    The lock file sits outside the mirror so discarding a broken mirror never
    deletes a lock another worker is waiting on. Uses the same POSIX
    ``flock`` / Windows ``LockFileEx`` helper as update admission, polled
    non-blocking up to ``timeout`` seconds.
    """
    lock_path = mirror.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                lock_file(descriptor, exclusive=True)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MirrorUnavailable(f"timed out waiting for {lock_path.name}") from None
                time.sleep(0.1)
            except OSError as exc:
                raise MirrorUnavailable(f"cannot lock {lock_path.name}: {exc}") from exc
        yield
    finally:
        os.close(descriptor)


def _forced(refspec: str) -> str:
    """Force-update a fetched ref: a persistent mirror must follow force-pushes."""
    return refspec if refspec.startswith("+") else f"+{refspec}"


def _target_refspecs(target_branch: str, *, local_branch: bool) -> list[str]:
    """Refspecs that bring the target branch up to date, and nothing else.

    ``origin/<target>`` matches what a full clone gives the agent. In a bare
    mirror the local ``<target>`` branch is refreshed too (no branch is ever
    checked out there; every worktree is detached). An unknown target falls
    back to all branches, still incremental.
    """
    if not target_branch:
        return ["+refs/heads/*:refs/remotes/origin/*"]
    specs = [f"+refs/heads/{target_branch}:refs/remotes/origin/{target_branch}"]
    if local_branch:
        specs.insert(0, f"+refs/heads/{target_branch}:refs/heads/{target_branch}")
    return specs


def _checkout_from_mirror(
    *,
    mirror: Path,
    clone_url: str,
    refspecs: list[str],
    sha: str,
    dest: Path,
    auth: list[str],
) -> None:
    """Fetch into the project's mirror and add a detached worktree at ``sha``.

    The whole fetch + ``worktree add`` runs under the mirror lock so concurrent
    workers never race on refs, packs, or worktree metadata. A git failure
    discards the mirror (still under the lock) before re-raising, so a corrupt
    mirror cannot block later reviews; the next review re-creates it.
    """
    with _mirror_lock(mirror):
        try:
            if not (mirror / "HEAD").is_file():
                shutil.rmtree(mirror, ignore_errors=True)
                _run_git(
                    ["git", *auth, "clone", "--bare", clone_url, str(mirror)],
                    cwd=None,
                    what="git mirror clone failed for the project",
                )
            _run_git(
                ["git", *auth, "fetch", "origin", *refspecs],
                cwd=mirror,
                what="git fetch failed for the change",
            )
            _run_git(
                ["git", "worktree", "add", "--detach", str(dest), sha],
                cwd=mirror,
                what="git worktree add failed for the change",
            )
        except Exception:
            shutil.rmtree(mirror, ignore_errors=True)
            raise


def git_checkout_change(
    *,
    clone_url: str,
    ref_fetch: str,
    sha: str,
    dest: Path,
    token: str,
    username: str,
    target_branch: str = "",
    mirror: Path | None = None,
) -> None:
    """Materialize the change at ``sha`` in ``dest`` (detached) over HTTPS.

    With ``mirror`` (what both providers pass), the project's bare mirror is
    created once, then each review only fetches the target branch and the
    change ref into it and adds a cheap ``git worktree`` at ``dest``. If any
    mirror step fails the mirror is discarded and the review falls back to a
    full clone, so a broken mirror can only cost one clone, never a review.

    Without ``mirror`` (or on fallback) ``dest`` is a full clone; right after a
    fresh clone only the change ref is fetched (the clone already has every
    branch), otherwise the target branch and change ref.

    Auth is supplied per-invocation via :func:`_git_auth_args` on every
    remote-touching call (clone, fetch), so the ``origin`` remote URL stays
    plain and **no credential is persisted to any git config**. ``worktree
    add`` and ``checkout`` are local and need no auth. Raises ``RuntimeError``
    with a secret-redacted reason on any git failure.
    """
    auth = _git_auth_args(token, username)
    dest.parent.mkdir(parents=True, exist_ok=True)
    change_refspec = _forced(ref_fetch)
    if mirror is not None and not (dest / ".git").exists():
        try:
            _checkout_from_mirror(
                mirror=mirror,
                clone_url=clone_url,
                refspecs=[*_target_refspecs(target_branch, local_branch=True), change_refspec],
                sha=sha,
                dest=dest,
                auth=auth,
            )
            return
        except Exception as exc:
            log(
                "git_mirror_fallback",
                mirror=mirror.name,
                discarded=not isinstance(exc, MirrorUnavailable) and not mirror.exists(),
                error=redact_secrets(str(exc))[-1000:],
            )
            # A half-added worktree would make the clone below fail.
            shutil.rmtree(dest, ignore_errors=True)
    fresh = not (dest / ".git").exists()
    if fresh:
        _run_git(
            ["git", *auth, "clone", clone_url, str(dest)],
            cwd=None,
            what="git clone failed for the change",
        )
    refspecs = [change_refspec]
    if not fresh:
        refspecs = [*_target_refspecs(target_branch, local_branch=False), change_refspec]
    _run_git(
        ["git", *auth, "fetch", "origin", *refspecs],
        cwd=dest,
        what="git fetch failed for the change",
    )
    _run_git(
        ["git", "checkout", "--detach", sha],
        cwd=dest,
        what="git checkout failed for the change",
    )


def _worktree_mirror(repo: Path) -> Path | None:
    """Return the Bubo mirror ``repo`` is a linked worktree of, if any.

    A linked worktree's ``.git`` is a file ``gitdir: <mirror>/worktrees/<id>``.
    Only mirrors under :func:`mirror_root` are trusted, so a worktree can never
    steer cleanup at an arbitrary repository.
    """
    gitfile = repo / ".git"
    if not gitfile.is_file():
        return None
    try:
        text = gitfile.read_text(encoding="utf-8").strip()
    except OSError, UnicodeError:
        return None
    if not text.startswith("gitdir:"):
        return None
    admin = Path(text.removeprefix("gitdir:").strip())
    if admin.parent.name != "worktrees":
        return None
    mirror = admin.parent.parent
    try:
        mirror.resolve().relative_to(mirror_root().resolve())
    except ValueError:
        return None
    return mirror


def remove_worktree(repo: Path) -> None:
    """Best-effort removal of a review checkout: worktree first, rmtree last.

    For a mirror worktree: ``git worktree remove --force``, then (if anything
    is left) ``rmtree``, then ``git worktree prune`` so the mirror forgets
    the admin entry either way. A plain clone is just removed. Never raises.
    """
    mirror = _worktree_mirror(repo)
    if mirror is None:
        shutil.rmtree(repo, ignore_errors=True)
        return
    try:
        with _mirror_lock(mirror, timeout=120):
            run_bounded(
                ["git", "worktree", "remove", "--force", str(repo)], cwd=mirror, timeout=120
            )
            shutil.rmtree(repo, ignore_errors=True)
            run_bounded(["git", "worktree", "prune"], cwd=mirror, timeout=120)
    except Exception as exc:
        log("git_worktree_remove_failed", mirror=mirror.name, error=type(exc).__name__)
    shutil.rmtree(repo, ignore_errors=True)


def prune_mirrors(keep_projects: Iterable[str]) -> list[Path]:
    """Delete mirrors of projects that are no longer configured.

    Skips a mirror that is locked (a review is fetching) or still has live
    worktrees. Lock files are left in place: unlinking a lock another process
    may be waiting on would let two holders in. Returns the removed mirrors.
    """
    root = mirror_root()
    if not root.is_dir():
        return []
    keep = {path_slug(project) for project in keep_projects}
    removed: list[Path] = []
    for mirror in sorted(root.glob("*.git")):
        match = _MIRROR_NAME.match(mirror.name)
        if match is None or match.group("slug") in keep:
            continue
        try:
            with _mirror_lock(mirror, timeout=0):
                worktrees = mirror / "worktrees"
                if worktrees.is_dir() and any(worktrees.iterdir()):
                    continue
                shutil.rmtree(mirror, ignore_errors=True)
        except MirrorUnavailable:
            continue
        if not mirror.exists():
            removed.append(mirror)
    return removed


class ScmProvider(Protocol):
    """Structural interface every source-control backend implements.

    Implementations are stateless — they read everything from the
    arguments and the environment. ``token`` and ``bot_username`` read the
    process environment (populated by ``bin/bubo`` from ``config/env.toml``).
    """

    name: str

    def token(self) -> str:
        """Return the API token from the environment, or raise ``ConfigError``."""
        ...

    def bot_username(self) -> str:
        """Return the bot account username (for outcome-sync attribution)."""
        ...

    def list_open_changes(self, cfg: ReviewConfig, project: str, token: str) -> list[JsonObject]:
        """List open MRs / PRs for ``project``."""
        ...

    def change_number(self, change: JsonObject) -> int:
        """Return the MR IID / PR number from a change payload."""
        ...

    def head_sha(self, change: JsonObject) -> str:
        """Return the head commit SHA from a change payload, or ``""``."""
        ...

    def get_change(self, cfg: ReviewConfig, token: str, project: str, number: int) -> JsonObject:
        """Re-fetch a single change for fresh diff refs."""
        ...

    def changed_lines(
        self, cfg: ReviewConfig, token: str, project: str, number: int
    ) -> dict[str, JsonObject]:
        """Return the added-line map for the change's diff."""
        ...

    def list_commits(
        self, cfg: ReviewConfig, token: str, project: str, number: int
    ) -> list[JsonObject]:
        """Return the change's commits, normalized to ``{sha, message, author}``.

        Used by the opt-in governance provenance capture to read declared-AI
        commit trailers. ``message`` is the full commit message; ``author`` is
        a display name. Returned dicts are intentionally small (not the raw
        provider payload) so the audit trail stays stable across providers.
        """
        ...

    def build_position(
        self, change: JsonObject, changed: dict[str, JsonObject], finding: JsonObject
    ) -> JsonObject | None:
        """Build the provider-specific inline-comment anchor, or ``None``."""
        ...

    def checkout(self, cfg: ReviewConfig, project: str, change: JsonObject, dest: Path) -> None:
        """Clone + fetch the change's head into ``dest`` (detached)."""
        ...

    def post_inline_comment(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        body: str,
        position: JsonObject,
    ) -> str:
        """Post one inline comment; return its thread/comment ID (or ``""``)."""
        ...

    def post_change_comment(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        body: str,
    ) -> str:
        """Post one change-level (non-inline) comment; return its ID (or ``""``).

        Used for narrative posts that are not anchored to a specific diff
        line — currently only the no-findings acknowledgement comment.
        Implementations are expected to be idempotent on exact-body match
        AND scoped to the bot's own authored comments: if a comment with
        the same body already exists *and was authored by the bot*,
        return its ID instead of posting a duplicate. A foreign author
        reproducing the body must NOT satisfy the match, or the bot
        would stop posting its own acknowledgement.
        """
        ...

    def fetch_outcome(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        bot_username: str,
    ) -> JsonObject:
        """Fetch a posted comment's current state and classify its outcome."""
        ...

    def finding_thread(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        bot_username: str,
        reply_marker: str,
    ) -> FindingThread:
        """Return the guarded state of a Bubo-owned persisted thread.

        Implementations must return ``FOREIGN`` unless the original finding
        comment belongs to ``bot_username``. They must detect ``reply_marker``
        in a prior Bubo reconciliation reply for retry idempotency.
        """
        ...

    def reply_to_finding_thread(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        body: str,
        reply_marker: str,
    ) -> FindingThreadReply:
        """Post/reuse a marked reply and confirm its provider-visible marker."""
        ...

    def resolve_finding_thread(
        self,
        cfg: ReviewConfig,
        token: str,
        project: str,
        number: int,
        thread_id: str,
        bot_username: str,
    ) -> FindingThreadResolution:
        """Resolve a guarded thread; report whether Bubo applied the mutation."""
        ...

    def review_prompt(
        self, project: str, change: JsonObject, cfg: ReviewConfig, *, extra_directive: str = ""
    ) -> str:
        """Build the per-change review task prompt.

        ``extra_directive`` is optional governance context (e.g. the
        heightened-scrutiny notice for an escalated change) appended after the
        review contract. Empty (the default) leaves the prompt unchanged, so
        existing callers and the provenance-off path are byte-identical.
        """
        ...
