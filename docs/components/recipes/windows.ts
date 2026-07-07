import type { OsFragment } from './types'

export const windows: OsFragment = {
  label: 'Windows',
  icon: '/windows.svg',
  shell: 'powershell',
  configPath: '$env:USERPROFILE\\.local\\share\\bubo\\config\\env.toml',
  prereqInstall: (cli) => `# PowerShell
irm https://astral.sh/uv/install.ps1 | iex
winget install Git.Git OpenJS.NodeJS GitHub.cli
${cli}`,
  exportLine: (name, ph) => `$env:${name} = "${ph}"`,
}
