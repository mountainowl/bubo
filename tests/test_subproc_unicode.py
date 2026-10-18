"""Reviewer output is UTF-8 regardless of the host console code page."""

import json
import os
import subprocess
import sys

import pytest

from bubo.findings import extract_findings
from bubo.subproc import run_bounded

TEXT = "Ą café 漢字 🦉"


def test_python_reviewer_round_trip_under_legacy_encoding(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1252")
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    payload = [{"file": "src/example.py", "line": 1, "body": TEXT}]
    script = f"import json; print(json.dumps({payload!a}, ensure_ascii=False))"
    result = run_bounded([sys.executable, "-c", script], env=env, timeout=5)
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout) == payload
    assert extract_findings(result.stdout)[0]["body"] == TEXT
    report = tmp_path / "review.md"
    report.write_text(result.stdout, encoding="utf-8")
    assert TEXT in report.read_text(encoding="utf-8")
    assert env["PYTHONIOENCODING"] == "cp1252"  # Caller mapping is unchanged.


def test_utf8_bytes_and_timeout_transcript_ignore_parent_locale(monkeypatch):
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1252")
    script = f"import os; os.write(1, {TEXT.encode('utf-8')!r})"
    result = run_bounded([sys.executable, "-c", script], timeout=5)
    assert result.stdout == TEXT
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        run_bounded([sys.executable, "-c", script + "; import time; time.sleep(30)"], timeout=1)
    assert caught.value.output == TEXT
