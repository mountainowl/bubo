# Configuration reference

All runtime config and credentials live in one TOML file. Copy the template
[`config/env.example.toml`](https://github.com/mountainowl/bubo/blob/main/config/env.example.toml)
to the gitignored `config/env.toml` and edit there:

```sh
cp config/env.example.toml config/env.toml
```

Every default shown below is the value bubo falls back to when a key is omitted —
leaving a key out is the same as setting it to its default. Any string value can
reference environment variables (`token = "${GITLAB_TOKEN}"`,
`url = "${GITLAB_URL:-https://gitlab.com}"`), so secrets never have to sit on
disk. Each section has its own table and a minimal example; a full copy-paste
**[quick-start config](#quick-start-config)** is at the end.

## `[scm]`

| Setting | Default | Purpose |
|---|---|---|
| `provider` | `gitlab` | Source-control backend — `gitlab` or `github`. Selects which provider the poller drives. `BUBO_PROVIDER=github` overrides it for a single run. |

```toml
[scm]
provider = "gitlab"   # or "github"
```

## `[gitlab]`

Used when `provider = "gitlab"`.

| Setting | Default | Purpose |
|---|---|---|
| `url` | `https://gitlab.com` | Web host the poller reads MRs from. For self-hosted GitLab, keep `api_url` on the same host. |
| `api_url` | `https://gitlab.com/api/v4` | REST API endpoint used by the GitLab MCP tools inside the review agent. |
| `bot_username` | `bubo` | Bot account name on posted threads; lets outcome sync tell bot comments from developer replies. |
| `denied_tools_regex` | `^(delete_.*\|merge_merge_request\|push_files)$` | Blocks dangerous GitLab MCP tools even if the agent can see them. |
| `token` | unset | GitLab token with `api` scope. Exported as `GITLAB_TOKEN`, `GITLAB_PERSONAL_ACCESS_TOKEN`, and `GLAB_TOKEN`. |

```toml
[gitlab]
url          = "https://gitlab.com"
api_url      = "https://gitlab.com/api/v4"
bot_username = "bubo"
token        = "${GITLAB_TOKEN}"   # api scope; keep the real value in the environment
```

## `[github]`

Used when `provider = "github"`. Needs the `gh` CLI (checkout) and a GitHub MCP
server on `PATH` (inline comments; falls back to REST otherwise).

| Setting | Default | Purpose |
|---|---|---|
| `api_url` | `https://api.github.com` | REST API base. Use `https://<host>/api/v3` for GitHub Enterprise Server. |
| `bot_username` | `bubo` | Bot account name on review comments; lets outcome sync separate bot comments from replies. |
| `token` | unset | GitHub token with pull-request read+write. Exported as `GITHUB_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, and `GH_TOKEN`. |

```toml
[github]
api_url      = "https://api.github.com"   # https://<host>/api/v3 for GHES
bot_username = "bubo"
token        = "${GITHUB_TOKEN}"           # pull-request read + write
```

## `[review]`

How the poller picks, paces, and filters reviews.

| Setting | Default | Purpose |
|---|---|---|
| `dry_run` | `true` | Plan findings into SQLite without posting comments. Flip to `false` once test reviews look right. |
| `max_merge_requests_per_poll` | `8` | Caps how many MRs one poll cycle queues. Higher values fork more workers at once. |
| `max_findings_per_merge_request` | `8` | Caps findings per MR and fills `{{MAX_FINDINGS_PER_REVIEW}}` in the prompt. |
| `timeout_seconds` | `1800` | Kills a review worker that runs too long. |
| `min_confidence` | `0.85` | Floor for the LLM's per-finding confidence (0.0–1.0); lower-scored findings are dropped. Inclusive on the high side. |
| `category_min_confidence` | `{}` | Per-canonical-category confidence floors — a finding must clear `max(min_confidence, floor)`. Only ever raises the bar. See [Calibrated per-class confidence](#calibrated-per-class-confidence). |
| `calibrate_confidence` | `false` | Derive per-category floors from this repo's dispute history. Manual floors win. See [Calibrated per-class confidence](#calibrated-per-class-confidence). |
| `calibrate_max_confidence` | `0.97` | Ceiling for a calibrated floor (kept below 1.0). Only used when `calibrate_confidence` is on. |
| `allowed_kinds` | `[]` | Whitelist of finding kinds to post — kept if its `severity`, `category`, or `type` matches (case-insensitive). `[]` = no kind filter. |
| `tone` | `"terse"` | Comment voice — `terse` / `collaborative` / `socratic` / `formal` / `casual`. Changes only how a finding reads. See [Review-comment tone](#review-comment-tone-moods). |
| `mode` | `"collaborate"` | Surface mode — which findings reach the poster. `collaborate` (all types) or `gate` (blocking defects only). See [Surface mode](#surface-mode-gate-vs-collaborate). |
| `suppress_disputed_classes` | `false` | Drop `category` classes this repo repeatedly rejects, using outcome history. See [Dispute-driven suppression](#dispute-driven-suppression). |
| `dispute_suppress_threshold` | `0.5` | Dispute rate (0.0–1.0) at or above which a category is suppressed. Only when suppression is on. |
| `dispute_suppress_min_samples` | `5` | Minimum recorded outcomes before a category's dispute rate is acted on. Only when suppression is on. |
| `verify_findings` | `false` | Master switch for pre-post verification. See [Verification before posting](#verification-before-posting). |
| `verify_lenses` | `["correctness", "in_diff", "reproduce"]` | Lenses run per finding (one LLM call each). |
| `verify_min_votes` | `2` | Lenses that must vote "real" (≥ floor) for a finding to survive. |
| `verify_confidence_floor` | `0.6` | Minimum verifier confidence (inclusive) for a real vote to count. |
| `verify_max_findings` | `5` | Cap on findings verified per change; the rest post unverified. |
| `verify_timeout_seconds` | `300` | Per-check wall-clock budget. |
| `verify_command` | `[]` | Verifier argv. Empty reuses `reviewer_command`; set a different model for real diversity. |

```toml
[review]
dry_run                        = true   # plan only; flip to false to start posting
max_merge_requests_per_poll    = 8
max_findings_per_merge_request = 8
timeout_seconds                = 1800
min_confidence                 = 0.85
allowed_kinds                  = []     # [] = post everything above min_confidence
tone                           = "terse"        # terse · collaborative · socratic · formal · casual
mode                           = "collaborate"  # or "gate" for a merge-blocking lane

# Opt-in precision levers (all off by default — see the subsections below):
# category_min_confidence   = { style = 0.95, design = 0.95, performance = 0.92 }
# calibrate_confidence      = false
# suppress_disputed_classes = false
# verify_findings           = false
```

## `[governance]`

AI-code provenance capture (opt-in; off by default).

| Setting | Default | Purpose |
|---|---|---|
| `capture_provenance` | `false` | Record a banded AI-provenance signal per change for audit. Capture only — no review-behavior change. See [Governance & provenance](#governance-provenance). |
| `ai_trailer_patterns` | built-in | Case-insensitive regexes matched against commit-message lines to detect *declared* AI assistance. Only when `capture_provenance` is on. |
| `sensitive_path_globs` | `[]` | `fnmatch` globs flagged when a change touches sensitive paths, e.g. `payments/**`, `*.pem`. |
| `rigor_modulation` | `false` | Phase 2. An escalated change gets a heightened-scrutiny prompt directive. Advisory; auto-implies the provenance fetch. |
| `escalate_bands` | `["likely_ai", "collaborative"]` | Provenance bands that escalate. `unknown` never escalates. |
| `rigor_require_sensitive` | `true` | Escalate only if the change also touches a `sensitive_path_globs` path; `false` escalates on band alone. |
| `policy_mode` | `off` | `off` / `report-only` / `soft`. When not `off`, an auditable decision is recorded per change. All modes advisory. |

```toml
[governance]
capture_provenance = false   # opt-in; capture only, never blocks a merge
# sensitive_path_globs = ["payments/**", "**/auth/**", "*.pem"]
# rigor_modulation     = false        # Phase 2 (advisory)
# policy_mode          = "off"        # off · report-only · soft
```

## `[poller]`

Filesystem layout and scheduling hints.

| Setting | Default | Purpose |
|---|---|---|
| `state_dir` | `var` | Holds SQLite state, logs, reports, worktrees, and rendered prompts. |
| `interval_seconds` | `900` | Suggested wait for long-running poll loops. Cron/systemd can use another interval. |
| `target_merge_request_iid` | unset | Single-MR debug filter. **Leave unset in production.** |

```toml
[poller]
state_dir        = "var"
interval_seconds = 900
# target_merge_request_iid = 123   # debug only; unset in production
```

## `[agents]`

Review-agent CLI configuration.

| Setting | Default | Purpose |
|---|---|---|
| `prompt_file` | `prompts/00-meta.md` | Meta prompt rendered before each review. |
| `llm_model` | `gpt-5.5` | Model passed to the review wrapper. Keep `[telemetry]` pricing aligned for cost metrics. |
| `llm_api_key` | unset | API key for whatever LLM you review with. Exported as `LLM_API_KEY` plus the name in `llm_api_key_env`. |
| `llm_api_key_env` | `OPENAI_API_KEY` | The env-var name your LLM CLI reads the key from — `ANTHROPIC_API_KEY` (Claude), `GEMINI_API_KEY` (Gemini), etc. Bubo is model-agnostic and does not guess it. |
| `reasoning_effort` | `medium` | `low` / `medium` / `high`. Higher is more thorough but costs more and runs longer. |
| `dry_run` | `true` | Dry-run hint exported to the agent as `REVIEW_DRY_RUN`, separate from `[review].dry_run` (which controls poster posting). |
| `codex_profile` | `bubo` | Codex profile used by the Codex wrapper. |
| `codex_sandbox` | `read-only` | Filesystem access for Codex review runs. See [Troubleshooting](troubleshooting.md) for the bubblewrap/AppArmor caveat. |
| `post_no_findings_comment` | `true` | Post one change-level "no issues found" ack on a clean review. Dedup'd by bot author + body; honors `[review].dry_run`. |
| `no_findings_comment_body` | `"Automated review ran — no issues found."` | Body of that ack, posted verbatim. Must be byte-identical across re-reviews for dedup. Empty/whitespace disables it. |

```toml
[agents]
prompt_file      = "prompts/00-meta.md"
llm_model        = "gpt-5.5"
llm_api_key      = "${LLM_API_KEY}"
llm_api_key_env  = "OPENAI_API_KEY"   # ANTHROPIC_API_KEY for Claude, etc.
reasoning_effort = "medium"
codex_profile    = "bubo"
codex_sandbox    = "read-only"
post_no_findings_comment = true
```

## `[telemetry]`

OpenTelemetry metrics, spans, and cost estimation.

| Setting | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Send OTel metrics and spans. SQLite state is written either way. |
| `service_name` | `bubo` | Service name shown in the OTel backend. Use the same value across a fleet. |
| `environment` | `prod` | Environment label for dashboards, e.g. `dev` / `staging` / `prod`. |
| `otlp_endpoint` | `http://127.0.0.1:4317` | OTLP/gRPC collector endpoint for metrics and traces. |
| `otlp_protocol` | `grpc` | OTLP transport. Only `grpc` is supported today (validated at startup). |
| `export_interval_seconds` | `30` | Metric export interval. Lower is fresher, at more exporter overhead. |
| `emit_finding_events` | `true` | Emit per-finding lifecycle metrics (planned, posted, skipped, resolved). |
| `emit_outcome_sync` | `true` | Emit metrics when outcome sync checks posted-finding status. |
| `input_per_1m` | `5.0` | Estimated input-token price per million tokens (cost metrics only). |
| `output_per_1m` | `30.0` | Estimated output-token price per million tokens. |
| `cached_input_per_1m` | `0.5` | Estimated cached-input price per million tokens. |

```toml
[telemetry]
enabled                 = false   # turn on once an OTLP collector is running
service_name            = "bubo"
environment             = "prod"
otlp_endpoint           = "http://127.0.0.1:4317"
otlp_protocol           = "grpc"
export_interval_seconds = 30
input_per_1m            = 5.0     # keep these aligned with [agents].llm_model
output_per_1m           = 30.0
cached_input_per_1m     = 0.5
```

## `[mcp_server]`

How `bubo-mcp` exposes itself. See [MCP server](mcp.md) for client setup.

| Setting | Default | Purpose |
|---|---|---|
| `transport` | `stdio` | `stdio` (client spawns the process per session, no network, no auth) or `http` (long-lived daemon, bearer-token auth required). |
| `host` | `127.0.0.1` | Bind address for `http`. `0.0.0.0` exposes it externally — put TLS in front. |
| `port` | `8765` | Bind port for `http`. Pick anything free. |
| `bearer_token` | unset | Required for `http`; clients must send `Authorization: Bearer <token>`. Prefer `${VAR}` interpolation so it never lands on disk. |

```toml
[mcp_server]
transport = "stdio"   # or "http" for a long-lived daemon
# host         = "127.0.0.1"
# port         = 8765
# bearer_token = "${BUBO_MCP_TOKEN}"   # required when transport = "http"
```

## `[[projects]]`

One array-of-tables block per repository to review. Add as many as you want; the
poller iterates them in order each cycle.

| Setting | Default | Purpose |
|---|---|---|
| `path` | — | Project path to poll, e.g. `group/repo`. Sub-groups (`group/sub/repo`) are supported; do not pre-encode it. |
| `enabled` | `true` | Turns polling for that project on or off without removing the block. |

```toml
[[projects]]
path    = "group/repo"
enabled = true

[[projects]]
path    = "group/another-repo"
enabled = false   # kept in config but skipped
```

## Review-comment tone (moods)

`[review].tone` chooses the *voice* of a posted finding — a pure presentation
choice, entirely yours. Bubo ships with `terse`, so behavior is unchanged unless
you opt in.

| tone | reads like |
| --- | --- |
| `terse` *(default)* | the structured `**Impact:** … **Evidence:** … **Fix:** …` render — byte-identical to bubo before this knob existed |
| `collaborative` | a senior engineer's inline note — acknowledges intent, gives a concrete example, suggests the fix |
| `socratic` | leads with a question that surfaces the gap and invites confirmation |
| `formal` | measured, complete sentences, no contractions — for regulated/enterprise review |
| `casual` | relaxed and brief |

**How it works.** For any non-`terse` tone, Bubo injects a short *voice
directive* into the review prompt (a register description plus one cross-domain
style example) asking the reviewer to add a single in-voice `comment` field per
finding. Bubo posts that `comment` in place of the structured render, at the cost
of a few extra output tokens per finding.

**What never changes with tone.** The structured fields
(`title`/`impact`/`evidence`/`fix`/`confidence`) are still recorded to SQLite
exactly as in `terse` mode, and the dedup **fingerprint is computed from those
fields, not the voiced comment**. So you can switch tone any time without
re-posting existing findings, splitting their accept/dispute history, or
disturbing the governance dataset — only the words developers read change.

**Measuring which mood works.** The active tone is recorded per run on the
`review_runs` row and surfaced in the `bubo report` audit trail (and its CSV
export), so you can A/B accept-vs-dispute rates by tone before committing your
team to one voice.

### The same finding in each tone

One real finding (a cookie-deletion bug, captured on a public PR) as each tone
would post it — identical severity/evidence/confidence underneath:

- **`terse`** *(default)* — `**Issue (non-blocking, correctness):** popitem removes duplicate-name cookies` followed by the structured `**Impact:** … **Evidence:** … **Fix:** … **Confidence:** 0.99` block.
- **`collaborative`** — "Heads up — this removes by name only, so if the jar has `sid` for `a.example` and `b.example`, one `popitem()` returns one pair but deletes both cookies. Probably worth clearing the specific cookie using its domain/path/name."
- **`socratic`** — "What happens here when the jar has the same cookie name for two domains? `del self[name]` goes through `remove_cookie_by_name` without domain/path, so this removes every matching cookie while returning only one pair — should we clear the selected cookie by domain/path/name instead?"
- **`formal`** — "When multiple domains contain the same cookie name, this deletes by name only and removes every matching cookie while returning a single pair. Recommend clearing the specific cookie selected by `popitem` using its domain, path, and name."
- **`casual`** — "Quick one — this deletes by name only, so same-name cookies on other domains/paths get cleared too. Grab the Cookie from the iterator and clear that exact domain/path/name."

## Surface mode: `gate` vs `collaborate`

`[review].mode` is a **precision/recall lever** — it chooses *which* findings
reach the poster, orthogonal to `tone` (which only changes how a surfaced
finding reads). It is **off by default** (`collaborate`), and the review prompt
always emits every finding type regardless of mode — `mode` only filters what
gets posted.

| mode | surfaces | use it for |
|---|---|---|
| `collaborate` *(default)* | bubo's full typed output — `issue`, **and** the deliberate `suggestion`/`question` collaborative modes — across every category | human-in-the-loop review, where a well-placed question or suggestion is a feature, not noise |
| `gate` | **only** blocking-severity *defect* findings: `type = issue`, severity in `blocking`/`high`/`critical`, and a **canonical category** in the defect set | a CI / merge-blocking bot that must keep false positives near zero |

`gate` raises precision at the cost of recall (a known trade-off — high-velocity
teams target a <5% false-positive rate, while enterprises still want ≥90%
recall), which is exactly why it is opt-in rather than the default.

**Why a mode, not just `allowed_kinds`.** `allowed_kinds` keeps a finding if
*any one* of its severity/category/type is whitelisted (an OR). `gate` is a
*conjunction* — assertion type **and** blocking severity **and** defect category
— which an OR-list cannot express. The two are independent filters: set both and
a finding must satisfy both.

### The canonical category taxonomy

Models emit wildly inconsistent `category` strings (`logic` vs `code-logic`,
`test`/`testing`/`test-coverage`, `documentation` vs `docs`), so `gate` first
normalizes each finding's category onto a fixed, orthogonal taxonomy before
deciding. The normalized value lives in a separate `category_canonical` field —
your original label is preserved verbatim in the posted body, the dedup
fingerprint, and the audit row, so turning `gate` on never re-posts or
re-fingerprints an existing finding.

- **Defect** (surfaced by `gate`): `correctness` · `security` · `concurrency` ·
  `resource` · `error_handling` · `performance`
- **Non-defect** (dropped by `gate`, kept by `collaborate`): `style` · `docs` ·
  `test` · `design` · `naming` · `other`

Unrecognized labels map to `other` (a non-defect bucket), so an unknown category
is never silently promoted into the merge gate. Synonyms are folded
automatically — including the review contract's own enum
(`failure`→`error_handling`, `compatibility`→`correctness`,
`maintainability`→`design`, `documentation`→`docs`).

**On severity.** The contract asks for `blocking`/`non-blocking`, but real models
also emit `high`/`critical`; `gate` accepts those so a severe defect is not
silently dropped. A finding with no explicit severity does **not** pass the gate
— a merge gate should block only on a clear high-severity signal.

> **Audit note.** Dispute-driven suppression (below) still keys on the *raw*
> `category`, by design: its job is to learn which labels *your team* rejects, so
> it must see the model's own words, not the normalized bucket.

## Calibrated per-class confidence

The global `min_confidence` (0.85) applies one bar to every finding. But a
self-reported 0.9 means very different things for a `correctness` claim versus a
`style` nit — weak models are reliably *overconfident* on trivia (the empirical
run carried a `Missing Semicolon` at 0.9 and an `Unsigned Comparison` at 1.0).
`[review].category_min_confidence` lets you set a **higher bar for noise-prone
classes** without touching the rest:

```toml
[review.category_min_confidence]
style = 0.95
design = 0.95
performance = 0.92
# correctness / security keep the global 0.85
```

A finding must clear `max(min_confidence, its-category-floor)`; one that clears
the global bar but falls under its category floor is dropped as
`confidence_below_category_floor` (distinct from the global
`confidence_below_threshold`, so you can see the per-class gate fire). Keys are
**canonical** categories (see [Surface mode](#surface-mode-gate-vs-collaborate)
for the taxonomy); an unknown key is a config error, not a silent no-op. A floor
**only ever raises** the bar — one set below `min_confidence` is ignored.

### Auto-calibration from dispute history

Setting the floors by hand is the deterministic path. With
`calibrate_confidence = true`, Bubo additionally **derives** them from this
repo's own accept/dispute signal: a category the team disputes more earns a
higher floor, linearly from `min_confidence` (0% disputed) up to
`calibrate_max_confidence` (100% disputed), once a category has at least
`dispute_suppress_min_samples` resolved outcomes. Manual
`category_min_confidence` entries win over a derived floor for the same category.

Two deliberate design points:

- **Calibration aggregates on the _canonical_ category**, even though
  dispute-*suppression* keys on the raw label. That split is intentional:
  suppression learns the exact labels your team rejects; calibration needs the
  dispute signal *un-fragmented*, or `test` / `testing` / `test-coverage` each
  look too thin to ever clear the sample gate. The canonical fold sums them.
- **Calibration is gentler than suppression.** Suppression stops a class from
  posting at all; calibration only raises its confidence bar (capped below 1.0),
  so a genuinely high-confidence finding in a disputed class still surfaces.

Like suppression, calibration is **self-reinforcing** — raising a class's floor
reduces its new outcomes, freezing the rate. The escape hatches are operator-
side: lower `calibrate_max_confidence`, raise `dispute_suppress_min_samples`, or
turn calibration off. It is **off by default**.

## Dispute-driven suppression

Bubo records, per finding, whether the developer accepted or disputed it — the
accept/dispute signal kept in the `finding_outcomes` table and surfaced by `bubo
--sync-outcomes`. **Dispute-driven suppression** turns that history into a
precision lever: when enabled, the poller stops posting finding `category`
classes a team has *repeatedly rejected on a given repo*, instead of
re-litigating the same noise on every merge request.

It is **off by default** (`[review].suppress_disputed_classes = false`). Reach
for it only after you've built up outcome history and watched a specific
category — say `documentation` or `maintainability` — generate persistent,
dismissed noise. The per-finding `min_confidence` and `allowed_kinds` filters are
the first-line controls; this is a sharper, repo-learned complement.

It's **conservative by design** and does nothing on a fresh install. Even with
the flag on, no category is suppressed until enough outcomes accrue to clear both
`dispute_suppress_min_samples` and `dispute_suppress_threshold` — so enabling it
early can't silence findings off a thin signal. It stays inert until the data
earns a suppression, which is the right behavior for a noise filter.

### How a category gets suppressed

For each repo, bubo joins `finding_outcomes` to `review_findings` and groups
by normalized `category`. A category is suppressed when **both** hold:

- at least `dispute_suppress_min_samples` (default `5`) of its findings have
  a recorded outcome, **and**
- the dispute rate — `disputed-or-false-positive ÷ total outcomes` — is at
  or above `dispute_suppress_threshold` (default `0.5`).

The denominator is *all* outcome rows for the category, including rows written
when an outcome-sync check failed (which count as not-disputed). That
deliberately dilutes the rate: the bias is toward **under**-suppressing, so a
genuinely useful class is never silenced off a thin or noisy signal. Suppressed
findings are dropped before posting and logged with reason
`disputed_class_suppressed` (visible in the JSON log stream and the
`finding_filtered` event), so you can always see what got swallowed.

### Known limitation: suppression is self-reinforcing

Once a category is suppressed, Bubo stops posting it, so no new `review_findings`
/ `finding_outcomes` rows accrue for it. Its dispute rate is therefore **frozen**
at the snapshot that crossed the threshold — there's no organic recovery path if
the team later starts caring about that class again. This is within the feature's
intent (a class the team keeps rejecting should stay gone), but it's a conscious
trade-off, not an accident. The escape hatches are all operator-side:

- raise `dispute_suppress_threshold` or `dispute_suppress_min_samples`, or
- set `suppress_disputed_classes = false` to re-post everything and rebuild
  fresh outcome history.

## Verification before posting

**Off by default.** When `verify_findings = true`, every finding about to be
posted is first re-checked by N independent "is this real?" lenses; a finding a
*majority refute* is dropped (recorded `refuted`) instead of posted. Each lens
re-asks a verifier to **try to refute** the finding from a different angle —
`correctness` (is it a real defect?), `in_diff` (does the cited line do what the
finding claims?), and `reproduce` (can the failure actually occur?) — and answers
with a strict JSON verdict `{"real": ..., "confidence": ..., "reason": ...}`. A
finding survives only if at least `verify_min_votes` lenses vote it real at or
above `verify_confidence_floor`.

The contract is deliberately **conservative**: an unparseable answer, a missing
`real` flag, or a missing/garbled `confidence` resolves to *not real* / zero
confidence, so uncertainty never sneaks a finding past the floor.

### Honesty: this is the weaker, correlated form by default

Stated plainly: with the default `verify_command` (empty → it reuses
`[agents].reviewer_command`), verification re-asks **the same model** with
different lens prompts. That shares the original finding's blind spots — the
**weaker, correlated** form of pre-post verification. The strong form the
literature credits with ~94–98% false-positive elimination is either a **hybrid
static-analysis + LLM** check or a **genuinely different model**. The real
diversity payoff lives in pointing `verify_command` at a different model:

```toml
[review]
verify_findings = true
# Reviewer is Codex (the default); verify with a different model for real
# independence.
verify_command = ["claude", "-p"]
```

Bubo never labels self-refutation as hard-proven "verified" anywhere in the
audit trail or telemetry — a survived finding is recorded as **not refuted** (the
`verified` column is `1`), and the per-lens vote tally is persisted in
`verify_votes` so you can see exactly what each lens said.

### Cost and the guardrails

Verification can fan out to up to `verify_max_findings × len(verify_lenses)`
extra **serial** LLM calls per change. That cost is why the feature is off by
default and why it's fenced in:

- It runs only on **in-diff, non-duplicate, post-filter survivors** — the
  findings actually about to be posted. Out-of-diff and policy-dropped
  findings are never verified, so no calls are wasted on findings that would
  be skipped anyway.
- `verify_max_findings` caps how many findings are verified per change.
  Findings past the cap **post unverified** (and the count is logged) —
  the cap never silently suppresses a real finding.
- Each check gets its own `verify_timeout_seconds` wall-clock budget.
- A check that times out, errors, or returns unparseable output is treated
  as **failed to run**, not as a refutation — so a verifier outage can never
  silently drop every finding. When no lens runs, the finding posts
  unverified and the event is logged.
- The per-change `verified` / `refuted` / `capped` counts are emitted as a
  `verification_summary` log line — there are **no silent caps**.
- Verification **re-runs each poll cycle** for findings not yet `posted`
  (refuted findings, and everything in dry-run, are re-verified at the same
  SHA until the SHA changes), so `verify_max_findings` is a per-cycle cap and
  you re-pay the cost each interval until the change moves on. This mirrors
  how dry-run already re-plans findings every cycle.

The verifier keys (`verify_findings`, `verify_lenses`, `verify_min_votes`,
`verify_confidence_floor`, `verify_max_findings`, `verify_timeout_seconds`,
`verify_command`) and their defaults are listed in the [`[review]`](#review)
table above.

## Governance & provenance

For regulated and enterprise teams whose governance functions must **approve and
monitor** AI-assisted code, Bubo can capture a per-change **provenance signal**
and keep it on-prem. There's no third party to ship code or audit data to —
the whole compliance pitch of a self-hosted, BYO-LLM code reviewer.

This is **off by default** (`[governance].capture_provenance = false`) and, in
its current phase, **captures only** — it doesn't change which findings post,
raise severities, or block anything. Rigor modulation and policy gates are a
later, separately opt-in phase.

To consume the captured provenance and governance decisions as auditable
metrics — a provenance breakdown, accept-vs-dispute rate, noise trend, ROI
proxy, **review latency** (p50/p95/max/avg seconds over the window),
**per-category dispute rates**, **no-findings acknowledgements**,
token/cost rollups, policy-decision stats, and a per-change audit trail —
use the read-only `bubo report` command (and the matching
`get_governance_report` MCP tool). See [operate.md](operate.md),
"Governance report".

The report surfaces the same dispute history that drives
[dispute-driven suppression](#dispute-driven-suppression), so an operator
can *see* the feedback loop, not just have it act silently:

- The `dispute_classes` section lists every finding category with recorded
  outcomes for the project as `{category, total, rejected, dispute_rate}`,
  ordered by dispute rate. These are the **raw, config-independent** stats.
- The dedicated `get_dispute_classes(project)` MCP tool additionally reads
  your real `dispute_suppress_threshold` / `dispute_suppress_min_samples`
  from `[review]` and adds a truthful `would_suppress` flag per category —
  i.e. whether the class *would* be suppressed **if** suppression were
  enabled. (The flag never uses hardcoded thresholds, so it cannot
  misreport what your config would actually do; if the config can't be
  read it falls back to raw stats with no flag.) The `bubo report` CLI path
  emits the raw stats only.
- The `latency` section reports wall-clock review-run latency
  (`finished_at - started_at`) over the window. The `acknowledgements`
  rollup nested in `reviews` makes the `{no_findings, success, failed}`
  status counts first-class, mirroring `reviews.by_status`.

### How capture works

Bubo already checks out a change before reviewing it, so it reads the change's
**commit trailers** and records a *banded* signal onto the review run. The
category is derived from what the commits **declare** — e.g. a `Co-authored-by:
Claude` trailer, or an `AI-assisted` trailer. Patterns match per commit-message
line and the built-in defaults are anchored to trailer shape, so a prose body
that merely mentions AI isn't read as a declaration. The result is one row per
change with `band`, `source`, `confidence`, the matched declaration lines (for
the audit trail), and any matched `sensitive_path_globs`.

### Two honesty rules (deliberate)

- **A band, never a verdict.** `band` is one of `unknown` / `likely_ai` /
  `collaborative`. Bubo never emits a binary "this is AI / this is human" label —
  post-hoc detection is fragile cross-domain, so a banded signal is the honest
  shape.
- **Declared ≠ detected, and `unknown` is the default.** The absence of an AI
  declaration is **not** proof of human authorship, so a change with no signal is
  `unknown`, never `human`. The `source` field (`trailer` vs `detection`) records
  *which kind* of evidence produced the band. This phase is **deterministic
  (`trailer`) only**; LLM-based AI *detection* is deliberately deferred and would
  surface as `source = detection` — distinct, so an auditor always knows whether
  they're looking at a declaration or a guess.

### Audit integrity

Provenance is computed once per run and persisted **write-once** — no code path
retroactively rewrites it. The signal also appears in the JSON log stream as a
`provenance_captured` event and (when telemetry is on) as the
`llm_review.provenance` metric, labelled by band and source.

### Capability boundary (read this)

Bubo **cannot block a merge** — it doesn't own CI or branch protection. It
produces auditable data and an advisory signal; acting on that signal (required
approvals, merge gates) stays with your pipeline. Capture is the foundation that
makes those downstream gates *possible*, not a gate itself.

### Phase 2 — rigor modulation & policy gates (opt-in)

Once provenance is captured, two opt-in capabilities *use* it. Both are
**advisory** and off by default.

A change **escalates** when its band is in `escalate_bands` (default
`likely_ai` + `collaborative`; `unknown` never escalates) and — unless
`rigor_require_sensitive = false` — it also touches a `sensitive_path_globs`
path. This single predicate is shared by both capabilities, so they always
agree.

- **Rigor modulation** (`rigor_modulation = true`): an escalated change injects
  a heightened-scrutiny directive (prioritize the security lens) into *that
  change's* review prompt. It's prompt context, never a verdict. (Honest
  limitation: whether the directive measurably improves the review is LLM
  behavior — Bubo guarantees the directive is *injected*, not that the model acts
  on it.)
- **Policy gate** (`policy_mode = report-only | soft`): records an auditable,
  **write-once** governance *decision* per change — `action` is `flag` (escalated)
  or `clear` — surfaced as a `governance_decision` log event and the
  `llm_review.governance` metric, and queryable for reporting. There's no
  `enforce` mode: every mode is advisory because Bubo cannot block a merge.

Turning on either capability auto-implies the provenance fetch even if
`capture_provenance = false` — the per-change commit read is what all three
share.

## Quick-start config

A minimal `config/env.toml` to get a first review running — GitLab, dry-run on,
one project. Copy it, fill in the two tokens, then `bubo doctor` and
`bubo-poller`. Every key not shown falls back to the defaults documented above.

```toml
# config/env.toml — minimal GitLab setup. Secrets via ${ENV}; nothing real on disk.
[scm]
provider = "gitlab"            # set "github" and use a [github] block instead for PRs

[gitlab]
token = "${GITLAB_TOKEN}"      # personal-access token, api scope

[review]
dry_run = true                 # plan only; flip to false once a real review looks right

[agents]
llm_model       = "gpt-5.5"
llm_api_key     = "${LLM_API_KEY}"
llm_api_key_env = "OPENAI_API_KEY"   # ANTHROPIC_API_KEY for Claude, etc.

[[projects]]
path    = "group/repo"
enabled = true
```
