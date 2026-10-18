<script>
  import ReviewSummary from "../components/ReviewSummary.svelte";
  import Pill from "../components/Pill.svelte";
  import { reviewSummary, reviewStatus } from "../lib/workbench.js";
  import ReviewDetail from "./ReviewDetail.svelte";
  import { ago, shortSha, statusTone } from "../lib/format.js";

  // `selected` is bindable so the dashboard can deep-link into a review.
  let { data, selected = $bindable(null) } = $props();

  let query = $state("");
  let statusFilter = $state("all");

  const reviews = $derived(data.reviews ?? []);
  const statuses = $derived([...new Set(reviews.map((r) => r.status))].sort());

  const filtered = $derived(
    reviews.filter((r) => {
      if (statusFilter !== "all" && r.status !== statusFilter) return false;
      if (query) {
        const q = query.toLowerCase();
        if (
          !r.project.toLowerCase().includes(q) &&
          !String(r.iid).includes(q) &&
          !String(r.sha).toLowerCase().includes(q)
        ) {
          return false;
        }
      }
      return true;
    }),
  );

  // Resolve the currently-selected review object by identity keys (so a
  // dashboard deep-link, which passes the row, lines up with the list row).
  const current = $derived.by(() => {
    if (!selected) return null;
    return (
      reviews.find(
        (r) => r.project === selected.project && r.iid === selected.iid && r.sha === selected.sha,
      ) ?? selected
    );
  });
</script>

{#if current}
  <ReviewDetail review={current} focusFingerprint={selected?.focusFingerprint} onback={() => (selected = null)} />
{:else}
  <div class="space-y-4">
    <h1 class="text-xl font-semibold">Review activity</h1>
    <p class="text-sm text-muted">Latest available review records, newest first. Up to 50 records are included in this local view.</p>
    <div class="flex flex-wrap items-center gap-2">
      <input
        class="flex-1 min-w-48 rounded-[var(--radius-token)] border border-border bg-surface px-3 py-1.5 text-sm"
        aria-label="Filter review activity"
        placeholder="Filter by project / iid / sha…"
        bind:value={query}
      />
      <select
        class="rounded-[var(--radius-token)] border border-border bg-surface px-3 py-1.5 text-sm"
        aria-label="Review status"
        bind:value={statusFilter}
      >
        <option value="all">all statuses</option>
        {#each statuses as s (s)}<option value={s}>{s}</option>{/each}
      </select>
      <span class="text-xs text-muted">{filtered.length} of {reviews.length}</span>
    </div>

    {#if filtered.length === 0}
      <p class="text-sm text-muted">No reviews match.</p>
    {:else}
      <div class="overflow-x-auto">
        <table class="activity-table">
          <caption class="sr-only">Recorded review activity</caption>
          <thead><tr><th scope="col">Review</th><th scope="col">Status</th><th scope="col">Revision</th><th scope="col">Evaluation / observations</th><th scope="col">Last updated</th></tr></thead>
          <tbody>
            {#each filtered as r (r.project + r.iid + r.sha)}
              {@const summary = reviewSummary(r)}
              <tr>
                <td><button class="text-left font-medium hover:underline" onclick={() => (selected = r)}>{r.project} !{r.iid}</button>{#if r.error}<p class="mt-1 text-xs text-danger">{r.error}</p>{/if}
                  <ReviewSummary review={r} limit={Number.POSITIVE_INFINITY} onopen={(review,focusFingerprint) => (selected = {...review,focusFingerprint})}/>
                </td>
                <td><Pill tone={statusTone(r.status)}>{reviewStatus(r.status)}</Pill>{#if r.detail?.run?.dry_run}<span class="block mt-1 text-xs text-muted">Dry run</span>{/if}</td>
                <td class="mono text-xs">{shortSha(r.sha)}</td>
                <td>{#if summary.available}{summary.verified} verified · {summary.refuted} refuted<div class="text-xs text-muted">{summary.unevaluated} not evaluated<br/>{summary.observed}/{summary.findings.length} observed</div>{:else}Not available{/if}</td>
                <td><time datetime={r.updated_at} title={r.updated_at}>{ago(r.updated_at)}</time></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>
{/if}

<style>
  .activity-table { width: 100%; min-width: 40rem; text-align: left; font-size: .875rem; }
  .activity-table th { font-weight: 500; color: var(--text-muted); }
  .activity-table th, .activity-table td { padding: 1rem 1rem 1rem 0; border-bottom: 1px solid var(--border); vertical-align: top; }
  .activity-table td:first-child { overflow-wrap: anywhere; max-width: 28rem; }
  .activity-table tr:hover { background: var(--surface-2); }
</style>
