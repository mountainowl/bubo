import nextra from 'nextra'

const withNextra = nextra({
  theme: 'nextra-theme-docs',
  themeConfig: './theme.config.tsx',
  defaultShowCopyCode: true,
})

// Static export for GitHub Pages. basePath is '/bubo' in CI (the project-site
// subpath) and empty locally so `next dev` serves at the root.
const basePath = process.env.NEXT_PUBLIC_BASE_PATH || ''

export default withNextra({
  output: 'export',
  images: { unoptimized: true },
  trailingSlash: true,
  basePath,
})
