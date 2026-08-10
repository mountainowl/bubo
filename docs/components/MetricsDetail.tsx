import styles from './metricsDetail.module.css'

type Row = { k: string; v: string; hero?: boolean }
type Group = { title: string; rows: Row[] }
type Marquee = { big: string; label: string; sub: string }

// Speed is a given for an LLM — not the pitch. Lead with scale + signal density
// (real findings, no noise), end with the CFO punch: the dollar value of reviewer
// time returned. Bubo aids reviewers, it doesn't replace them — augmentation tone,
// not displacement. Dollar/hour figures are estimates; see the footnote.
const MARQUEE: Marquee[] = [
  { big: '75.3k', label: 'Added LoC reviewed', sub: 'across 108 instrumented runs in 5 projects' },
  { big: '0', label: 'False positives · 0 disputes', sub: 'across 191 tracked outcomes' },
  { big: '156', label: 'Findings accepted', sub: '91 blocking · 81.7% acceptance rate' },
  { big: '~$22.2K', label: 'Reviewer time returned', sub: 'for $36.83 of recorded cost — est. (see note)' },
]

// Measurement window and freshness stamp — sourced from the production report
// (reviewer.sqlite, all history in the current database).
const PERIOD = 'May 28 – Aug 10, 2026'
const LAST_UPDATED = 'August 10, 2026'

const GROUPS: Group[] = [
  {
    title: 'Return on investment',
    rows: [
      { k: 'Loaded reviewer rate', v: '~$90/hr' },
      { k: 'Reviewer time returned', v: '~$22.2K', hero: true },
      { k: 'Return on recorded spend', v: '~602×' },
      { k: 'Recorded cost / accepted finding', v: '$0.24' },
      { k: 'Accepted findings / recorded $', v: '4.24' },
      { k: 'Peer AI reviewers', v: '$24–30/dev-mo' },
    ],
  },
  {
    title: 'Reviewer time freed',
    rows: [
      { k: 'Added LoC reviewed', v: '75,320' },
      { k: 'Human first pass @300 LoC/hr', v: '251.1 hrs' },
      { k: 'Bubo review wall time', v: '4.6 hrs' },
      { k: 'Reviewer hours returned (est.)', v: '~246.5', hero: true },
      { k: 'Reviewer-days returned (est.)', v: '~30.8' },
      { k: 'Cost / reviewer-hour', v: '$0.15' },
    ],
  },
  {
    title: 'Outcomes & precision',
    rows: [
      { k: 'Findings returned', v: '218' },
      { k: 'Posted', v: '194 (89.0%)' },
      { k: 'Outcomes tracked', v: '191' },
      { k: 'Developer replied', v: '155' },
      { k: 'Accepted / resolved', v: '156', hero: true },
      { k: '— of which blocking', v: '91' },
      { k: 'Acceptance rate', v: '81.7%', hero: true },
      { k: 'Disputed', v: '0' },
      { k: 'False positives', v: '0', hero: true },
    ],
  },
  {
    title: 'Code reviewed',
    rows: [
      { k: 'Projects', v: '5' },
      { k: 'Added LoC measured', v: '75,320', hero: true },
      { k: 'LoC-instrumented runs', v: '108 of 276' },
      { k: 'Instrumentation coverage', v: '39.1%' },
      { k: 'Blocking findings', v: '126' },
      { k: 'Non-blocking findings', v: '92' },
    ],
  },
  {
    title: 'Cost & efficiency',
    rows: [
      { k: 'Total recorded cost', v: '$36.83', hero: true },
      { k: 'Total tokens', v: '25,565,663' },
      { k: 'Avg tokens / run', v: '92,629' },
      { k: 'Avg recorded cost / run', v: '$0.13' },
      { k: 'Runs with recorded cost', v: '218 of 276' },
      { k: 'Latency p50 / p95', v: '128s / 308s' },
    ],
  },
  {
    title: 'Aggregates',
    rows: [
      { k: 'Period', v: PERIOD },
      { k: 'Total review runs', v: '276', hero: true },
      { k: 'Successful or clean', v: '269' },
      { k: 'Completion rate', v: '97.5%' },
      { k: 'Failed runs', v: '6 (2.2%)' },
      { k: 'Total run wall time', v: '11.1 hrs' },
    ],
  },
]

