<script>
  // Inline SVG sparkline over a numeric series. Pure presentation; the series
  // comes straight from a precomputed report section (e.g. noise_trend).
  let { values = [], width = 120, height = 28, stroke = "var(--brand)" } = $props();

  const points = $derived.by(() => {
    const xs = values.map((v) => Number(v) || 0);
    if (xs.length === 0) return "";
    const max = Math.max(...xs, 1);
    const min = Math.min(...xs, 0);
    const span = max - min || 1;
    const stepX = xs.length > 1 ? width / (xs.length - 1) : 0;
    return xs
      .map((v, i) => {
        const x = i * stepX;
        const y = height - ((v - min) / span) * (height - 2) - 1;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  });
</script>

{#if values.length}
  <svg {width} {height} viewBox={`0 0 ${width} ${height}`} class="overflow-visible">
    <polyline
      fill="none"
      {stroke}
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"
      points={points}
    />
  </svg>
{:else}
  <span class="text-xs text-muted">no data</span>
{/if}
