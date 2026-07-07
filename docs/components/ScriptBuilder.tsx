import { useState } from 'react'
import Link from 'next/link'
import styles from './quickstart.module.css'
import {
  CodeBlock,
  Tabs,
  buildScript,
  type Agent,
  type OS,
  type Scm,
  type Method,
} from './quickstartShared'

export function ScriptBuilder() {
  const [agent, setAgent] = useState<Agent>('codex')
  const [os, setOs] = useState<OS>('mac')
  const [scm, setScm] = useState<Scm>('gitlab')
  const [install, setInstall] = useState<Method>('pip')
  const script = buildScript(agent, os, scm, install)
  const filename = os === 'windows' ? 'bubo-quickstart.ps1' : 'bubo-quickstart.sh'

  return (
    <div className={styles.page}>
      <div className={styles.inner}>
        <Link href="/recipes" className={styles.backLink}>
          ← Back to the recipe
        </Link>

        <header className={styles.header}>
          <div className={styles.kicker}>Quickstart · Script</div>
          <h1 className={styles.title}>Generate an install script</h1>
        </header>

        <div className={styles.scriptPanel}>
          <div className={styles.selectorRow}>
            <span className={styles.selectorLabel}>Agent</span>
            <Tabs
              value={agent}
              onChange={setAgent}
              options={[
                { id: 'codex', label: 'OpenAI Codex' },
                { id: 'claude', label: 'Anthropic Claude' },
              ]}
            />
          </div>
          <div className={styles.selectorRow}>
            <span className={styles.selectorLabel}>Platform</span>
            <Tabs
              value={os}
              onChange={setOs}
              options={[
                { id: 'mac', label: 'macOS' },
                { id: 'linux', label: 'Linux' },
                { id: 'windows', label: 'Windows' },
              ]}
            />
          </div>
          <div className={styles.selectorRow}>
            <span className={styles.selectorLabel}>Host</span>
            <Tabs
              value={scm}
              onChange={setScm}
              options={[
                { id: 'gitlab', label: 'GitLab', icon: 'gitlab' },
                { id: 'github', label: 'GitHub', icon: 'github' },
              ]}
            />
          </div>
          <div className={styles.selectorRow}>
            <span className={styles.selectorLabel}>Install</span>
            <Tabs
              value={install}
              onChange={setInstall}
              options={[
                { id: 'uv', label: 'uv' },
                { id: 'pip', label: 'pip' },
                { id: 'docker', label: 'Docker' },
                { id: 'source', label: 'source' },
              ]}
            />
          </div>

          <CodeBlock code={script} download={filename} />

          <div className={styles.note}>
            {install === 'docker' ? (
              <>
                Derives an image with the agent CLI baked in, then runs bubo with a persisted{' '}
                <code>/home/bubo</code> mount. Ends on a dry-run.
              </>
            ) : (
              <>
                Installs dependencies, writes <code>env.toml</code>, and runs one dry-run review cycle. Persist the{' '}
                <code>export</code>s in your shell profile.
              </>
            )}{' '}
            Per-step detail:{' '}
            <Link href="/recipes" className={styles.link}>
              the recipe
            </Link>
            .
          </div>
        </div>
      </div>
    </div>
  )
}
