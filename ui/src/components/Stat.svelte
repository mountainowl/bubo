<script>
  let { label, value, sub = null, plain = false } = $props();
</script>

<div class={plain ? "stat-plain" : "rounded-[var(--radius-token)] border border-border bg-surface p-4"}>
  <div class={plain ? "stat-label" : "text-xs font-medium text-muted uppercase tracking-wide"}>{label}</div>
  <div class={plain ? "stat-value" : "mt-1 text-2xl font-semibold tabular-nums"}>{value}</div>
  {#if sub}<div class={plain ? "stat-context" : "mt-0.5 text-xs text-muted"}>{sub}</div>{/if}
</div>

<style>
  .stat-plain { min-width: 0; padding: .3rem 0 .6rem; }
  .stat-label { font-size: .78rem; line-height: 1.45; color: var(--text-muted); max-width: 24ch; }
  .stat-value { margin-top: .4rem; font-size: clamp(1.7rem, 3vw, 2.35rem); font-weight: 500; line-height: 1.15; letter-spacing: -.055em; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
  .stat-context { margin-top: .5rem; max-width: 29ch; font-size: .72rem; line-height: 1.5; color: var(--text-muted); }
</style>
