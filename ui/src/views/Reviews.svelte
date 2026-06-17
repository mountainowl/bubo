<script>
  import Card from "../components/Card.svelte";
  import Pill from "../components/Pill.svelte";
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
  <ReviewDetail review={current} onback={() => (selected = null)} />
{:else}
  <div class="space-y-4">
    <div class="flex flex-wrap items-center gap-2">
      <input
        class="flex-1 min-w-48 rounded-[var(--radius-token)] border border-border bg-surface px-3 py-1.5 text-sm"
        placeholder="Filter by project / iid / sha…"
        bind:value={query}
      />
      <select
        class="rounded-[var(--radius-token)] border border-border bg-surface px-3 py-1.5 text-sm"
        bind:value={statusFilter}
      >
        <option value="all">all statuses</option>
        {#each statuses as s (s)}<option value={s}>{s}</option>{/each}
      </select>
      <span class="text-xs text-muted">{filtered.length} of {reviews.length}</span>
    </div>

    <Card>
      {#if filtered.length === 0}
        <p class="text-sm text-muted">No reviews match.</p>
      {:else}
        <ul class="divide-y divide-border">
          {#each filtered as r (r.project + r.iid + r.sha)}
            <li>
              <button
                class="flex w-full items-center gap-3 py-2 px-1 text-left hover:bg-surface-2 rounded"
                onclick={() => (selected = r)}
              >
                <Pill tone={statusTone(r.status)}>{r.status}</Pill>
                <span class="truncate text-sm font-medium">{r.project}</span>
                <span class="text-sm text-muted">!{r.iid}</span>
                <span class="mono text-xs text-muted">{shortSha(r.sha)}</span>
                <span class="ml-auto text-xs text-muted">
                  {(r.detail?.findings?.length ?? 0)} findings · {ago(r.updated_at)}
                </span>
              </button>
            </li>
          {/each}
        </ul>
      {/if}
    </Card>
  </div>
{/if}
