<script>
  import { reviewSummary, findingTitle } from '../lib/workbench.js';
  import { outcomeLabel, usd, secs, ago } from '../lib/format.js';
  let { review, onopen, limit = 2 } = $props();
  const s = $derived(reviewSummary(review));
  const preview = $derived((s.priority.length ? s.priority : s.findings).slice(0,limit));
</script>
<div class="review-summary">
  {#if !s.available}<p class="muted">Finding detail unavailable</p>
  {:else if !s.findings.length}<p class="muted">{s.empty}</p>
  {:else}
    <p><strong>{s.findings.length} recorded {s.findings.length === 1 ? 'finding' : 'findings'}</strong>{#if s.priority.length}<span class="followup"> · {s.priority.length} priority follow-up{s.priority.length === 1 ? '' : 's'}</span>{/if}</p>
    {#if s.priority.length}<p class="muted">{s.priorityObserved} observed without closure · {s.priority.length-s.priorityObserved} awaiting observation</p>{/if}
  {/if}
  {#if s.available || review.detail?.run}
    <details>
      <summary>Findings and latest-run detail</summary>
      {#if preview.length}
        <ul>{#each preview as f}<li>
          <button onclick={() => onopen(review,f.fingerprint)}>{findingTitle(f)}</button>
          <span>{f.severity || 'Severity unknown'} · {f.outcome ? `Observed: ${outcomeLabel(f.outcome)}` : 'Not yet observed'}</span>
          <span class="location">{f.file || 'File not recorded'}:{f.line ?? '—'}</span>
          {#if f.outcome?.last_checked_at}<span>Last observed {ago(f.outcome.last_checked_at)}</span>{/if}
        </li>{/each}</ul>
        {#if s.findings.length > preview.length}<button class="all-findings" onclick={() => onopen(review)}>View all {s.findings.length} recorded findings</button>{/if}
      {/if}
      {#if s.available && s.findings.length}
        <p class="muted">{s.verified} verified · {s.refuted} refuted · {s.unevaluated} not evaluated</p>
        <p class="muted">{s.observed}/{s.findings.length} records have outcome observations</p>
      {/if}
      {#if review.detail?.run}
        <p class="execution">Latest run{review.detail.run.dry_run ? ' (dry run)' : ''}: {review.detail.run.model || 'model unknown'}<br/>
          {s.cost === null ? 'Cost not recorded' : `${usd(s.cost)} recorded cost`} · {s.duration === null ? 'Completed duration not recorded' : `${secs(s.duration)} duration`}
        </p>
      {/if}
      <p class="muted scope">Finding records belong to this revision and may span retries. Latest-run cost and model do not describe every finding.</p>
    </details>
  {/if}
</div>
<style>
  .review-summary { font-size:.78rem; line-height:1.55; }
  p { margin:.2rem 0; }
  strong { font-weight:550; }
  .muted,li span { color:var(--text-muted); font-size:.73rem; }
  .followup { color:var(--text); }
  summary { cursor:pointer; color:var(--brand); margin-top:.55rem; font-size:.73rem; }
  ul { margin:.8rem 0; }
  li { padding:.6rem 0; border-bottom:1px solid var(--border); }
  li button, .all-findings { text-align:left; color:var(--brand); }
  li button:hover,.all-findings:hover { text-decoration:underline; }
  li span { display:block; margin-top:.15rem; overflow-wrap:anywhere; }
  .location { font-family:ui-monospace,"SF Mono",Menlo,monospace; }
  .all-findings { margin-bottom:.7rem; }
  .execution { margin-top:.8rem; }
  .scope { margin-top:.7rem; }
</style>
