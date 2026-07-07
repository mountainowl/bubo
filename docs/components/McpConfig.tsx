import { useState } from 'react'
import type { ReactNode } from 'react'
import { CodeBlock } from './quickstartShared'
import styles from './mcpConfig.module.css'

type Mode = 'local' | 'ssh' | 'http'

const TABS: { id: Mode; label: string; blurb: string }[] = [
  { id: 'local', label: 'Local', blurb: 'Codex and the reviewer on one machine — stdio, the default.' },
  { id: 'ssh', label: 'SSH', blurb: 'Codex local, reviewer on a remote host — stdio tunnelled over SSH.' },
  { id: 'http', label: 'HTTP', blurb: 'Reviewer reachable over HTTP with a bearer token — org-wide.' },
]

const CLIENT = '~/.codex/config.toml'

type Block = { label: string; code: string }
type Info = { label?: string; title: string; body: ReactNode; code?: string }
type Config = { steps: ReactNode[]; blocks: Block[]; info?: Info[]; note?: ReactNode }

const CONFIG: Record<Mode, Config> = {
  local: {
    steps: [
      'Nothing to enable — stdio is the default transport.',
      <>
        Point the client at <code>bin/bubo mcp</code>.
      </>,
    ],
    blocks: [
      {
        label: `Client — ${CLIENT}`,
        code: `[mcp_servers.bubo]
command = "/absolute/path/to/bubo/bin/bubo"
args    = ["mcp"]
startup_timeout_sec = 20
tool_timeout_sec    = 1800   # >= [review].timeout_seconds for review_change`,
      },
    ],
  },
  ssh: {
    steps: [
      <>
        Set up <strong>passwordless key-based SSH</strong> to the reviewer host (below) — a password
        or passphrase prompt would corrupt the stdio stream.
      </>,
      <>
        Point Codex at <code>ssh</code> running the remote <code>bubo mcp</code>.
      </>,
    ],
    info: [
      {
        label: 'Prerequisite',
        title: 'Set up key-based SSH for stdio',
        body: (
          <>
            The MCP client runs the SSH command and speaks JSON-RPC over its stdin/stdout, so auth
            must be non-interactive — a password prompt, key passphrase, or login banner gets mixed
            into that stream and hangs the agent. Pin the user and key in <code>~/.ssh/config</code>,
            then test with <code>BatchMode=yes</code>: if it prompts, fix that before pointing the
            agent at it.
          </>
        ),
        code: `# 1) Create a dedicated client key (pick a passphrase, or -N "" for none).
ssh-keygen -t ed25519 -f ~/.ssh/bubo-reviewer -C "bubo-mcp"
#    Passphrase set? Load it once so connections never prompt:
#    ssh-add ~/.ssh/bubo-reviewer

# 2) Install the public key on the reviewer host.
ssh-copy-id -i ~/.ssh/bubo-reviewer.pub bubo@bubo.example.com

# 3) Pin user + key under a stable Host alias (used by ~/.codex/config.toml).
cat >> ~/.ssh/config <<'EOF'
Host bubo-reviewer
  HostName bubo.example.com
  User bubo
  IdentityFile ~/.ssh/bubo-reviewer
  IdentitiesOnly yes
  BatchMode yes          # fail fast instead of prompting for a password
  LogLevel QUIET         # no client-side banner noise on stdout
EOF

# 4) Verify it connects with no prompt.
ssh -o BatchMode=yes bubo-reviewer '/opt/bubo/bin/bubo mcp --help >/dev/null'`,
      },
    ],
    blocks: [
      {
        label: `Client — ${CLIENT}`,
        code: `[mcp_servers.bubo]
command = "ssh"
args = [
    "-T",                              # no pty; keeps stdout clean for MCP framing
    "-o", "ServerAliveInterval=30",    # keep long review_change calls alive across NAT
    "bubo-reviewer",                   # ssh_config Host alias, or user@host
    "/opt/bubo/bin/bubo mcp",          # absolute path on the server
]
startup_timeout_sec = 30
tool_timeout_sec    = 1800`,
      },
    ],
  },
  http: {
    steps: [
      <>
        On the reviewer host, set <code>[mcp_server]</code> to HTTP with a bearer token.
      </>,
      <>
        Run <code>bin/bubo mcp</code> (e.g. under a systemd unit).
      </>,
      'Front it with TLS (nginx/caddy) or a VPN — the server does not terminate TLS.',
      'Point the client at the URL with the matching token.',
    ],
    blocks: [
      {
        label: 'Reviewer host — config/env.toml',
        code: `[mcp_server]
transport    = "http"
host         = "0.0.0.0"               # or a specific interface
port         = 8765
bearer_token = "\${BUBO_MCP_TOKEN}"     # generate: openssl rand -hex 32`,
      },
      {
        label: `Client — ${CLIENT}`,
        code: `[mcp_servers.bubo]
url          = "https://reviewer.example.com/mcp"
bearer_token = "..."                   # matches BUBO_MCP_TOKEN
startup_timeout_sec = 30
tool_timeout_sec    = 1800`,
      },
    ],
    note: (
      <>
        The exact client key for HTTP servers varies across Codex versions — check{' '}
        <code>codex --help</code> if these names look wrong.
      </>
    ),
  },
}

export function McpConfig() {
  const [mode, setMode] = useState<Mode>('local')
  const cfg = CONFIG[mode]
  const active = TABS.find((t) => t.id === mode)!

  return (
    <div className={styles.wrap}>
      <div className={styles.tabs} role="tablist">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={mode === t.id}
            className={`${styles.tab} ${mode === t.id ? styles.tabActive : ''}`}
            onClick={() => setMode(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <p className={styles.blurb}>{active.blurb}</p>

      <ol className={styles.steps}>
        {cfg.steps.map((s, i) => (
          <li key={i}>{s}</li>
        ))}
      </ol>

      {cfg.info?.map((info) => (
        <div key={info.title} className={styles.info}>
          <span className={styles.infoLabel}>{info.label ?? 'Information'}</span>
          <h4 className={styles.infoTitle}>{info.title}</h4>
          <p className={styles.infoBody}>{info.body}</p>
          {info.code && <CodeBlock code={info.code} />}
        </div>
      ))}

      {cfg.blocks.map((b) => (
        <div key={b.label} className={styles.block}>
          <span className={styles.blockLabel}>{b.label}</span>
          <CodeBlock code={b.code} />
        </div>
      ))}

      {cfg.note && <p className={styles.note}>{cfg.note}</p>}
    </div>
  )
}
