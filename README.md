# Bubo 🦉

[![PyPI](https://img.shields.io/pypi/v/bubo?logo=pypi&logoColor=white)](https://pypi.org/project/bubo/)
[![Python 3.14+](https://img.shields.io/badge/python-3.14%2B-3776ab?logo=python&logoColor=white)](https://github.com/mountainowl/bubo/blob/main/pyproject.toml)
[![Docker: GHCR](https://img.shields.io/badge/docker-ghcr.io-2496ED?logo=docker&logoColor=white)](https://github.com/mountainowl/bubo/pkgs/container/bubo)
[![CI](https://github.com/mountainowl/bubo/actions/workflows/ci.yml/badge.svg)](https://github.com/mountainowl/bubo/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://img.shields.io/ossf-scorecard/github.com/mountainowl/bubo?label=OpenSSF%20Scorecard)](https://scorecard.dev/viewer/?uri=github.com/mountainowl/bubo)
[![Signed with cosign](https://img.shields.io/badge/release-cosign%20signed-2bb4ab?logo=sigstore&logoColor=white)](https://github.com/mountainowl/bubo/releases)
[![SLSA 3](https://slsa.dev/images/gh-badge-level3.svg)](https://slsa.dev)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Managed with uv](https://img.shields.io/badge/managed%20with-uv-2f3542)](https://github.com/astral-sh/uv)
[![Docs](https://img.shields.io/badge/docs-mountainowl.github.io%2Fbubo-4f62ad)](https://mountainowl.github.io/bubo/)
[![License: MIT](https://img.shields.io/badge/license-MIT-111827)](https://github.com/mountainowl/bubo/blob/main/LICENSE)

## Enterprise Grade Compliant Agentic AI code reviews**
- Self-hosted
- Bring-your-own-LLM
- Supports GitLab & GitHub
- Direct inline comments
- Governance, provenance & audit 
- OpenTelemetry metrics

> [Bubo](https://en.wikipedia.org/wiki/Bubo_(genus)) is the genus of the `great
horned and eagle owls` — patient night hunters that sit silent, see in the dark,
and strike only when sure. 

Code review is the exact same idea. Bubo reviews your code commits with **the LLM you choose** (Codex, Claude, or any model your CLI
drives) and posts only the findings worth acting on as inline threads — no chatbot noise, no praise, no summaries.

## Features at a glance

| | |
|---|---|
| 🧠 **Bring your own LLM** | Codex, Claude, or any model your CLI drives — no vendor lock-in. |
| 🔒 **Self-hosted** | Code, diffs, and review data stay on your infrastructure. |
| 🔀 **GitLab/GitHub** | MRs and PRs, one config, identical behavior on both. |
| 🎯 **Signal over noise** | Only actionable inline findings; one "all good" ack on a clean change. |
| 🎭 **Moods** | Pick the review voice — terse / collaborative / socratic / formal / casual. |
| 📉 **Learns you** | Suppresses finding-classes you and your team repeatedly disputes. |
| ✅ **Verify before posting** | Optional "is this real?" passes drop findings that don't hold up. |
| 🛡️ **Compliance & Governance-ready** | AI-code provenance, rigor modulation, auditable on-prem report. |
| 🔌 **MCP + CI** | Built-in `bubo-mcp` server + a GitHub Action to review PRs in CI. |

## 📖 Documentation

**Rendered docs live at → [mountainowl.github.io/bubo](https://mountainowl.github.io/bubo/)**
— the canonical reference. The `docs/*.md` files below are its source; this README
is a teaser.

👉 New here? The **[Recipes](https://mountainowl.github.io/bubo/recipes/)**
([docs/recipes.md](https://github.com/mountainowl/bubo/blob/main/docs/recipes.md)) are copy-paste setups for GitLab and
GitHub, using Codex (the bundled default) or Claude as the review agent.

## What a review looks like

Findings are posted inline in a fixed shape — `Issue` / `Impact` / `Evidence` /
`Fix` / `Confidence`:

```text
Issue: HS256 JWT fallback is skipped when Cognito URL construction fails.
Impact: Valid local/shared-secret JWT requests return 500 instead of authenticating.
Evidence: The changed interceptor rethrows InvalidAwsUrlException before fallback runs.
Fix: Treat Cognito validation construction failures as failed Cognito auth when fallback is allowed.
Confidence: 0.94
```

Found nothing? Bubo posts one short change-level acknowledgement
(`Automated review ran — no issues found.`) so a clean MR/PR reads differently
from one the reviewer never touched. It's on by default, dedup'd by bot author +
exact body, and configurable under `[agents]` (see the
[configuration reference](https://github.com/mountainowl/bubo/blob/main/docs/configuration.md)).

## 🎭 Give it a mood

A finding only helps if someone reads it. The shape above is `terse` — the
default, and the right call when you want pure signal. But a robotic comment gets
skimmed; one that sounds like a teammate gets *fixed*. So Bubo lets you pick the
**voice**. One knob:

```toml
[review]
tone = "collaborative"   # terse · collaborative · socratic · formal · casual
```

Below is **one real finding** — a cookie-deletion bug Bubo caught on a public PR
— wearing four moods. Same bug, same evidence, same `0.99` confidence underneath.
Only the words change:

> 🤝 **collaborative** — *for teams who want a teammate, not a linter*
> Heads up — this removes by name only, so if the jar has `sid` for `a.example`
> and `b.example`, one `popitem()` returns one pair but deletes both cookies.
> Probably worth clearing the specific cookie using its domain/path/name.

> 🤔 **socratic** — *for mentoring and review-as-teaching*
> What happens here when the jar has the same cookie name for two domains?
> `del self[name]` goes through `remove_cookie_by_name` without domain/path, so
> this removes every matching cookie while returning only one pair — should we
> clear the selected cookie by domain/path/name instead?

> 🏛️ **formal** — *for regulated shops and a clean audit trail*
> When multiple domains contain the same cookie name, this deletes by name only
> and removes every matching cookie while returning a single pair. Recommend
> clearing the specific cookie selected by `popitem` using its domain, path, and
> name.

> 😎 **casual** — *for startups who keep it light*
> Quick one — this deletes by name only, so same-name cookies on other
> domains/paths get cleared too. Grab the Cookie from the iterator and clear
> that exact domain/path/name.

**The catch? There isn't one.** Mood changes *only the words a developer reads*.
Severity, evidence, confidence, the dedup fingerprint, and every row in your
governance/audit dataset stay identical across tones — so you tune the voice for
your humans without touching the data your compliance reports run on. Ships as
`terse`; opt in when you're ready, set it once, leave it to the operator.
→ [tone reference](https://github.com/mountainowl/bubo/blob/main/docs/configuration.md#review-comment-tone-moods)

More sanitized review examples are in
[docs/examples/README.md](https://github.com/mountainowl/bubo/blob/main/docs/examples/README.md).

## 60-second quickstart

Install the prereqs (uv, Python 3.14+, Git, plus the CLI for your SCM and a Codex
agent — see [prerequisites](https://github.com/mountainowl/bubo/blob/main/docs/prerequisites.md)), then:

```sh
uv tool install bubo     # from PyPI (or: pip install bubo)
# or track the main branch:
#   uv tool install git+https://github.com/mountainowl/bubo
bubo init                # idempotent; --dry-run to preview

# Edit ~/.local/share/bubo/config/env.toml:
#   [gitlab].token, [agents].llm_model, [agents].llm_api_key,
#   [agents].llm_api_key_env, and at least one [[projects]] entry.

bubo doctor              # verify before first poll
bubo-poller              # one poll cycle; exits at the end
```

The first cycle runs with `[review].dry_run = true` (the default) — findings are
planned, no comments posted. Flip to `false` once a real review looks right. Full
walkthrough in the **[Recipes](https://mountainowl.github.io/bubo/recipes/)** and
[install and configure](https://github.com/mountainowl/bubo/blob/main/docs/install-and-configure.md); poller flags and the
bundled MCP server are in [run](https://github.com/mountainowl/bubo/blob/main/docs/run.md).

Prefer a container? A multi-arch image is published to GHCR each release:

```sh
docker pull ghcr.io/mountainowl/bubo
docker run --rm ghcr.io/mountainowl/bubo bubo report   # or bubo init / bubo-poller
```

The image ships bubo + git; the review-agent CLI (Codex/Claude) is BYO — derive
your own image (`FROM ghcr.io/mountainowl/bubo`) or mount it in.

## Further reading

These render on the [docs site](https://mountainowl.github.io/bubo/) and as plain
Markdown in the repo:

| Doc | What's in it |
|---|---|
| [Prerequisites](https://github.com/mountainowl/bubo/blob/main/docs/prerequisites.md) | macOS / Linux runtime, per-provider tools, credentials, install verification. |
| [Install and configure](https://github.com/mountainowl/bubo/blob/main/docs/install-and-configure.md) | `uv tool install`, `bubo init`, the minimum `config/env.toml`, GitLab and GitHub bot setup. |
| [Run](https://github.com/mountainowl/bubo/blob/main/docs/run.md) | One-off review, the poller, the bundled `bubo-mcp` MCP server, and upstream wrappers. |
| [Configuration reference](https://github.com/mountainowl/bubo/blob/main/docs/configuration.md) | Every `[scm]` / `[gitlab]` / `[github]` / `[review]` / `[poller]` / `[agents]` / `[telemetry]` / `[[projects]]` setting and its default. |
| [Operate](https://github.com/mountainowl/bubo/blob/main/docs/operate.md) | Remote deploy, scheduling under cron or systemd, `--sync-outcomes` grading, one-shot backfill. |
| [Telemetry](https://github.com/mountainowl/bubo/blob/main/docs/telemetry.md) | Emitted `llm_review.*` metrics, ready-made dashboard queries, cardinality discipline. |

## Status

- **GitLab & GitHub posting via polling** — production path, at outcome-metric
  parity. Set `[scm].provider = "github"` (or `BUBO_PROVIDER=github`).
- **MCP server (`bubo-mcp`)** — read-only metrics + triggered reviews; stdio or HTTP.
- **Codex or Claude** — Bubo runs the review through a wrapper around your agent
  CLI. Codex ships pre-wired as the bundled default; Claude works the same way
  once you point the wrapper at it.
- **Webhook-driven triggering** — not yet; polling is the only path.

Review execution sits outside CI/CD by design. Run it as a poller beside your existing pipelines.

## Security

- `config/env.toml` is gitignored and holds tokens. **Do not print or commit
  real values from it.**
- Review-agent stdout is redacted (`GITLAB_TOKEN=`, `OPENAI_API_KEY=`, `glpat-…`,
  `sk-…`, and credentialed Git URLs) before it touches reports, logs, or the
  database error column.
- The reviewer subprocess runs under a strict env allowlist — host secrets aren't
  passed wholesale into the LLM agent. Releases are cosign-signed with an SBOM.
  Report vulnerabilities per [`SECURITY.md`](https://github.com/mountainowl/bubo/blob/main/SECURITY.md).

## Bot avatar

Upload [`assets/bubo.png`](assets/bubo.png) as the GitLab (or future GitHub) bot avatar.

![Bubo avatar preview](https://raw.githubusercontent.com/mountainowl/bubo/main/docs/images/bubo-avatar-preview.png)

## Community

[Contributing](https://github.com/mountainowl/bubo/blob/main/CONTRIBUTING.md) · [Security policy](https://github.com/mountainowl/bubo/blob/main/SECURITY.md) ·
[Support](SUPPORT.md) · [Code of conduct](CODE_OF_CONDUCT.md)
