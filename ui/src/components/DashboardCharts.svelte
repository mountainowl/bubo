<script>
  import { dashboardCharts } from '../lib/dashboard-charts.js';
  let { metric, scope = null } = $props();
  const charts = $derived(dashboardCharts(metric, scope));
  const money = value => `$${value.toFixed(2)}`;
  const width = (value, total) => total > 0 ? 100 * value / total : 0;
  const tone = key => ({successful:'ok',failed:'danger',unfinished:'warn',verified:'brand',refuted:'danger'}[key] || 'muted');
  const groups = $derived(charts ? [
    {title:'Review execution', rows:charts.delivery, total:charts.runCount, dollars:false,
      caption:charts.scope==='dry-run' ? `${charts.runCount} dry-run executions; no reviews posted.` : `${charts.runCount} production runs; ${charts.dryRuns} dry runs excluded.`},
    {title:'Recorded spend', rows:charts.spend, total:charts.spend.reduce((n,r)=>n+r.value,0), dollars:true,
      caption:`${charts.costRecorded}/${charts.runCount} costs recorded; ${charts.costMissing} missing.`},
    {title:'Finding evaluation', rows:charts.evaluation, total:charts.findingCount, dollars:false,
      caption:`${charts.findingCount} finding records. Evaluation is not accuracy.`},
  ] : []);
</script>

{#if !charts}
  <p class="unavailable">Chart data is unavailable for this period.</p>
{:else}
  <div class="charts">
    {#each groups as group}
      <figure>
        <figcaption>{group.title}</figcaption>
        <p class="caption">{group.caption}</p>
        <div class="bars" role="img" aria-label={`${group.title}: ${group.rows.map(r=>`${r.label} ${group.dollars ? (r.recorded ? money(r.value) : 'unrecorded') : r.value}`).join(', ')}`}>
          {#each group.rows as row}
            <div class="bar-row">
              <div class="bar-label"><span>{row.label}</span><strong>{group.dollars ? (row.recorded ? money(row.value) : '—') : row.value}</strong></div>
              <div class="track"><span class={tone(row.key)} style:width={`${width(row.value,group.total)}%`}></span></div>
            </div>
          {/each}
        </div>
        {#if group.dollars}<p class="foot">{charts.costRecorded ? `${money(group.total)} recorded total.` : 'No recorded costs.'} Missing costs are not $0.</p>
        {:else if !group.total}<p class="foot">No records in this period.</p>{/if}
      </figure>
    {/each}
    <figure>
      <figcaption>Audit coverage</figcaption>
      {#if charts.scope==='dry-run'}
        <p class="caption">Production audit coverage is not applicable to these dry runs.</p>
        <p class="foot">Findings and exact-run records are available in Activity and Reports. No developer acceptance or compliance result is inferred.</p>
      {:else}
      <p class="caption">Run-linked evidence across {charts.audit.denominator} production runs.</p>
      {#each [{label:'Policy recorded',value:charts.audit.policyRecorded},{label:'Known provenance',value:charts.audit.provenanceKnown}] as row}
        <div class="bar-row">
          <div class="bar-label"><span>{row.label}</span><strong>{row.value===null ? 'Unavailable' : `${row.value}/${charts.audit.denominator}`}</strong></div>
          {#if row.value!==null}<div class="track" role="img" aria-label={`${row.label}: ${row.value} of ${charts.audit.denominator}`}><span class="brand" style:width={`${width(row.value,charts.audit.denominator)}%`}></span></div>{/if}
        </div>
      {/each}
      <p class="foot">Coverage of linked records, not compliance certification.</p>
      {/if}
    </figure>
  </div>
{/if}

<style>
  .charts { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); column-gap:2rem; row-gap:2.25rem; margin-top:1.7rem; }
  figure { margin:0; min-width:0; }
  figcaption { font-size:.95rem; font-weight:600; }
  .caption,.foot,.unavailable { font-size:.75rem; line-height:1.5; color:var(--text-muted); }
  .caption { margin:.4rem 0 1rem; min-height:2.25rem; }
  .foot { margin-top:1rem; }
  .bar-row { margin:.8rem 0; }
  .bar-label { display:flex; justify-content:space-between; gap:.5rem; font-size:.75rem; margin-bottom:.35rem; }
  strong { font-weight:600; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .track { height:.65rem; background:var(--surface-2); border-bottom:1px solid var(--border); overflow:hidden; }
  .track span { display:block; height:100%; background:var(--text-muted); }
  .track .ok { background:var(--ok); } .track .danger { background:var(--danger); }
  .track .warn { background:var(--warn); } .track .brand { background:var(--brand); }
  @media(max-width:600px) { .charts { grid-template-columns:1fr; gap:1.5rem; } .caption { min-height:0; } }
</style>
