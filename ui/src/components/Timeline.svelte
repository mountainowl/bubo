<script>
  import { secs, ago } from "../lib/format.js";
  // The DB records only the run span (started_at/finished_at) per run — there
  // are no per-stage (checkout/agent/post) spans in the schema yet. We show the
  // honest end-to-end span and label it as such rather than fabricate stages.
  let { started_at = null, finished_at = null } = $props();

  const duration = $derived.by(() => {
    if (!started_at || !finished_at) return null;
    const s = new Date(started_at).getTime();
    const f = new Date(finished_at).getTime();
    if (Number.isNaN(s) || Number.isNaN(f)) return null;
    return (f - s) / 1000;
  });
</script>

<div class="space-y-2">
  <div class="flex items-center gap-3 text-sm">
    <span class="h-2 w-2 rounded-full bg-brand"></span>
    <span class="text-muted w-20">checkout</span>
    <span class="flex-1 border-t border-dashed border-border"></span>
    <span class="text-muted w-20">agent</span>
    <span class="flex-1 border-t border-dashed border-border"></span>
    <span class="text-muted w-16 text-right">post</span>
  </div>
  <div class="flex justify-between text-xs text-muted">
    <span>started {ago(started_at)}</span>
    {#if duration != null}
      <span class="font-medium text-text">end-to-end {secs(duration)}</span>
    {:else}
      <span>in progress / no finish time</span>
    {/if}
    <span>{finished_at ? `finished ${ago(finished_at)}` : "—"}</span>
  </div>
  <p class="text-xs text-muted italic">
    Per-stage spans (checkout / agent / post) are not yet recorded in the DB —
    this shows the recorded run span only.
  </p>
</div>
