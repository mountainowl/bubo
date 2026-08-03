import { readFileSync } from 'node:fs'
import nextra from 'nextra'

const withNextra = nextra({
  theme: 'nextra-theme-docs',
  themeConfig: './theme.config.tsx',
  defaultShowCopyCode: true,
})

// Static export for GitHub Pages. basePath is '/bubo' in CI (the project-site
// subpath) and empty locally so `next dev` serves at the root.
const basePath = process.env.NEXT_PUBLIC_BASE_PATH || ''
const pyproject = readFileSync(new URL('../pyproject.toml', import.meta.url), 'utf8')
const versionMatch = pyproject.match(/\[project\][\s\S]*?^version\s*=\s*"([^"]+)"/m)

if (!versionMatch) {
  throw new Error('Unable to read the Bubo version from pyproject.toml')
}

const buboVersion = versionMatch[1]

export default withNextra({
  output: 'export',
  images: { unoptimized: true },
  trailingSlash: true,
  basePath,
  env: { NEXT_PUBLIC_BUBO_VERSION: buboVersion },
})
