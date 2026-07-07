import type { ReactNode } from 'react'
import Link from 'next/link'
import styles from './landing.module.css'
import { SiteFooter } from './SiteFooter'

const ACCENT = '#1d4ed8'
const SYMBOLS: [string, number, number, number, number][] = [
  ['</>', 50, 50, -15, 24],
  ['{}', 150, 100, 10, 20],
  ['=>', 250, 80, -5, 18],
  ['[]', 100, 200, 15, 22],
  ['<>', 300, 180, -10, 20],
  ['()', 200, 250, 5, 24],
  ['::', 50, 320, -8, 18],
  ['==', 350, 300, 12, 22],
  ['++', 150, 350, -15, 20],
  [';', 250, 370, 8, 24],
]

const ABOUT =
  "Bubo stays quiet until it has evidence, and speaks only when something's worth your time. Your LLM, self-hosted. No filler. No praise. No generic summaries."

const CAPABILITIES = [
  'Bring-your-own-LLM',
  'Self-hosted',
  'GitLab + GitHub',
  'Signal over noise',
  'Review moods',
  'Dispute-driven learning',
  'Verify before posting',
  'Governance & provenance',
  'OpenTelemetry',
  'MCP',
  'GitHub Action CI',
]

const FEATURES: { group: string; items: [string, ReactNode][] }[] = [
  {
    group: 'Reviews worth reading',
    items: [
      ['Only what matters', 'What’s wrong, why, and how to fix it. No praise, no summaries.'],
      ['Dry run', 'Runs a real review and enumerates findings, but posts nothing.'],
      ['Pick the tone', 'Terse, collaborative, socratic, formal, or casual.'],
    ],
  },
  {
    group: 'Use the AI you already trust',
    items: [
      ['Bring your own model', 'Codex, Claude, or any CLI-driven model. No lock-in.'],
      ['SCM', 'Works with GitLab and GitHub.'],
    ],
  },
  {
    group: 'Decide what gets flagged',
    items: [
      ['Gate or collaborate', 'Merge-blockers only, or everything — suggestions and questions too.'],
      ['Confidence calibration', 'Post only what it’s sure of; raise the bar per issue type.'],
      ['Learns your taste', 'Keeps flagging what you dismiss? It stops.'],
      ['Double-checks first', 'An optional second model drops findings that don’t hold up.'],
    ],
  },
  {
    group: 'Fits how you work',
    items: [
      ['MCP', 'Connect your editor to trigger an interactive review on demand.'],
      ['Reviews in CI', 'A GitHub Action that comments on PRs in your pipeline.'],
      ['A simple dashboard', 'Read-only view of recent reviews, health, and reports.'],
    ],
  },
  {
    group: 'Governance, compliance & ROI',
    items: [
      ['AI-code provenance', 'Identifies AI-assisted and sensitive changes for closer review; it does not block merges by itself.'],
      ['Audit trail', 'A write-once, chronological record of every decision and its lifecycle.'],
      ['Self-hosted control', 'Repository data stays within the infrastructure and model path you configure.'],
      ['ROI reporting', 'Accept/dispute rate, bugs caught, noise, and cost.'],
      [
        'Redaction and signed releases',
        <>
          Credentials scrubbed before the model sees the diff. Signed releases,{' '}
          <Link href="/overview#security" className={styles.featureLink}>
            SBOM
          </Link>
          .
        </>,
      ],
    ],
  },
]

