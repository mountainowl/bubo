<script>
  import { reviewScope } from "../lib/dashboard-charts.js";
  import Stat from '../components/Stat.svelte';
  import MetricSection from '../components/MetricSection.svelte';
  import FindingQuality from '../components/FindingQuality.svelte';
  import { usd, num, pct, secs, shortSha } from '../lib/format.js';
  import { reportSelection, auditReview, filterAudit, auditCsv } from '../lib/report.js';

  let { data, onopen } = $props();
  let windowLabel = $state('7d');
  let query = $state('');
  let requestedScope = $state(null);
  const selection = $derived(reviewScope(m, requestedScope));
  const runLabel = $derived(selection.scope==='dry-run' ? 'Dry-run' : 'Production');
  const selected = $derived(reportSelection(data, windowLabel));
  const m = $derived(selected.metrics);
  const p = $derived(selection.runs);
  const evidence = $derived(m?.audit_evidence);
  const rows = $derived(filterAudit(selected.audit, query));
  function download(contents, type, extension) {
    const url = URL.createObjectURL(new Blob([contents], {type}));
    const link = document.createElement('a');
    link.href = url; link.download = `bubo-report-${windowLabel}.${extension}`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
  function exportReport() {
    download(JSON.stringify({schema_version:1, generated_at:data.meta?.generated_at,
      version:data.version, scope:selection.scope, selected_run_metrics:p, metrics:m, audit_sample:selected.audit,
      cohorts:{production:'Non-dry runs started within the window; terminal rates require a finished timestamp and terminal status.',
        findings:'Finding records updated within the window; outcome observations join those records.',
        governance:'Decisions created within the window.', audit:'Latest at most 50 runs; a sample, not aggregate denominators.'}}, null, 2), 'application/json', 'json');
  }
</script>

<div class="reports">
  <header>
    <div><h1>Reports</h1><p>Review results, feedback, and control evidence for a defined period.</p></div>
    <div class="controls">
      <label>Runs <select value={selection.scope} onchange={event => requestedScope=event.currentTarget.value}><option value="production">Production</option><option value="dry-run">Dry run · no posting</option></select></label>
      <div class="periods" aria-label="Report period">
        {#each [['today','24 hours'],['7d','7 days'],['30d','30 days']] as [label,title]}
          <button aria-pressed={windowLabel === label} onclick={() => windowLabel = label}>{title}</button>
        {/each}
      </div>
      <button disabled={!m} onclick={exportReport}>Export JSON</button>
    </div>
  </header>
  <p class="note">Snapshot: {data.meta?.generated_at ?? 'Unknown'}. Local review history only; deployment outcomes and measured human time savings are not collected.</p>
  {#if m && p}
    <MetricSection title="Review operating results">
      <p class="note">{m.since} → {m.until}. {runLabel} runs started in this period. {selection.scope==='dry-run' ? 'Reviews were not posted.' : 'Dry runs are excluded.'}</p>
      <div class="summary">
        <Stat plain label="Successful review executions" value={num(p.successful)} sub={`${num(p.successful)} / ${num(p.terminal)} terminal runs · ${pct(p.success_rate)}`} />
        <Stat plain label={`Recorded ${runLabel.toLowerCase()} spend`} value={p.cost_usd.recorded ? usd(p.cost_usd.total) : '—'} sub={`${num(p.cost_usd.recorded)} / ${num(p.runs)} runs with cost · ${num(p.cost_usd.missing)} unknown`} />
        <Stat plain label="Resolved without rejection" value={num(m.outcomes.resolved_without_rejection)} sub={`${num(m.outcomes.resolved_without_rejection)} / ${num(m.outcomes.observed)} observed findings · ${pct(m.outcomes.observed ? m.outcomes.resolved_without_rejection / m.outcomes.observed : null)}`} />
      </div>
      <p class="note">{num(p.runs)} {runLabel.toLowerCase()} runs; {num(p.failed)} terminal failures; {num(p.runs - p.terminal)} without a terminal result. {#if selection.scope==='production'}{num(m.usage.dry_runs)} dry runs excluded.{/if} Resolution records discussion state, not measured savings or a confirmed code fix.</p>
      <p class="note">{runLabel} duration: median {secs(p.latency.p50)}, p95 {secs(p.latency.p95)}, mean {secs(p.latency.mean)} across {num(p.latency.count)} valid duration records. Percentiles interpolate sorted durations; standard deviation {secs(p.latency.stddev)} uses the population denominator.</p>
    </MetricSection>

    <MetricSection title="Finding quality & feedback">
      <p class="note">Findings updated in the selected period and their recorded outcome observations. This cohort is separate from runs started in the period.</p>
      <FindingQuality findings={m.findings} outcomes={m.outcomes} />
      <div class="evidence-sources">
        {#each [{title:'Developer disposition', counts:m.outcomes.dispositions}, {title:'Resolution source', counts:m.outcomes.resolution_sources}, {title:'Resolution evidence', counts:m.outcomes.resolution_evidence}] as group}
          <div><h3>{group.title}</h3>
            {#each Object.entries(group.counts ?? {}) as [label,count]}
              <p class="note">{label.replaceAll('_',' ')}: {num(count)} / {num(m.outcomes.observed)} observed findings</p>
            {:else}<p class="note">No observations recorded.</p>{/each}
          </div>
        {/each}
      </div>
    </MetricSection>

    <MetricSection title="Control evidence">
      <div class="summary">
        <Stat plain label="Policy decision coverage" value={pct(evidence.policy_coverage)} sub={evidence.available ? `${num(evidence.policy_recorded)} / ${num(evidence.production_runs)} production runs · ${num(evidence.policy_missing)} missing` : 'Policy decision table unavailable'} />
        <Stat plain label="Known provenance signal" value={pct(evidence.provenance_coverage)} sub={`${num(evidence.provenance_known)} / ${num(evidence.production_runs)} production runs · ${num(evidence.provenance_unknown)} unknown`} />
        <Stat plain label="Security findings needing attention" value={num(m.security_attention.open)} sub={`${num(m.security_attention.open)} / ${num(m.security_attention.observed)} observed priority security findings; ${num(m.security_attention.unobserved)} unobserved`} />
      </div>
      <p class="note">Policy coverage matches decisions to production run IDs, including decisions recorded later. Known provenance means likely AI or collaborative signals; it is not authorship proof. Priority security means posted blocking, high, or critical findings, excluding refuted findings. Unobserved findings are unknown. These are evidence records, not a compliance certification.</p>
      <div class="control-list"><h3>Policy decisions created in this period</h3><p class="note">{num(m.governance.total)} decisions; {num(m.governance.triggered)} triggered. This decision-time cohort can differ from run coverage.</p>
        {#each Object.entries(m.governance.actions ?? {}) as [action,count]}<span>{action}: {num(count)} / {num(m.governance.total)}</span>{/each}
      </div>
    </MetricSection>
  {:else}
    <p role="status">{data.dashboard?.metrics?.error ?? 'No coherent metrics are available for this period.'}</p>
  {/if}

  <MetricSection title="Audit register">
    <p class="note">Latest at most 50 run records in this period, one row per run. {num(selected.audit.length)} available; {num(rows.length)} match the search. This sample includes dry runs and is not the aggregate denominator. Details open only for an exact exported run match.</p>
    <div class="audit-controls"><label>Search runs<input type="search" bind:value={query} placeholder="Project, run, model, status, policy…" /></label><button disabled={!rows.length} onclick={() => download(auditCsv(rows), 'text/csv;charset=utf-8', 'csv')}>Export filtered CSV</button></div>
    <div class="table-scroll"><table><caption class="sr-only">Sampled review run audit register</caption><thead><tr><th>Review / run</th><th>Started</th><th>Status</th><th>Model</th><th>Mode</th><th>Cost</th><th>Policy / provenance</th><th>Evidence</th></tr></thead>
      <tbody>{#each rows as row}
        {@const review = auditReview(data.reviews, row)}
        <tr><td>{row.project} #{row.iid}<small>{row.run_id ?? 'Unknown run'} · {shortSha(row.sha)}</small></td><td>{row.started_at ?? 'Unknown'}</td><td>{row.status ?? 'Unknown'}</td><td>{row.model ?? 'Unknown'}</td><td>{row.dry_run ? 'Dry run' : 'Production'}</td><td>{row.cost_recorded === true ? usd(row.cost_usd) : 'Unknown'}</td><td>{row.policy_action ?? 'Unknown'}<small>{row.provenance_band ?? 'Unknown provenance'}</small></td><td>{#if review && onopen}<button onclick={() => onopen(review)}>Open review</button>{:else}<span class="note">Details unavailable</span>{/if}</td></tr>
      {:else}<tr><td colspan="8">No matching audit rows.</td></tr>{/each}</tbody>
    </table></div>
  </MetricSection>
</div>
<style>
  .reports {display:grid;gap:2.5rem;max-width:1320px;margin:auto} header {display:flex;justify-content:space-between;gap:1.5rem;flex-wrap:wrap} h1 {font-size:1.8rem;font-weight:550;letter-spacing:-.04em} header p,.note {font-size:.78rem;color:var(--text-muted);line-height:1.65} .note {margin-bottom:1rem} .controls,.periods,.audit-controls {display:flex;gap:.5rem;align-items:center;flex-wrap:wrap} button {font-size:.78rem;border:1px solid var(--border);padding:.45rem .7rem;border-radius:4px} button[aria-pressed=true] {background:var(--brand);color:var(--brand-fg)} button:disabled {opacity:.45} .evidence-sources {display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1.5rem;margin-top:1.5rem} .summary {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:2rem;margin:1.5rem 0} .control-list {margin-top:1.5rem} h3 {font-size:.9rem;font-weight:550} .control-list span {display:inline-block;font-size:.8rem;margin-right:1.5rem} .audit-controls {justify-content:space-between;margin:1.25rem 0} label {display:flex;gap:.75rem;align-items:center;font-size:.8rem} input {padding:.5rem;background:var(--surface);border:1px solid var(--border);min-width:260px;border-radius:4px} .table-scroll {overflow:auto} table {width:100%;text-align:left;font-size:.76rem} th {font-weight:550;color:var(--text-muted)} th,td {padding:.8rem .6rem;border-bottom:1px solid var(--border);vertical-align:top} small {display:block;color:var(--text-muted);font-size:.68rem;margin-top:.3rem;overflow-wrap:anywhere} @media(max-width:700px){.summary{grid-template-columns:1fr;gap:1rem}.audit-controls label{display:grid}input{min-width:0;width:100%}}
</style>
