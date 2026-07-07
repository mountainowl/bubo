import { useState, type Dispatch, type SetStateAction } from 'react'
import styles from './quickstart.module.css'
import { asset } from './asset'

export type Agent = 'codex' | 'claude'
export type OS = 'mac' | 'linux' | 'windows'
export type Method = 'uv' | 'pip' | 'docker' | 'source'
export type Scm = 'gitlab' | 'github'

export function CodeBlock({ code, download }: { code: string; download?: string }) {
  const [copied, setCopied] = useState(false)
  const [downloaded, setDownloaded] = useState(false)
  return (
    <div className={styles.code}>
      <div className={styles.codeActions}>
        {download && (
          <button
            className={styles.iconBtn}
            title={downloaded ? 'Downloaded' : 'Download script'}
            aria-label={downloaded ? 'Downloaded' : 'Download script'}
            onClick={() => {
              const blob = new Blob([code], { type: 'text/plain;charset=utf-8' })
              const url = URL.createObjectURL(blob)
              const link = document.createElement('a')
              link.href = url
              link.download = download
              document.body.appendChild(link)
              link.click()
              link.remove()
              URL.revokeObjectURL(url)
              setDownloaded(true)
              setTimeout(() => setDownloaded(false), 1200)
            }}
          >
            {downloaded ? (
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <polyline points="20 6 9 17 4 12" />
              </svg>
            ) : (
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
            )}
          </button>
        )}
        <button
          className={styles.iconBtn}
          title={copied ? 'Copied' : 'Copy'}
          aria-label={copied ? 'Copied' : 'Copy'}
          onClick={() => {
            navigator.clipboard?.writeText(code)
            setCopied(true)
            setTimeout(() => setCopied(false), 1200)
          }}
        >
          {copied ? (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <polyline points="20 6 9 17 4 12" />
            </svg>
          ) : (
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
            </svg>
          )}
        </button>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  )
}

export function Tabs<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { id: T; label: string; icon?: string }[]
  value: T
  onChange: Dispatch<SetStateAction<T>>
}) {
  return (
    <div className={styles.tabs}>
      {options.map((o) => (
        <button
          key={o.id}
          className={value === o.id ? `${styles.tab} ${styles.tabActive}` : styles.tab}
          onClick={() => onChange(o.id)}
        >
          {o.icon && (
            <span
              className={styles.tabIcon}
              style={{
                WebkitMaskImage: `url(${asset(`/${o.icon}.svg`)})`,
                maskImage: `url(${asset(`/${o.icon}.svg`)})`,
              }}
              aria-hidden="true"
            />
          )}
          {o.label}
        </button>
      ))}
    </div>
  )
}

export const AGENT = {
  codex: {
    label: 'OpenAI',
    icon: 'openai',
    tag: 'bundled default',
    desc: 'Codex CLI',
    cli: 'npm install -g @openai/codex',
    cliName: 'Codex',
    keyEnv: 'OPENAI_API_KEY',
    keyUrl: 'https://platform.openai.com/api-keys',
    keyName: 'OpenAI',
    model: 'gpt-5.5',
  },
  claude: {
    label: 'Anthropic',
    icon: 'anthropic',
    tag: '',
    desc: 'Claude CLI — set reviewer_command and go.',
    cli: 'npm install -g @anthropic-ai/claude-code',
    cliName: 'Claude',
    keyEnv: 'ANTHROPIC_API_KEY',
    keyUrl: 'https://console.anthropic.com/settings/keys',
    keyName: 'Anthropic',
    model: 'claude-opus-4-8',
  },
} as const

export const OS_PREREQS: Record<OS, string> = {
  mac: `# Homebrew — runtime, Git, provider CLIs, Node (for the agent CLI)
brew install uv git node glab gh`,
  linux: `# uv (Python 3.14+ toolchain)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Debian/Ubuntu — Git, Node, GitHub CLI
sudo apt-get update && sudo apt-get install -y git nodejs npm gh

# GitLab CLI (glab): https://gitlab.com/gitlab-org/cli#installation`,
  windows: `# PowerShell
irm https://astral.sh/uv/install.ps1 | iex
winget install Git.Git OpenJS.NodeJS GitHub.cli`,
}