export function Landing() {
  return (
    <div className={styles.page}>
      {/* HERO */}
      <section className={styles.hero}>
        <div className={styles.gradient} aria-hidden="true" />
        <svg aria-hidden="true" className={styles.pattern}>
          <defs>
            <pattern id="grid" x="50%" y={-1} width={200} height={200} patternUnits="userSpaceOnUse">
              <path d="M.5 200V.5H200" fill="none" />
            </pattern>
            <pattern id="symbols" x="0" y="0" width={400} height={400} patternUnits="userSpaceOnUse">
              {SYMBOLS.map(([t, x, y, r, s], i) => (
                <text
                  key={i}
                  x={x}
                  y={y}
                  fill={ACCENT}
                  fontFamily="monospace"
                  fontSize={s}
                  transform={`rotate(${r} ${x} ${y})`}
                >
                  {t}
                </text>
              ))}
            </pattern>
          </defs>
          <rect fill="url(#symbols)" width="100%" height="100%" opacity="0.2" />
          <rect fill="url(#grid)" width="100%" height="100%" strokeWidth={0} />
        </svg>

        <div className={styles.heroInner}>
          <h2 className={styles.hello}>Hoo-hoo-hooooo… hoo-hoo 👋</h2>
          <h1 className={styles.name}>
            I&apos;m <span style={{ color: ACCENT }}>Bubo</span> 🦉
          </h1>
          <p className={styles.tagline}>
            Self-hosted, <span className={styles.signalKey}>high signal-density</span>{' '}
            agentic AI code reviewer.
          </p>
          <div className={styles.actions}>
            <Link href="/recipes" className={styles.cta}>
              Quickstart <span aria-hidden="true">→</span>
            </Link>
            <Link href="/overview" className={styles.ctaGhost}>
              Read the documentation
            </Link>
          </div>
        </div>

        <div className={styles.social}>
          <a
            href="https://github.com/mountainowl/bubo"
            target="_blank"
            rel="noreferrer"
            aria-label="GitHub"
          >
            <svg
              xmlns="http://www.w3.org/2000/svg"
              width="28"
              height="28"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path stroke="none" d="M0 0h24v24H0z" fill="none" />
              <path d="M9 19c-4.3 1.4 -4.3 -2.5 -6 -3m12 5v-3.5c0 -1 .1 -1.4 -.5 -2c2.8 -.3 5.5 -1.4 5.5 -6a4.6 4.6 0 0 0 -1.3 -3.2a4.2 4.2 0 0 0 -.1 -3.2s-1.1 -.3 -3.5 1.3a12.3 12.3 0 0 0 -6.2 0c-2.4 -1.6 -3.5 -1.3 -3.5 -1.3a4.2 4.2 0 0 0 -.1 3.2a4.6 4.6 0 0 0 -1.3 3.2c0 4.6 2.7 5.7 5.5 6c-.6 .6 -.6 1.2 -.5 2v3.5" />
            </svg>
          </a>
        </div>
      </section>

      {/* ABOUT */}
      <section className={styles.section}>
        <div className={styles.grid}>
          <div className={styles.head}>
            <h2 className={styles.title}>About</h2>
            <div className={styles.bar} />
          </div>
          <div className={styles.body}>
            <p className={styles.about}>{ABOUT}</p>
          </div>
        </div>
      </section>

      {/* CAPABILITIES */}
      <section className={styles.section}>
        <div className={styles.grid}>
          <div className={styles.head}>
            <h2 className={styles.title}>Capabilities</h2>
            <div className={styles.bar} />
          </div>
          <div className={styles.body}>
            <div className={styles.pills}>
              {CAPABILITIES.map((c) => (
                <span key={c} className={styles.pill}>
                  {c}
                </span>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* FEATURES */}
      <section className={styles.section}>
        <div className={styles.grid}>
          <div className={styles.head}>
            <h2 className={styles.title}>Features</h2>
            <div className={styles.bar} />
          </div>
          <div className={styles.body}>
            <div className={styles.featureGroups}>
              {FEATURES.map((g) => (
                <div key={g.group} className={styles.featureGroup}>
                  <h3 className={styles.featureGroupTitle}>{g.group}</h3>
                  <ul className={styles.featureList}>
                    {g.items.map(([name, desc]) => (
                      <li key={name} className={styles.featureItem}>
                        <span className={styles.featureName}>{name}</span>
                        <span className={styles.featureDesc}>{desc}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      <SiteFooter />
    </div>
  )
}
