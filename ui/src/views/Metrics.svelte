<script>
  import MetricSection from "../components/MetricSection.svelte";
  import FindingQuality from "../components/FindingQuality.svelte";
  import Stat from "../components/Stat.svelte";
  import Breakdown from "../components/Breakdown.svelte";
  import { usd, num, secs } from "../lib/format.js";
  let { metrics, label, scenarioOpen = $bindable(false) } = $props();
  const m = $derived(metrics?.windows?.find(w => w.label === label));
  const rate = (n,d) => d ? `${(n/d*100).toFixed(1)}%` : '—';
  let minutes = $state(undefined);
  let hourly = $state(undefined);
  let overhead = $state(undefined);
  const valid = $derived([minutes, hourly, overhead].every(v => Number.isFinite(v) && v >= 0));
  const avoidedHours = $derived(valid && m ? m.production.successful * minutes / 60 : 0);
  const benefit = $derived(avoidedHours * (hourly ?? 0));
  const investment = $derived(m ? m.production.cost_usd.total + (overhead ?? 0) : 0);
</script>

{#if metrics?.error}<p role="alert" class="brief-error text-danger">{metrics.error}</p>{/if}

{#if m}
<div class="metrics-brief">
  <details class="brief-disclosure"><summary>Delivery, cost and quality detail</summary><div class="pt-4 space-y-6">
  {#if m.usage.runs}
    <MetricSection title="Production cost and reliability">
      <div class="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat plain label="Production success" value={rate(m.production.successful,m.production.terminal)} sub={`${m.production.successful} / ${m.production.terminal} terminal runs`} />
        <Stat plain label="Terminal failure rate" value={rate(m.production.failed,m.production.terminal)} sub={`${m.production.failed} failed · ${m.production.runs-m.production.terminal} other or unfinished`} />
        <Stat plain label="Recorded production cost" value={m.production.cost_usd.recorded ? usd(m.production.cost_usd.total) : '—'} sub={`${m.production.cost_usd.recorded} / ${m.production.runs} runs have cost`} />
        <Stat plain label="Mean cost / success" value={m.production.successful_usage.cost_per_recorded_run == null ? '—' : usd(m.production.successful_usage.cost_per_recorded_run)} sub={`${m.production.successful_usage.cost_usd.recorded} / ${m.production.successful} successes have cost`} />
      </div>
      {#if m.production.failed}<p class="mt-3 text-sm">Recorded failed-run spend: <strong>{m.production.failed_usage.cost_usd.recorded ? usd(m.production.failed_usage.cost_usd.total) : '—'}</strong> · {m.production.failed_usage.cost_usd.recorded} / {m.production.failed} failures have cost.</p>{/if}
      <p class="mt-3 text-xs text-muted">Production excludes dry runs. Terminal runs have a finish timestamp and success, no_findings or failed status; success includes no_findings. Mean cost uses only successful runs with recorded cost. {m.usage.dry_runs} dry runs excluded.</p>
      <details class="mt-4 text-sm"><summary>Speed, tokens and measurement coverage</summary>
        <div class="mt-3 grid gap-4 sm:grid-cols-3"><Stat plain label="Median successful duration" value={m.production.successful_usage.latency.count ? secs(m.production.successful_usage.latency.p50) : '—'} sub={`${m.production.successful_usage.latency.count} valid spans`}/><Stat plain label="p95 successful duration" value={m.production.successful_usage.latency.count ? secs(m.production.successful_usage.latency.p95) : '—'}/><Stat plain label="Population standard deviation" value={m.production.successful_usage.latency.count ? secs(m.production.successful_usage.latency.stddev) : '—'}/></div>
        <div class="overflow-x-auto mt-4"><table class="w-full text-left"><thead><tr><th>Production usage</th><th>Total</th><th>Recorded / runs</th></tr></thead><tbody>{#each ['tokens_input','tokens_output','tokens_cached','tokens_total','lines_reviewed'] as key}<tr><td class="py-2">{key.replaceAll('_',' ')}</td><td>{m.production[key].recorded ? num(m.production[key].total) : '—'}</td><td>{m.production[key].recorded} / {m.production.runs}</td></tr>{/each}</tbody></table></div>
        <p class="mt-3 text-xs text-muted">Missing costs are unknown, not free. Tokens are independent totals; cached tokens are not added again. Lines may repeat across retries.</p>
      </details>
    </MetricSection>
  {/if}

  {#if m.findings.total}
    <MetricSection title="Finding quality and feedback">
      <FindingQuality findings={m.findings} outcomes={m.outcomes}/>

      <details class="mt-4 text-sm"><summary>Finding and feedback detail</summary><div class="mt-4 grid gap-6 sm:grid-cols-2"><Breakdown title="Severity" values={m.findings.severity}/><Breakdown title="Category" values={m.findings.categories}/></div><div class="mt-4 grid gap-6 sm:grid-cols-2 lg:grid-cols-3"><Breakdown title="Finding type" values={m.findings.types}/><Breakdown title="Finding status" values={m.findings.statuses}/><Breakdown title="Developer disposition" values={m.outcomes.dispositions}/><Breakdown title="Resolution source" values={m.outcomes.resolution_sources}/><Breakdown title="Resolution evidence" values={m.outcomes.resolution_evidence}/><Breakdown title="Disposition evidence" values={m.outcomes.disposition_evidence}/></div>
        <div class="mt-4 grid grid-cols-2 gap-4 lg:grid-cols-4">{#each ['developer_replied','duplicate','deleted','merged_unresolved','reply_classified'] as key}<Stat plain label={key.replaceAll('_',' ')} value={rate(m.outcomes[key],m.outcomes.observed)} sub={`${m.outcomes[key]} / ${m.outcomes.observed} observed`}/>{/each}</div>
      </details>
    </MetricSection>
  {/if}

  </div></details>
  <details class="brief-disclosure"><summary>Model comparison and policy activity</summary><div class="pt-4 space-y-6">
  {#if m.models.some(row => row.production.runs)}
    <MetricSection title="Compare model cost and reliability">
      <div class="overflow-x-auto"><table class="w-full text-left text-sm"><thead><tr><th class="py-2">Model / family</th><th>Production success</th><th>Mean cost / success</th><th>Recorded spend share</th><th>p95 successful duration</th></tr></thead><tbody>
        {#each m.models.filter(row => row.production.runs) as row}<tr class="border-t border-border"><td class="py-3 pr-3 break-all">{row.model}<div class="text-xs text-muted">{row.family}</div></td><td>{rate(row.production.successful,row.production.terminal)}<div class="text-xs text-muted">{row.production.successful}/{row.production.terminal} terminal</div></td><td>{row.production.successful_usage.cost_per_recorded_run == null ? '—' : usd(row.production.successful_usage.cost_per_recorded_run)}<div class="text-xs text-muted">{row.production.successful_usage.cost_usd.recorded}/{row.production.successful} successes recorded</div></td><td>{row.production.cost_usd.recorded ? rate(row.production.cost_usd.total,m.production.cost_usd.total) : '—'}<div class="text-xs text-muted">{row.production.cost_usd.recorded}/{row.production.runs} costs recorded</div></td><td>{row.production.successful_usage.latency.count ? secs(row.production.successful_usage.latency.p95) : '—'}<div class="text-xs text-muted">{row.production.successful_usage.latency.count} valid spans</div></td></tr>{/each}
      </tbody></table></div>
      <p class="mt-3 text-xs text-muted">Spend share divides each model’s recorded production cost by total recorded production cost. Missing costs can bias this comparison. Compare coverage and workload before drawing conclusions. These are observed cohorts, not a controlled model benchmark. Family is inferred from the model name, not the API host.</p>
    </MetricSection>
  {/if}

  {#if m.governance.total}
    <MetricSection title="Where did policy intervene?">
      <div class="grid gap-4 sm:grid-cols-3"><Stat plain label="Rules triggered" value={rate(m.governance.triggered,m.governance.total)} sub={`${m.governance.triggered} / ${m.governance.total} decisions`}/><Stat plain label="Extra rigor injected" value={num(m.governance.rigor_injected)}/><Stat plain label="Provenance coverage" value={rate(m.provenance.recorded,m.usage.runs)} sub={`${m.provenance.recorded} / ${m.usage.runs} all runs`}/></div>
      <div class="mt-4"><Breakdown title="Recorded policy action" values={m.governance.actions}/></div>
      <p class="mt-3 text-xs text-muted">Recorded evidence, not compliance certification. No inference is made about reviews without policy decisions.</p>
    </MetricSection>
  {/if}
  </div></details>
  <details class="brief-disclosure"><summary class="font-medium">Audit details and configuration</summary><div class="mt-4 grid gap-6 sm:grid-cols-2 lg:grid-cols-3"><Breakdown title="Matched rules" values={m.governance.rules}/><Breakdown title="Policy mode" values={m.governance.modes}/><Breakdown title="Provenance band" values={m.provenance.bands}/><Breakdown title="Provenance source" values={m.provenance.sources}/><Breakdown title="Reconciliation state" values={m.reconciliations.states}/><Breakdown title="Reconciliation verdict" values={m.reconciliations.verdicts}/><Breakdown title="Review mode" values={m.review_modes}/><Breakdown title="Review tone" values={m.tones}/><Breakdown title="Prompt version" values={m.prompt_versions}/></div></details>
</div>
{/if}
<details class="brief-disclosure"><summary class="font-medium">Data inventory and formulas</summary>
  <dl class="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">{#each Object.entries(metrics?.inventory ?? {}) as [table,count]}<div><dt class="text-sm break-all">{table.replaceAll('_',' ')}</dt><dd class="font-semibold">{count == null ? 'Table unavailable' : `${num(count)} records`}</dd></div>{/each}</dl>
  <p class="mt-4 text-xs text-muted">Trailing 24h/7d/30d UTC windows. Runs use start time; findings and reconciliations use update time; policy decisions use creation time. Outcomes join the selected findings. Duration = finish − start, excluding invalid/negative spans; percentiles interpolate at (n − 1) × p. Zero denominators display —. Current state is not complete event history. Up to 50 recent reviews; metrics fail explicitly rather than truncate after the shared 50,000-row budget.</p>
</details>

{#if m}
  <details id="planning-estimate" bind:open={scenarioOpen} class="brief-disclosure"><summary class="font-medium">Planning estimate (assumptions)</summary><div class="mt-4">
    <p class="text-sm text-muted mb-4">Enter your assumptions. This estimates potential review-time value, not measured savings or avoided incidents. Includes {m.production.successful} successful production runs.</p>
    <div class="grid gap-4 sm:grid-cols-3">{#each ['minutes','hourly','overhead'] as field}
      <label class="text-sm">{field === 'minutes' ? 'Minutes saved per successful production run' : field === 'hourly' ? 'Hourly value (USD)' : 'Other period costs (USD)'}
      {#if field === 'minutes'}<input type="number" min="0" step="any" bind:value={minutes} class="block w-full mt-1 border-b border-border bg-transparent py-2"/>{:else if field === 'hourly'}<input type="number" min="0" step="any" bind:value={hourly} class="block w-full mt-1 border-b border-border bg-transparent py-2"/>{:else}<input type="number" min="0" step="any" bind:value={overhead} class="block w-full mt-1 border-b border-border bg-transparent py-2"/>{/if}</label>{/each}</div>
    {#if valid}<div class="mt-4 grid grid-cols-2 gap-4 lg:grid-cols-4"><Stat plain label="Estimated hours" value={avoidedHours.toFixed(1)}/><Stat plain label="Potential value" value={usd(benefit)}/><Stat plain label="Estimated net value" value={usd(benefit-investment)}/><Stat plain label="Estimated ROI" value={investment ? `${((benefit-investment)/investment*100).toFixed(1)}%` : '—'}/></div>{:else}<p class="mt-3 text-muted">Enter all three assumptions to calculate an estimate; use zero for no other costs.</p>{/if}
    <p class="mt-3 text-sm">Break-even time per successful production run: <strong>{valid && hourly > 0 && m.production.successful > 0 ? `${(investment * 60 / (m.production.successful * hourly)).toFixed(1)} minutes` : '—'}</strong>. Required time value to cover recorded production cost plus your other costs; unknown costs can raise this threshold.</p>
    <p class="mt-3 text-xs text-muted">Hours = successful production runs × minutes ÷ 60. Value = hours × hourly value. Investment = recorded production model cost + other costs. Net = value − investment. ROI = net ÷ investment; undefined at zero investment. {m.production.cost_usd.missing} production runs lack cost, so investment may be incomplete. Assumptions stay in this page only.</p>
  </div></details>
{/if}
<style>
  .metrics-brief { display: flex; flex-direction: column; gap: 2.6rem; }
  .brief-disclosure { border-top: 1px solid var(--border); padding: 1.1rem 0; }
  .brief-disclosure > summary { font-size: .9rem; color: var(--text-muted); font-weight: 450; }
  .brief-disclosure[open] > summary { color: var(--text); }
  .brief-error { padding: 1rem 0; border-top: 2px solid var(--danger); }
  :global(.metrics-brief table) { font-variant-numeric: tabular-nums; }
  :global(.metrics-brief th) { color: var(--text-muted); font-size: .75rem; font-weight: 500; padding-right: 1.2rem; padding-bottom: .8rem; }
  :global(.metrics-brief td) { padding-right: 1.2rem; vertical-align: top; }
  :global(.metrics-brief details:not(.brief-disclosure) > summary) { color: var(--text-muted); font-size: .8rem; }
  :global(.metrics-brief details:not(.brief-disclosure)[open] > summary) { color: var(--text); }
  :global(.metrics-brief p) { max-width: 78ch; line-height: 1.65; }
  :global(.metrics-brief input) { border-radius: 0; border-width: 0 0 1px; background: transparent; }
  @media (max-width: 600px) { .metrics-brief { gap: 2rem; } }
</style>
