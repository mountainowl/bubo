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

  const TABS = [
    { id: "dashboard", label: "Dashboard" },
    { id: "reviews", label: "Activity" },
    { id: "reports", label: "Reports" },
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
    let stopped = false;
    let timer;
    async function refresh() {
      try {
        const next = await loadSnapshot();
        if (stopped) return;
        data = next;
        error = null;
        if (next.meta?.live) timer = setTimeout(refresh, 5000);
      } catch {
        if (stopped) return;
        error = "Data is unavailable. Check the local dashboard process or regenerate the snapshot.";
        timer = setTimeout(refresh, 5000);
      }
    }
    refresh();
    if (isEmbed) {
      startHeightSync();
      listenForThemeSync((t) => (theme = t));
    }
    return () => { stopped = true; clearTimeout(timer); };
  });

  function openReview(r, focusFingerprint = null) {
    selectedReview = { ...r, focusFingerprint };
    tab = "reviews";
  }

  function toggleTheme() {
    theme = theme === "dark" ? "light" : "dark";
  }
</script>

<div class="mx-auto max-w-6xl p-4 sm:p-6">
  {#if !isEmbed}
    <header class="mb-8 flex flex-wrap items-center gap-x-8 gap-y-3 border-b border-border pb-4">
      <div class="flex items-center gap-2">
        <span class="text-xl font-bold text-brand">bubo</span>
        <span class="text-sm text-muted">operator</span>
      </div>
      <nav aria-label="Dashboard sections" class="order-3 flex w-full flex-wrap gap-5 sm:order-none sm:w-auto">
        {#each TABS as t (t.id)}
          <button
            class="border-b-2 py-2 text-sm font-medium transition-colors {tab === t.id ? 'border-brand text-text' : 'border-transparent text-muted hover:text-text'}"
            aria-current={tab === t.id ? "page" : undefined}
            onclick={() => (tab = t.id)}
          >
            {t.label}
          </button>
        {/each}
      </nav>
      <button
        class="ml-auto rounded-full p-2 text-muted hover:bg-surface-2 hover:text-text"
        onclick={toggleTheme}
        title="Toggle theme"
        aria-label={theme === "dark" ? "Use light theme" : "Use dark theme"}
      >
        {theme === "dark" ? "☀" : "☾"}
      </button>
    </header>
  {:else}
    <!-- Embedded: minimal tab strip only, no branding chrome. -->
    <nav aria-label="Dashboard sections" class="mb-6 flex flex-wrap gap-5">
      {#each TABS as t (t.id)}
        <button
          class="border-b-2 py-2 text-sm font-medium {tab === t.id ? 'border-brand text-text' : 'border-transparent text-muted hover:text-text'}"
          aria-current={tab === t.id ? "page" : undefined}
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
      <Dashboard {data} onopen={openReview} onreviews={() => { selectedReview = null; tab = "reviews"; }} />
    {:else if tab === "reviews"}
      <Reviews {data} bind:selected={selectedReview} />
    {:else if tab === "reports"}
      <Reports {data} onopen={openReview} />
    {/if}

    {#if !isEmbed}
      <footer class="mt-8 text-xs text-muted">
        Snapshot {data.meta?.generated_at} · schema v{data.meta?.schema_version} ·
        {data.meta?.live ? "Live local view · refreshes every 5 seconds" : "Read-only snapshot"}
      </footer>
    {/if}
  {/if}
</div>
