<script>
  import { loadSnapshot } from "./lib/data.js";
  import {
    isEmbed,
    initialTheme,
    applyTheme,
    startHeightSync,
    listenForThemeSync,
  } from "./lib/embed.js";
  import Dashboard from "./views/Dashboard.svelte";
  import Reviews from "./views/Reviews.svelte";
  import Reports from "./views/Reports.svelte";
  import Config from "./views/Config.svelte";

  const TABS = [
    { id: "dashboard", label: "Dashboard" },
    { id: "reviews", label: "Reviews" },
    { id: "reports", label: "Reports" },
    { id: "config", label: "Config" },
  ];

  let data = $state(null);
  let error = $state(null);
  let tab = $state("dashboard");
  let theme = $state(initialTheme());
  // Bindable selected-review so the dashboard can deep-link into Reviews.
  let selectedReview = $state(null);

  // Tag <body> for embed + apply theme reactively.
  $effect(() => {
    document.body.classList.toggle("embed", isEmbed);
  });
  $effect(() => applyTheme(theme));

  // One-time: load the snapshot, wire iframe height + theme sync.
  $effect(() => {
    loadSnapshot()
      .then((d) => (data = d))
      .catch((e) => (error = String(e)));
    if (isEmbed) {
      startHeightSync();
      listenForThemeSync((t) => (theme = t));
    }
  });

  function openReview(r) {
    selectedReview = r;
    tab = "reviews";
  }

  function toggleTheme() {
    theme = theme === "dark" ? "light" : "dark";
  }
</script>

<div class="mx-auto max-w-6xl p-4 sm:p-6">
  {#if !isEmbed}
    <header class="mb-5 flex items-center gap-4">
      <div class="flex items-center gap-2">
        <span class="text-xl font-bold text-brand">bubo</span>
        <span class="text-sm text-muted">operator</span>
      </div>
      <nav class="flex gap-1">
        {#each TABS as t (t.id)}
          <button
            class="rounded-[var(--radius-token)] px-3 py-1.5 text-sm font-medium {tab === t.id ? 'bg-brand text-brand-fg' : 'text-muted hover:bg-surface-2'}"
            onclick={() => (tab = t.id)}
          >
            {t.label}
          </button>
        {/each}
      </nav>
      <button
        class="ml-auto rounded-[var(--radius-token)] border border-border px-2 py-1 text-sm"
        onclick={toggleTheme}
        title="Toggle theme"
      >
        {theme === "dark" ? "☀" : "☾"}
      </button>
    </header>
  {:else}
    <!-- Embedded: minimal tab strip only, no branding chrome. -->
    <nav class="mb-4 flex gap-1">
      {#each TABS as t (t.id)}
        <button
          class="rounded-[var(--radius-token)] px-2.5 py-1 text-xs font-medium {tab === t.id ? 'bg-brand text-brand-fg' : 'text-muted hover:bg-surface-2'}"
          onclick={() => (tab = t.id)}
        >
          {t.label}
        </button>
      {/each}
    </nav>
  {/if}

  {#if error}
    <div class="rounded-[var(--radius-token)] border border-border bg-danger-bg p-4 text-sm text-danger">
      Could not load snapshot: {error}
      <div class="mt-1 text-muted">
        Generate one with <code class="mono">bubo ui-export</code>.
      </div>
    </div>
  {:else if !data}
    <p class="text-sm text-muted">Loading snapshot…</p>
  {:else}
    {#if tab === "dashboard"}
      <Dashboard {data} onopen={openReview} />
    {:else if tab === "reviews"}
      <Reviews {data} bind:selected={selectedReview} />
    {:else if tab === "reports"}
      <Reports {data} />
    {:else if tab === "config"}
      <Config {data} />
    {/if}

    {#if !isEmbed}
      <footer class="mt-8 text-xs text-muted">
        Snapshot {data.meta?.generated_at} · schema v{data.meta?.schema_version} ·
        read-only export (no live data, no config write)
      </footer>
    {/if}
  {/if}
</div>
