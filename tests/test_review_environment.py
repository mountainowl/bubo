import subprocess
import sys

from bubo import paths, poller, review_environment


def test_poller_compatibility_entrypoint_keeps_patchable_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    source = {"PATH": "/usr/bin", "GITHUB_TOKEN": "must-not-pass"}
    assert poller.reviewer_env(source) == review_environment.reviewer_env(source, root=tmp_path)
    assert poller.reviewer_env(source)["BUBO_ROOT"] == str(tmp_path)


def test_runner_import_does_not_load_orchestrator():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import bubo.review_runner; assert 'bubo.poller' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_legacy_findings_parser_remains_explicitly_exported():
    assert "extract_findings" in poller.__all__
    assert poller.extract_findings("[]") == []
