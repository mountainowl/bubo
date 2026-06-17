<script>
  // tone: ok | warn | danger | info | muted | brand
  let { tone = "muted", children } = $props();
  const map = {
    ok: "bg-ok-bg text-ok",
    warn: "bg-warn-bg text-warn",
    danger: "bg-danger-bg text-danger",
    info: "bg-info-bg text-info",
    brand: "bg-brand text-brand-fg",
    muted: "bg-surface-2 text-muted",
  };
</script>

<span
  class="inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium {map[tone] ?? map.muted}"
>
  {@render children?.()}
</span>
