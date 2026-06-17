<script>
  import Card from "../components/Card.svelte";
  import Pill from "../components/Pill.svelte";
  import Timeline from "../components/Timeline.svelte";
  import { renderMarkdown } from "../lib/markdown.js";
  import {
    usd,
    num,
    ago,
    shortSha,
    statusTone,
    outcomeLabel,
    outcomeTone,
  } from "../lib/format.js";

  let { review, onback } = $props();

  const detail = $derived(review.detail ?? {});
  const findings = $derived(detail.findings ?? []);
  const run = $derived(detail.run);
  const governance = $derived(detail.governance ?? []);

  function provenanceTone(band) {
    if (band === "likely_ai") return "warn";
    if (band === "collaborative") return "info";
    return "muted";
  }

  function confidencePct(c) {
    return c == null ? "—" : `${Math.round(Number(c) * 100)}%`;
  }
</script>

<div class="space-y-4">
  <button class="text-sm text-brand hover:underline" onclick={onback}>← back to reviews</button>

  <!-- The change -->
  <div class="flex flex-wrap items-center gap-3">
    <Pill tone={statusTone(review.status)}>{review.status}</Pill>
    <h1 class="text-lg font-semibold">{review.project} <span class="text-muted">!{review.iid}</span></h1>
    <span class="mono text-sm text-muted">{shortSha(review.sha)}</span>
    <span class="ml-auto text-xs text-muted">{ago(review.updated_at)}</span>
  </div>

  {#if review.error}
    <Card title="Error">
      <pre class="mono text-xs text-danger whitespace-pre-wrap">{review.error}</pre>
    </Card>
  {/if}

  <!-- Run summary: provenance band, tokens/cost -->
  <div class="grid gap-4 md:grid-cols-2">
    <Card title="Provenance & cost">
      {#if run}
        <div class="flex flex-wrap items-center gap-2 text-sm">
          {#if run.provenance_band}
            <Pill tone={provenanceTone(run.provenance_band)}>{run.provenance_band}</Pill>
            <span class="text-muted">via {run.provenance_source ?? "?"}</span>
          {:else}
            <span class="text-muted">no provenance recorded</span>
          {/if}
        </div>
        <div class="mt-3 grid grid-cols-2 gap-3 text-sm">
          <div><span class="text-muted">tokens</span> · {num(run.tokens_total)}</div>
          <div><span class="text-muted">cost</span> · {usd(run.cost_usd)}</div>
          <div><span class="text-muted">model</span> · {run.model ?? "—"}</div>
          <div><span class="text-muted">mode</span> · {run.review_mode ?? "—"}</div>
        </div>
        {#if run.sensitive_paths_count > 0}
          <p class="mt-2 text-xs text-warn">{run.sensitive_paths_count} sensitive path(s) touched</p>
        {/if}
      {:else}
        <p class="text-sm text-muted">No run telemetry recorded for this revision.</p>
      {/if}
    </Card>

    <Card title="Stage timeline">
      <Timeline started_at={run?.started_at} finished_at={run?.finished_at} />
    </Card>
  </div>

  {#if governance.length}
    <Card title="Governance decisions">
      <ul class="space-y-1 text-sm">
        {#each governance as g (g.run_id)}
          <li class="flex items-center gap-2">
            <Pill tone={g.triggered ? "warn" : "muted"}>{g.action}</Pill>
            <span class="text-muted">{g.mode}</span>
            {#if g.reason}<span class="text-xs text-muted">— {g.reason}</span>{/if}
          </li>
        {/each}
      </ul>
    </Card>
  {/if}

  <!-- Findings -->
  <Card title={`Findings (${findings.length})`}>
    {#if findings.length === 0}
      <p class="text-sm text-muted">No findings recorded for this review.</p>
    {:else}
      <ul class="space-y-4">
        {#each findings as f (f.fingerprint)}
          <li class="rounded-[var(--radius-token)] border border-border p-3">
            <div class="mb-2 flex flex-wrap items-center gap-2 text-xs">
              {#if f.severity}<Pill tone={f.severity === "blocking" ? "danger" : "muted"}>{f.severity}</Pill>{/if}
              {#if f.category}<Pill tone="brand">{f.category}</Pill>{/if}
              <Pill tone={outcomeTone(f.outcome)}>{outcomeLabel(f.outcome)}</Pill>
              <span class="text-muted">confidence {confidencePct(f.confidence)}</span>
              {#if f.file}
                <span class="mono text-muted ml-auto">{f.file}{f.line ? `:${f.line}` : ""}</span>
              {/if}
            </div>
            <!-- Issue/Impact/Evidence/Fix live in the rendered markdown body. -->
            <div class="prose-bubo text-sm leading-relaxed">{@html renderMarkdown(f.body)}</div>
          </li>
        {/each}
      </ul>
    {/if}
  </Card>
</div>
