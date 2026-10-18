<script>
  let { title, children } = $props();
</script>
<section class="metric-section">
  {#if title}<h2>{title}</h2>{/if}
  {@render children?.()}
</section>
<style>
  .metric-section { border-top: 1px solid var(--border); padding-top: 1.75rem; }
  h2 { margin: 0 0 1.3rem; color: var(--text); font-size: 1.18rem; font-weight: 560; letter-spacing: -.025em; line-height: 1.4; }
  @media (max-width: 600px) { .metric-section { padding-top: 1.35rem; } h2 { font-size: 1.08rem; margin-bottom: 1.1rem; } }
</style>
