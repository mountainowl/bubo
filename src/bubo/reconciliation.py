"""Small pure helpers for conservative finding-thread reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from bubo.findings import finding_body
from bubo.hash_utils import stable_hash
from bubo.types import JsonObject


class ReconciliationStatus(StrEnum):
    """Whether a scan may truthfully publish a no-findings acknowledgement."""

    RESOLVED = "resolved"
    SURVIVES = "survives"
    BLOCKED = "blocked"
    NO_CANDIDATES = "no_candidates"


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """Conservative result of reconciling every eligible prior finding."""

    status: ReconciliationStatus

    @property
    def permits_no_findings_acknowledgement(self) -> bool:
        """Return whether this result leaves a clean-scan acknowledgement safe."""
        return self.status in {
            ReconciliationStatus.RESOLVED,
            ReconciliationStatus.NO_CANDIDATES,
        }


def finding_identity(project: str, iid: int, finding: JsonObject) -> str:
    """Return an exact, conservative cross-head identity.

    SHA and line are intentionally excluded because rebases move lines. A
    changed canonical body is a non-match, which leaves a thread open rather
    than risking a false resolution.
    """
    return stable_hash(
        {
            "project": project,
            "iid": iid,
            "file": finding.get("file") or finding.get("path"),
            "body": " ".join(finding_body(finding).split()),
        }
    )


def reconciliation_marker(
    project: str, iid: int, prior_sha: str, fingerprint: str, head_sha: str
) -> str:
    """Hidden stable marker used to make provider reply retries idempotent."""
    key = stable_hash(
        {
            "project": project,
            "iid": iid,
            "prior_sha": prior_sha,
            "fingerprint": fingerprint,
            "head": head_sha,
        }
    )
    return f"<!-- bubo-reconcile:{key} -->"


def verified_fixed_reply(head_sha: str, marker: str) -> str:
    """Render a fixed acknowledgement; verifier prose is never published."""
    return f"Verified fixed as of {head_sha}.\n\n{marker}"


__all__ = [
    "ReconciliationOutcome",
    "ReconciliationStatus",
    "finding_identity",
    "reconciliation_marker",
    "verified_fixed_reply",
]
