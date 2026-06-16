"""String enums for the values stored in SQLite status columns.

These were strings scattered through the code until they got promoted to
enums — the enum identity prevents typos like ``"queud"`` from silently
breaking dedup logic. ``StrEnum`` (PEP 663, Python 3.11+) makes the members
compare equal to their string values so existing SQLite rows keep working.

Adding a value here is a schema-friendly change: SQLite stores the new
string, ``already_seen``-style lookups need to be updated to include or
exclude it explicitly, and the corresponding test in
``tests/test_poller_telemetry_state.py`` should be updated.
"""

from __future__ import annotations

from enum import StrEnum


class ReviewStatus(StrEnum):
    """Lifecycle status of a per-(project, iid, sha) review row.

    Values:

    * ``QUEUED`` — worker has been forked but has not yet started.
    * ``RUNNING`` — worker has begun the review (transitioned at the top
      of :func:`bubo.poller.worker`).
    * ``SUCCESS`` — review completed and at least one finding was posted
      or planned.
    * ``NO_FINDINGS`` — review completed cleanly and produced zero
      findings. Terminal; the MR will not be re-reviewed at this SHA.
    * ``FAILED`` — review raised an exception. Subject to a TTL re-queue
      so transient failures self-heal on the next poll.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    NO_FINDINGS = "no_findings"
    FAILED = "failed"


class FindingStatus(StrEnum):
    """Lifecycle status of a single finding within a review.

    Values:

    * ``PLANNED`` — recorded in dry-run mode; no GitLab post.
    * ``POSTED`` — successfully posted as an inline GitLab discussion.
    * ``SKIPPED`` — could not be mapped to a changed line in the diff, or
      filtered by the policy filter, or de-duplicated against a prior
      run.
    * ``PENDING_EXTERNAL_ID`` — the GitLab POST appeared to succeed but
      the response did not contain a discussion ID. Re-checked on the
      next outcome sync.
    * ``REFUTED`` — the opt-in verification pass (off by default) ran
      independent "is this real?" checks and a majority refuted the
      finding, so it was dropped instead of posted. Recorded for audit.
    """

    PLANNED = "planned"
    POSTED = "posted"
    SKIPPED = "skipped"
    PENDING_EXTERNAL_ID = "pending_external_id"
    REFUTED = "refuted"


class ReviewMode(StrEnum):
    """What scope the agent reviewed.

    Only ``DIFF`` is implemented today. A future ``FULL`` mode is
    anticipated for whole-file or whole-repo reviews; until then the field
    exists purely for metric-attribute compatibility.
    """

    DIFF = "diff"
