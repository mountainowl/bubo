import type { ReactNode } from 'react'
import Link from 'next/link'
import styles from './overviewSections.module.css'
import { asset } from './asset'

const CAPABILITIES: ReactNode[] = [
  'Self-hosted automated review and inline posting',
  'Bring-your-own-LLM',
  'GitLab or GitHub',
  'Built-in metrics',
  'Governance, provenance, audit, and ROI metrics',
  <>
    <a href="https://modelcontextprotocol.io/docs/getting-started/intro" className={styles.link}>
      MCP
    </a>{' '}
    for on-demand reviews
  </>,
]

export function Highlights() {
  return (
    <section className={styles.section}>
      <div className={styles.quote}>
        <a href="https://en.wikipedia.org/wiki/Bubo_(genus)" className={styles.link}>
          Bubo
        </a>{' '}
        is patient — it sits silently, sees all, and strikes only when sure. It watches your
        repositories and speaks only when it finds something.
      </div>

      <h2 className={styles.title}>
        Agentic AI code review <span className={styles.titleAccent}>— with the LLM of your choice</span>
      </h2>
      <div className={styles.bar} />

      <div className={styles.featGrid}>
        {CAPABILITIES.map((c, i) => (
          <div key={i} className={styles.feat}>
            <span className={styles.idx}>{String(i + 1).padStart(2, '0')}</span>
            <span className={styles.featText}>{c}</span>
          </div>
        ))}
      </div>
    </section>
  )
}

const EYE_OFF = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M3 3l18 18" />
    <path d="M10.6 10.6a2 2 0 002.8 2.8" />
    <path d="M9.4 5.2A9 9 0 0121 12a14 14 0 01-2 2.7M6.1 6.1A14 14 0 003 12a9 9 0 0011 6.6" />
  </svg>
)
const ALERT = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M10.3 3.8L2 18a2 2 0 001.7 3h16.6a2 2 0 001.7-3L13.7 3.8a2 2 0 00-3.4 0z" />
    <path d="M12 9v4M12 17h.01" />
  </svg>
)
const BADGE = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="12" cy="9" r="6" />
    <path d="M9 13.5L8 21l4-2.5 4 2.5-1-7.5" />
    <path d="M9.5 9l1.7 1.7L14.5 7.5" />
  </svg>
)

export function SecurityPosture() {
  return (
    <section className={styles.section}>
      <p className={styles.eyebrow}>Trust</p>
      <h2 className={styles.title}>Security &amp; compliance posture</h2>
      <div className={styles.bar} />

      <div className={styles.secGrid}>
        <div className={styles.secCard}>
          <div className={styles.secCardHead}>
            <span className={styles.icon}>{EYE_OFF}</span>
            <h3 className={styles.secTitle}>Secrets never leak</h3>
          </div>
          <p className={styles.secBody}>
            Sensitive information is redacted before it touches the LLM, reports, logs, or the
            database.
          </p>
        </div>

        <div className={styles.secCard}>
          <div className={styles.secCardHead}>
            <span className={styles.icon}>{BADGE}</span>
            <h3 className={styles.secTitle}>Signed &amp; attested</h3>
          </div>
          <p className={styles.secBody}>
            Release artifacts are <strong>cosign-signed</strong> keyless via{' '}
            <a href="https://www.sigstore.dev/" className={styles.link}>Sigstore</a> + GitHub OIDC,
            carry <a href="https://slsa.dev/" className={styles.link}>SLSA Build L3</a> provenance, and
            ship an <a href="https://spdx.dev/" className={styles.link}>SBOM in SPDX JSON</a> — all
            independently verifiable.
          </p>
        </div>

        <div className={styles.secCard}>
          <div className={styles.secCardHead}>
            <span className={styles.icon}>{ALERT}</span>
            <h3 className={styles.secTitle}>Report a vuln</h3>
          </div>
          <p className={styles.secBody}>
            Disclose responsibly per{' '}
            <a href="https://github.com/mountainowl/bubo/blob/main/SECURITY.md" className={styles.link}>
              SECURITY.md
            </a>
            .
          </p>
        </div>
      </div>
    </section>
  )
}

const STARTS: { href: string; title: string; sub: string; icon: ReactNode; brand?: string; external?: boolean }[] = [
  {
    href: '/recipes',
    title: 'Copy-paste recipes',
    sub: 'Choose your platform, agent, and install method.',
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M8 6h12M8 12h12M8 18h12M3 6h.01M3 12h.01M3 18h.01" />
      </svg>
    ),
  },
  {
    href: 'https://github.com/mountainowl/bubo',
    title: 'Source on GitHub',
    sub: 'MIT-licensed, self-hostable, BYO-LLM.',
    brand: '/github.svg',
    external: true,
    icon: null,
  },
]

export function GetStarted() {
  return (
    <section className={styles.section}>
      <h2 className={styles.title}>Get started</h2>
      <div className={styles.bar} />

      <div className={styles.ctaGrid}>
        {STARTS.map((s) => {
          const inner = (
            <>
              <span className={styles.ctaTop}>
                <span className={styles.ctaIcon}>
                  {s.brand ? <img src={asset(s.brand)} alt="" /> : s.icon}
                </span>
                <span className={styles.ctaTitle}>{s.title}</span>
                <span className={styles.arrow} aria-hidden="true">→</span>
              </span>
              <span className={styles.ctaSub}>{s.sub}</span>
            </>
          )
          return s.external ? (
            <a key={s.href} href={s.href} className={styles.ctaCard}>
              {inner}
            </a>
          ) : (
            <Link key={s.href} href={s.href} className={styles.ctaCard}>
              {inner}
            </Link>
          )
        })}
      </div>
    </section>
  )
}
