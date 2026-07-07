import type { OsFragment } from './types'

export const mac: OsFragment = {
  label: 'macOS',
  icon: '/apple.svg',
  shell: 'bash',
  configPath: '~/.local/share/bubo/config/env.toml',
  prereqInstall: (cli) => `# Homebrew — runtime, Git, provider CLIs, Node (for the agent CLI)
brew install uv git node glab gh
${cli}`,
  exportLine: (name, ph) => `export ${name}="${ph}"`,
}
