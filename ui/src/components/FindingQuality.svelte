<script>
  let { findings, outcomes } = $props();
  const evaluated = $derived(findings.verified + findings.refuted);
  const unknown = $derived(Math.max(0, findings.total - evaluated));
  const percent = (n,d) => d ? 100*n/d : 0;
  const share = (n,d) => d ? `${percent(n,d).toFixed(1)}%` : '—';
  const feedback = $derived([
    {label:'Resolved discussions', count:outcomes.resolved, color:'var(--ok)'},
    {label:'Marked false positive', count:outcomes.false_positive, color:'var(--danger)'},
    {label:'Disputed', count:outcomes.disputed, color:'var(--warn)'},
    {label:'Developer replied', count:outcomes.developer_replied, color:'var(--brand)'},
  ]);
</script>

<p class="cohort">{findings.total} finding records in this period. Evaluation and developer feedback measure different things.</p>
<div class="quality-columns">
  <section aria-label="Finding evaluation chart">
    <h3>Evaluation results</h3>
    <p class="chart-caption">{evaluated} of {findings.total} evaluated <strong>{share(evaluated,findings.total)} coverage</strong></p>
    <svg viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label={`${findings.verified} verified, ${findings.refuted} refuted, ${unknown} not evaluated`}>
      <rect width="100" height="8" fill="var(--border)"/>
      <rect width={percent(findings.verified,findings.total)} height="8" fill="var(--brand)"/>
      <rect x={percent(findings.verified,findings.total)} width={percent(findings.refuted,findings.total)} height="8" fill="var(--danger)"/>
    </svg>
    <dl class="legend">
      <div><dt><i style="background:var(--brand)"></i>Verified</dt><dd>{findings.verified}</dd></div>
      <div><dt><i style="background:var(--danger)"></i>Refuted</dt><dd>{findings.refuted}</dd></div>
      <div><dt><i style="background:var(--border)"></i>Not evaluated</dt><dd>{unknown}</dd></div>
    </dl>
    <p class="takeaway">{#if evaluated}<strong>{share(findings.verified,evaluated)}</strong> verified among evaluated findings.{:else}No recorded evaluation yet.{/if}</p>
    <p class="note">Recorded evaluation is not ground truth. Unevaluated findings are excluded from the verified share.</p>
  </section>
  <section aria-label="Developer feedback chart">
    <h3>Developer feedback</h3>
    <p class="chart-caption">{outcomes.observed} of {findings.total} observed <strong>{share(outcomes.observed,findings.total)} coverage</strong></p>
    <svg viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label={`${outcomes.observed} observed, ${outcomes.unobserved} without outcome observations`}>
      <rect width="100" height="8" fill="var(--border)"/>
      <rect width={percent(outcomes.observed,findings.total)} height="8" fill="var(--brand)"/>
    </svg>
    <p class="note observation-gap">{outcomes.unobserved} without outcome observations</p>
    {#if outcomes.observed}
      <div class="feedback-bars">
        {#each feedback as row}
          <div class="feedback-row"><div><span>{row.label}</span><span>{row.count} / {outcomes.observed} <strong>{share(row.count,outcomes.observed)}</strong></span></div>
            <svg viewBox="0 0 100 4" preserveAspectRatio="none" role="img" aria-label={`${row.label}: ${row.count} of ${outcomes.observed} observed findings`}><rect width="100" height="4" fill="var(--surface-2)"/><rect width={percent(row.count,outcomes.observed)} height="4" fill={row.color}/></svg>
          </div>
        {/each}
      </div>
      <p class="note">Each bar uses observed findings. Flags can overlap; they do not add to 100%. Resolved means discussion state, not a confirmed code fix.</p>
    {:else}<p class="takeaway">No outcome observations yet. Feedback rates are unknown.</p>{/if}
  </section>
</div>
<p class="note">Records across revisions can refer to the same issue; these are not unique-issue counts.</p>

<style>
  .cohort { color:var(--text-muted); font-size:.85rem; margin-bottom:1.5rem; }
  .quality-columns { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:2.5rem; margin-bottom:1.5rem; }
  h3 { font-size:.95rem; font-weight:550; margin-bottom:.8rem; }
  .chart-caption { display:flex; flex-wrap:wrap; justify-content:space-between; gap:.3rem; font-size:.75rem; color:var(--text-muted); margin-bottom:.6rem; }
  .chart-caption strong { font-weight:500; color:var(--text); }
  svg { display:block; width:100%; height:14px; }
  .legend { display:grid; gap:.65rem; margin-top:1rem; font-size:.8rem; }
  .legend div,.feedback-row>div { display:flex; justify-content:space-between; gap:1rem; }
  .legend dt { display:flex; align-items:center; gap:.5rem; }
  .legend dd { font-variant-numeric:tabular-nums; }
  i { width:8px; height:8px; display:inline-block; }
  .takeaway { font-size:.9rem; margin:1.25rem 0 .5rem; }
  .note { color:var(--text-muted); font-size:.75rem; line-height:1.6; max-width:70ch; }
  .observation-gap { margin:.4rem 0 1.1rem; }
  .feedback-bars { display:grid; gap:.9rem; margin-bottom:1rem; }
  .feedback-row { font-size:.75rem; font-variant-numeric:tabular-nums; }
  .feedback-row strong { font-weight:500; margin-left:.5rem; }
  .feedback-row svg { height:5px; margin-top:.4rem; }
  @media(max-width:640px) { .quality-columns { grid-template-columns:1fr; gap:2rem; } }
</style>
