<script>
  import { usd, shortSha } from '../lib/format.js';
  import { findingTitle, auditReview } from '../lib/workbench.js';
  let { metric: m, report, reviews = [], onopen, onplanning } = $props();
  let evidence = $state('');
  const rate = (n,d) => d ? `${(n/d*100).toFixed(1)}%` : '—';
  const audit = $derived((report?.audit ?? []).filter(row => !row.dry_run && Date.parse(row.started_at) >= Date.parse(m.since) && Date.parse(row.started_at) <= Date.parse(m.until)));
  const findings = $derived(reviews.flatMap(review => (review.detail?.findings ?? []).map(finding => ({review,finding}))).filter(({finding}) => finding.outcome && Date.parse(finding.updated_at)>=Date.parse(m.since) && Date.parse(finding.updated_at)<=Date.parse(m.until)).slice(0,12));
  const policy = $derived(Object.entries(m.governance.actions).map(([action,count]) => `${count} ${action}`).join(' · '));
</script>

<div class="decision-rollup">
  <p class="cohorts">Run measures: production runs started in this period. Outcome measures: findings updated in this period. Policy activity: decisions created in this period. These are separate cohorts, not one financial return calculation.</p>
  <dl>
    <div class="rollup-row"><dt>Recorded spend</dt><dd>
      <strong>{m.production.cost_usd.recorded ? usd(m.production.cost_usd.total) : '—'}</strong>
      <p>{m.production.cost_usd.recorded}/{m.production.runs} production runs have cost. {m.production.successful}/{m.production.terminal} terminal runs succeeded.</p>
      <p>Failed-run spend: {m.production.failed_usage.cost_usd.recorded ? usd(m.production.failed_usage.cost_usd.total) : '—'} ({m.production.failed_usage.cost_usd.recorded}/{m.production.failed} recorded). Mean cost per successful run: {m.production.successful_usage.cost_per_recorded_run == null ? '—' : usd(m.production.successful_usage.cost_per_recorded_run)} ({m.production.successful_usage.cost_usd.recorded}/{m.production.successful} recorded).</p>
    </dd><button aria-expanded={evidence==='runs'} onclick={() => evidence=evidence==='runs'?'':'runs'}>Inspect run evidence ↓</button></div>
    <div class="rollup-row"><dt>Audit evidence</dt><dd>
      <strong>{m.audit_evidence?.available ? rate(m.audit_evidence.policy_recorded,m.audit_evidence.production_runs) : '—'} policy coverage</strong>
      <p>{#if m.audit_evidence?.available}{m.audit_evidence.policy_recorded}/{m.audit_evidence.production_runs} production runs have a linked policy decision; {m.audit_evidence.policy_missing} have no policy record.{:else}Policy evidence unavailable.{/if}</p>
      <p>Known provenance: {rate(m.audit_evidence?.provenance_known,m.audit_evidence?.production_runs)} ({m.audit_evidence?.provenance_known ?? 0}/{m.audit_evidence?.production_runs ?? 0} production runs). This cohort includes unfinished runs, so a missing policy record may be pending. Linked decisions may have been recorded outside this window. This is evidence coverage, not compliance certification or authorship proof.</p>
    </dd><button aria-expanded={evidence==='runs'} onclick={() => evidence=evidence==='runs'?'':'runs'}>Inspect sampled records ↓</button></div>
    <div class="rollup-row"><dt>Policy activity</dt><dd>
      <strong>{m.governance.triggered}/{m.governance.total} decisions triggered</strong>
      <p>{policy || 'No policy actions recorded in this decision-created cohort.'}</p>
      <p>{Object.entries(m.governance.modes).map(([mode,count]) => `${count} ${mode}`).join(' · ')} Advisory warnings do not establish that a merge was prevented.</p>
      {#if Object.keys(m.governance.rules).length}<details><summary>Matched rule counts</summary><ul>{#each Object.entries(m.governance.rules) as [rule,count]}<li>{rule}: {count}</li>{/each}</ul></details>{/if}
    </dd><button aria-expanded={evidence==='runs'} onclick={() => evidence=evidence==='runs'?'':'runs'}>Inspect review policy ↓</button></div>
    <div class="rollup-row"><dt>Observed resolution</dt><dd>
      <strong>{rate(m.outcomes.resolved_without_rejection,m.outcomes.observed)} resolved without rejection</strong>
      <p>{m.outcomes.resolved_without_rejection ?? 0}/{m.outcomes.observed} observed findings are resolved without a dispute or false-positive flag; {m.outcomes.blocking_resolved_without_rejection ?? 0} are blocking.</p>
      <p>Outcome coverage: {m.outcomes.observed}/{m.findings.total} findings ({rate(m.outcomes.observed,m.findings.total)}); {m.outcomes.unobserved} unobserved. Discussion resolution is not proof of a code fix, avoided loss or measured savings.</p>
    </dd><button aria-expanded={evidence==='findings'} onclick={() => evidence=evidence==='findings'?'':'findings'}>Inspect finding evidence ↓</button></div>
    <div class="rollup-row planning-row"><dt>Financial scenario</dt><dd><p>Use your hourly value, time saved and other costs to estimate net value and the break-even minutes per successful production review.</p></dd><a href="#planning-estimate" onclick={onplanning}>Set assumptions ↓</a></div>
  </dl>

  {#if evidence==='runs'}
    <section class="evidence" aria-label="Sampled production run evidence">
      <h3>Sampled production run evidence</h3><p>Up to 50 exported audit rows; {audit.length} production rows match this window. Aggregate coverage can include more runs than this drilldown. Open a review for its available run and policy record; older run details may not be embedded. These run samples are not the complete decision-created policy cohort.</p>
      {#if audit.length}<div class="overflow-x-auto"><table><thead><tr><th>Review / revision</th><th>Run</th><th>Status</th><th>Recorded cost</th><th>Policy action</th><th>Provenance</th></tr></thead><tbody>{#each audit as row}{@const embedded = auditReview(reviews, row)}<tr><td><button disabled={!embedded} onclick={() => onopen(embedded)}>{row.project} !{row.iid}<span class="mono">{shortSha(row.sha)}{!embedded ? ' · run detail not embedded' : ''}</span></button></td><td class="mono">{row.run_id || 'Not exported'}</td><td>{row.status}</td><td>{row.cost_recorded ? usd(row.cost_usd) : '—'}</td><td>{row.policy_action || 'Not recorded'}</td><td>{row.provenance_band || 'Unknown'}</td></tr>{/each}</tbody></table></div>{:else}<p>No matching audit rows embedded. Aggregate results do not imply the records are included in this snapshot.</p>{/if}
    </section>
  {:else if evidence==='findings'}
    <section class="evidence" aria-label="Sampled observed finding evidence"><h3>Sampled observed finding evidence</h3><p>Up to 12 observed findings from embedded review details, filtered by finding update time. This is a limited drilldown, not the full denominator.</p>
      {#if findings.length}<ul>{#each findings as {review,finding}}<li><button onclick={() => onopen(review,finding.fingerprint)}>{findingTitle(finding)}</button><span>{review.project} !{review.iid} · {finding.file}:{finding.line ?? '—'} · {finding.outcome.resolved && !finding.outcome.disputed && !finding.outcome.false_positive ? 'Resolved without rejection' : 'Other observed state'}</span></li>{/each}</ul>{:else}<p>No matching observed findings embedded.</p>{/if}
    </section>
  {/if}
</div>
<style>
  .cohorts,.rollup-row p,.evidence>p { font-size:.75rem; line-height:1.65; color:var(--text-muted); max-width:78ch; }
  .cohorts { margin-bottom:1rem; }
  .rollup-row { display:grid; grid-template-columns:10rem minmax(0,1fr) 10rem; gap:1rem; border-top:1px solid var(--border); padding:1.15rem 0; align-items:start; }
  dt { font-size:.85rem; font-weight:600; color:var(--text-muted); }
  dd strong { font-size:1.15rem; font-weight:550; color:var(--text); letter-spacing:-.025em; font-variant-numeric:tabular-nums; }
  dd p { margin-top:.35rem; }
  .rollup-row>button,.rollup-row>a,.evidence button { text-align:left; color:var(--brand); font-size:.75rem; }
  details { margin-top:.5rem; font-size:.75rem; }
  .evidence { border-top:1px solid var(--border); padding:1rem 0; }
  h3 { font-size:.9rem; font-weight:550; margin-bottom:.5rem; }
  table { width:100%; margin-top:.8rem; text-align:left; font-size:.75rem; }
  th,td { padding:.6rem .75rem .6rem 0; border-bottom:1px solid var(--border); vertical-align:top; }
  th { color:var(--text-muted); font-weight:500; }
  td button { overflow-wrap:anywhere; }
  td button:disabled { color:var(--text-muted); cursor:default; }
  td span,.evidence li span { display:block; margin-top:.25rem; font-size:.7rem; color:var(--text-muted); }
  .evidence li { padding:.7rem 0; border-bottom:1px solid var(--border); overflow-wrap:anywhere; }
  @media(max-width:700px) { .rollup-row { grid-template-columns:1fr; gap:.4rem; } .rollup-row>button,.rollup-row>a { margin-top:.3rem; } }
</style>
