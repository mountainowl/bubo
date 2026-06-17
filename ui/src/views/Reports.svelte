<script>
  import Card from "../components/Card.svelte";
  import Stat from "../components/Stat.svelte";
  import Sparkline from "../components/Sparkline.svelte";
  import { usd, num, pct, secs } from "../lib/format.js";

  let { data } = $props();

  const windows = $derived(data.reports ?? []);
  let windowLabel = $state("7d");
  let exec = $state(false);

  const current = $derived(
    windows.find((w) => w.label === windowLabel)?.report ?? windows[0]?.report,
  );
  const reviews = $derived(current?.reviews ?? {});
  const outcomes = $derived(current?.outcomes ?? {});
  const latency = $derived(current?.latency ?? {});
  const roi = $derived(current?.roi ?? {});
  const noise = $derived(current?.noise_trend ?? []);
  const disputeClasses = $derived(current?.dispute_classes ?? []);

  // Sparkline series from the per-day noise trend.
  const findingsSeries = $derived(noise.map((b) => b.findings));
  const fpSeries = $derived(noise.map((b) => b.false_positive));
</script>

<div class="space-y-4">
  <div class="flex flex-wrap items-center gap-2">
    <div class="flex gap-1 rounded-[var(--radius-token)] border border-border p-0.5">
      {#each windows as w (w.label)}
        <button
          class="rounded px-3 py-1 text-sm {windowLabel === w.label ? 'bg-brand text-brand-fg' : 'text-muted hover:bg-surface-2'}"
          onclick={() => (windowLabel = w.label)}
        >
          {w.label}
        </button>
      {/each}
    </div>
    <label class="ml-auto flex items-center gap-2 text-sm text-muted">
      <input type="checkbox" bind:checked={exec} /> exec rollup
    </label>
  </div>

  {#if exec}
    <!-- Exec rollup preset: big numbers + sparklines. -->
    <div class="grid gap-4 sm:grid-cols-3">
      <Stat label="Total reviews" value={num(reviews.reviews_total)} />
      <Stat label="Findings acted on" value={num(roi.accepted)} sub={`${num(roi.blocking_accepted)} blocking`} />
      <Stat label="Total cost" value={usd(reviews.cost_usd_sum)} />
      <Stat label="Accept rate" value={pct(outcomes.accept_rate)} />
      <Stat label="Accepted / $" value={num(roi.accepted_per_usd)} />
      <Stat label="Dispute rate" value={pct(outcomes.dispute_rate)} />
    </div>
    <div class="grid gap-4 sm:grid-cols-2">
      <Card title="Findings / day">
        <Sparkline values={findingsSeries} width={300} height={48} />
      </Card>
      <Card title="False positives / day">
        <Sparkline values={fpSeries} width={300} height={48} stroke="var(--danger)" />
      </Card>
    </div>
  {:else}
    <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <Stat label="Runs" value={num(reviews.reviews_total)} />
      <Stat label="Findings" value={num(reviews.findings_total)} />
      <Stat label="Accept rate" value={pct(outcomes.accept_rate)} />
      <Stat label="Dispute rate" value={pct(outcomes.dispute_rate)} />
      <Stat label="False-positive rate" value={pct(outcomes.false_positive_rate)} />
      <Stat label="Cost" value={usd(reviews.cost_usd_sum)} sub={`${num(reviews.tokens_total_sum)} tokens`} />
      <Stat label="Latency p50" value={secs(latency.p50_seconds)} />
      <Stat label="Latency p95" value={secs(latency.p95_seconds)} />
    </div>

    <div class="grid gap-4 lg:grid-cols-2">
      <Card title="Noise trend (findings / FP per day)">
        {#if noise.length === 0}
          <p class="text-sm text-muted">No per-day outcome data in this window.</p>
        {:else}
          <div class="flex items-center gap-4">
            <Sparkline values={findingsSeries} width={260} height={48} />
            <Sparkline values={fpSeries} width={260} height={48} stroke="var(--danger)" />
          </div>
        {/if}
      </Card>

      <Card title="Per-category dispute rates">
        {#if disputeClasses.length === 0}
          <p class="text-sm text-muted">
            No per-category dispute data (this is reported per-project; the
            export covers all projects).
          </p>
        {:else}
          <ul class="space-y-1 text-sm">
            {#each disputeClasses as c (c.category)}
              <li class="flex items-center gap-2">
                <span class="w-32 truncate">{c.category}</span>
                <span class="text-muted">{pct(c.dispute_rate)}</span>
                <span class="text-xs text-muted">({c.rejected}/{c.total})</span>
              </li>
            {/each}
          </ul>
        {/if}
      </Card>
    </div>
  {/if}
</div>
