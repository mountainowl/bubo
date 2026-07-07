import { useState } from 'react'
import { CodeBlock } from './quickstartShared'
import { composeRecipe } from './recipes/compose'
import type { AgentId, InstallId, OsId, ScmId } from './recipes/types'
import styles from './recipePicker.module.css'
import { asset } from './asset'

const OSES: { id: OsId; label: string; icon: string }[] = [
  { id: 'mac', label: 'macOS', icon: '/apple.svg' },
  { id: 'linux', label: 'Linux', icon: '/linux.svg' },
  { id: 'windows', label: 'Windows', icon: '/windows.svg' },
]
const PLATFORMS: { id: ScmId; label: string; icon: string }[] = [
  { id: 'gitlab', label: 'GitLab', icon: '/gitlab.svg' },
  { id: 'github', label: 'GitHub', icon: '/github.svg' },
]
const AGENTS: { id: AgentId; label: string; icon: string }[] = [
  { id: 'codex', label: 'Codex', icon: '/openai.svg' },
  { id: 'claude', label: 'Claude', icon: '/anthropic.svg' },
  { id: 'selfhosted', label: 'Self-hosted', icon: '/server.svg' },
]
const INSTALLS: { id: InstallId; label: string; icon: string }[] = [
  { id: 'uv', label: 'uv', icon: '/uv.svg' },
  { id: 'pip', label: 'pip', icon: '/pypi.svg' },
  { id: 'docker', label: 'Docker', icon: '/docker.svg' },
]

type View = 'none' | 'recipe' | 'prompt'

