<script>
  import { reviewScope } from "../lib/dashboard-charts.js";
  import DashboardCharts from "../components/DashboardCharts.svelte";
  import ReviewSummary from "../components/ReviewSummary.svelte";
  import { ago, statusTone } from "../lib/format.js";
  import { reviewStatus } from "../lib/workbench.js";

  let { data, onopen, onreviews = () => {} } = $props();
  const recent = $derived(data.dashboard?.recent ?? []);
  let windowLabel = $state('today');
  let requestedScope = $state(null);
  const selectedScope = $derived(reviewScope(metric, requestedScope).scope);
  const metric = $derived(data.dashboard?.metrics?.windows?.find(row => row.label===windowLabel));
  const labels = $derived((data.dashboard?.metrics?.windows ?? []).map(r => r.label));
  const queue = $derived(recent.slice(0, 5));
  const detailFor = review => data.reviews?.find(r => r.project===review.project && r.iid===review.iid && r.sha===review.sha);
</script>

<div class="review-workbench">
  {#if data.meta?.sample}<p class="sample-note"><strong>Sample data</strong> · Synthetic reviews and findings; not production activity.</p>{/if}
  <header>
    <div><h1>Code reviews</h1><p class="dateline">{data.meta?.live ? 'Live local view' : 'Local snapshot'} · <time datetime={data.meta?.generated_at}>{ago(data.meta?.generated_at)}</time>{#if data.meta?.live} · refreshes every 5 seconds{/if}</p></div>
    <span class="version">bubo {data.version?.installed ?? '—'}</span>
  </header>
  {#if data.meta?.error}<p role="alert" class="text-danger">{data.meta.error}</p>{/if}
  {#if !data.meta?.db_present}<p class="text-muted text-sm">No local review history yet. Run a review to populate this view.{#if !data.meta?.live} Regenerate with <code class="mono">bubo ui-export</code>.{/if}</p>{/if}

  <div class="review-columns">
  <section aria-labelledby="metrics-title" class="performance">
    <div class="section-header"><h2 id="metrics-title">Review performance</h2><label>Runs <select value={selectedScope} onchange={event => requestedScope=event.currentTarget.value}><option value="production">Production</option><option value="dry-run">Dry run · no posting</option></select></label><label>Period <select bind:value={windowLabel} disabled={!labels.length}>{#if !labels.length}<option value="today">No data yet</option>{:else}{#each labels as label}<option value={label}>{label==='today'?'Last 24 hours':label==='7d'?'Last 7 days':'Last 30 days'}</option>{/each}{/if}</select></label></div>
    <DashboardCharts {metric} scope={selectedScope}/>
  </section>

  {#if recent.length}
  <section class="review-activity" aria-labelledby="queue-title">
    <div class="section-header"><h2 id="queue-title">Review activity</h2><button class="text-sm text-brand hover:underline" onclick={onreviews}>View review activity table</button></div>
    <div class="queue-list">
      {#each queue as review}
        {@const detail = detailFor(review)}
        <article class="queue-row">
          <div class="activity-heading"><button class="review-identity" onclick={() => onopen(review)}>{review.project} <span>!{review.iid}</span></button><time datetime={review.updated_at} title={review.updated_at}>{ago(review.updated_at)}</time></div>
          <p class="activity-status"><span class={`state ${statusTone(review.status)}`}>{reviewStatus(review.status)}</span>{#if detail?.detail?.run?.dry_run}<span class="dry-run">Dry run</span>{/if}</p>
          <ReviewSummary review={detail || review} {onopen}/>
        </article>
      {/each}
    </div>
    <p class="scope-note">Latest five review records, newest first. Queued/running states can be stale; they are not process heartbeats. This list does not follow the selected metrics period.</p>
  </section>
  {/if}

  </div>


</div>

<style>
  .review-workbench { display:flex; flex-direction:column; gap:2rem; }
  .review-columns { display:grid; grid-template-columns:minmax(0,1fr) minmax(16rem,.45fr); gap:2rem 2.5rem; align-items:start; }
  .review-columns:empty { display:none; }
  .performance { min-width:0; }
  .review-activity { min-width:0; }
  .review-activity { grid-column:2; grid-row:1; padding-left:1.5rem; border-left:1px solid var(--border); }


  header { padding:.25rem 0 1.25rem; border-bottom:2px solid var(--text); }
  header,.section-header { display:flex; align-items:baseline; justify-content:space-between; gap:1rem; flex-wrap:wrap; }
  h1 { font-size:1.65rem; line-height:1.3; font-weight:550; letter-spacing:-.035em; }
  h2 { font-size:1.05rem; font-weight:650; letter-spacing:-.02em; }
  .dateline,.version,.scope-note { font-size:.75rem; color:var(--text-muted); line-height:1.6; }
  .dateline { margin-top:.4rem; }
  .sample-note { color:var(--text-muted); font-size:.75rem; }
  .queue-list { margin-top:1rem; border-top:1px solid var(--border); }
  .queue-row { padding:1rem 0; border-bottom:1px solid var(--border); min-width:0; }
  .activity-heading { display:flex; align-items:baseline; justify-content:space-between; gap:.8rem; }
  .review-identity { font-size:.85rem; font-weight:550; text-align:left; overflow-wrap:anywhere; }
  .review-identity:hover { color:var(--brand); text-decoration:underline; }
  .review-identity span { color:var(--text-muted); white-space:nowrap; }
  .activity-heading time { font-size:.7rem; color:var(--text-muted); white-space:nowrap; }
  .activity-status { display:flex; align-items:center; gap:.5rem; margin:.35rem 0; }
  .state { font-size:.73rem; }
  .dry-run { font-size:.68rem; color:var(--text-muted); border:1px dashed var(--border); padding:0 .3rem; }
  .section-header label { color:var(--text-muted); font-size:.75rem; }
  select { color:var(--text); background:var(--surface); border:1px solid var(--border); padding:.35rem .5rem; margin-left:.4rem; }
  .state.ok { color:var(--ok); }
  .state.danger { color:var(--danger); }
  .state.warn { color:var(--warn); }
  @media(max-width:900px) { .review-columns { grid-template-columns:1fr; } .review-activity { grid-column:1; grid-row:auto; border-left:0; padding-left:0; } }

</style>
