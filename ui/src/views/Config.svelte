<script>
  import Card from "../components/Card.svelte";
  import Pill from "../components/Pill.svelte";

  let { data } = $props();
  const rows = $derived(data.config ?? []);

  function display(v) {
    if (v == null) return "—";
    if (Array.isArray(v)) return v.length ? v.join(", ") : "[]";
    if (typeof v === "object") return JSON.stringify(v);
    return String(v);
  }

  function changed(row) {
    return JSON.stringify(row.value) !== JSON.stringify(row.default);
  }
</script>

<div class="space-y-4">
  <Card>
    <p class="text-sm text-muted">
      Configuration shown <strong>read-only</strong>. Live, validated config
      editing is a later server phase and is intentionally out of scope here —
      edit <code class="mono">config/env.toml</code> directly; the poller
      re-reads it each cycle.
    </p>
  </Card>

  <Card title="Review configuration">
    <div class="overflow-x-auto">
      <table class="w-full table-fixed text-sm">
        <thead>
          <tr class="text-left text-xs text-muted uppercase">
            <th class="w-48 py-2 pr-4">Setting</th>
            <th class="w-40 py-2 pr-4">Value</th>
            <th class="w-32 py-2 pr-4">Default</th>
            <th class="py-2">Description</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-border align-top">
          {#each rows as row (row.name)}
            <tr>
              <td class="py-2 pr-4 mono break-words">
                {row.name}
                {#if changed(row)}<Pill tone="info">overridden</Pill>{/if}
              </td>
              <td class="py-2 pr-4 mono break-words">{display(row.value)}</td>
              <td class="py-2 pr-4 mono text-muted break-words">{display(row.default)}</td>
              <td class="py-2 text-muted">{row.description || "—"}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </Card>
</div>
