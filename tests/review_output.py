"""Live-review fixtures: explicit completion separate from finding contents."""

import json


def review_output(
    findings=None,
    *,
    status="complete",
    mode="diff",
    questions=None,
    unreviewed=None,
    limitations=None,
    omitted=0,
):
    normalized = []
    for finding in findings or []:
        normalized.append(
            {
                "title": "Concrete defect",
                "file": "a.py",
                "line": 1,
                "type": "issue",
                "severity": "blocking",
                "confidence": 0.95,
                "category": "correctness",
                "impact": "Observable wrong behavior",
                "evidence": "The changed path violates the supplied contract",
                "fix": "Preserve the contract",
                **finding,
            }
        )
    return json.dumps(
        {
            "schema_version": 1,
            "mode": mode,
            "status": status,
            "findings": normalized,
            "questions": questions or [],
            "coverage": {
                "reviewed": ["declared diff"],
                "unreviewed": unreviewed or [],
                "limitations": limitations or [],
            },
            "omitted_findings_count": omitted,
        }
    )
