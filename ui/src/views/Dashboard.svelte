<script>
  import Card from "../components/Card.svelte";
  import Stat from "../components/Stat.svelte";
  import Pill from "../components/Pill.svelte";
  import { usd, num, pct, ago, shortSha, statusTone } from "../lib/format.js";

  let { data, onopen } = $props();

  const health = $derived(data.health ?? { status: "empty" });
  const healthTone = $derived(
    health.status === "ok" ? "ok" : health.status === "stale" ? "danger" : "muted",
  );

  // "today" window = the 24h report; the dashboard's posted/accept/cost tiles.
  const today = $derived((data.reports ?? []).find((r) => r.label === "today")?.report);
  const reviewsToday = $derived(today?.reviews ?? {});
  const outcomesToday = $derived(today?.outcomes ?? {});
  const acceptRate = $derived(outcomesToday.accept_rate);
  const recent = $derived(data.dashboard?.recent ?? []);
  const failures = $derived(recent.filter((r) => r.status === "failed"));
</script>

<div class="space-y-5">
  <!-- Health + version banner -->
  <div class="flex flex-wrap items-center gap-3">
    <Pill tone={healthTone}>
      <span class="h-2 w-2 rounded-full bg-current"></span>
      health: {health.status}
    </Pill>
    {#if health.last_status}
      <span class="text-sm text-muted">
        last {health.last_status} · {ago(health.last_updated_at)}
      </span>
    {/if}
    <span class="ml-auto text-xs text-muted">
      bubo {data.version?.installed ?? "?"}
      {#if data.version?.update}· update {data.version.update} available{/if}
    </span>
  </div>

  {#if !data.meta?.db_present}
    <Card>
      <p class="text-sm text-muted">
        No state database found yet. Run a review (or <code class="mono">bubo init</code>)
        and re-run <code class="mono">bubo ui-export</code> to populate this view.
      </p>
    </Card>
  {/if}

  <!-- In-flight / queue + today's numbers -->
  <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
    <Stat label="In-flight / queued" value={num(data.inflight)} />
    <Stat label="Reviews (24h)" value={num(reviewsToday.reviews_total)} />
    <Stat label="Accept rate (24h)" value={pct(acceptRate)} sub={`${num(outcomesToday.resolved)} resolved`} />
    <Stat label="Cost (24h)" value={usd(reviewsToday.cost_usd_sum)} sub={`${num(reviewsToday.tokens_total_sum)} tokens`} />
  </div>

  <div class="grid gap-5 lg:grid-cols-3">
    <!-- Recent reviews -->
    <div class="lg:col-span-2">
      <Card title="Recent reviews">
        {#if recent.length === 0}
          <p class="text-sm text-muted">No reviews recorded.</p>
        {:else}
          <ul class="divide-y divide-border">
            {#each recent as r (r.project + r.iid + r.sha)}
              <li>
                <button
                  class="flex w-full items-center gap-3 py-2 text-left hover:bg-surface-2 rounded px-1"
                  onclick={() => onopen(r)}
                >
                  <Pill tone={statusTone(r.status)}>{r.status}</Pill>
                  <span class="truncate text-sm font-medium">{r.project}</span>
                  <span class="text-sm text-muted">!{r.iid}</span>
                  <span class="mono text-xs text-muted">{shortSha(r.sha)}</span>
                  <span class="ml-auto text-xs text-muted">{ago(r.updated_at)}</span>
                </button>
              </li>
            {/each}
          </ul>
        {/if}
      </Card>
    </div>

    <!-- Failures -->
    <Card title="Recent failures">
      {#if failures.length === 0}
        <p class="text-sm text-muted">No failures in recent activity.</p>
      {:else}
        <ul class="space-y-2">
          {#each failures as f (f.project + f.iid + f.sha)}
            <li class="text-sm">
              <button class="text-left hover:underline" onclick={() => onopen(f)}>
                <span class="font-medium">{f.project}</span> !{f.iid}
              </button>
              {#if f.error}
                <div class="mono text-xs text-danger truncate">{f.error}</div>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    </Card>
  </div>
</div>
