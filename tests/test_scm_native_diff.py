"""Parity checks for the checkout-only native changed-line map."""

import subprocess
from pathlib import Path

from bubo.scm.base import (
    _changed_lines_from_native_diff,
    change_base_sha,
    change_head_sha,
    native_changed_lines,
)


def test_native_diff_metadata_accepts_gitlab_and_github_immutable_refs() -> None:
    assert change_base_sha({"diff_refs": {"base_sha": "base"}}) == "base"
    assert change_head_sha({"diff_refs": {"head_sha": "head"}}) == "head"
    assert change_base_sha({"base": {"sha": "base"}}) == "base"
    assert change_head_sha({"head": {"sha": "head"}}) == "head"


def test_native_diff_parser_preserves_add_delete_rename_binary_and_hunks() -> None:
    diff = """diff --git a/add.py b/add.py
new file mode 100644
--- /dev/null
+++ b/add.py
@@ -0,0 +1,2 @@
+one
+two
diff --git a/remove.py b/remove.py
deleted file mode 100644
--- a/remove.py
+++ /dev/null
@@ -1 +0,0 @@
-gone
diff --git a/old.py b/new.py
similarity index 100%
rename from old.py
rename to new.py
diff --git a/blob.bin b/blob.bin
new file mode 100644
Binary files /dev/null and b/blob.bin differ
diff --git a/multi.py b/multi.py
--- a/multi.py
+++ b/multi.py
@@ -1,0 +2 @@
+first
@@ -8,0 +10,2 @@
+second
+third
"""
    changed = _changed_lines_from_native_diff(diff)
    assert changed is not None
    assert changed["add.py"]["new_lines"] == {1, 2}
    assert changed["remove.py"]["new_lines"] == set()
    assert changed["new.py"] == {"new_path": "new.py", "old_path": "old.py", "new_lines": set()}
    assert changed["blob.bin"]["new_lines"] == set()
    assert changed["multi.py"]["new_lines"] == {2, 10, 11}


def test_native_diff_parser_accepts_empty_generated_diff() -> None:
    assert _changed_lines_from_native_diff("") == {}
    assert _changed_lines_from_native_diff("not a diff") is None


def test_native_diff_parser_falls_back_for_quoted_paths() -> None:
    quoted_only = """diff --git "a/space file.py" "b/space file.py"
--- "a/space file.py"
+++ "b/space file.py"
@@ -1 +1 @@
-old
+new
"""
    mixed = """diff --git a/plain.py b/plain.py
--- a/plain.py
+++ b/plain.py
@@ -0,0 +1 @@
+plain
diff --git "a/space file.py" "b/space file.py"
--- "a/space file.py"
+++ "b/space file.py"
@@ -0,0 +1 @@
+quoted
"""

    assert _changed_lines_from_native_diff(quoted_only) is None
    assert _changed_lines_from_native_diff(mixed) is None


def test_native_diff_parser_falls_back_for_left_only_quoted_header() -> None:
    diff = """diff --git "a/space file.py" b/space-file.py
similarity index 100%
rename from space file.py
rename to space-file.py
"""

    assert _changed_lines_from_native_diff(diff) is None


def test_native_diff_parser_falls_back_for_right_only_quoted_header() -> None:
    diff = """diff --git a/space-file.py "b/space file.py"
similarity index 100%
rename from space-file.py
rename to space file.py
"""

    assert _changed_lines_from_native_diff(diff) is None


def test_native_diff_parser_discards_prior_entries_after_one_sided_quoted_rename() -> None:
    diff = """diff --git a/plain.py b/plain.py
--- a/plain.py
+++ b/plain.py
@@ -0,0 +1 @@
+plain
diff --git "a/old name.py" b/new-name.py
similarity index 100%
rename from old name.py
rename to new-name.py
"""

    assert _changed_lines_from_native_diff(diff) is None


def test_native_diff_reads_checked_out_immutable_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "bubo@example.test")
    _git(repo, "config", "user.name", "Bubo")
    (repo / "a.py").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.py").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "head")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    changed = native_changed_lines({"diff_refs": {"base_sha": base, "head_sha": head}}, repo)

    assert changed == {"a.py": {"new_path": "a.py", "old_path": "a.py", "new_lines": {2}}}


def test_native_diff_requires_checkout_and_objects(tmp_path: Path) -> None:
    assert (
        native_changed_lines({"base": {"sha": "base"}, "head": {"sha": "head"}}, tmp_path) is None
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True)
