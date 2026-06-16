# Security Policy

## Supported Versions

Bubo is pre-1.0 and ships from `main`. The latest tagged release and the
current `main` branch receive security fixes; older tags do not.

| Version           | Supported |
| ----------------- | --------- |
| `main` (HEAD)     | Yes       |
| Latest tag        | Yes       |
| Older tags        | No        |

## Reporting a vulnerability

**Do not open a public issue or pull request for vulnerabilities, leaked
credentials, or any finding whose disclosure would put a user at risk.**

Report privately via [GitHub Security Advisories][advisories] on this
repository. If GitHub Advisories is unavailable to you, email
**security@mountainowl.dev** instead.

[advisories]: https://github.com/mountainowl/bubo/security/advisories/new

Please include:

- Affected commit SHA or release tag (`git rev-parse HEAD` from the
  vulnerable checkout is fine).
- A clear reproduction: minimal config snippet, command sequence, and
  the observed unsafe behavior.
- Expected vs. actual impact (what an attacker can read, exfiltrate,
  modify, or execute).
- Any masked logs or stack traces (scrub tokens, host names, repo names
  you don't intend to disclose).

## Response timeline

| Stage                              | Target time      |
| ---------------------------------- | ---------------- |
| Acknowledge receipt                | within 3 business days |
| Initial triage + severity estimate | within 7 business days |
| Fix or mitigation in `main`        | within 30 days for High/Critical, best-effort otherwise |
| Coordinated disclosure / advisory  | after a fix is available, or 90 days from report — whichever comes first |

If you do not hear back within 3 business days, escalate by emailing the
address above with `[FOLLOW-UP]` in the subject.

## Vulnerability types in scope

We treat these as security issues:

- **Secret exposure.** Any code path that logs, persists, transmits, or
  echoes a real API token, GitLab/GitHub PAT, or LLM provider key — including
  via the review subprocess's stdout/stderr, SQLite, structured logs, or
  posted comments.
- **Privilege escalation in the agent subprocess.** Anything that lets the
  reviewer agent read host environment variables outside
  `REVIEWER_ENV_ALLOWLIST`, write outside its sandbox, or call dangerous MCP
  tools that the safe defaults block (branch deletion, force-push, merge).
- **Prompt-injection-driven exfiltration.** Reviewed code or MR/PR
  descriptions causing the agent to leak secrets, post unintended content,
  or perform writes against the SCM.
- **Authorization bypass.** Posting comments, fetching diffs, or syncing
  outcomes against a project the configured token should not have access
  to.
- **SQL injection or unsafe deserialization** in the SQLite or config
  layers.
- **Supply-chain compromise.** A pinned dependency, GitHub Action, or MCP
  server wrapper resolving to malicious code.

Out of scope:

- The reviewer agent producing a bad review (incorrect, biased, low-quality
  findings). This is a quality issue, not a security one.
- The default `[review].dry_run = true` posture being "too cautious."
- Documentation typos or broken links that do not lead to exploitable
  behavior.

## Secret handling

Bubo is designed to run against private repositories. Keep these rules:

- Store tokens only in ignored `config/env.toml` or host secret management
  (systemd `LoadCredential=`, GitHub Actions secrets, etc.).
- Never paste raw tokens into issues, PRs, logs, screenshots, or examples.
- Sanitize screenshots before publishing them.
- Treat reviewed code, MR/PR descriptions, comments, and generated files as
  untrusted input — they can attempt prompt injection.

## Runtime permissions

The default Codex profile uses read-only sandboxing and blocks dangerous
GitLab MCP tools such as branch deletion, pushes, and merge actions. Keep
that safety boundary intact unless a deployment explicitly requires a
different trust model.

## Coordinated disclosure

We will credit reporters in the published advisory unless they ask to
remain anonymous. We do not currently operate a bug bounty.
