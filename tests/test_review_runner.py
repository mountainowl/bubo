import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from bubo import review_runner
from bubo.review_config import DEFAULT_REVIEWER_COMMAND, ReviewConfig


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_missing_sandbox_fails_closed(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(review_runner.sys, "platform", platform)
    monkeypatch.setattr(review_runner.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="requires"):
        review_runner.sandbox_command(["agent"], tmp_path, tmp_path)


def test_macos_only_scratch_gets_filesystem_write_exception(monkeypatch, tmp_path):
    monkeypatch.setattr(review_runner.sys, "platform", "darwin")
    monkeypatch.setattr(review_runner.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    scratch = tmp_path / 'scratch "quoted"'
    source = tmp_path / "source"
    command = review_runner.sandbox_command(["agent", "arg"], scratch, source)
    assert command[:2] == ["sandbox-exec", "-p"]
    assert command[2] == (
        '(version 1)(allow default)(deny file-write*)'
        f'(allow file-write* (subpath {json.dumps(str(scratch))}) (literal "/dev/null"))'
    )
    assert command[3:] == ["agent", "arg"]
    assert str(source) not in command[2]


def test_linux_root_readonly_and_only_scratch_writable(monkeypatch, tmp_path):
    monkeypatch.setattr(review_runner.sys, "platform", "linux")
    monkeypatch.setattr(review_runner.shutil, "which", lambda _: "/usr/bin/bwrap")
    scratch, source = tmp_path / "scratch", tmp_path / "source"
    command = review_runner.sandbox_command(["agent"], scratch, source)
    assert command.count("--bind") == 1
    index = command.index("--bind")
    assert command[index:index + 3] == ["--bind", str(scratch), str(scratch)]
    index = command.index("--ro-bind")
    assert command[index:index + 3] == ["--ro-bind", "/", "/"]
    assert command[-4:] == ["--chdir", str(source), "--", "agent"]


@pytest.fixture
def mocked_runner(monkeypatch):
    monkeypatch.setattr(review_runner, "sandbox_command", lambda command, *_: command)


def test_codex_uses_only_separate_final_message(mocked_runner, monkeypatch, tmp_path):
    def run(command, **kwargs):
        final = Path(command[command.index("--output-last-message") + 1])
        final.write_text('{"status":"incomplete"}', encoding="utf-8")
        return CompletedProcess(command, 0, stdout='codex\n{"status":"complete"}', stderr="")

    monkeypatch.setattr(review_runner, "run_bounded", run)
    original = ["/usr/bin/codex", "exec", "prompt"]
    result = review_runner.run_review(original, cwd=tmp_path, env={"HOME": str(tmp_path)})
    assert result.stdout == '{"status":"incomplete"}'
    assert original == ["/usr/bin/codex", "exec", "prompt"]


@pytest.mark.parametrize("final_content", [None, "x" * 33])
def test_missing_or_oversized_final_never_salvages_transcript(
    mocked_runner, monkeypatch, tmp_path, final_content
):
    def run(command, **kwargs):
        if final_content is not None:
            Path(command[command.index("--output-last-message") + 1]).write_text(final_content)
        return CompletedProcess(command, 0, stdout='codex\n{"status":"complete"}', stderr="")

    monkeypatch.setattr(review_runner, "run_bounded", run)
    with pytest.raises(ValueError, match="bounded final"):
        review_runner.run_review(["codex", "exec", "prompt"], cwd=tmp_path,
                                 env={"HOME": str(tmp_path)}, max_stdout_bytes=32)


def test_child_credentials_stripped_and_writes_redirected(mocked_runner, monkeypatch, tmp_path):
    captured = {}

    def run(command, **kwargs):
        captured.update(kwargs)
        env = kwargs["env"]
        scratch = Path(env["HOME"])
        assert scratch.exists()
        assert scratch != tmp_path
        assert kwargs["cwd"] == tmp_path.resolve()
        for key in ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "SECRET_OTHER"):
            assert key not in env
        for key in ("TMPDIR", "TMP", "TEMP"):
            assert env[key] == str(scratch)
        assert env["CODEX_HOME"] == str(scratch / ".codex")
        return CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(review_runner, "run_bounded", run)
    review_runner.run_review(["other-agent"], cwd=tmp_path, env={
        "HOME": str(tmp_path), "PATH": "/usr/bin", "GITHUB_TOKEN": "secret",
        "GH_TOKEN": "secret", "GITLAB_TOKEN": "secret", "SECRET_OTHER": "secret",
    })
    assert not Path(captured["env"]["HOME"]).exists()


def test_nonzero_codex_exit_is_preserved(mocked_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(review_runner, "run_bounded", lambda command, **_: CompletedProcess(
        command, 9, stdout="failed", stderr="provider error"
    ))
    result = review_runner.run_review(["codex", "exec"], cwd=tmp_path,
                                      env={"HOME": str(tmp_path)})
    assert result.returncode == 9


def test_default_codex_global_options_still_use_final_message(mocked_runner, monkeypatch, tmp_path):
    def run(command, **kwargs):
        index = command.index("exec")
        assert command[index + 1] == "--output-last-message"
        Path(command[index + 2]).write_text("trusted final", encoding="utf-8")
        return CompletedProcess(command, 0, stdout="untrusted transcript", stderr="")

    monkeypatch.setattr(review_runner, "run_bounded", run)
    result = review_runner.run_review(list(DEFAULT_REVIEWER_COMMAND), cwd=tmp_path,
                                      env={"HOME": str(tmp_path)})
    assert result.stdout == "trusted final"


def test_final_envelope_is_separate_from_token_usage_transcript(mocked_runner, monkeypatch, tmp_path):
    from bubo.telemetry.cost import parse_codex_token_usage

    envelope = json.dumps({
        "schema_version": 1, "mode": "diff", "status": "complete",
        "coverage": {"reviewed": [], "unreviewed": [], "limitations": []},
        "findings": [], "questions": [], "omitted_findings_count": 0,
    })
    transcript = "tool output, not the final result\ntokens used\n63,272\n"

    def run(command, **kwargs):
        final = Path(command[command.index("--output-last-message") + 1])
        final.write_text(envelope, encoding="utf-8")
        return CompletedProcess(command, 0, stdout=transcript, stderr="")

    monkeypatch.setattr(review_runner, "run_bounded", run)
    result = review_runner.run_review(["codex", "exec"], cwd=tmp_path,
                                      env={"HOME": str(tmp_path)})
    assert result.stdout == envelope
    assert result.diagnostic_stdout == transcript
    usage = parse_codex_token_usage(result.diagnostic_stdout)
    assert usage.total == 63_272


@pytest.mark.parametrize("authorized", [False, True])
def test_custom_provider_and_config_environment_are_scoped(
    mocked_runner, monkeypatch, tmp_path, authorized
):
    original = tmp_path / "custom-claude"
    original.mkdir()
    credentials = original / ".credentials.json"
    credentials.write_text("original credentials")

    def run(command, **kwargs):
        env = kwargs["env"]
        scratch = Path(env["HOME"])
        assert env["CLAUDE_CONFIG_DIR"] == str(scratch / ".claude")
        assert env["XDG_CONFIG_HOME"] == str(scratch / "config")
        copied = Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
        assert not copied.is_symlink()
        assert copied.read_text() == "original credentials"
        assert copied.stat().st_mode & 0o777 == 0o600
        copied.write_text("refreshed disposable credentials")
        assert credentials.read_text() == "original credentials"
        if authorized:
            assert env["LLM_API_KEY"] == "provider-secret"
            assert env["LLM_BASE_URL"] == "https://provider.example/v1"
        else:
            assert "LLM_API_KEY" not in env
            assert "LLM_BASE_URL" not in env
        return CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(review_runner, "run_bounded", run)
    review_runner.run_review(["claude", "-p", "prompt"], cwd=tmp_path,
        review_config=ReviewConfig(llm_base_url="https://provider.example/v1") if authorized else None,
        env={"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(original),
             "XDG_CONFIG_HOME": str(tmp_path / "old-config"), "LLM_API_KEY": "provider-secret",
             "LLM_BASE_URL": "https://provider.example/v1"})
