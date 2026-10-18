<script>
  import MetricSection from "../components/MetricSection.svelte";
  import { tick } from "svelte";
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

  let { review, onback, focusFingerprint = null } = $props();

  $effect(() => {
    const target = focusFingerprint;
    let cancelled = false;
    if (target) tick().then(() => {
      if (cancelled) return;
      const finding = document.getElementById(`finding-${target}`);
      finding?.focus({ preventScroll: true });
      finding?.scrollIntoView({ block: "start", behavior: "instant" });
    });
    return () => { cancelled = true; };
  });

  const detail = $derived(review.detail ?? {});
  const findings = $derived(detail.findings ?? []);
  const run = $derived(detail.run);
  const governance = $derived(detail.governance ?? []);
  let copyStatus = $state('');
  const handoff = $derived([
    `Project: ${review.project}`, `Review: ${review.iid}`, `Revision: ${review.sha}`,
    `Run: ${run?.run_id ?? 'not recorded'}`, `Status: ${review.status}`,
    `Dry run: ${run?.dry_run ?? 'not recorded'}`,
    `Started: ${run?.started_at ?? 'not recorded'}`, `Finished: ${run?.finished_at ?? 'not recorded'}`,
    `Model: ${run?.model ?? 'not recorded'}`,
    `Recorded cost (USD): ${run?.cost_recorded === false ? 'unknown' : run?.cost_usd ?? 'unknown'}`,
    ...(review.error ? [`Error: ${review.error}`] : []),
    ...findings.filter(f => !focusFingerprint || f.fingerprint === focusFingerprint).map(f =>
      `Finding: ${f.fingerprint} | ${f.file}:${f.line ?? '?'} | ${f.severity ?? 'unknown'}\nLast observed: ${f.outcome?.last_checked_at ?? 'not recorded'}\n${f.body ?? ''}`),
  ].join('\n'));
  async function copyHandoff() {
    try { await navigator.clipboard.writeText(handoff); copyStatus = 'Copied'; }
    catch { copyStatus = 'Clipboard unavailable. Select and copy the reference below.'; }
  }

  function provenanceTone(band) {
    if (band === "likely_ai") return "warn";
    if (band === "collaborative") return "info";
    return "muted";
  }

</script>

<div class="space-y-4">
  <button class="text-sm text-brand hover:underline" onclick={onback}>← back to reviews</button>

  <!-- The change -->
  <div class="flex flex-wrap items-center gap-3">
    <Pill tone={statusTone(review.status)}>{review.status}</Pill>
    {#if run?.dry_run}<Pill>Dry run</Pill>{/if}
    <h1 class="text-lg font-semibold">{review.project} <span class="text-muted">!{review.iid}</span></h1>
    <span class="mono text-sm text-muted">{shortSha(review.sha)}</span>
    <span class="ml-auto text-xs text-muted">{ago(review.updated_at)}</span>
  </div>

  {#if review.error}
    <MetricSection title="Error">
      <pre class="mono text-xs text-danger whitespace-pre-wrap">{review.error}</pre>
    </MetricSection>
  {/if}

  <!-- Findings -->
  <MetricSection title={`Findings (${findings.length})`}>
    {#if findings.length === 0}
      <p class="text-sm text-muted">No findings recorded for this review.</p>
    {:else}
      <ul class="space-y-4">
        {#each findings as f (f.fingerprint)}
          <li id={`finding-${f.fingerprint}`} tabindex="-1" class="finding-evidence">
            <div class="mb-2 flex flex-wrap items-center gap-2 text-xs">
              {#if f.severity}<Pill tone={f.severity === "blocking" ? "danger" : "muted"}>{f.severity}</Pill>{/if}
              {#if f.category}<Pill tone="brand">{f.category}</Pill>{/if}
              <Pill tone={outcomeTone(f.outcome)}>{outcomeLabel(f.outcome)}</Pill>
              <span class="text-muted">{f.verified === 1 || f.verified === true ? "Verified" : f.verified === 0 || f.verified === false ? "Refuted" : "Not evaluated"}</span>
              {#if f.status}<span class="text-muted">{f.status}</span>{/if}
              {#if f.file}
                <span class="mono text-muted ml-auto">{f.file}{f.line ? `:${f.line}` : ""}</span>
              {/if}
            </div>
            {#if f.outcome?.last_checked_at}<p class="text-xs text-muted mb-3">Last observed <time datetime={f.outcome.last_checked_at} title={f.outcome.last_checked_at}>{ago(f.outcome.last_checked_at)}</time></p>{/if}
            <!-- Issue/Impact/Evidence/Fix live in the rendered markdown body. -->
            <div class="prose-bubo text-sm leading-relaxed">{@html renderMarkdown(f.body)}</div>
          </li>
        {/each}
      </ul>
    {/if}
  </MetricSection>
  <details class="review-context"><summary>Review reference for handoff</summary>
    <button class="text-sm text-brand hover:underline" onclick={copyHandoff}>Copy review reference</button>
    <span role="status" class="ml-3 text-xs text-muted">{copyStatus}</span>
    <textarea aria-label="Review reference" readonly value={handoff} rows="10" class="mt-3 w-full bg-surface text-sm mono p-3 border border-border"></textarea>
  </details>
  <details class="review-context"><summary>Review execution and governing rules</summary>
  <!-- Run summary: provenance band, tokens/cost -->
  <div class="grid gap-4 md:grid-cols-2">
    <MetricSection title="Provenance & cost">
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
          <div><span class="text-muted">cost</span> · {run.cost_recorded === false || run.cost_usd == null ? "not recorded" : usd(run.cost_usd)}</div>
          <div><span class="text-muted">model</span> · {run.model ?? "—"}</div>
          <div><span class="text-muted">mode</span> · {run.review_mode ?? "—"}</div>
        </div>
        {#if run.sensitive_paths_count > 0}
          <p class="mt-2 text-xs text-warn">{run.sensitive_paths_count} sensitive path(s) touched</p>
        {/if}
      {:else}
        <p class="text-sm text-muted">No run telemetry recorded for this revision.</p>
      {/if}
    </MetricSection>

    <MetricSection title="Stage timeline">
      <Timeline started_at={run?.started_at} finished_at={run?.finished_at} />
    </MetricSection>
  </div>

  {#if governance.length}
    <MetricSection title="Governance decisions">
      <ul class="space-y-1 text-sm">
        {#each governance as g (g.run_id)}
          <li class="flex items-center gap-2">
            <Pill tone={g.triggered ? "warn" : "muted"}>{g.action}</Pill>
            <span class="text-muted">{g.mode}</span>
            {#if g.reason}<span class="text-xs text-muted">— {g.reason}</span>{/if}
          </li>
        {/each}
      </ul>
    </MetricSection>
  {/if}


  </details>
</div>

<style>
  .finding-evidence { padding: 1.25rem 0; border-bottom: 1px solid var(--border); scroll-margin-top: 1.5rem; overflow-wrap: anywhere; }
  .finding-evidence:last-child { border-bottom: 0; }
  .finding-evidence:focus { outline: 2px solid var(--brand); outline-offset: .6rem; }
  .finding-evidence :global(.prose-bubo) { max-width: 78ch; }
  .review-context { margin-top: 2rem; }
  .review-context summary { cursor: pointer; padding: 1rem 0; color: var(--text-muted); }
</style>