export function MetricsDetail() {
  return (
    <section className={styles.wrap}>
      <p className={styles.eyebrow}>Real production metrics</p>
      <h2 className={styles.title}>Measured RoI</h2>
      <div className={styles.bar} />

      <div className={styles.stamp}>
        <span className={styles.live}>
          <span className={styles.dot} aria-hidden="true" />
          Real production numbers
        </span>
        <span>Measured {PERIOD}</span>
        <span>Last updated {LAST_UPDATED}</span>
      </div>

      <p className={styles.lede}>
        Speed is a given. Bubo&apos;s edge is <strong>signal density</strong>: multi-agent orchestration
        and deep codebase context surface only real structural and security flaws — in minutes — saving
        hundreds of senior-engineer hours.
      </p>

      <div className={styles.marquee}>
        {MARQUEE.map((m) => (
          <div key={m.label} className={styles.mItem}>
            <div className={styles.mBig}>{m.big}</div>
            <div className={styles.mLabel}>{m.label}</div>
            <div className={styles.mSub}>{m.sub}</div>
          </div>
        ))}
      </div>

      <div className={styles.grid}>
        {GROUPS.map((g) => (
          <div key={g.title} className={styles.panel}>
            <h3 className={styles.panelTitle}>{g.title}</h3>
            <dl className={styles.rows}>
              {g.rows.map((r) => (
                <div key={r.k} className={`${styles.row} ${r.hero ? styles.hero : ''}`}>
                  <dt className={styles.k}>{r.k}</dt>
                  <dd className={styles.v}>{r.v}</dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
      </div>

      <div className={styles.note}>
        <p className={styles.noteLead}>
          Note: these are real production numbers, not a benchmark or simulation. They are drawn from
          all Bubo history in the live production database across five GitLab projects, measured
          continuously from <strong>May 28, 2026</strong> through <strong>August 10, 2026</strong>.
          Last updated <strong>{LAST_UPDATED}</strong>.
        </p>
        <p className={styles.noteBody}>
          Coverage is partial and stated rather than smoothed over. Reviewer-time figures use only the
          108 of 276 runs (39.1%) carrying LoC instrumentation, and count added lines only. Recorded
          provider cost covers 218 of 276 runs (79.0%) — 58 runs have zero or missing model pricing, so
          the true spend is higher than $36.83. The ~602× return charges that full recorded cost against
          only the reviewer-time value measurable in the instrumented subset; it is a directional
          provider-cost ratio, not audited all-in ROI. Host, engineering, and operational costs are not
          recorded. &ldquo;Accepted&rdquo; means resolved and neither disputed nor marked false-positive;
          zero recorded false positives reflects the outcome labels present in production, not
          independently proven precision.
        </p>
        <p className={styles.noteBody}>
          Reviewer-hours and dollar figures are estimates. Careful peer review runs ~200–400 LoC/hr
          (300 midpoint); review effectiveness drops sharply above ~200 LoC/hr.
          <sup className={styles.cite}>
            <a href="#mref-1">[1]</a>
            <a href="#mref-2">[2]</a>
          </sup>{' '}
          Reviewer time is valued at a fully-loaded ~$90/hr — the BLS May 2024 median for software
          developers ($133,080, ~$64/hr) at a conservative 1.4× benefits/overhead load.
          <sup className={styles.cite}>
            <a href="#mref-3">[3]</a>
          </sup>{' '}
          Peer AI reviewers list at ~$24–30/developer-month (CodeRabbit, Greptile, Qodo, Graphite).
          <sup className={styles.cite}>
            <a href="#mref-4">[4]</a>
          </sup>{' '}
          Bubo performs technical review — correctness, structure, and security — not
          subject-matter-expert or domain-specific review, and reads diff LoC at scale where a human
          would sample and skim mechanical changes; treat the figures as directional.
        </p>
        <ul className={styles.refs}>
          <li id="mref-1">
            <span className={styles.refNum}>[1]</span> Kemerer &amp; Paulk, “The Impact of Design and
            Code Reviews on Software Quality,” IEEE Trans. Software Eng., 2009.{' '}
            <a
              href="https://sites.pitt.edu/~ckemerer/PSP_Data.pdf"
              className={styles.noteLink}
              target="_blank"
              rel="noreferrer"
            >
              sites.pitt.edu
            </a>
          </li>
          <li id="mref-2">
            <span className={styles.refNum}>[2]</span> SmartBear, “Best Practices for Peer Code Review” (SmartBear/Cisco study).{' '}
            <a
              href="https://smartbear.com/learn/code-review/best-practices-for-peer-code-review/"
              className={styles.noteLink}
              target="_blank"
              rel="noreferrer"
            >
              smartbear.com
            </a>
          </li>
          <li id="mref-3">
            <span className={styles.refNum}>[3]</span> U.S. Bureau of Labor Statistics, “Software
            Developers,” Occupational Outlook Handbook, May 2024.{' '}
            <a
              href="https://www.bls.gov/ooh/computer-and-information-technology/software-developers.htm"
              className={styles.noteLink}
              target="_blank"
              rel="noreferrer"
            >
              bls.gov
            </a>
          </li>
          <li id="mref-4">
            <span className={styles.refNum}>[4]</span> Vendor list pricing, accessed 2026: CodeRabbit, Greptile, Qodo, Graphite.
          </li>
        </ul>
      </div>
    </section>
  )
}
