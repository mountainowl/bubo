// Prefix a public-asset path (e.g. "/github.svg") with the deploy basePath so raw
// <img>/mask-image/background refs resolve on the GitHub Pages subpath. Empty in
// dev and preview; "/bubo" in the Pages build (set via NEXT_PUBLIC_BASE_PATH).
// Next rewrites <Link> and _next/ assets automatically, but not raw asset URLs.
export const asset = (path: string): string =>
  `${process.env.NEXT_PUBLIC_BASE_PATH ?? ''}${path}`
