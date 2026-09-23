"""Provider-neutral persistence and parsing for historical review comments."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bubo.db import connect_db, record_finding, record_finding_outcome
from bubo.hash_utils import stable_hash
from bubo.statuses import FindingStatus
from bubo.types import JsonObject

_NOTE_HEADER = re.compile(
    r"^\*\*(Issue|Suggestion|Question) \((blocking|non-blocking), ([^)]+)\):\*\* (.+)$",
    re.IGNORECASE,
)
_NOTE_CONFIDENCE = re.compile(r"\*\*Confidence:\*\*\s*([0-9.]+)", re.IGNORECASE)


@dataclass(frozen=True)
class BackfilledComment:
    """Provider-neutral data needed to persist one historical bot comment."""

    discussion_id: str
    note_id: str
    sha: str
    body: str
    file: str
    line: Any


def first_bot_note(discussion: JsonObject, bot_username: str) -> JsonObject | None:
    for item in discussion.get("notes") or []:
        if not isinstance(item, dict):
            continue
        if ((item.get("author") or {}).get("username") or "") == bot_username:
            return item
    return None


def finding_from_comment(comment: BackfilledComment) -> JsonObject:
    first_line = comment.body.splitlines()[0] if comment.body else ""
    match = _NOTE_HEADER.match(first_line)
    confidence_match = _NOTE_CONFIDENCE.search(comment.body)
    finding: JsonObject = {
        "file": comment.file,
        "line": comment.line,
        "body": comment.body,
        "confidence": float(confidence_match.group(1)) if confidence_match else None,
    }
    if match:
        finding.update(
            {
                "type": match.group(1).lower(),
                "severity": match.group(2).lower(),
                "category": match.group(3).lower(),
                "title": match.group(4).strip(),
            }
        )
    return finding


def record_backfilled_comment(
    project: str, iid: int, comment: BackfilledComment, outcome: JsonObject
) -> int:
    """Upsert one comment/outcome; return one only when it was newly imported."""
    with connect_db() as db:
        row = db.execute(
            """
            select sha,fingerprint from review_findings
            where project=? and iid=? and discussion_id=? and status=?
            order by case when run_id is null then 1 else 0 end
            limit 1
            """,
            (project, iid, comment.discussion_id, FindingStatus.POSTED),
        ).fetchone()
    if row is not None:
        record_finding_outcome(
            project=project,
            iid=iid,
            sha=str(row[0]),
            fingerprint=str(row[1]),
            discussion_id=comment.discussion_id,
            outcome=outcome,
        )
        return 0
    fingerprint = stable_hash(
        {
            "project": project,
            "iid": iid,
            "sha": comment.sha,
            "discussion_id": comment.discussion_id,
            "note_id": comment.note_id,
        }
    )
    record_finding(
        project=project,
        iid=iid,
        sha=comment.sha,
        fingerprint=fingerprint,
        finding=finding_from_comment(comment),
        status=FindingStatus.POSTED,
        body=comment.body,
        discussion_id=comment.discussion_id,
        note_id=comment.note_id,
    )
    record_finding_outcome(
        project=project,
        iid=iid,
        sha=comment.sha,
        fingerprint=fingerprint,
        discussion_id=comment.discussion_id,
        outcome=outcome,
    )
    return 1
