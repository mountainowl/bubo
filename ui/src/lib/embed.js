// Embed + theming glue. The SAME build is both the standalone page and the
// iframe surface:
//   * ?embed=1 strips the page chrome (handled in App.svelte) and tags <body>.
//   * As an iframe, we post our content height to the host so it can size the
//     frame, and we accept a {type:'bubo:theme', theme:'light'|'dark'} message
//     so the host can sync our theme to its own.
// No host is required — outside an iframe these are harmless no-ops.

const params = new URLSearchParams(location.search);

export const isEmbed = params.get("embed") === "1";

// Initial theme: ?theme=dark wins, else the host's prefers-color-scheme.
export function initialTheme() {
  const q = params.get("theme");
  if (q === "dark" || q === "light") return q;
  if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
    return "dark";
  }
  return "light";
}

export function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
}

// Report our height to the host (debounced via rAF) so an iframe can grow/shrink.
export function startHeightSync() {
  if (window.parent === window) return; // not framed — nothing to sync
  let queued = false;
  const post = () => {
    queued = false;
    const height = document.documentElement.scrollHeight;
    window.parent.postMessage({ type: "bubo:height", height }, "*");
  };
  const schedule = () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(post);
  };
  new ResizeObserver(schedule).observe(document.documentElement);
  schedule();
}

// Accept theme-sync messages from the host. Returns the live theme setter so
// the caller can re-render. Host sends { type:'bubo:theme', theme:'dark' }.
export function listenForThemeSync(setTheme) {
  window.addEventListener("message", (ev) => {
    const data = ev.data;
    if (data && data.type === "bubo:theme" && (data.theme === "dark" || data.theme === "light")) {
      setTheme(data.theme);
    }
  });
}
