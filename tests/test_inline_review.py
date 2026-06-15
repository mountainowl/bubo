import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bubo import paths, poller
from bubo.review_config import ReviewConfig

ROOT = Path(__file__).resolve().parents[1]


class InlineReviewTests(unittest.TestCase):
    def test_filter_findings_by_policy_drops_low_confidence(self):
        from bubo.findings import filter_findings_by_policy

        findings = [
            {"severity": "blocking", "confidence": 0.95, "title": "high"},
            {"severity": "blocking", "confidence": 0.50, "title": "low"},
            {"severity": "blocking", "confidence": 0.85, "title": "on threshold"},
            {"severity": "blocking", "title": "no confidence field"},
            {"severity": "blocking", "confidence": "not a number", "title": "garbled"},
        ]

        kept, dropped = filter_findings_by_policy(findings, min_confidence=0.85)

        kept_titles = [f["title"] for f in kept]
        self.assertEqual(["high", "on threshold"], kept_titles)
        dropped_reasons = {f["title"]: reason for f, reason in dropped}
        self.assertEqual("confidence_below_threshold", dropped_reasons["low"])
        self.assertEqual("confidence_below_threshold", dropped_reasons["no confidence field"])
        self.assertEqual("confidence_below_threshold", dropped_reasons["garbled"])

    def test_filter_findings_by_policy_applies_allowed_kinds_whitelist(self):
        from bubo.findings import filter_findings_by_policy

        findings = [
            {"severity": "blocking", "category": "correctness", "confidence": 0.9, "title": "a"},
            {"severity": "non-blocking", "category": "security", "confidence": 0.9, "title": "b"},
            {"severity": "non-blocking", "category": "style", "confidence": 0.9, "title": "c"},
            {"type": "suggestion", "confidence": 0.9, "title": "d"},
        ]

        # Allowlist matches if EITHER severity OR category OR type matches.
        kept, dropped = filter_findings_by_policy(
            findings,
            min_confidence=0.85,
            allowed_kinds=["blocking", "security", "suggestion"],
        )

        self.assertEqual(["a", "b", "d"], [f["title"] for f in kept])
        self.assertEqual(1, len(dropped))
        self.assertEqual("kind_not_allowed", dropped[0][1])

    def test_filter_findings_by_policy_empty_allowed_kinds_is_no_filter(self):
        from bubo.findings import filter_findings_by_policy

        findings = [
            {"severity": "blocking", "confidence": 0.9, "title": "a"},
            {"severity": "info", "confidence": 0.9, "title": "b"},
        ]

        kept, dropped = filter_findings_by_policy(findings, min_confidence=0.85, allowed_kinds=[])

        self.assertEqual(2, len(kept))
        self.assertEqual([], dropped)

    def test_review_config_parses_min_confidence_and_allowed_kinds(self):
        from bubo.review_config import review_config_from_dict

        cfg = review_config_from_dict(
            {
                "review": {
                    "min_confidence": 0.7,
                    "allowed_kinds": ["BLOCKING", "Security"],
                }
            }
        )

        self.assertEqual(0.7, cfg.min_confidence)
        # Stored lowercase for case-insensitive matching.
        self.assertEqual(["blocking", "security"], cfg.allowed_kinds)

    def test_review_config_default_min_confidence_is_eighty_five_percent(self):
        from bubo.review_config import DEFAULT_MIN_CONFIDENCE, review_config_from_dict

        cfg = review_config_from_dict({})

        self.assertEqual(0.85, DEFAULT_MIN_CONFIDENCE)
        self.assertEqual(0.85, cfg.min_confidence)
        self.assertEqual([], cfg.allowed_kinds)

    def test_filter_findings_by_policy_suppresses_disputed_categories(self):
        from bubo.findings import filter_findings_by_policy

        findings = [
            {"category": "documentation", "confidence": 0.99, "title": "doc nit"},
            {"category": "Documentation ", "confidence": 0.99, "title": "doc nit spaced"},
            {"category": "security", "confidence": 0.99, "title": "real bug"},
            {"severity": "blocking", "confidence": 0.99, "title": "no category"},
        ]

        kept, dropped = filter_findings_by_policy(
            findings,
            min_confidence=0.85,
            suppressed_categories=["documentation"],
        )

        # The disputed class is dropped (case- and whitespace-insensitive);
        # a finding with no `category` is never suppressed.
        self.assertEqual(["real bug", "no category"], [f["title"] for f in kept])
        dropped_reasons = {f["title"]: reason for f, reason in dropped}
        self.assertEqual(
            {
                "doc nit": "disputed_class_suppressed",
                "doc nit spaced": "disputed_class_suppressed",
            },
            dropped_reasons,
        )

    def test_filter_findings_by_policy_empty_suppressed_categories_is_no_filter(self):
        from bubo.findings import filter_findings_by_policy

        findings = [
            {"category": "documentation", "confidence": 0.99, "title": "a"},
            {"category": "security", "confidence": 0.99, "title": "b"},
        ]

        kept, dropped = filter_findings_by_policy(
            findings, min_confidence=0.85, suppressed_categories=[]
        )

        self.assertEqual(["a", "b"], [f["title"] for f in kept])
        self.assertEqual([], dropped)

    def test_review_config_parses_dispute_suppression_settings(self):
        from bubo.review_config import review_config_from_dict

        cfg = review_config_from_dict(
            {
                "review": {
                    "suppress_disputed_classes": True,
                    "dispute_suppress_threshold": 0.6,
                    "dispute_suppress_min_samples": 8,
                }
            }
        )

        self.assertTrue(cfg.suppress_disputed_classes)
        self.assertEqual(0.6, cfg.dispute_suppress_threshold)
        self.assertEqual(8, cfg.dispute_suppress_min_samples)

    def test_review_config_dispute_suppression_defaults_off(self):
        from bubo.review_config import review_config_from_dict

        cfg = review_config_from_dict({})

        # Disabled by default — the load-bearing constraint for this feature.
        self.assertFalse(cfg.suppress_disputed_classes)
        self.assertEqual(0.5, cfg.dispute_suppress_threshold)
        self.assertEqual(5, cfg.dispute_suppress_min_samples)

    def test_extract_json_findings_from_plain_array(self):
        raw = json.dumps(
            [
                {
                    "type": "issue",
                    "severity": "blocking",
                    "category": "correctness",
                    "file": "src/A.java",
                    "line": 12,
                    "body": "Impact: bad\nEvidence: changed line\nFix: change it",
                }
            ]
        )

        findings = poller.extract_findings(raw)

        self.assertEqual(1, len(findings))
        self.assertEqual("src/A.java", findings[0]["file"])

    def test_runner_does_not_forward_stdin_to_child_processes(self):
        class FakeProcess:
            returncode = 0

            def __init__(self) -> None:
                self.kwargs = {}

            def communicate(self, timeout=None):
                return ("", None)

        fake = FakeProcess()
        with patch("bubo.poller.subprocess.Popen", return_value=fake) as mocked:
            poller.run(["cmd"])

        self.assertIs(mocked.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(mocked.call_args.kwargs["start_new_session"])

    def test_extract_json_findings_can_be_capped(self):
        raw = json.dumps(
            [{"file": f"src/{idx}.java", "line": idx, "body": "Fix it"} for idx in range(10)]
        )

        findings = poller.extract_findings(raw, max_findings=3)

        self.assertEqual(3, len(findings))
        self.assertEqual("src/2.java", findings[-1]["file"])

    def test_review_prompt_uses_structured_contract_not_raw_body(self):
        prompt = poller.review_prompt(
            "example/enabled-repo",
            {
                "web_url": "https://gitlab.com/example/enabled-repo/-/merge_requests/269",
                "iid": 269,
                "title": "Move auth",
                "source_branch": "feature",
                "target_branch": "master",
                "sha": "abc",
            },
            ReviewConfig(max_findings_per_merge_request=8),
        )

        self.assertIn("Use the `code-reviewer` skill", prompt)
        self.assertIn("title", prompt)
        self.assertIn("impact", prompt)
        self.assertIn("evidence", prompt)
        self.assertIn("fix", prompt)
        self.assertIn("confidence", prompt)
        self.assertIn("Return at most 8 findings", prompt)
        self.assertNotIn("- body", prompt)

    def test_finding_body_renders_review_contract_fields(self):
        body = poller.finding_body(
            {
                "type": "issue",
                "severity": "blocking",
                "category": "correctness",
                "title": "offset is now required",
                "impact": "Existing clients fail before reaching the repository.",
                "evidence": "The changed @RequestParam lacks a defaultValue.",
                "fix": "Restore the default value.",
                "confidence": 0.98,
            }
        )

        self.assertIn("**Issue (blocking, correctness):** offset is now required", body)
        self.assertIn("\n\n**Impact:** Existing clients fail", body)
        self.assertIn("\n\n**Evidence:** The changed", body)
        self.assertIn("\n\n**Fix:** Restore", body)
        self.assertIn("\n\n**Confidence:** 0.98", body)

    def test_default_reviewer_command_is_noninteractive_codex(self):
        from bubo.review_config import DEFAULT_REVIEWER_COMMAND

        cmd = DEFAULT_REVIEWER_COMMAND
        self.assertEqual("codex", cmd[0])
        self.assertIn("--ask-for-approval", cmd)
        self.assertIn("never", cmd)
        self.assertIn("exec", cmd)
        self.assertIn("--profile", cmd)
        self.assertIn("bubo", cmd)
        self.assertIn("--skip-git-repo-check", cmd)

    def test_extract_json_findings_from_codex_transcript_with_duplicate_arrays(self):
        raw = """OpenAI Codex v0.132.0
codex
[{\"type\":\"finding\",\"severity\":\"high\",\"category\":\"correctness\",\"title\":\"one\",\"file\":\"src/A.java\",\"line\":12,\"evidence\":\"x\"}]
tokens used
63,272
[{\"type\":\"finding\",\"severity\":\"high\",\"category\":\"correctness\",\"title\":\"one\",\"file\":\"src/A.java\",\"line\":12,\"evidence\":\"x\"}]
"""

        findings = poller.extract_findings(raw)

        self.assertEqual(1, len(findings))
        self.assertEqual("one", findings[0]["title"])

    def test_extract_json_findings_prefers_final_codex_block_over_prompt_schema(self):
        raw = """OpenAI Codex v0.134.0
user
Schema:
[
  {\"type\":\"issue\",\"severity\":\"blocking\",\"category\":\"correctness\",\"title\":\"schema\",\"file\":\"path/to/file\",\"line\":123}
]
codex
[
  {\"type\":\"issue\",\"severity\":\"blocking\",\"category\":\"correctness\",\"title\":\"real\",\"file\":\"src/A.java\",\"line\":12}
]
tokens used
42
[
  {\"type\":\"issue\",\"severity\":\"blocking\",\"category\":\"correctness\",\"title\":\"real\",\"file\":\"src/A.java\",\"line\":12}
]
"""

        findings = poller.extract_findings(raw)

        self.assertEqual(1, len(findings))
        self.assertEqual("real", findings[0]["title"])
        self.assertEqual("src/A.java", findings[0]["file"])

    def test_extract_json_findings_keeps_empty_final_codex_review(self):
        raw = """OpenAI Codex v0.134.0
user
Schema:
[
  {\"type\":\"issue\",\"severity\":\"blocking\",\"category\":\"correctness\",\"title\":\"schema\",\"file\":\"path/to/file\",\"line\":123}
]
codex
[]
"""

        findings = poller.extract_findings(raw)

        self.assertEqual([], findings)

    def test_extract_json_findings_ignores_trailing_empty_example_array(self):
        raw = """codex
[{\"type\":\"issue\",\"severity\":\"blocking\",\"category\":\"correctness\",\"title\":\"real\",\"file\":\"src/A.java\",\"line\":12}]
No findings for optional categories: []
"""

        findings = poller.extract_findings(raw)

        self.assertEqual(1, len(findings))
        self.assertEqual("real", findings[0]["title"])

    def test_redact_secrets_masks_tokens_in_persisted_text(self):
        raw = "fatal: https://oauth2:abc123@gitlab.com/group/repo OPENAI_API_KEY=sk-secret glpat-token"

        redacted = poller.redact_secrets(raw)

        self.assertNotIn("abc123", redacted)
        self.assertNotIn("sk-secret", redacted)
        self.assertNotIn("glpat-token", redacted)
        self.assertIn("<redacted>", redacted)

    def test_reviewer_env_does_not_forward_parent_secrets(self):
        source = {
            "PATH": "/usr/bin",
            "HOME": "/home/reviewer",
            "GITLAB_TOKEN": "gitlab-secret",
            "OPENAI_API_KEY": "openai-secret",
            "ANTHROPIC_API_KEY": "anthropic-secret",
        }

        env = poller.reviewer_env(source)

        self.assertEqual("/usr/bin", env["PATH"])
        self.assertIn("BUBO_ROOT", env)
        self.assertNotIn("BUBO_PROMPT", env)
        self.assertNotIn("LLM_REVIEW_MAX_FINDINGS", env)
        self.assertNotIn("GITLAB_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_mcp_call_tool_uses_bounded_communicate(self):
        class FakeProcess:
            returncode = 0

            def __init__(self) -> None:
                self.input = ""
                self.timeout = None

            def communicate(self, input=None, timeout=None):
                self.input = input or ""
                self.timeout = timeout
                return ('{"jsonrpc":"2.0","id":2,"result":{"ok":true}}\n', "")

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

            def __enter__(self):
                return self

            def __exit__(self, *_: object):
                return None

        fake = FakeProcess()
        # The MCP client now lives in `bubo.mcp`. Patch its
        # subprocess import and import MCP_TIMEOUT_SECONDS from the same
        # module rather than from poller.
        from bubo import mcp as mcp_module

        with patch("bubo.mcp.subprocess.Popen", return_value=fake):
            result = poller.mcp_call_tool("tool", {"a": 1})

        self.assertEqual({"ok": True}, result)
        self.assertEqual(mcp_module.MCP_TIMEOUT_SECONDS, fake.timeout)
        self.assertIn('"method": "tools/call"', fake.input)

    def test_fork_worker_closes_parent_log_handle(self):
        class FakeLog:
            closed = False

            def close(self):
                self.closed = True

        class FakeProc:
            pid = 99

        fake_log = FakeLog()
        with tempfile.TemporaryDirectory() as tmp, patch.object(paths, "LOGS", Path(tmp)):
            with patch("pathlib.Path.open", return_value=fake_log):
                with patch("bubo.poller.subprocess.Popen", return_value=FakeProc()):
                    pid = poller.fork_worker(Path("job.json"))

        self.assertEqual(99, pid)
        self.assertTrue(fake_log.closed)

    def test_run_kills_process_group_on_timeout(self):
        class FakeProcess:
            pid = 123
            returncode = None

            def communicate(self, timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired(["cmd"], timeout, output="partial")
                self.returncode = -9
                return ("partial", None)

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        fake = FakeProcess()
        with patch("bubo.poller.subprocess.Popen", return_value=fake):
            with patch("bubo.poller.os.killpg") as killpg:
                with patch("bubo.poller.os.getpgid", return_value=456):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        poller.run(["cmd"], timeout=1)

        killpg.assert_called_once()

    def test_review_prompt_pins_review_contract_vocabulary(self):
        prompt = poller.review_prompt(
            "example/enabled-repo",
            {
                "web_url": "url",
                "iid": 1,
                "title": "t",
                "source_branch": "s",
                "target_branch": "t",
                "sha": "abc",
            },
            ReviewConfig(max_findings_per_merge_request=5),
        )

        self.assertIn("type must be one of: issue, suggestion, question", prompt)
        self.assertIn("severity must be one of: blocking, non-blocking", prompt)
        self.assertIn("confidence must be a number from 0 to 1", prompt)

    def test_read_config_defaults_max_findings_to_five(self):
        original_config = poller.CONFIG
        with tempfile.TemporaryDirectory() as tmp:
            try:
                poller.CONFIG = Path(tmp) / "env.toml"
                poller.CONFIG.write_text(
                    """
[gitlab]
url = "https://gitlab.com"

[[projects]]
path = "example/enabled-repo"
enabled = true
""",
                    encoding="utf-8",
                )

                cfg = poller.read_config()

                self.assertEqual(5, cfg.max_findings_per_merge_request)
            finally:
                poller.CONFIG = original_config

    def test_meta_prompt_limit_is_rendered_from_config(self):
        prompt = (
            "Return JSON.\nMaximum {{MAX_FINDINGS_PER_REVIEW}} findings total for the packet.\n"
        )

        rendered = poller.render_meta_prompt(prompt, 8)

        self.assertIn("Maximum 8 findings total", rendered)
        self.assertNotIn("{{MAX_FINDINGS_PER_REVIEW}}", rendered)

    def test_extract_json_findings_from_fenced_block(self):
        raw = '```json\n[{"file":"src/A.java","line":12,"body":"Fix it"}]\n```'

        findings = poller.extract_findings(raw)

        self.assertEqual(12, findings[0]["line"])

    def test_changed_lines_parser_tracks_new_lines_only(self):
        diff = {
            "new_path": "src/A.java",
            "old_path": "src/A.java",
            "diff": "@@ -10,2 +10,3 @@\n old\n+new one\n same\n+new two\n",
        }

        from bubo.findings import changed_lines_from_diffs

        changed = changed_lines_from_diffs([diff])

        self.assertIn(11, changed["src/A.java"]["new_lines"])
        self.assertIn(13, changed["src/A.java"]["new_lines"])
        self.assertNotIn(10, changed["src/A.java"]["new_lines"])

    def test_build_position_requires_changed_line(self):
        from bubo.findings import build_position

        mr = {"diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}}
        changed = {
            "src/A.java": {
                "old_path": "src/A.java",
                "new_path": "src/A.java",
                "new_lines": {12},
            }
        }

        position = build_position(mr, changed, {"file": "src/A.java", "line": 12})

        self.assertEqual("text", position["position_type"])
        self.assertEqual(12, position["new_line"])
        self.assertIsNone(build_position(mr, changed, {"file": "src/A.java", "line": 99}))

    def test_fingerprint_is_stable_for_same_finding(self):
        finding = {"file": "src/A.java", "line": 12, "body": "Impact: bad\nFix: yes"}

        one = poller.finding_fingerprint("p", 1, "abc", finding)
        two = poller.finding_fingerprint("p", 1, "abc", finding)

        self.assertEqual(one, two)
        self.assertEqual(64, len(one))

    def test_mcp_thread_args_url_encode_project_and_string_iid(self):
        from bubo import mcp

        args = mcp.thread_args(
            "example/enabled-repo",
            269,
            "body",
            {"position_type": "text", "new_line": 12},
        )

        self.assertEqual("example%2Fenabled-repo", args["project_id"])
        self.assertEqual("269", args["merge_request_iid"])
        self.assertEqual("body", args["body"])
        self.assertEqual(12, args["position"]["new_line"])

    def test_gitlab_provider_post_uses_mcp_discussion_id(self):
        from bubo.scm.gitlab import GitLabProvider

        with patch("bubo.mcp.call_tool", return_value={"id": "disc-1"}):
            discussion_id = GitLabProvider().post_inline_comment(
                ReviewConfig(), "token", "group/repo", 1, "body", {"position_type": "text"}
            )

        self.assertEqual("disc-1", discussion_id)

    def test_gitlab_provider_post_falls_back_to_existing_body_match(self):
        from bubo.scm.gitlab import GitLabProvider

        with patch("bubo.mcp.call_tool", return_value={}):
            with patch(
                "bubo.gitlab.find_discussion_by_body", return_value="disc-existing"
            ) as finder:
                with patch("bubo.gitlab.create_merge_request_discussion") as creator:
                    discussion_id = GitLabProvider().post_inline_comment(
                        ReviewConfig(), "token", "group/repo", 1, "body", {"position_type": "text"}
                    )

        self.assertEqual("disc-existing", discussion_id)
        finder.assert_called_once()
        creator.assert_not_called()

    def test_gitlab_provider_post_falls_back_to_rest_create(self):
        from bubo.scm.gitlab import GitLabProvider

        with patch("bubo.mcp.call_tool", return_value={}):
            with patch("bubo.gitlab.find_discussion_by_body", return_value=""):
                with patch(
                    "bubo.gitlab.create_merge_request_discussion",
                    return_value={"id": "disc-rest"},
                ):
                    discussion_id = GitLabProvider().post_inline_comment(
                        ReviewConfig(), "token", "group/repo", 1, "body", {"position_type": "text"}
                    )

        self.assertEqual("disc-rest", discussion_id)


if __name__ == "__main__":
    unittest.main()
