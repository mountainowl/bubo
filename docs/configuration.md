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
