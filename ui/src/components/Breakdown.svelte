<script>
  import { num } from "../lib/format.js";
  let { title, values = {} } = $props();
  const rows = $derived(Object.entries(values).sort((a,b) => b[1]-a[1]));
  const total = $derived(rows.reduce((sum, row) => sum + row[1], 0));
</script>
<div>
  <h3 class="mb-2 text-sm font-semibold">{title}</h3>
  {#if rows.length}
    <dl class="space-y-2">
      {#each rows as [name, count]}
        <div><div class="flex justify-between gap-3 text-sm"><dt class="break-words min-w-0">{name.replaceAll('_',' ')}</dt><dd>{num(count)}</dd></div>
          <meter class="h-2 w-full" aria-label={`${title}: ${name}`} min="0" max={Math.max(total,1)} value={count}></meter></div>
      {/each}
    </dl>
  {:else}<p class="text-sm text-muted">Not recorded.</p>{/if}
</div>
