<script>
  import { num, secs } from '../lib/format.js';
  let { metric } = $props();
  const v = $derived(metric.validation);
  const rows = $derived([
    {label:'Successful',value:v.successful},
    {label:'Failed',value:v.failed},
    {label:'Unfinished',value:v.unfinished},
    {label:'Other finished',value:v.completed-v.successful-v.failed},
  ]);
</script>
<section class="validation" aria-labelledby="validation-title">
  <h2 id="validation-title">Validation runs <small>Dry run · no posting</small></h2>
  <p>{num(v.runs)} review executions in this period. These runs are excluded from production results.</p>
  <div class="layout">
    <figure><figcaption>Execution results</figcaption>
      {#each rows as row}<div class="bar"><span>{row.label}</span><strong>{num(row.value)}</strong><div class="track" role="img" aria-label={`${row.label}: ${row.value} of ${v.runs}`}><i style:width={`${v.runs ? 100*row.value/v.runs : 0}%`}></i></div></div>{/each}
    </figure>
    <figure><figcaption>Measured usage</figcaption>
      <dl><dt>Recorded tokens</dt><dd>{v.tokens_total.recorded ? num(v.tokens_total.total) : 'Unknown'}</dd><dt>Token coverage</dt><dd>{num(v.tokens_total.recorded)} / {num(v.runs)} runs</dd><dt>Median execution time</dt><dd>{secs(v.latency.p50)}</dd><dt>Duration coverage</dt><dd>{num(v.latency.count)} / {num(v.runs)} runs</dd><dt>Cost coverage</dt><dd>{num(v.cost_usd.recorded)} / {num(v.runs)} runs</dd></dl>
      <p>Missing prices are unknown, not free usage.</p>
    </figure>
  </div>
  <p>Finding records across this period: {num(metric.findings?.statuses?.planned ?? 0)} planned; {num(metric.findings?.statuses?.posted ?? 0)} posted. Planned findings are model outputs, not confirmed defects or developer acceptance.</p>
</section>
<style>
.validation{margin:1.5rem 0 2rem}h2{font-size:1rem;font-weight:600}small{font-size:.75rem;font-weight:400;color:var(--text-muted);margin-left:.6rem}p{font-size:.76rem;color:var(--text-muted);line-height:1.6;margin:.6rem 0 1rem}.layout{display:grid;grid-template-columns:1fr 1fr;gap:2rem}figure{margin:0}figcaption{font-weight:600;font-size:.85rem;margin:.5rem 0 1rem}.bar{display:grid;grid-template-columns:1fr auto;gap:.3rem;font-size:.75rem;margin:.75rem 0}.track{grid-column:1/-1;background:var(--surface-2);height:.65rem}.track i{display:block;height:100%;background:var(--brand)}strong,dd{font-variant-numeric:tabular-nums}dl{display:grid;grid-template-columns:1fr auto;gap:.6rem;font-size:.75rem}dd{margin:0;text-align:right}@media(max-width:600px){.layout{grid-template-columns:1fr}}
</style>
