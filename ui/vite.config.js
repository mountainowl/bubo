import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { viteSingleFile } from "vite-plugin-singlefile";
import tailwindcss from "@tailwindcss/vite";

// Single-file build: the whole app (JS + CSS) inlines into one index.html so
// the export directory has no separate chunk files. This makes the file://
// story bulletproof — only data.json / data.js are loaded at runtime, and the
// app falls back to the inlined window.__BUBO_DATA__ (data.js) when fetch is
// blocked under the file: scheme. base:'./' keeps every asset reference
// relative so the same build works at file://, a sub-path on GitHub Pages /
// S3, or inside an iframe.
export default defineConfig({
  base: "./",
  plugins: [svelte(), tailwindcss(), viteSingleFile()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    target: "es2022",
  },
});
