<script>
  let { label, value, sub = null } = $props();
</script>

<div class="rounded-[var(--radius-token)] border border-border bg-surface p-4">
  <div class="text-xs font-medium text-muted uppercase tracking-wide">{label}</div>
  <div class="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
  {#if sub}<div class="mt-0.5 text-xs text-muted">{sub}</div>{/if}
</div>