export const METHOD: Record<Method, string> = {
  uv: `uv tool install bubo`,
  pip: `pip install bubo
# or, isolated from your global packages:
pipx install bubo`,
  docker: `docker pull ghcr.io/mountainowl/bubo
# multi-arch image — your chosen review-agent CLI is bring-your-own inside it`,
  source: `uv tool install git+https://github.com/mountainowl/bubo`,
}

export const SCM: Record<
  Scm,
  { label: string; env: string; prefix: string; tokenHint: string; tokenUrl: string; tokenLabel: string; path: string }
> = {
  gitlab: {
    label: 'GitLab',
    env: 'GITLAB_TOKEN',
    prefix: 'glpat-...',
    tokenHint: 'a token with API scope',
    tokenUrl: 'https://gitlab.com/-/user_settings/personal_access_tokens',
    tokenLabel: 'gitlab.com → access tokens',
    path: 'your-group/your-repo',
  },
  github: {
    label: 'GitHub',
    env: 'GITHUB_TOKEN',
    prefix: 'ghp-...',
    tokenHint: 'a PAT with pull-request read + write',
    tokenUrl: 'https://github.com/settings/tokens',
    tokenLabel: 'github.com → settings → tokens',
    path: 'owner/repo',
  },
}

export const INSTALL_LINE: Record<'uv' | 'pip' | 'source', string> = {
  uv: 'uv tool install bubo',
  pip: 'pip install bubo',
  source: 'uv tool install git+https://github.com/mountainowl/bubo',
}

