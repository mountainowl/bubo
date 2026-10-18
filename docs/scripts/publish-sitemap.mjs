import assert from 'node:assert/strict'
import { readdir, readFile, writeFile } from 'node:fs/promises'
import { join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const output = fileURLToPath(new URL('../out/', import.meta.url))
const base = 'https://mountainowl.github.io/bubo/'
const urls = []
const descriptions = new Set()

// Use exported pages so the sitemap cannot advertise an unbuilt source route.
async function visit(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.name.startsWith('_') || entry.name === '404' || entry.name === '404.html') continue
    const path = join(directory, entry.name)
    if (entry.isDirectory()) {
      await visit(path)
    } else if (entry.name.endsWith('.html')) {
      const route = relative(output, path).split(sep).join('/')
      const url = base + route.replace(/index\.html$/, '')
      const html = await readFile(path, 'utf8')
      const canonical = [...html.matchAll(/<link\b[^>]*rel="canonical"[^>]*>/g)]
      const description = [...html.matchAll(/<meta\b[^>]*name="description"[^>]*>/g)]
      assert.equal(canonical.length, 1, `${route}: expected one canonical`)
      assert.ok(canonical[0][0].includes(`href="${url}"`), `${route}: wrong canonical`)
      assert.equal(description.length, 1, `${route}: expected one description`)
      const content = description[0][0].match(/content="([^"]+)"/)?.[1]
      assert.ok(content, `${route}: missing description`)
      assert.ok(!descriptions.has(content), `${route}: duplicate description`)
      descriptions.add(content)
      assert.match(html, /<link rel="describedby" href="\/bubo\/llms\.txt"\s*\/?\s*>/, `${route}: missing llms link`)
      const schemas = [...html.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)]
        .map((match) => JSON.parse(match[1]))
      const software = schemas.find((schema) => schema['@type'] === 'SoftwareApplication')
      assert.equal(software?.offers?.['@type'], 'Offer', `${route}: missing free Offer`)
      assert.equal(software.offers.price, 0)
      assert.equal(software.offers.priceCurrency, 'USD')
      urls.push(url)
    }
  }
}

await visit(output)
assert.ok(urls.length, 'No documentation pages exported')
const escapeXml = (value) => value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
await writeFile(join(output, 'sitemap.xml'),
  '<?xml version="1.0" encoding="UTF-8"?>\n' +
  '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
  urls.sort().map((url) => `  <url><loc>${escapeXml(url)}</loc></url>`).join('\n') +
  '\n</urlset>\n')
console.log(`Validated SEO metadata and generated sitemap for ${urls.length} documentation pages.`)
