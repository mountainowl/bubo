<script>
  let { title = null, children } = $props();
</script>

<div class="rounded-[var(--radius-token)] border border-border bg-surface p-4">
  {#if title}
    <h2 class="mb-3 text-sm font-semibold text-muted uppercase tracking-wide">{title}</h2>
  {/if}
  {@render children?.()}
</div>
