# bubo operator UI (read-only v1)

A static SPA (Svelte 5 + Vite + Tailwind v4) that renders a `data.json`
snapshot produced by `bubo ui-export`. No server, no auth, no daemon.

## How it runs

`bubo ui-export --out DIR` writes:

- `data.json` — the snapshot (report + recent reviews + config schema + version)
- `data.js` — the same payload as `window.__BUBO_DATA__` (the `file://` fallback)
- `index.html` (+ inlined JS/CSS) — this built SPA

Open `DIR/index.html` via `file://`, host it on GitHub Pages / S3, or drop it in
an iframe. The page `fetch()`es `./data.json` when served and falls back to the
inlined `window.__BUBO_DATA__` under `file://` (where `fetch` of a sibling file
is blocked).

## Views

- **Dashboard** — health pill + version, in-flight/queue, recent reviews,
  today's posted / accept-rate / cost, failures.
- **Reviews** — filterable list → detail (the change, each finding with
  Issue/Impact/Evidence/Fix/Confidence rendered from the comment body, outcome,
  provenance band, tokens/cost, run-span timeline).
- **Reports** — windowed aggregates (today / 7d / 30d) with an exec-rollup
  preset (big numbers + sparklines).
- **Config** — read-only schema + values + descriptions. Editing is a later
  server phase, intentionally out of scope.

## Embedding

`?embed=1` strips the page chrome; the same build posts its height to a host
iframe (`postMessage {type:'bubo:height'}`) and accepts a theme-sync message
(`{type:'bubo:theme', theme:'light'|'dark'}`). `?theme=dark` forces a theme.

## Theming

All colors are CSS-variable design tokens (light/dark + brand indigo) in
`src/app.css`. Override the `--*` variables to re-theme the whole surface.

## Build / regenerate

For v1 the built assets in `dist/` are **committed** so the Python wheel ships
them without a Node build in CI. To regenerate after changing the source:

```sh
npm --prefix ui install
npm --prefix ui run build
```

`dist/` is force-included into the wheel under `bubo/_assets/ui/` (see
`pyproject.toml`); `bubo ui-export` copies it next to `data.json`.
