// Snapshot loader. The app is a static page over a data.json the Python
// `bubo ui-export` writes. Two load paths, in order:
//   1. fetch('./data.json') — served builds (GitHub Pages / S3 / iframe),
//      where the snapshot can be refreshed by re-running the export.
//   2. window.__BUBO_DATA__ — the file:// fallback (data.js), since Chromium
//      blocks fetch() of a sibling file under the file: scheme.
// Either way the same shape is returned.

export async function loadSnapshot() {
  // file:// can't fetch a sibling reliably — prefer the inlined global there.
  const isFile = location.protocol === "file:";
  if (!isFile) {
    try {
      const res = await fetch("./data.json", { cache: "no-store" });
      if (res.ok) return await res.json();
    } catch {
      /* fall through to the inlined global */
    }
  }
  if (typeof window !== "undefined" && window.__BUBO_DATA__) {
    return window.__BUBO_DATA__;
  }
  // Last resort for file:// when data.js failed to load too.
  try {
    const res = await fetch("./data.json", { cache: "no-store" });
    if (res.ok) return await res.json();
  } catch {
    /* nothing more to try */
  }
  throw new Error("no snapshot: data.json not reachable and window.__BUBO_DATA__ unset");
}