export function buildScript(agent: Agent, os: OS, scm: Scm, install: Method): string {
  const a = AGENT[agent]
  const s = SCM[scm]

  // Docker → bubo's image is BYO-agent, so derive one with the CLI baked in and
  // persist /home/bubo (config + agent profile + SQLite) via one bind mount.
  if (install === 'docker') {
    const cliPkg = a.cli.replace(/^npm install -g /, '')
    if (os === 'windows') {
      const reviewerPwsh = agent === 'claude' ? `\nreviewer_command = ["claude", "-p"]` : ''
      return `# Windows PowerShell — paste into a PowerShell prompt (Docker Desktop required)

# ── edit these three, then run the whole script ──
$env:${a.keyEnv} = "<your-${a.keyName.toLowerCase()}-key>"
$env:${s.env} = "<${s.prefix}>"
$PROJECT = "${s.path}"
# ─────────────────────────────────────────────────

$WORKDIR = "$env:USERPROFILE\\bubo-docker"
New-Item -ItemType Directory -Force -Path "$WORKDIR\\home" | Out-Null
Set-Location $WORKDIR

# 1) derive an image with the ${a.cliName} CLI baked in (bubo ships BYO-agent)
@"
FROM ghcr.io/mountainowl/bubo
USER root
RUN apt-get update \\
 && apt-get install -y --no-install-recommends nodejs npm \\
 && npm install -g ${cliPkg} \\
 && rm -rf /var/lib/apt/lists/*
USER bubo
"@ | Set-Content -Path Dockerfile -Encoding utf8
docker build -t bubo-local .

# 2) run bubo in the container, persisting its home
function brun { docker run --rm -v "$WORKDIR\\home:/home/bubo" -e ${a.keyEnv} -e ${s.env} bubo-local @args }

# 3) initialise, then write config
brun bubo init
@"
[scm]
provider = "${scm}"

[${scm}]
token = "$env:${s.env}"

[agents]
llm_model       = "${a.model}"
llm_api_key_env = "${a.keyEnv}"${reviewerPwsh}

[[projects]]
path = "$PROJECT"
enabled = true
"@ | Set-Content -Path "$WORKDIR\\home\\.local\\share\\bubo\\config\\env.toml" -Encoding utf8

# 4) verify + first (dry-run) review
brun bubo doctor
brun bubo-poller`
    }
    const reviewer = agent === 'claude' ? `\nreviewer_command = ["claude", "-p"]` : ''
    return `#!/usr/bin/env bash
set -euo pipefail

# ── edit these three, then run the whole script ──
export ${a.keyEnv}="<your-${a.keyName.toLowerCase()}-key>"
export ${s.env}="<${s.prefix}>"
export PROJECT="${s.path}"
# ─────────────────────────────────────────────────

WORKDIR="\${BUBO_DOCKER_DIR:-$HOME/bubo-docker}"
mkdir -p "$WORKDIR/home"
cd "$WORKDIR"

# 1) derive an image with the ${a.cliName} CLI baked in (bubo ships BYO-agent)
cat > Dockerfile <<'DOCKER'
FROM ghcr.io/mountainowl/bubo
USER root
RUN apt-get update \\
 && apt-get install -y --no-install-recommends nodejs npm \\
 && npm install -g ${cliPkg} \\
 && rm -rf /var/lib/apt/lists/*
USER bubo
DOCKER
docker build -t bubo-local .

# 2) run bubo in the container, persisting its home (config, agent profile, SQLite)
#    native Linux: if bind-mount writes fail, add  --user "$(id -u):$(id -g)"
brun() {
  docker run --rm -v "$WORKDIR/home:/home/bubo" \\
    -e ${a.keyEnv} -e ${s.env} bubo-local "$@"
}

# 3) initialise, then write config
brun bubo init
cat > "$WORKDIR/home/.local/share/bubo/config/env.toml" <<EOF
[scm]
provider = "${scm}"

[${scm}]
token = "\${${s.env}}"

[agents]
llm_model       = "${a.model}"
llm_api_key_env = "${a.keyEnv}"${reviewer}

[[projects]]
path = "\${PROJECT}"
enabled = true
EOF

# 4) verify + first (dry-run) review
brun bubo doctor
brun bubo-poller`
  }

  // Windows → native PowerShell (no bash/WSL assumed)
  if (os === 'windows') {
    const installPwsh = { uv: 'uv tool install bubo', pip: 'pip install bubo', source: 'uv tool install git+https://github.com/mountainowl/bubo' }[install]
    const reviewerPwsh = agent === 'claude' ? `\nreviewer_command = ["claude", "-p"]` : ''
    return `# Windows PowerShell — paste into a PowerShell prompt

# ── edit these three, then run the whole script ──
$env:${a.keyEnv} = "<your-${a.keyName.toLowerCase()}-key>"
$env:${s.env} = "<${s.prefix}>"
$PROJECT = "${s.path}"
# ─────────────────────────────────────────────────

# 1) prerequisites + ${a.cliName} CLI
irm https://astral.sh/uv/install.ps1 | iex
winget install Git.Git OpenJS.NodeJS GitHub.cli
${a.cli}

# 2) install bubo
${installPwsh}

# 3) initialise + write config
bubo init
$root = if ($env:BUBO_ROOT) { $env:BUBO_ROOT } else { "$env:USERPROFILE\\.local\\share\\bubo" }
@"
[scm]
provider = "${scm}"

[${scm}]
token = "$env:${s.env}"

[agents]
llm_model       = "${a.model}"
llm_api_key_env = "${a.keyEnv}"${reviewerPwsh}

[[projects]]
path = "$PROJECT"
enabled = true
"@ | Set-Content -Path "$root\\config\\env.toml" -Encoding utf8

# 4) verify + run the first (dry-run) review
bubo doctor
bubo-poller`
  }

  const prereq =
    os === 'mac'
      ? 'brew install uv git node glab gh'
      : `curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt-get update && sudo apt-get install -y git nodejs npm gh
# glab (GitLab CLI): https://gitlab.com/gitlab-org/cli#installation`
  const reviewer = agent === 'claude' ? `\nreviewer_command = ["claude", "-p"]` : ''
  return `#!/usr/bin/env bash
set -euo pipefail
# ── edit these three, then run the whole script ──
export ${a.keyEnv}="<your-${a.keyName.toLowerCase()}-key>"
export ${s.env}="<${s.prefix}>"
export PROJECT="${s.path}"
# ─────────────────────────────────────────────────

# 1) prerequisites + ${a.cliName} CLI
${prereq}
${a.cli}

# 2) install bubo
${INSTALL_LINE[install]}

# 3) initialise + write config
bubo init
CFG="\${BUBO_ROOT:-$HOME/.local/share/bubo}/config/env.toml"
cat > "$CFG" <<EOF
[scm]
provider = "${scm}"

[${scm}]
token = "\${${s.env}}"

[agents]
llm_model       = "${a.model}"
llm_api_key_env = "${a.keyEnv}"${reviewer}

[[projects]]
path = "\${PROJECT}"
enabled = true
EOF

# 4) verify + run the first (dry-run) review
bubo doctor
bubo-poller`
}
