import type { OsFragment } from './types'

export const linux: OsFragment = {
  label: 'Linux',
  icon: '/linux.svg',
  shell: 'bash',
  configPath: '~/.local/share/bubo/config/env.toml',
  prereqInstall: (cli) => `# uv (Python 3.14+ toolchain)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Debian/Ubuntu — Git, Node, GitHub CLI
sudo apt-get update && sudo apt-get install -y git nodejs npm gh
# glab (GitLab CLI): https://gitlab.com/gitlab-org/cli#installation
${cli}`,
  exportLine: (name, ph) => `export ${name}="${ph}"`,
}
