import { copyFile, mkdir } from 'node:fs/promises'

const outputDirectory = new URL('../out/', import.meta.url)

await mkdir(outputDirectory, { recursive: true })
await copyFile(new URL('../../llms.txt', import.meta.url), new URL('llms.txt', outputDirectory))