function Group<T extends string>({
  step,
  options,
  value,
  onChange,
}: {
  step: string
  options: { id: T; label: string; icon: string }[]
  value: T
  // NoInfer keeps T from being inferred off the setter — a Dispatch<SetStateAction<T>>
  // arg otherwise widens T to its `string` constraint and breaks assignability.
  onChange: (v: NoInfer<T>) => void
}) {
  return (
    <div className={styles.group}>
      <span className={styles.groupLabel}>{step}</span>
      <div className={styles.cards}>
        {options.map((o) => (
          <button
            key={o.id}
            type="button"
            onClick={() => onChange(o.id)}
            aria-pressed={value === o.id}
            className={`${styles.card} ${value === o.id ? styles.active : ''}`}
          >
            <span className={styles.tile}>
              <img src={asset(o.icon)} alt="" />
            </span>
            <span className={styles.cardLabel}>{o.label}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function Chips({
  o,
  ag,
  platform,
  inst,
}: {
  o: { label: string; icon: string }
  ag: { label: string; icon: string }
  platform: { label: string; icon: string }
  inst: { label: string; icon: string }
}) {
  const chip = (c: { label: string; icon: string }) => (
    <span className={styles.recipeChip}>
      <span className={styles.recipeIconTile}>
        <img src={asset(c.icon)} alt="" />
      </span>
      {c.label}
    </span>
  )
  return (
    <h3 className={styles.recipeTitle}>
      {chip(o)}
      <span className={styles.recipeOp}>·</span>
      {chip(ag)}
      <span className={styles.recipeOp}>+</span>
      {chip(platform)}
      <span className={styles.recipeOp}>·</span>
      {chip(inst)}
    </h3>
  )
}

export function RecipePicker() {
  const [os, setOs] = useState<OsId>('mac')
  const [scm, setScm] = useState<ScmId>('gitlab')
  const [agent, setAgent] = useState<AgentId>('codex')
  const [install, setInstall] = useState<InstallId>('uv')
  const [view, setView] = useState<View>('none')

  const recipe = composeRecipe(scm, agent, install, os)
  const o = OSES.find((x) => x.id === os)!
  const platform = PLATFORMS.find((p) => p.id === scm)!
  const ag = AGENTS.find((a) => a.id === agent)!
  const inst = INSTALLS.find((i) => i.id === install)!

  return (
    <section className={styles.wrap}>
      <p className={styles.eyebrow}>Quick path</p>
      <h2 className={styles.title}>Pick your stack</h2>
      <div className={styles.bar} />

      <div className={styles.groups}>
        <Group step="1 · OS" options={OSES} value={os} onChange={setOs} />
        <span className={styles.plus} aria-hidden="true">+</span>
        <Group step="2 · Platform" options={PLATFORMS} value={scm} onChange={setScm} />
        <span className={styles.plus} aria-hidden="true">+</span>
        <Group step="3 · Agent" options={AGENTS} value={agent} onChange={setAgent} />
        <span className={styles.plus} aria-hidden="true">+</span>
        <Group step="4 · Install" options={INSTALLS} value={install} onChange={setInstall} />
      </div>

      <div className={styles.go}>
        <button type="button" className={styles.cta} onClick={() => setView('recipe')}>
          Show install steps <span aria-hidden="true">↓</span>
        </button>
        <button type="button" className={styles.ctaAlt} onClick={() => setView('prompt')}>
          Generate agent install prompt <span aria-hidden="true">↓</span>
        </button>
      </div>

      {view !== 'none' && (
        <div className={styles.recipe}>
          <Chips o={o} ag={ag} platform={platform} inst={inst} />

          {view === 'recipe' && (
            <>
              <ol className={styles.steps}>
                <li className={styles.step}>
                  <span className={styles.stepNum}>1</span>
                  <div className={styles.stepBody}>
                    <h4 className={styles.stepTitle}>Prerequisites</h4>
                    <ul className={styles.prereqs}>
                      {recipe.prereqs.map((p) => (
                        <li key={p.text}>
                          {p.text}
                          {p.links && p.links.length > 0 && (
                            <span className={styles.preLinks}>
                              {p.links.map((l, i) => (
                                <span key={l.href}>
                                  {i > 0 && <span className={styles.preSep}> · </span>}
                                  <a
                                    href={l.href.startsWith('http') ? l.href : asset(l.href)}
                                    className={styles.preLink}
                                    {...(l.href.startsWith('http')
                                      ? { target: '_blank', rel: 'noreferrer' }
                                      : {})}
                                  >
                                    {l.label} ↗
                                  </a>
                                </span>
                              ))}
                            </span>
                          )}
                        </li>
                      ))}
                    </ul>
                    {recipe.prereqInstall && (
                      <>
                        <p className={styles.stepIntro}>Install them:</p>
                        <CodeBlock code={recipe.prereqInstall} />
                      </>
                    )}
                  </div>
                </li>

                <li className={styles.step}>
                  <span className={styles.stepNum}>2</span>
                  <div className={styles.stepBody}>
                    <h4 className={styles.stepTitle}>Install Bubo</h4>
                    <CodeBlock code={recipe.installCode} />
                  </div>
                </li>

                <li className={styles.step}>
                  <span className={styles.stepNum}>3</span>
                  <div className={styles.stepBody}>
                    <h4 className={styles.stepTitle}>Configure</h4>
                    <p className={styles.stepIntro}>{recipe.initIntro}</p>
                    <CodeBlock code={recipe.configCode} />
                    {recipe.extraConfig && (
                      <>
                        <p className={styles.stepIntro}>{recipe.extraConfig.intro}</p>
                        <CodeBlock code={recipe.extraConfig.code} />
                      </>
                    )}
                  </div>
                </li>

                <li className={styles.step}>
                  <span className={styles.stepNum}>4</span>
                  <div className={styles.stepBody}>
                    <h4 className={styles.stepTitle}>Verify &amp; run</h4>
                    <CodeBlock code={recipe.runCode} />
                  </div>
                </li>
              </ol>

              <p className={styles.recipeNote}>
                The first run is <strong>dry-run by default</strong> — it plans findings and posts
                nothing. Flip <code>[review].dry_run = false</code> when it looks right, then{' '}
                <a href={asset('/operate/#schedule-the-poller')} className={styles.link}>
                  schedule it
                </a>
                .
              </p>
            </>
          )}

          {view === 'prompt' && (
            <>
              <p className={styles.lede}>
                <strong>Safeguard:</strong> the agent reads and validates the context but installs
                nothing until you explicitly reply <code>install bubo</code>.
              </p>
              <ol className={styles.steps}>
                <li className={styles.step}>
                  <span className={styles.stepNum}>1</span>
                  <div className={styles.stepBody}>
                    <h4 className={styles.stepTitle}>Save the context</h4>
                    <p className={styles.stepIntro}>
                      Fill in the <code>{'<…>'}</code> values and save it locally (e.g.{' '}
                      <code>~/bubo-install.toml</code>) — never commit it.
                    </p>
                    <CodeBlock code={recipe.contextData} download="bubo-install.toml" />
                  </div>
                </li>

                <li className={styles.step}>
                  <span className={styles.stepNum}>2</span>
                  <div className={styles.stepBody}>
                    <h4 className={styles.stepTitle}>Run the install prompt</h4>
                    <p className={styles.stepIntro}>
                      Paste this into Claude or Codex, pointing it at the file above:
                    </p>
                    <CodeBlock code={recipe.installPrompt} />
                  </div>
                </li>
              </ol>

              <div className={styles.note}>
                <strong>Permissions.</strong> Installing runs shell commands (package installs,
                writing config, maybe a scheduler). Grant permissions up front, or approve each
                requested action as the agent runs.
              </div>
            </>
          )}
        </div>
      )}
    </section>
  )
}
