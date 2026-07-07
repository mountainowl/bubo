import styles from './metricsDetail.module.css'

type Row = { k: string; v: string; hero?: boolean }
type Group = { title: string; rows: Row[] }
type Marquee = { big: string; label: string; sub: string }

// Speed is a given for an LLM — not the pitch. Lead with scale + signal density
// (real findings, no noise), end with the CFO punch: the dollar value of reviewer
// time returned. Bubo aids reviewers, it doesn't replace them — augmentation tone,
// not displacement. Dollar/hour figures are estimates; see the footnote.
const MARQUEE: Marquee[] = [
  { big: '259.5k', label: 'Changed LoC reviewed', sub: 'across 2,809 files in 5 projects' },
  { big: '0', label: 'False positives · 0 disputes', sub: 'high signal density' },
  { big: '43', label: 'Findings accepted', sub: '31 blocking · 65 resolved' },
  { big: '~$77K', label: 'Reviewer time returned', sub: 'for ~$200 of run cost — est. (see note)' },
]

const GROUPS: Group[] = [
  {
    title: 'Return on investment',
    rows: [
      { k: 'Loaded reviewer rate', v: '~$90/hr' },
      { k: 'Reviewer time returned', v: '~$77K', hero: true },
      { k: 'Return on spend', v: '~385×' },
      { k: 'Peer AI reviewers', v: '$24–30/dev-mo' },
    ],
  },
  {
    title: 'Reviewer time freed',
    rows: [
      { k: 'Human first pass @300 LoC/hr', v: '865 hrs' },
      { k: 'Bubo review wall time', v: '4.67 hrs' },
      { k: 'Reviewer hours freed (est.)', v: '~861', hero: true },
      { k: 'Reviewer-days freed (est.)', v: '~108' },
      { k: 'Cost / reviewer-hour', v: '~$0.23' },
    ],
  },
  {
    title: 'Outcomes & precision',
    rows: [
      { k: 'Findings tracked', v: '91' },
      { k: 'Developer replied', v: '70' },
      { k: 'Findings accepted', v: '43' },
      { k: '— of which blocking', v: '31' },
      { k: 'Resolved', v: '65' },
      { k: 'Resolution rate', v: '71.4%', hero: true },
      { k: 'Disputed', v: '0' },
      { k: 'False positives', v: '0', hero: true },
    ],
  },
  {
    title: 'Code reviewed',
    rows: [
      { k: 'Projects', v: '5' },
      { k: 'Files', v: '2,809' },
      { k: 'Added LoC', v: '147,316' },
      { k: 'Removed LoC', v: '112,233' },
      { k: 'Changed LoC', v: '259,549', hero: true },
      { k: 'Attempted LoC', v: '271,034' },
    ],
  },
  {
    title: 'Cost & efficiency',
    rows: [
      { k: 'Total cost', v: '~$200', hero: true },
      { k: 'Total tokens', v: '13,125,521' },
      { k: 'Cost / 1k LoC', v: '~$0.77' },
      { k: 'Tokens / LoC', v: '50.6' },
      { k: 'Changed LoC / $', v: '~1,300' },
    ],
  },
  {
    title: 'Aggregates',
    rows: [
      { k: 'Period', v: 'Jun 1–29, 2026' },
      { k: 'Total review runs', v: '124', hero: true },
      { k: 'Completed', v: '118' },
      { k: 'Completion rate', v: '95.2%' },
      { k: 'Findings returned', v: '74' },
      { k: 'Posted', v: '66' },
    ],
  },
]

export function MetricsDetail() {
  return (
    <section className={styles.wrap}>
      <p className={styles.eyebrow}>Real production metrics</p>
      <h2 className={styles.title}>Measured RoI</h2>
      <div className={styles.bar} />

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
