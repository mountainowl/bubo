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
      <td>Dry-run default for the manual <code>bubo-codex</code> wrapper, separate from <code>[review].dry_run</code> which controls poller posting.</td>
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
