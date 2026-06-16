# Configuration reference

Public defaults live in [`config/env.example.toml`](https://github.com/mountainowl/bubo/blob/main/config/env.example.toml).
Copy it to ignored `config/env.toml` before running. Runtime config and
credentials live in that one TOML file.

<table>
  <thead>
    <tr>
      <th>Setting</th>
      <th>Default</th>
      <th>Purpose / impact</th>
    </tr>
  </thead>
  <tbody>
    <tr><th colspan="3"><code>[scm]</code></th></tr>
    <tr>
      <td><code>provider</code></td>
      <td><code>gitlab</code></td>
      <td>Source-control backend: <code>gitlab</code> or <code>github</code>. Selects which provider the poller drives. <code>BUBO_PROVIDER=github</code> overrides it for a single run.</td>
    </tr>
    <tr><th colspan="3"><code>[gitlab]</code></th></tr>
    <tr>
      <td><code>url</code></td>
      <td><code>https://gitlab.com</code></td>
      <td>Web host the poller reads MRs from. For self-hosted GitLab, keep <code>api_url</code> on the same host.</td>
    </tr>
    <tr>
      <td><code>api_url</code></td>
      <td><code>https://gitlab.com/api/v4</code></td>
      <td>API endpoint used by MCP tools inside the review agent.</td>
    </tr>
    <tr>
      <td><code>bot_username</code></td>
      <td><code>bubo</code></td>
      <td>Lets outcome sync separate bot comments from developer replies.</td>
    </tr>
    <tr>
      <td><code>denied_tools_regex</code></td>
      <td><code>^(delete_.*|merge_merge_request|push_files)$</code></td>
      <td>Blocks dangerous GitLab MCP tools even if the agent can see them.</td>
    </tr>
    <tr>
      <td><code>token</code></td>
      <td>unset</td>
      <td>GitLab token with <code>api</code> scope. Exported as <code>GITLAB_TOKEN</code>, <code>GITLAB_PERSONAL_ACCESS_TOKEN</code>, and <code>GLAB_TOKEN</code>.</td>
    </tr>
    <tr><th colspan="3"><code>[github]</code></th></tr>
    <tr>
      <td><code>api_url</code></td>
      <td><code>https://api.github.com</code></td>
      <td>REST API base. Use <code>https://&lt;host&gt;/api/v3</code> for GitHub Enterprise Server.</td>
    </tr>
    <tr>
      <td><code>bot_username</code></td>
      <td><code>bubo</code></td>
      <td>Lets outcome sync separate bot comments from developer replies.</td>
    </tr>
    <tr>
      <td><code>token</code></td>
      <td>unset</td>
      <td>GitHub token with pull-request read+write. Exported as <code>GITHUB_TOKEN</code>, <code>GITHUB_PERSONAL_ACCESS_TOKEN</code>, and <code>GH_TOKEN</code>.</td>
    </tr>
    <tr><th colspan="3"><code>[review]</code></th></tr>
    <tr>
      <td><code>dry_run</code></td>
      <td><code>true</code></td>
      <td>Stores planned findings without posting comments. Set <code>false</code> after test reviews look right.</td>
    </tr>
    <tr>
      <td><code>max_merge_requests_per_poll</code></td>
      <td><code>8</code></td>
      <td>Caps how many MRs one poll cycle queues. Higher values can fork more workers at once.</td>
    </tr>
    <tr>
      <td><code>max_findings_per_merge_request</code></td>
      <td><code>8</code></td>
      <td>Caps findings per MR and fills <code>{{MAX_FINDINGS_PER_REVIEW}}</code> in the prompt.</td>
    </tr>
    <tr>
      <td><code>timeout_seconds</code></td>
      <td><code>1800</code></td>
      <td>Kills a review worker that runs too long.</td>
    </tr>
    <tr>
      <td><code>min_confidence</code></td>
      <td><code>0.85</code></td>
      <td>Floor for the LLM's per-finding confidence (0.0–1.0). Findings below this score are dropped before posting or planning. Inclusive on the high side.</td>
    </tr>
    <tr>
      <td><code>allowed_kinds</code></td>
      <td><code>[]</code></td>
      <td>Whitelist of finding kinds to post. A finding is kept if its <code>severity</code>, <code>category</code>, or <code>type</code> appears here (case-insensitive). Empty list = no kind filter — post everything that clears <code>min_confidence</code>. Common values: <code>"blocking"</code>, <code>"non-blocking"</code>, <code>"security"</code>, <code>"correctness"</code>, <code>"performance"</code>, <code>"issue"</code>, <code>"suggestion"</code>.</td>
    </tr>
    <tr>
      <td><code>tone</code></td>
      <td><code>"terse"</code></td>
      <td>Review-comment voice ("mood"): <code>terse</code> (default) / <code>collaborative</code> / <code>socratic</code> / <code>formal</code> / <code>casual</code>. Affects ONLY how a posted finding reads. <code>terse</code> posts the structured Impact/Evidence/Fix render unchanged; other tones ask the reviewer for an in-voice <code>comment</code> field and post that instead. The structured fields and the dedup fingerprint are identical across tones, so switching never re-posts a finding or splits its outcome history. See <a href="#review-comment-tone-moods">Review-comment tone</a> below.</td>
    </tr>
    <tr>
      <td><code>suppress_disputed_classes</code></td>
      <td><code>false</code></td>
      <td>Opt-in. When <code>true</code>, drop finding <code>category</code> classes this repo has repeatedly rejected, using the accept/dispute outcome history. Off by default. See <a href="#dispute-driven-suppression">Dispute-driven suppression</a> below.</td>
    </tr>
    <tr>
      <td><code>dispute_suppress_threshold</code></td>
      <td><code>0.5</code></td>
      <td>Dispute rate (0.0–1.0) at or above which a category is suppressed. Only consulted when <code>suppress_disputed_classes = true</code>.</td>
    </tr>
    <tr>
      <td><code>dispute_suppress_min_samples</code></td>
      <td><code>5</code></td>
      <td>Minimum recorded outcomes in a category before its dispute rate is acted on. Only consulted when <code>suppress_disputed_classes = true</code>.</td>
    </tr>
    <tr><th colspan="3"><code>[governance]</code></th></tr>
    <tr>
      <td><code>capture_provenance</code></td>
      <td><code>false</code></td>
      <td>Opt-in. When <code>true</code>, record a banded AI-provenance signal per change for audit. Captures only — no review-behavior change. See <a href="#governance-provenance">Governance &amp; provenance</a> below.</td>
    </tr>
    <tr>
      <td><code>ai_trailer_patterns</code></td>
      <td>built-in</td>
      <td>Case-insensitive regexes matched against commit-message lines to detect <em>declared</em> AI assistance. Defaults match <code>Generated-by</code>/<code>AI-assisted</code> trailers and <code>Co-authored-by</code> naming a known agent. Only consulted when <code>capture_provenance = true</code>.</td>
    </tr>
    <tr>
      <td><code>sensitive_path_globs</code></td>
      <td><code>[]</code></td>
      <td><code>fnmatch</code> globs (case-preserving) flagged when a change touches sensitive paths, e.g. <code>payments/**</code>, <code>*.pem</code>. Recorded for audit and used as the sensitive-path half of the escalation predicate.</td>
    </tr>
    <tr>
      <td><code>rigor_modulation</code></td>
      <td><code>false</code></td>
      <td>Opt-in (Phase 2). When <code>true</code>, an escalated change injects a heightened-scrutiny directive into its review prompt. Advisory — adds prompt context, never a verdict or merge block. Auto-implies the provenance fetch.</td>
    </tr>
    <tr>
      <td><code>escalate_bands</code></td>
      <td><code>["likely_ai","collaborative"]</code></td>
      <td>Provenance bands that escalate (shared by rigor modulation and the policy gate). <code>unknown</code> never escalates.</td>
    </tr>
    <tr>
      <td><code>rigor_require_sensitive</code></td>
      <td><code>true</code></td>
      <td>When <code>true</code>, a change escalates only if it also touches a <code>sensitive_path_globs</code> path; <code>false</code> escalates on band alone.</td>
    </tr>
    <tr>
      <td><code>policy_mode</code></td>
      <td><code>off</code></td>
      <td><code>off</code> / <code>report-only</code> / <code>soft</code>. When not <code>off</code>, an auditable governance decision is recorded per change. All modes advisory — no <code>enforce</code> (bubo does not own CI/branch protection).</td>
    </tr>
    <tr><th colspan="3"><code>[poller]</code></th></tr>
    <tr>
      <td><code>state_dir</code></td>
      <td><code>var</code></td>
      <td>Stores SQLite state, logs, reports, worktrees, and rendered prompts.</td>
    </tr>
    <tr>
      <td><code>interval_seconds</code></td>
      <td><code>900</code></td>
      <td>Suggested wait for long-running poll loops. Cron/systemd can use another interval.</td>
    </tr>
    <tr>
      <td><code>target_merge_request_iid</code></td>
      <td>unset</td>
      <td>Temporary single-MR filter. Leave unset in production.</td>
    </tr>
    <tr><th colspan="3"><code>[agents]</code></th></tr>
    <tr>
      <td><code>prompt_file</code></td>
      <td><code>prompts/00-meta.md</code></td>
      <td>Meta prompt rendered before each review.</td>
    </tr>
    <tr>
      <td><code>llm_model</code></td>
      <td><code>gpt-5.5</code></td>
      <td>Model passed to the review wrapper. Keep telemetry pricing aligned for cost metrics.</td>
    </tr>
    <tr>
      <td><code>llm_api_key</code></td>
      <td>unset</td>
      <td>API key for whatever LLM you review with. Exported as the generic <code>LLM_API_KEY</code> plus the operator-named variable in <code>llm_api_key_env</code>.</td>
    </tr>
    <tr>
      <td><code>llm_api_key_env</code></td>
      <td><code>OPENAI_API_KEY</code></td>
      <td>The env-var name your LLM CLI/SDK reads the key from — Bubo is model-agnostic and does not guess it. Set to <code>OPENAI_API_KEY</code> (OpenAI/Codex), <code>ANTHROPIC_API_KEY</code> (Claude), <code>GEMINI_API_KEY</code> (Gemini), or whatever your CLI expects. Blank exports only <code>LLM_API_KEY</code>.</td>
    </tr>
    <tr>
      <td><code>reasoning_effort</code></td>
      <td><code>medium</code></td>
      <td>Review reasoning level. Higher values can cost more and run longer.</td>
    </tr>
    <tr>
      <td><code>dry_run</code></td>
      <td><code>true</code></td>
      <td>Dry-run hint exported to the review agent as <code>REVIEW_DRY_RUN</code>, separate from <code>[review].dry_run</code> which controls poller posting.</td>
    </tr>
    <tr>
      <td><code>codex_profile</code></td>
      <td><code>bubo</code></td>
      <td>Codex profile used by the Codex wrapper.</td>
    </tr>
    <tr>
      <td><code>codex_sandbox</code></td>
      <td><code>read-only</code></td>
      <td>Filesystem access passed to Codex review runs.</td>
    </tr>
    <tr>
      <td><code>post_no_findings_comment</code></td>
      <td><code>true</code></td>
      <td>When a review finishes with zero findings, post a single change-level acknowledgement so authors and approvers can tell <em>reviewer ran and passed</em> from <em>reviewer never ran</em>. Dedup'd by bot author + exact body; honors <code>[review].dry_run</code>; a post failure is a soft error that does NOT flip the review to <code>FAILED</code>. Set <code>false</code> to restore the previous silent-on-clean behavior.</td>
    </tr>
    <tr>
      <td><code>no_findings_comment_body</code></td>
      <td><code>"Automated review ran — no issues found."</code></td>
      <td>Body of the acknowledgement, posted verbatim. Customize for localization or branding. Do NOT embed per-run values (URLs, timestamps) — the body must be byte-identical across re-reviews for dedup to work. Empty/whitespace-only disables posting.</td>
    </tr>
    <tr><th colspan="3"><code>[telemetry]</code></th></tr>
    <tr>
      <td><code>enabled</code></td>
      <td><code>false</code></td>
      <td>Sends OTel metrics and spans when enabled. SQLite state is still written either way.</td>
    </tr>
    <tr>
      <td><code>service_name</code></td>
      <td><code>bubo</code></td>
      <td>Service name shown in the OTel backend.</td>
    </tr>
    <tr>
      <td><code>environment</code></td>
      <td><code>prod</code></td>
      <td>Environment label for dashboards, such as <code>dev</code>, <code>staging</code>, or <code>prod</code>.</td>
    </tr>
    <tr>
      <td><code>otlp_endpoint</code></td>
      <td><code>http://127.0.0.1:4317</code></td>
      <td>Collector endpoint for metrics and traces.</td>
    </tr>
    <tr>
      <td><code>otlp_protocol</code></td>
      <td><code>grpc</code></td>
      <td>OTLP transport. Only <code>grpc</code> is supported today.</td>
    </tr>
    <tr>
      <td><code>export_interval_seconds</code></td>
      <td><code>30</code></td>
      <td>Metric export interval. Lower values make dashboards fresher.</td>
    </tr>
    <tr>
      <td><code>emit_finding_events</code></td>
      <td><code>true</code></td>
      <td>Emits finding lifecycle metrics like planned, posted, skipped, and resolved.</td>
    </tr>
    <tr>
      <td><code>emit_outcome_sync</code></td>
      <td><code>true</code></td>
      <td>Emits metrics when outcome sync checks posted finding status.</td>
    </tr>
    <tr>
      <td><code>input_per_1m</code></td>
      <td><code>5.0</code></td>
      <td>Estimated input-token price per million tokens for cost metrics.</td>
    </tr>
    <tr>
      <td><code>output_per_1m</code></td>
      <td><code>30.0</code></td>
      <td>Estimated output-token price per million tokens for cost metrics.</td>
    </tr>
    <tr>
      <td><code>cached_input_per_1m</code></td>
      <td><code>0.5</code></td>
      <td>Estimated cached-input price per million tokens for cost metrics.</td>
    </tr>
    <tr><th colspan="3"><code>[[projects]]</code></th></tr>
    <tr>
      <td><code>path</code></td>
      <td>sample repos</td>
      <td>GitLab project path to poll, for example <code>group/repo</code>.</td>
    </tr>
    <tr>
      <td><code>enabled</code></td>
      <td><code>true</code></td>
      <td>Turns polling for that project on or off.</td>
    </tr>
  </tbody>
</table>

## Review-comment tone (moods)

`[review].tone` chooses the *voice* of a posted finding. It is purely a
presentation choice and is left entirely to the operator — bubo ships with
`terse` so behavior is unchanged unless you opt in.

| tone | reads like |
| --- | --- |
| `terse` *(default)* | the structured `**Impact:** … **Evidence:** … **Fix:** …` render — byte-identical to bubo before this knob existed |
| `collaborative` | a senior engineer's inline note — acknowledges intent, gives a concrete example, suggests the fix |
| `socratic` | leads with a question that surfaces the gap and invites confirmation |
| `formal` | measured, complete sentences, no contractions — for regulated/enterprise review |
| `casual` | relaxed and brief |

**How it works.** For any non-`terse` tone, bubo injects a short *voice
directive* into the review prompt (a register description plus one
cross-domain style example) asking the reviewer to add a single in-voice
`comment` field per finding. bubo posts that `comment` in place of the
structured render. This costs a few extra output tokens per finding.

**What never changes with tone.** The structured fields
(`title`/`impact`/`evidence`/`fix`/`confidence`) are still recorded to SQLite
exactly as in `terse` mode, and the dedup **fingerprint is computed from those
fields, not the voiced comment**. So you can change tone at any time without
re-posting existing findings, splitting their accept/dispute history, or
disturbing the governance dataset — only the words developers read change.

### The same finding in each tone

One real finding (a cookie-deletion bug, captured on a public PR) as each tone
would post it — identical severity/evidence/confidence underneath:

- **`terse`** *(default)* — `**Issue (non-blocking, correctness):** popitem removes duplicate-name cookies` followed by the structured `**Impact:** … **Evidence:** … **Fix:** … **Confidence:** 0.99` block.
- **`collaborative`** — "Heads up — this removes by name only, so if the jar has `sid` for `a.example` and `b.example`, one `popitem()` returns one pair but deletes both cookies. Probably worth clearing the specific cookie using its domain/path/name."
- **`socratic`** — "What happens here when the jar has the same cookie name for two domains? `del self[name]` goes through `remove_cookie_by_name` without domain/path, so this removes every matching cookie while returning only one pair — should we clear the selected cookie by domain/path/name instead?"
- **`formal`** — "When multiple domains contain the same cookie name, this deletes by name only and removes every matching cookie while returning a single pair. Recommend clearing the specific cookie selected by `popitem` using its domain, path, and name."
- **`casual`** — "Quick one — this deletes by name only, so same-name cookies on other domains/paths get cleared too. Grab the Cookie from the iterator and clear that exact domain/path/name."

## Dispute-driven suppression

Bubo records, per finding, whether the developer accepted or disputed it —
the accept/dispute signal kept in the `finding_outcomes` table and surfaced
by `bubo --sync-outcomes`. **Dispute-driven suppression** turns that history
into a precision lever: when enabled, the poller stops posting finding
`category` classes that a team has *repeatedly rejected on a given repo*,
instead of re-litigating the same noise on every merge request.

It is **off by default** (`[review].suppress_disputed_classes = false`).
Reach for it only after you have built up outcome history and have observed
a specific category — say `documentation` or `maintainability` — generating
persistent, dismissed noise. The per-finding `min_confidence` and
`allowed_kinds` filters are the first-line controls; this is a sharper,
repo-learned complement.

The feature is **conservative by design**: it is a precision lever for teams
with accumulated accept/dispute history, and it does nothing on a fresh
install. Even with the flag turned on, no category is suppressed until enough
outcomes accrue to clear both `dispute_suppress_min_samples` and
`dispute_suppress_threshold` — so enabling it early cannot silence findings
off a thin signal. It simply stays inert until the data earns a suppression,
which is the correct, cautious behavior for a noise filter.

### How a category gets suppressed

For each repo, bubo joins `finding_outcomes` to `review_findings` and groups
by normalized `category`. A category is suppressed when **both** hold:

- at least `dispute_suppress_min_samples` (default `5`) of its findings have
  a recorded outcome, **and**
- the dispute rate — `disputed-or-false-positive ÷ total outcomes` — is at
  or above `dispute_suppress_threshold` (default `0.5`).

The denominator is *all* outcome rows for the category, including rows
written when an outcome-sync check failed (which count as not-disputed).
That deliberately dilutes the rate: the bias is toward **under**-suppressing,
so a genuinely useful class is never silenced off a thin or noisy signal.
Suppressed findings are dropped before posting and logged with reason
`disputed_class_suppressed` (visible in the JSON log stream and the
`finding_filtered` event), so an operator can always see what got swallowed.

### Known limitation: suppression is self-reinforcing

Once a category is suppressed, bubo stops posting it, so no new
`review_findings` / `finding_outcomes` rows accrue for that category. Its
dispute rate is therefore **frozen** at the snapshot that crossed the
threshold — there is no organic recovery path if the team later starts
caring about that class again. This is within the feature's intent (a class
the team keeps rejecting should stay gone), but it is a conscious trade-off,
not an accident. The escape hatches are all operator-side:

- raise `dispute_suppress_threshold` or `dispute_suppress_min_samples`, or
- set `suppress_disputed_classes = false` to re-post everything and rebuild
  fresh outcome history.

## Governance & provenance

For regulated and enterprise teams whose governance functions must **approve and
monitor** AI-assisted code, bubo can capture a per-change **provenance signal**
and keep it on-prem — there is no third party to ship code or audit data to,
which is the whole compliance pitch of a self-hosted, BYO-LLM reviewer.

This is **off by default** (`[governance].capture_provenance = false`) and, in
its current phase, **captures only** — it does not change which findings post,
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

bubo already checks out a change before reviewing it, so it reads the change's
**commit trailers** and records a *banded* signal onto the review run. A
category is derived from what the commits **declare** — e.g. a
`Co-authored-by: Claude` trailer, or an `AI-assisted` trailer. Patterns are
matched per commit-message line and the built-in defaults are anchored to
trailer shape, so a prose body that merely mentions AI is not read as a
declaration. The result is one row per change with `band`, `source`,
`confidence`, the matched declaration lines (for the audit trail), and any
matched `sensitive_path_globs`.

### Two honesty rules (deliberate)

- **A band, never a verdict.** `band` is one of `unknown` / `likely_ai` /
  `collaborative`. bubo never emits a binary "this is AI / this is human" label —
  post-hoc detection is fragile cross-domain, so a banded signal is the honest
  shape.
- **Declared ≠ detected, and `unknown` is the default.** The absence of an AI
  declaration is **not** proof of human authorship, so a change with no signal is
  `unknown`, never `human`. The `source` field (`trailer` vs `detection`) records
  *which kind* of evidence produced the band. This phase is **deterministic
  (`trailer`) only**; LLM-based AI *detection* is deliberately deferred and would
  surface as `source = detection` — distinct, so an auditor always knows whether
  they are looking at a declaration or a guess.

### Audit integrity

Provenance is computed once per run and persisted **write-once** — no code path
retroactively rewrites it. The signal also appears in the JSON log stream as a
`provenance_captured` event and (when telemetry is on) as the
`llm_review.provenance` metric, labelled by band and source.

### Capability boundary (read this)

bubo **cannot block a merge** — it does not own CI or branch protection. It
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
  change's* review prompt. It is prompt context, never a verdict. (Honest
  limitation: whether the directive measurably improves the review is LLM
  behavior — bubo guarantees the directive is *injected*, not that the model
  acts on it.)
- **Policy gate** (`policy_mode = report-only | soft`): records an auditable,
  **write-once** governance *decision* per change — `action` is `flag` (escalated)
  or `clear` — surfaced as a `governance_decision` log event and the
  `llm_review.governance` metric, and queryable for reporting. There is no
  `enforce` mode: every mode is advisory because bubo cannot block a merge.

Turning on either capability auto-implies the provenance fetch even if
`capture_provenance = false` — the per-change commit read is what all three
share.
